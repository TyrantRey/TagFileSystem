# Code by AkinoAlice@TyrantRey

"""Self-update (DESIGN/v0-2-0.md): ``tfs update``, ``tfs upgrade``, the root
registry (§5), snapshots and their retention (§7 step 5 and 11).

**Standard library only.** ``tfs upgrade`` copies this file to a temporary
directory and re-runs it from there (§6): once the sequence starts,
``git checkout`` rewrites ``src/`` and ``uv sync`` rewrites ``.venv``, so
nothing outside the standard library can be trusted to import. A test
parses this module and fails on any other import. The few things it needs
from the rest of the package (the lock file format, the root layout, the
control channel's wire format) are mirrored here, with the constants named
after their originals.

Two Windows facts shape the hand-off (both verified on a real machine, both
fatal if ignored — ``uv`` cannot replace a file another process has mapped):

* The editable install rewrites ``Scripts/tfs.exe`` whenever the version in
  ``pyproject.toml`` changes, which is every upgrade, and that launcher is
  the very process waiting for the upgrade to finish. So the sync runs with
  ``--no-install-project --inexact``: dependencies reach the target lock,
  the editable ``.pth`` keeps pointing at ``src/`` (the checked-out code is
  what runs), and the project's own ``.dist-info`` stays at the previous
  version until the next plain ``uv sync``. ``version.py`` reads
  ``pyproject.toml`` first for exactly that reason.
* The same holds for compiled dependencies (``_pydantic_core.pyd``), so the
  waiting process must never have imported them: ``tag_file_system.__main__``
  routes ``tfs upgrade`` here *before* the Typer app (and pydantic) is
  imported, and the orchestrator runs on the base interpreter, not the one
  inside ``.venv``.

Everything else is driven by subprocess — ``git``, ``uv``, ``pytest`` and
``tfs`` itself, the latter as ``python -P -m tag_file_system.cli`` rather
than ``uv run tfs`` (whose implicit sync would hit the launcher problem).
"""

import argparse
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Mirrors of root.py / config.py (which cannot be imported here).
TFS_DIR = ".tfs"
LOCK_FILE = "lock"
TOKEN_FILE = "token"
CONFIG_FILE = "config.toml"
DB_FILE = ("db", "system.db")
BACKUPS_DIR = "backups"  # under .tfs/: Zone.TFS, so the watcher ignores it
DEFAULT_BIND, DEFAULT_PORT, DEFAULT_STOP_TIMEOUT = "127.0.0.1", 7411, 30.0
STALE_FOREIGN_LOCK_SECONDS = 6 * 3600  # root.DEFAULT_STALE_AFTER_SECONDS

PACKAGE = "tag_file_system"
SCHEMA_FILE = "src/tag_file_system/database/migrations.py"
REGISTRY_ENV = "TFS_REGISTRY"  # overrides the registry location (tests, containers)

KEEP_DEFAULT = 3
HEALTH_TIMEOUT = 15.0
STOP_GRACE_SECONDS = 5.0  # on top of the daemon's stop_timeout_seconds
START_TIMEOUT = 120.0
SYNC_TIMEOUT = 15 * 60.0
TESTS_TIMEOUT = 45 * 60.0

_MAX_PID = 2**31 - 1
_HASH = re.compile(r"[0-9a-f]{40}")
_RELEASE_TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_SCHEMA_LINE = re.compile(r"^SCHEMA_VERSION\s*=\s*(\d+)\s*(?:#.*)?$", re.MULTILINE)
_BACKUP_NAME = re.compile(r"^(\d{8}T\d{6}Z)-(.+?)(?:-(\d+))?\.db$")
_UNSAFE_LABEL = re.compile(r"[^A-Za-z0-9._-]+")


class UpdateError(Exception):
    """Preflight refused, or a step could not be carried out."""


class NothingToDo(UpdateError):
    """Already where the upgrade would go: not a failure."""


class ControlDown(UpdateError):
    """No daemon answers at the address."""


def short(commit: str | None) -> str:
    return commit[:7] if commit else "unknown"


def _tail(text: str, lines: int = 12) -> str:
    kept = [line for line in text.strip().splitlines() if line.strip()]
    return "\n".join(kept[-lines:])


def human_size(num: int | float) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"  # pragma: no cover


# ---------------------------------------------------------------- registry


def registry_path() -> Path:
    """``%APPDATA%\\tfs\\roots.json`` / ``$XDG_CONFIG_HOME/tfs/roots.json``
    (§5). Never ``~/.tfs``: ``Root.discover`` would take the home directory
    for a root."""
    override = os.environ.get(REGISTRY_ENV)
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "tfs" / "roots.json"


def _read_registry(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    roots = data.get("roots") if isinstance(data, dict) else None
    if not isinstance(roots, list):
        return []
    return [r for r in roots if isinstance(r, str) and r.strip()]


def _write_registry(path: Path, roots: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(
        json.dumps({"version": 1, "roots": roots}, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


def register_root(root: Path | str) -> None:
    """Remember ``root``. Best effort: a read-only home directory must not
    break ``tfs query``."""
    text = str(Path(root).resolve())
    path = registry_path()
    try:
        roots = _read_registry(path)
        if text in roots:
            return
        _write_registry(path, [*roots, text])
    except OSError:
        pass


def known_roots() -> list[Path]:
    """Every registered root that still is one. Entries are hints: a path
    without ``.tfs/`` (deleted, moved, unmounted) is dropped, never an
    error."""
    path = registry_path()
    roots = _read_registry(path)
    live = [r for r in roots if (Path(r) / TFS_DIR).is_dir()]
    if live != roots:
        try:
            _write_registry(path, live)
        except OSError:
            pass
    return [Path(r) for r in live]


# --------------------------------------------------------------- processes


def run_command(
    cmd: list[str], cwd: Path | str | None = None, timeout: float | None = None
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        raise UpdateError(f"{cmd[0]} is not installed or not on PATH") from None
    except subprocess.TimeoutExpired:
        raise UpdateError(
            f"{' '.join(cmd[:3])} did not finish within {timeout:.0f}s"
        ) from None


def pid_alive(pid: int) -> bool:
    """Mirror of ``root.pid_alive``: never ``os.kill`` on Windows (it would
    terminate the process), access-denied counts as alive."""
    if pid <= 0 or pid > _MAX_PID:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --------------------------------------------------------------------- git


def git(repo: Path | str, *args: str, timeout: float = 300.0) -> str:
    result = run_command(["git", "-C", str(repo), *args], timeout=timeout)
    if result.returncode != 0:
        raise UpdateError(
            f"git {' '.join(args[:2])} failed: {_tail(result.stderr or result.stdout, 5)}"
        )
    return result.stdout.strip()


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = _RELEASE_TAG.match(text.strip())
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def parse_remote_tags(text: str) -> tuple[dict[str, str], set[str]]:
    """``git ls-remote --tags`` output as ``({annotated tag: commit},
    {lightweight tags})``. An annotated tag lists twice, the ``^{}`` line
    carrying the commit it points at; a lightweight tag lists once."""
    objects: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[1].startswith("refs/tags/"):
            continue
        name = parts[1][len("refs/tags/") :]
        if name.endswith("^{}"):
            peeled[name[:-3]] = parts[0]
        else:
            objects[name] = parts[0]
    annotated = {name: peeled[name] for name in objects if name in peeled}
    lightweight = {name for name in objects if name not in peeled}
    return annotated, lightweight


def remote_tags(repo: Path) -> tuple[dict[str, str], set[str]]:
    try:
        text = git(repo, "ls-remote", "--tags", "origin", timeout=120.0)
    except UpdateError as e:
        raise UpdateError(f"origin is not reachable: {e}") from None
    return parse_remote_tags(text)


@dataclass
class HeadState:
    commit: str
    branch: str | None  # None when detached
    tag: str | None  # a tag exactly at HEAD
    dirty: bool

    @property
    def ref(self) -> str:
        """What a revert checks out: the branch, else the tag, else the commit."""
        if self.branch:
            return self.branch
        if self.tag:
            return f"refs/tags/{self.tag}"
        return self.commit

    def describe(self, version: str) -> str:
        where = f"on {self.branch}" if self.branch else f"at {self.tag or 'no tag'}"
        return f"{version} ({short(self.commit)}) {where}"


def head_state(repo: Path) -> HeadState:
    commit = git(repo, "rev-parse", "HEAD")
    if not _HASH.fullmatch(commit):
        raise UpdateError(f"git rev-parse HEAD said {commit!r}")
    branch = run_command(
        ["git", "-C", str(repo), "symbolic-ref", "-q", "--short", "HEAD"]
    )
    # Several tags may sit on HEAD (a release plus `latest`, say): the
    # release-shaped one is the identity.
    at_head = git(repo, "tag", "--points-at", "HEAD").splitlines()
    tag = next(
        (t for t in at_head if parse_version(t)), at_head[0] if at_head else None
    )
    dirty = bool(git(repo, "status", "--porcelain", "--untracked-files=no"))
    return HeadState(
        commit=commit,
        branch=branch.stdout.strip() if branch.returncode == 0 else None,
        tag=tag,
        dirty=dirty,
    )


def file_at(repo: Path, ref: str, path: str) -> str:
    """The content of ``path`` at ``ref`` — read, never imported."""
    return git(repo, "show", f"{ref}:{path}")


def schema_version_in(source: str) -> int | None:
    match = _SCHEMA_LINE.search(source)
    return int(match.group(1)) if match else None


def project_version_in(pyproject: str) -> str | None:
    try:
        data = tomllib.loads(pyproject)
    except tomllib.TOMLDecodeError:
        return None
    project = data.get("project")
    value = project.get("version") if isinstance(project, dict) else None
    return value if isinstance(value, str) and value.strip() else None


# ------------------------------------------------------------------- roots


def find_root(start: Path | None) -> Path:
    """``Root.discover`` without importing it: walk up for ``.tfs/``."""
    origin = (Path.cwd() if start is None else Path(start)).resolve()
    for candidate in (origin, *origin.parents):
        if (candidate / TFS_DIR).is_dir():
            return candidate
    raise UpdateError(
        f"{origin} is not inside a TagFileSystem root (run `tfs init` first)"
    )


def read_lock(root: Path) -> dict[str, Any] | None:
    """``.tfs/lock`` as written by ``root.LockInfo.dumps``; ``None`` when
    absent or unusable."""
    try:
        data = json.loads((root / TFS_DIR / LOCK_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    pid, hostname, created_at = (
        data.get("pid"),
        data.get("hostname"),
        data.get("created_at"),
    )
    if isinstance(pid, bool) or not isinstance(pid, int) or not 0 < pid <= _MAX_PID:
        return None
    if not isinstance(hostname, str) or not hostname:
        return None
    if isinstance(created_at, bool) or not isinstance(created_at, (int, float)):
        return None
    port = data.get("port")
    bind = data.get("bind")
    return {
        "pid": pid,
        "hostname": hostname,
        "created_at": float(created_at),
        "port": port if isinstance(port, int) and not isinstance(port, bool) else None,
        "bind": bind if isinstance(bind, str) and bind.strip() else None,
        "upgrade": data.get("upgrade") is True,
    }


def lock_state(root: Path) -> tuple[str, dict[str, Any] | None]:
    """``free`` / ``daemon`` / ``upgrade`` (a live process on this host),
    ``foreign`` (another host's, recent) or ``stale``."""
    info = read_lock(root)
    if info is None:
        return "free", None
    if info["hostname"] == socket.gethostname():
        if not pid_alive(info["pid"]):
            return "stale", info
        return ("upgrade" if info["upgrade"] else "daemon"), info
    if time.time() - info["created_at"] > STALE_FOREIGN_LOCK_SECONDS:
        return "stale", info
    return "foreign", info


def write_marker(root: Path) -> None:
    """Step 3: take ``.tfs/lock`` for this upgrade. Replaces the daemon's
    own lock — the daemon only ever removes a lock it wrote, so it leaves the
    marker alone when it stops — and, since the pid is this process's, a
    marker outlived by a crash goes stale like any lock."""
    lock = root / TFS_DIR / LOCK_FILE
    data = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "created_at": time.time(),
        "port": None,
        "bind": None,
        "upgrade": True,
    }
    tmp = lock.with_name(f"{lock.name}.{os.getpid()}.upgrade.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, lock)


def remove_marker(root: Path) -> bool:
    """Drop the marker, and only the marker: never someone else's lock."""
    info = read_lock(root)
    if (
        info is None
        or not info["upgrade"]
        or info["pid"] != os.getpid()
        or info["hostname"] != socket.gethostname()
    ):
        return False
    try:
        (root / TFS_DIR / LOCK_FILE).unlink()
    except FileNotFoundError:
        pass
    return True


def daemon_config(root: Path) -> tuple[str, int, float]:
    """``[daemon] bind, port, stop_timeout_seconds`` of the root, defaults
    for anything missing or broken (a running daemon's lock knows better)."""
    bind, port, stop = DEFAULT_BIND, DEFAULT_PORT, DEFAULT_STOP_TIMEOUT
    try:
        data = tomllib.loads(
            (root / TFS_DIR / CONFIG_FILE).read_text(encoding="utf-8-sig")
        )
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return bind, port, stop
    daemon = data.get("daemon") if isinstance(data, dict) else None
    if not isinstance(daemon, dict):
        return bind, port, stop
    if isinstance(daemon.get("bind"), str) and daemon["bind"].strip():
        bind = daemon["bind"].strip()
    candidate = daemon.get("port")
    if isinstance(candidate, int) and not isinstance(candidate, bool):
        if 0 < candidate <= 65535:
            port = candidate
    candidate = daemon.get("stop_timeout_seconds")
    if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
        if candidate >= 0:
            stop = float(candidate)
    return bind, port, stop


def read_token(root: Path) -> str:
    try:
        return (root / TFS_DIR / TOKEN_FILE).read_text(encoding="utf-8").strip()
    except OSError as e:
        raise UpdateError(f"cannot read the token of {root}: {e}") from None


def read_user_version(db: Path) -> int:
    try:
        connection = sqlite3.connect(db, timeout=5.0)
    except sqlite3.Error as e:
        raise UpdateError(f"cannot open {db}: {e}") from None
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.Error as e:
        raise UpdateError(f"cannot read {db}: {e}") from None
    finally:
        connection.close()


@dataclass
class RootState:
    path: str
    running: bool = False
    pid: int | None = None
    bind: str | None = None
    port: int | None = None
    stop_timeout: float = DEFAULT_STOP_TIMEOUT
    user_version: int | None = None  # None: no database file
    skipped: str | None = None  # why the upgrade leaves this root alone
    upgrading: int | None = None  # pid of another upgrade's marker
    snapshot: str | None = None  # filled in by the orchestrator

    @property
    def root(self) -> Path:
        return Path(self.path)

    @property
    def db(self) -> Path:
        return self.root.joinpath(TFS_DIR, *DB_FILE)


def inspect_root(root: Path) -> RootState:
    state = RootState(path=str(root))
    state.bind, state.port, state.stop_timeout = daemon_config(root)
    kind, info = lock_state(root)
    if kind == "daemon" and info is not None:
        state.running = True
        state.pid = info["pid"]
        state.bind = info["bind"] or state.bind
        state.port = info["port"] or state.port
    elif kind == "foreign" and info is not None:
        state.skipped = f"its daemon runs on {info['hostname']} (pid {info['pid']})"
    elif kind == "upgrade" and info is not None:
        state.upgrading = info["pid"]
        state.skipped = f"an upgrade is already in progress (pid {info['pid']})"
    if state.db.is_file():
        state.user_version = read_user_version(state.db)
    return state


# ----------------------------------------------------------------- control


def _connect_host(bind: str) -> str:
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(bind, bind)
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return host


def control_call(
    bind: str, port: int, token: str, method: str, path: str, timeout: float = 5.0
) -> dict[str, Any]:
    """One request on the daemon's control channel (``services.control``)."""
    url = f"http://{_connect_host(bind)}:{port}{path}"
    request = urllib.request.Request(
        url, method=method, headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise UpdateError(f"the daemon answered {e.code} on {path}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ControlDown(f"no daemon answers at {url}: {e}") from None


# ----------------------------------------------------------------- backups


@dataclass
class Backup:
    path: Path
    size: int
    label: str  # the tag (or master-<hash>) the snapshot came from
    created: datetime | None


def backups_dir(root: Path) -> Path:
    return root / TFS_DIR / BACKUPS_DIR


def snapshot_name(label: str, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{_UNSAFE_LABEL.sub('_', label).strip('_') or 'unknown'}.db"


def _verify_database(path: Path) -> None:
    try:
        connection = sqlite3.connect(path, timeout=5.0)
        try:
            verdict = connection.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            connection.close()
    except sqlite3.Error as e:
        raise UpdateError(f"{path} is not a usable database: {e}") from None
    if verdict != "ok":
        raise UpdateError(f"{path} failed quick_check: {verdict}")


def snapshot(root: Path, label: str, now: datetime | None = None) -> Path:
    """``VACUUM INTO .tfs/backups/<utc>-<label>.db`` (step 5): WAL is on, so
    a plain file copy would be a silently incomplete backup. The copy is
    checked before it counts as one."""
    db = root.joinpath(TFS_DIR, *DB_FILE)
    if not db.is_file():
        raise UpdateError(f"{db} does not exist")
    folder = backups_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / snapshot_name(label, now)
    counter = 1
    while target.exists():
        counter += 1
        target = folder / f"{snapshot_name(label, now)[:-3]}-{counter}.db"
    try:
        connection = sqlite3.connect(db, timeout=30.0)
        try:
            connection.execute("VACUUM INTO ?", (str(target),))
        finally:
            connection.close()
        _verify_database(target)
    except (sqlite3.Error, UpdateError) as e:
        try:
            target.unlink()
        except OSError:
            pass
        raise UpdateError(f"snapshot of {db} failed: {e}") from None
    return target


def list_backups(root: Path) -> list[Backup]:
    """Snapshots of ``root``, newest first."""
    folder = backups_dir(root)
    found: list[Backup] = []
    try:
        entries = list(folder.iterdir())
    except OSError:
        return found
    for path in entries:
        match = _BACKUP_NAME.match(path.name)
        if match is None or not path.is_file():
            continue
        try:
            created: datetime | None = datetime.strptime(
                match.group(1), "%Y%m%dT%H%M%SZ"
            ).replace(tzinfo=UTC)
        except ValueError:
            created = None
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        found.append(
            Backup(path=path, size=size, label=match.group(2), created=created)
        )
    found.sort(key=lambda b: b.path.name, reverse=True)
    return found


def prune_backups(
    root: Path, keep: int = KEEP_DEFAULT, dry_run: bool = False
) -> list[Backup]:
    """Remove all but the ``keep`` newest snapshots; returns what was (or
    would be) removed."""
    if keep < 0:
        raise ValueError("keep must be at least 0")
    doomed = list_backups(root)[keep:]
    if not dry_run:
        for backup in doomed:
            backup.path.unlink()
    return doomed


def restore_snapshot(root: Path, snapshot_path: Path) -> None:
    """Put a snapshot back as the live database. The daemon must be stopped:
    the WAL and shm sidecars of the *current* database are removed, since
    they belong to the state being discarded."""
    _verify_database(snapshot_path)
    db = root.joinpath(TFS_DIR, *DB_FILE)
    staged = db.with_name(db.name + ".restoring")
    shutil.copy2(snapshot_path, staged)
    for suffix in ("-wal", "-shm"):
        try:
            db.with_name(db.name + suffix).unlink()
        except FileNotFoundError:
            pass
    os.replace(staged, db)


# -------------------------------------------------------------------- plan


@dataclass
class TestResult:
    run: int
    passed: int
    skipped: int


@dataclass
class Plan:
    """Everything the orchestrator needs, decided before anything is touched
    and written to JSON for the re-exec'd copy."""

    repo: str
    python: str  # the venv interpreter: runs pytest and tfs
    from_hash: str
    from_tag: str | None
    from_ref: str  # what a revert checks out
    from_version: str
    from_schema: int | None
    to_tag: str
    to_hash: str
    to_version: str
    to_schema: int
    roots: list[RootState] = field(default_factory=list)
    skip_tests: bool = False
    keep: int = KEEP_DEFAULT
    health_timeout: float = HEALTH_TIMEOUT
    interactive: bool = False
    # Overrides for tests (a fake uv, a fake tfs): {"uv": [...], ...}
    commands: dict[str, list[str]] = field(default_factory=dict)
    started_at: float = 0.0

    def command(self, name: str) -> list[str]:
        defaults = {
            "uv": ["uv"],
            # -P: never let the working directory shadow the package
            "tfs": [self.python, "-P", "-m", f"{PACKAGE}.cli"],
            "pytest": [self.python, "-P", "-m", "pytest"],
        }
        return list(self.commands.get(name) or defaults[name])

    @property
    def affected(self) -> list[RootState]:
        return [r for r in self.roots if not r.skipped]

    @property
    def running(self) -> list[RootState]:
        return [r for r in self.affected if r.running]

    @property
    def schema_changes(self) -> bool:
        return any(
            r.user_version is not None and r.user_version != self.to_schema
            for r in self.affected
        )

    @property
    def snapshot_label(self) -> str:
        return self.from_tag or f"master-{short(self.from_hash)}"

    def dump(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Plan":
        data = json.loads(path.read_text(encoding="utf-8"))
        data["roots"] = [RootState(**r) for r in data.get("roots", [])]
        return cls(**data)


@dataclass
class Check:
    """What ``tfs update`` reports (nothing is changed)."""

    repo: str
    head: HeadState
    current_version: str
    current_schema: int | None
    tags: list[str]  # annotated release tags on origin, newest first
    target: str | None
    to_hash: str | None
    to_version: str | None
    to_schema: int | None
    available: bool
    roots: list[RootState]

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "current": {
                "version": self.current_version,
                "hash": self.head.commit,
                "branch": self.head.branch,
                "tag": self.head.tag,
                "schema": self.current_schema,
            },
            "latest": None
            if self.target is None
            else {
                "tag": self.target,
                "hash": self.to_hash,
                "version": self.to_version,
                "schema": self.to_schema,
            },
            "available": self.available,
            "tags": self.tags,
            "roots": [asdict(r) for r in self.roots],
        }


def repo_dir() -> Path | None:
    """The checkout this module runs from; mirrors ``version.repo_dir``."""
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / "pyproject.toml").is_file() and (candidate / ".git").exists():
        return candidate
    return None


def check(
    root: Path,
    *,
    repo: Path | None = None,
    to: str | None = None,
    fetch: bool = True,
) -> Check:
    """Preflight without consequences (``tfs update``): register the root,
    read the registry, inspect the checkout, ask origin for its tags."""
    register_root(root)
    roots = [inspect_root(r) for r in known_roots()]

    repo = repo if repo is not None else repo_dir()
    if repo is None:
        raise UpdateError(
            "this install is not a git checkout; self-update needs one "
            "(git clone, then uv sync)"
        )
    if not (repo / ".git").exists():
        raise UpdateError(f"{repo} has no .git directory")
    git(repo, "--version")
    head = head_state(repo)
    if head.dirty:
        raise UpdateError(f"{repo} has uncommitted changes; commit or stash them first")
    if head.branch is not None and head.branch != "master":
        raise UpdateError(
            f"HEAD is on branch {head.branch}; self-update works from master or "
            f"from a release tag"
        )
    if head.branch is None and head.tag is None:
        raise UpdateError(
            f"HEAD is detached at {short(head.commit)}, not at a release tag; "
            f"self-update works from master or from a release tag"
        )

    annotated, lightweight = remote_tags(repo)
    if fetch:
        git(repo, "fetch", "--tags", "--quiet", "origin", timeout=600.0)
    releases = {
        name: commit for name, commit in annotated.items() if parse_version(name)
    }
    tags = sorted(releases, key=lambda t: parse_version(t) or (0, 0, 0), reverse=True)

    current_version = project_version_in(_read(repo / "pyproject.toml")) or "0.0.0"
    current_schema = schema_version_in(_read(repo / SCHEMA_FILE))

    if to is not None:
        if to not in releases:
            if to in lightweight:
                raise UpdateError(
                    f"tag {to} on origin is lightweight; self-update follows "
                    f"annotated tags only"
                )
            if to in annotated:
                raise UpdateError(f"tag {to} is not a release tag (vA.B.C)")
            raise UpdateError(f"origin has no tag {to}")
        target: str | None = to
    else:
        target = tags[0] if tags else None

    to_hash = to_version = None
    to_schema = None
    available = False
    if target is not None:
        to_hash = releases[target]
        to_version = project_version_in(file_at(repo, target, "pyproject.toml"))
        to_schema = schema_version_in(file_at(repo, target, SCHEMA_FILE))
        if to_version is None or to_schema is None:
            raise UpdateError(
                f"cannot read the version or SCHEMA_VERSION of {target}; is it a "
                f"TagFileSystem release?"
            )
        if to is not None:
            available = to_hash != head.commit
        else:
            here, there = parse_version(current_version), parse_version(to_version)
            available = to_hash != head.commit and (
                here is None or there is None or there > here
            )
    return Check(
        repo=str(repo),
        head=head,
        current_version=current_version,
        current_schema=current_schema,
        tags=tags,
        target=target,
        to_hash=to_hash,
        to_version=to_version,
        to_schema=to_schema,
        available=available,
        roots=roots,
    )


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise UpdateError(f"cannot read {path}: {e}") from None


def preflight(
    root: Path,
    *,
    repo: Path | None = None,
    to: str | None = None,
    skip_tests: bool = False,
    wait: float | None = None,
    python: str | None = None,
    commands: dict[str, list[str]] | None = None,
    health_timeout: float = HEALTH_TIMEOUT,
    keep: int = KEEP_DEFAULT,
    interactive: bool = False,
) -> Plan:
    """Step 1 of §7: everything that can be checked while the daemon still
    serves, every network operation included. Raises ``UpdateError`` with
    nothing touched, or ``NothingToDo``."""
    report = check(root, repo=repo, to=to)
    if report.target is None or report.to_hash is None:
        raise UpdateError("origin has no release tags (annotated vA.B.C)")
    if report.to_hash == report.head.commit:
        raise NothingToDo(
            f"already at {report.target} ({short(report.to_hash)}); nothing to do"
        )
    if to is None and not report.available:
        raise NothingToDo(
            f"{report.head.describe(report.current_version)} is not behind the "
            f"latest release {report.target} ({report.to_version}); nothing to do "
            f"(--to TAG moves to a tag explicitly)"
        )
    assert report.to_version is not None and report.to_schema is not None

    for state in report.roots:
        if state.upgrading is not None:
            raise UpdateError(
                f"an upgrade is already in progress for {state.path} "
                f"(pid {state.upgrading}); wait for it to finish"
            )
    affected = [r for r in report.roots if not r.skipped]
    for state in affected:
        if state.user_version is not None and state.user_version > report.to_schema:
            raise UpdateError(
                f"{report.target} has schema version {report.to_schema}, below "
                f"{state.path} (schema {state.user_version}): that daemon would "
                f"refuse to open its database; not downgrading"
            )

    plan = Plan(
        repo=report.repo,
        python=python or sys.executable,
        from_hash=report.head.commit,
        from_tag=report.head.tag,
        from_ref=report.head.ref,
        from_version=report.current_version,
        from_schema=report.current_schema,
        to_tag=report.target,
        to_hash=report.to_hash,
        to_version=report.to_version,
        to_schema=report.to_schema,
        roots=report.roots,
        skip_tests=skip_tests,
        keep=keep,
        health_timeout=health_timeout,
        interactive=interactive,
        commands=commands or {},
        started_at=time.time(),
    )

    uv = plan.command("uv")
    if shutil.which(uv[0]) is None and not Path(uv[0]).exists():
        raise UpdateError(f"{uv[0]} is not installed or not on PATH")
    result = run_command([*uv, "--version"], timeout=60.0)
    if result.returncode != 0:
        raise UpdateError(f"uv --version failed: {_tail(result.stderr, 3)}")
    if not skip_tests:
        result = run_command([*plan.command("pytest"), "--version"], timeout=120.0)
        if result.returncode != 0:
            raise UpdateError(
                "pytest is not importable here; `uv sync` installs it (dev group), "
                "or pass --skip-tests"
            )

    _drain(plan, wait)
    return plan


def _drain(plan: Plan, wait: float | None) -> None:
    """Refuse while runs are in flight; ``wait`` seconds of grace first
    (default: the roots' ``stop_timeout_seconds``). Never interrupt a run to
    upgrade."""
    running = plan.running
    if not running:
        return
    if wait is None:
        wait = max(r.stop_timeout for r in running)
    deadline = time.monotonic() + max(0.0, wait)
    tokens = {r.path: read_token(r.root) for r in running}
    while True:
        busy: dict[str, list[str]] = {}
        for state in running:
            assert state.bind is not None and state.port is not None
            try:
                health = control_call(
                    state.bind, state.port, tokens[state.path], "GET", "/health"
                )
            except ControlDown as e:
                raise UpdateError(
                    f"the daemon of {state.path} (pid {state.pid}) holds the lock "
                    f"but does not answer: {e}; `tfs stop` it first"
                ) from None
            in_flight = health.get("in_flight") or []
            if in_flight:
                busy[state.path] = [str(i) for i in in_flight]
        if not busy:
            return
        if time.monotonic() >= deadline:
            listed = "; ".join(f"{p}: {len(ids)} run(s)" for p, ids in busy.items())
            raise UpdateError(
                f"runs are in flight ({listed}); an upgrade never interrupts a run: "
                f"try again later, or raise --wait"
            )
        time.sleep(1.0)


# ------------------------------------------------------------ orchestrator


class StepFailed(Exception):
    """A step after preflight failed: revert."""


class RevertFailed(Exception):
    """The revert itself failed: stop, print the manual steps."""

    def __init__(self, failures: list[str]) -> None:
        super().__init__("; ".join(failures))
        self.failures = failures


def say(message: str) -> None:
    print(f"upgrade: {message}", flush=True)


def _checkout(plan: Plan, ref: str, expected: str) -> None:
    args = ["checkout", "--quiet"]
    if ref.startswith("refs/tags/") or _HASH.fullmatch(ref):
        args.append("--detach")
    try:
        git(plan.repo, *args, ref)
    except UpdateError as e:
        raise StepFailed(str(e)) from None
    actual = git(plan.repo, "rev-parse", "HEAD")
    if actual != expected:
        raise StepFailed(
            f"after checkout of {ref}, HEAD is {short(actual)} instead of "
            f"{short(expected)}"
        )


def _sync(plan: Plan) -> None:
    # --no-install-project --inexact: see the module docstring.
    result = run_command(
        [*plan.command("uv"), "sync", "--locked", "--no-install-project", "--inexact"],
        cwd=plan.repo,
        timeout=SYNC_TIMEOUT,
    )
    if result.returncode != 0:
        raise StepFailed(f"uv sync failed:\n{_tail(result.stderr or result.stdout)}")


_SUMMARY = re.compile(r"(\d+) (passed|failed|skipped|error|errors)\b")


def parse_pytest_summary(output: str) -> TestResult:
    counts = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
    for line in reversed(output.splitlines()):
        found = _SUMMARY.findall(line)
        if found:
            for number, kind in found:
                counts["error" if kind.startswith("error") else kind] += int(number)
            break
    return TestResult(
        run=sum(counts.values()), passed=counts["passed"], skipped=counts["skipped"]
    )


def _run_tests(plan: Plan) -> TestResult:
    say("running the test suite")
    result = run_command(
        [*plan.command("pytest"), "-q", "-p", "no:cacheprovider"],
        cwd=plan.repo,
        timeout=TESTS_TIMEOUT,
    )
    summary = parse_pytest_summary(result.stdout)
    if result.returncode != 0:
        raise StepFailed(
            f"the test suite failed:\n{_tail(result.stdout or result.stderr, 15)}"
        )
    say(f"tests: {summary.passed} passed, {summary.skipped} skipped")
    return summary


def _stop_daemon(state: RootState, timeout: float) -> None:
    if state.pid is None or not pid_alive(state.pid):
        return
    assert state.bind is not None and state.port is not None
    token = read_token(state.root)
    try:
        control_call(state.bind, state.port, token, "POST", "/stop")
    except ControlDown as e:
        if pid_alive(state.pid):
            raise UpdateError(
                f"the daemon of {state.path} (pid {state.pid}) does not answer: {e}"
            ) from None
        return
    deadline = time.monotonic() + timeout
    while pid_alive(state.pid):
        if time.monotonic() >= deadline:
            raise UpdateError(
                f"the daemon of {state.path} (pid {state.pid}) did not stop within "
                f"{timeout:.0f}s"
            )
        time.sleep(0.2)


def _stop_by_lock(state: RootState, timeout: float) -> None:
    """Stop whatever daemon holds the root now (a daemon the upgrade
    started): its address comes from the lock it wrote."""
    kind, info = lock_state(state.root)
    if kind != "daemon" or info is None:
        return
    current = RootState(
        path=state.path,
        running=True,
        pid=info["pid"],
        bind=info["bind"] or state.bind,
        port=info["port"] or state.port,
        stop_timeout=state.stop_timeout,
    )
    _stop_daemon(current, timeout)


def _start_daemon(plan: Plan, state: RootState) -> None:
    remove_marker(state.root)
    result = run_command(
        [*plan.command("tfs"), "--root", state.path, "start", "-d"],
        cwd=state.path,
        timeout=START_TIMEOUT,
    )
    if result.returncode != 0:
        raise StepFailed(
            f"tfs start failed for {state.path}:\n{_tail(result.stdout + result.stderr)}"
        )


def _verify_health(plan: Plan, state: RootState) -> None:
    """Step 9: the daemon that came up must run the target commit."""
    kind, info = lock_state(state.root)
    bind, port = state.bind or DEFAULT_BIND, state.port or DEFAULT_PORT
    if kind == "daemon" and info is not None:
        bind, port = info["bind"] or bind, info["port"] or port
    token = read_token(state.root)
    deadline = time.monotonic() + plan.health_timeout
    last = "no answer yet"
    while time.monotonic() < deadline:
        try:
            health = control_call(bind, port, token, "GET", "/health", timeout=2.0)
        except ControlDown as e:
            last = str(e)
            time.sleep(0.5)
            continue
        got_hash, got_version = health.get("hash"), health.get("version")
        if got_hash != plan.to_hash:
            raise StepFailed(
                f"the daemon of {state.path} reports commit {short(got_hash)}, "
                f"not {short(plan.to_hash)}"
            )
        if got_version != plan.to_version:
            say(
                f"note: the daemon of {state.path} reports version {got_version!r}, "
                f"the tag says {plan.to_version!r}"
            )
        say(f"{state.path}: daemon pid {health.get('pid')} runs {short(got_hash)}")
        return
    raise StepFailed(
        f"no /health from the daemon of {state.path} within "
        f"{plan.health_timeout:.0f}s ({last})"
    )


def _record(plan: Plan, state: RootState, tests: TestResult | None) -> str | None:
    """Step 10: the ``upgrades`` row, written by the new code."""
    remove_marker(state.root)
    payload = {
        "from_tag": plan.from_tag,
        "from_hash": plan.from_hash,
        "to_tag": plan.to_tag,
        "to_hash": plan.to_hash,
        "schema_before": state.user_version,
        "tests_run": tests.run if tests else None,
        "tests_passed": tests.passed if tests else None,
        "tests_skipped": tests.skipped if tests else None,
        "snapshot_path": state.snapshot,
        "outcome": "ok",
        "started_at": plan.started_at,
    }
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", prefix="tfs-upgrade-", delete=False, encoding="utf-8"
    ) as handle:
        json.dump(payload, handle)
        record_path = handle.name
    try:
        result = run_command(
            [*plan.command("tfs"), "--root", state.path, "record-upgrade", record_path],
            cwd=state.path,
            timeout=START_TIMEOUT,
        )
    finally:
        try:
            os.unlink(record_path)
        except OSError:
            pass
    if result.returncode != 0:
        return (
            f"{state.path}: the upgrade row could not be written "
            f"({_tail(result.stdout + result.stderr, 3)})"
        )
    return None


def _revert(
    plan: Plan,
    restore: bool,
    snapshots: dict[str, Path],
    started_new: list[RootState],
    stopped: list[RootState],
) -> None:
    failures: list[str] = []
    for state in started_new:
        try:
            _stop_by_lock(state, state.stop_timeout + STOP_GRACE_SECONDS)
        except UpdateError as e:
            failures.append(f"stop the new daemon of {state.path}: {e}")
    if restore:
        for path, snap in snapshots.items():
            try:
                restore_snapshot(Path(path), snap)
                say(f"restored {path} from {snap}")
            except (UpdateError, OSError) as e:
                failures.append(f"restore {path} from {snap}: {e}")
    try:
        _checkout(plan, plan.from_ref, plan.from_hash)
        _sync(plan)
        say(f"code back at {plan.from_version} ({short(plan.from_hash)})")
    except (StepFailed, UpdateError) as e:
        failures.append(f"code revert: {e}")
    if failures:
        raise RevertFailed(failures)
    _restart(plan, stopped)


def _restart(plan: Plan, stopped: list[RootState]) -> None:
    for state in stopped:
        try:
            _start_daemon(plan, state)
            say(f"{state.path}: daemon restarted")
        except StepFailed as e:
            say(f"{state.path}: the daemon did not restart ({e}); run `tfs start -d`")


def manual_steps(plan: Plan, snapshots: dict[str, Path], failures: list[str]) -> str:
    lines = [
        "The upgrade failed and could not be reverted automatically; nothing more",
        "will be attempted. What failed:",
        *(f"  - {f}" for f in failures),
        "To restore by hand:",
        f"  1. cd {plan.repo}",
        f"  2. git checkout {plan.from_ref}      # commit {plan.from_hash}",
        "  3. uv sync --locked",
    ]
    step = 4
    for path, snap in snapshots.items():
        db = Path(path).joinpath(TFS_DIR, *DB_FILE)
        lines.append(f"  {step}. copy {snap}")
        lines.append(f"     over {db}")
        lines.append(f"     and delete {db.name}-wal and {db.name}-shm next to it")
        step += 1
    for state in plan.affected:
        lock = state.root / TFS_DIR / LOCK_FILE
        lines.append(f"  {step}. delete {lock} if it names pid {os.getpid()}")
        step += 1
    for state in plan.running:
        lines.append(f"  {step}. tfs --root {state.path} start -d")
        step += 1
    return "\n".join(lines)


def run_plan(plan: Plan) -> int:
    """Steps 3-11 of §7, on the temporary copy. Exit code: 0 upgraded,
    1 failed and reverted (or refused), 2 failed and the revert failed too."""
    roots, running = plan.affected, plan.running
    marked: list[RootState] = []
    stopped: list[RootState] = []
    started_new: list[RootState] = []
    snapshots: dict[str, Path] = {}
    stage = "prepare"
    say(
        f"{plan.from_version} ({short(plan.from_hash)}) -> {plan.to_tag} "
        f"({short(plan.to_hash)}) in {plan.repo}"
    )
    try:
        for state in roots:
            write_marker(state.root)
            marked.append(state)
        say(f"locked {len(marked)} root(s)")

        for state in running:
            say(f"{state.path}: stopping the daemon (pid {state.pid})")
            _stop_daemon(state, state.stop_timeout + STOP_GRACE_SECONDS)
            stopped.append(state)

        for state in roots:
            if state.user_version is None:
                continue
            path = snapshot(state.root, plan.snapshot_label)
            state.snapshot = str(path)
            snapshots[state.path] = path
            say(
                f"{state.path}: snapshot {path.name} ({human_size(path.stat().st_size)})"
            )

        stage = "code"
        say(f"checking out {plan.to_tag}")
        _checkout(plan, f"refs/tags/{plan.to_tag}", plan.to_hash)
        say("syncing dependencies")
        _sync(plan)

        stage = "tests"
        tests = None if plan.skip_tests else _run_tests(plan)

        stage = "start"
        for state in running:
            say(f"{state.path}: starting the daemon")
            started_new.append(state)
            _start_daemon(plan, state)
            _verify_health(plan, state)

        stage = "record"
        problems = [
            p
            for p in (
                _record(plan, s, tests) for s in roots if s.user_version is not None
            )
            if p
        ]

        for state in roots:
            removed = prune_backups(state.root, plan.keep)
            if removed:
                say(f"{state.path}: pruned {len(removed)} older snapshot(s)")
        _offer_cleanup(plan, snapshots)

        for problem in problems:
            say(problem)
        if problems:
            say(
                f"upgraded to {plan.to_version} ({short(plan.to_hash)}), but "
                f"{len(problems)} root(s) could not record it"
            )
            return 1
        say(f"upgraded to {plan.to_version} ({short(plan.to_hash)})")
        return 0
    except (StepFailed, UpdateError) as e:
        say(f"failed while {_describe_stage(stage)}: {e}")
        if stage == "prepare":
            _restart(plan, stopped)
            say("nothing was changed")
            return 1
        try:
            _revert(plan, stage in ("start", "record"), snapshots, started_new, stopped)
        except RevertFailed as failure:
            print(manual_steps(plan, snapshots, failure.failures), flush=True)
            return 2
        say(
            "reverted"
            + (" (code and every database)" if stage in ("start", "record") else "")
            + f"; the snapshots stay in .tfs/{BACKUPS_DIR}/"
        )
        return 1
    finally:
        for state in marked:
            remove_marker(state.root)


def _describe_stage(stage: str) -> str:
    return {
        "prepare": "preparing (locks, stopping daemons, snapshots)",
        "code": "swapping the code",
        "tests": "testing",
        "start": "starting the daemons",
        "record": "recording",
    }.get(stage, stage)


def _offer_cleanup(plan: Plan, snapshots: dict[str, Path]) -> None:
    """Step 11: this upgrade's snapshots only, default keep."""
    if not snapshots:
        return
    total = sum(p.stat().st_size for p in snapshots.values() if p.exists())
    say(f"this upgrade's snapshot(s), {human_size(total)}:")
    for path in snapshots.values():
        say(f"  {path}")
    if not plan.interactive:
        say(f"kept (`tfs backup prune` trims to the newest {plan.keep})")
        return
    try:
        answer = input("upgrade: delete them now? [y/N] ")
    except (EOFError, OSError):
        answer = ""
    if answer.strip().lower() in ("y", "yes"):
        for path in snapshots.values():
            try:
                path.unlink()
            except OSError:
                pass
        say("deleted")
    else:
        say("kept")


# -------------------------------------------------------------- entrypoint


def print_plan(plan: Plan) -> None:
    print("upgrade plan")
    print(f"  checkout   {plan.repo}")
    where = f"at {plan.from_tag}" if plan.from_tag else f"on {plan.from_ref}"
    print(f"  from       {plan.from_version} ({short(plan.from_hash)}) {where}")
    print(f"  to         {plan.to_tag} = {plan.to_version} ({short(plan.to_hash)})")
    if plan.schema_changes:
        print(f"  schema     changes to {plan.to_schema} (migration on first open)")
    else:
        print(f"  schema     {plan.to_schema}, unchanged")
    print(
        "  tests      skipped (--skip-tests)"
        if plan.skip_tests
        else "  tests      full suite before the daemons restart"
    )
    print(f"  roots      {len(plan.roots)} known ({registry_path()}):")
    for state in plan.roots:
        if state.skipped:
            print(f"    {state.path}: skipped, {state.skipped}")
            continue
        daemon = f"daemon pid {state.pid}" if state.running else "no daemon"
        if state.user_version is None:
            schema = "no database"
        elif state.user_version == plan.to_schema:
            schema = f"schema {state.user_version} unchanged"
        else:
            schema = f"schema {state.user_version} -> {plan.to_schema}"
        line = f"    {state.path}: {daemon}, {schema}"
        if state.user_version is not None:
            line += f", snapshot {TFS_DIR}/{BACKUPS_DIR}/{snapshot_name(plan.snapshot_label)}"
        print(line)


def hand_off(plan: Plan) -> int:
    """§6: copy this module out of the checkout and run it there, on the
    base interpreter, forwarding its exit code."""
    workdir = Path(tempfile.mkdtemp(prefix="tfs-upgrade-"))
    script = workdir / "tfs_updater.py"
    plan_path = workdir / "plan.json"
    try:
        shutil.copy2(__file__, script)
        plan.dump(plan_path)
        python = getattr(sys, "_base_executable", None) or sys.executable
        result = subprocess.run([python, "-P", str(script), "--plan", str(plan_path)])
        return result.returncode
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    """``tfs upgrade [--root R] [--to TAG] [--dry-run] [--yes] [--skip-tests]
    [--wait SEC]``, reached through ``tag_file_system.__main__`` before
    anything outside the standard library is imported."""
    parser = argparse.ArgumentParser(
        prog="tfs upgrade",
        description="Move this checkout to a release tag and restart the daemons "
        "(DESIGN/v0-2-0.md). Snapshots every registered root first; a failed "
        "upgrade is reverted.",
    )
    parser.add_argument(
        "--root",
        "-r",
        type=Path,
        default=None,
        help="The managed root (default: discovered from CWD).",
    )
    parser.add_argument(
        "--to",
        metavar="TAG",
        default=None,
        help="A release tag on origin (default: the newest).",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the plan and change nothing."
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Consent to a schema change without a prompt.",
    )
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="Skip the test suite (and nothing else).",
    )
    parser.add_argument(
        "--wait",
        type=float,
        metavar="SEC",
        default=None,
        help="Seconds to wait for in-flight runs to finish (default: stop_timeout_seconds).",
    )
    args = parser.parse_args(argv)

    interactive = sys.stdin is not None and sys.stdin.isatty()
    try:
        root = find_root(args.root)
        plan = preflight(
            root,
            to=args.to,
            skip_tests=args.skip_tests,
            wait=args.wait,
            interactive=interactive and not args.yes,
        )
    except NothingToDo as e:
        print(e)
        return 0
    except UpdateError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print_plan(plan)
    if args.dry_run:
        return 0
    if plan.schema_changes:
        if not args.yes:
            if not interactive:
                print(
                    "error: the schema changes; pass --yes to consent without a prompt",
                    file=sys.stderr,
                )
                return 1
            try:
                answer = input("Proceed? [y/N] ")
            except (EOFError, OSError):
                answer = ""
            if answer.strip().lower() not in ("y", "yes"):
                print("aborted; nothing was changed")
                return 1
    else:
        print("the schema does not change: no migration, no prompt")
    return hand_off(plan)


if __name__ == "__main__":  # the re-exec'd copy
    _parser = argparse.ArgumentParser()
    _parser.add_argument("--plan", required=True, type=Path)
    sys.exit(run_plan(Plan.load(_parser.parse_args().plan)))
