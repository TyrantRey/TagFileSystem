# Code by AkinoAlice@TyrantRey

"""The managed root (DESIGN.md §2): discovery, ``init``, zones, lock, token.

<root>/
  .tfs/config.toml   .tfs/db/   .tfs/lock   .tfs/token
  script/            add-ons
  ...                everything else is managed data
"""

import json
import math
import os
import secrets
import shutil
import socket
import sys
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath

from tag_file_system.config import Config
from tag_file_system.core.paths import has_parent_reference, is_anchored

TFS_DIR = ".tfs"
SCRIPT_DIR = "script"
CONFIG_FILE = "config.toml"
DB_DIR = "db"
DB_FILE = "system.db"
LOCK_FILE = "lock"
TOKEN_FILE = "token"

# A lock from *another host* older than this is assumed abandoned (its pid
# cannot be checked from here). Locks from this host are judged by the pid.
DEFAULT_STALE_AFTER_SECONDS = 6 * 3600

_MAX_PID = 2**31 - 1
_LOCK_TIMEOUT_SECONDS = 5.0  # acquire gives up after this
_LOCK_GRACE_SECONDS = 2.0  # an unreadable lock younger than this is "being written"
_ABSURD_FUTURE = 10 * 365 * 24 * 3600  # a created_at further ahead is corrupt, not skew


class RootError(Exception):
    """Base class for root-layout errors."""


class NotARoot(RootError):
    """No ``.tfs/`` directory here or in any ancestor."""


class RootExists(RootError):
    """``init`` refused: the directory, an ancestor or a descendant is a root."""


class OutsideRoot(RootError):
    """A path was given that is not under the root."""


class LockHeld(RootError):
    """Another live daemon holds ``.tfs/lock``."""

    def __init__(self, info: "LockInfo") -> None:
        super().__init__(
            f"root is locked by pid {info.pid} on {info.hostname} "
            f"(since {info.created_at_text})"
        )
        self.info = info


class Zone(StrEnum):
    """What a path under the root is, from the engine's point of view."""

    TFS = "tfs"  # .tfs/: ignored by the watcher
    SCRIPT = "script"  # script/: add-ons, routed to the reloader
    DATA = "data"  # everything else: the pipeline


def _same_name(a: str, b: str) -> bool:
    """Compare path components the way the host filesystem does."""
    return os.path.normcase(a) == os.path.normcase(b)


def find_root(start: Path) -> Path | None:
    """The nearest ancestor of ``start`` (inclusive) containing ``.tfs/``."""
    start = Path(start).resolve()
    for candidate in (start, *start.parents):
        if (candidate / TFS_DIR).is_dir():
            return candidate
    return None


def _find_nested_root(directory: Path) -> Path | None:
    """A ``.tfs/`` directory anywhere *below* ``directory``."""
    for current, dirs, _files in os.walk(directory):
        if TFS_DIR in dirs:
            return Path(current)
    return None


def _missing_ancestors(directory: Path) -> list[Path]:
    """The directories ``mkdir(parents=True)`` would create, deepest first."""
    missing: list[Path] = []
    for candidate in (directory, *directory.parents):
        if candidate.exists():
            break
        missing.append(candidate)
    return missing


class Root:
    def __init__(self, path: Path) -> None:
        self.path = Path(path).resolve()

    def __repr__(self) -> str:
        return f"Root({str(self.path)!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Root) and other.path == self.path

    def __hash__(self) -> int:
        return hash(self.path)

    # ---------------------------------------------------------------- layout

    @property
    def tfs_dir(self) -> Path:
        return self.path / TFS_DIR

    @property
    def script_dir(self) -> Path:
        return self.path / SCRIPT_DIR

    @property
    def config_path(self) -> Path:
        return self.tfs_dir / CONFIG_FILE

    @property
    def db_dir(self) -> Path:
        return self.tfs_dir / DB_DIR

    @property
    def db_path(self) -> Path:
        return self.db_dir / DB_FILE

    @property
    def lock_path(self) -> Path:
        return self.tfs_dir / LOCK_FILE

    @property
    def token_path(self) -> Path:
        return self.tfs_dir / TOKEN_FILE

    # ------------------------------------------------------------- lifecycle

    @classmethod
    def discover(cls, start: Path | None = None) -> "Root":
        """The root containing ``start`` (default: CWD), walking up like git."""
        start = Path.cwd() if start is None else start
        found = find_root(start)
        if found is None:
            raise NotARoot(
                f"{Path(start).resolve()} is not inside a TagFileSystem root"
            )
        return cls(found)

    @classmethod
    def init(cls, directory: Path, config: Config | None = None) -> "Root":
        """Turn ``directory`` into a root. Nothing already there is moved.

        Refused when ``directory``, an ancestor or a descendant is already a
        root. Either everything is created or nothing is left behind.
        """
        directory = Path(directory).resolve()
        if directory.exists() and not directory.is_dir():
            raise RootError(f"{directory} exists and is not a directory")

        probe = directory if directory.exists() else directory.parent
        existing = find_root(probe)
        if existing is not None:
            raise RootExists(f"{existing} is already a TagFileSystem root")
        if directory.exists():
            nested = _find_nested_root(directory)
            if nested is not None:
                raise RootExists(f"{nested} inside {directory} is already a root")
            if (directory / TFS_DIR).exists():
                raise RootError(f"{directory / TFS_DIR} exists and is not a directory")

        root = cls(directory)
        created_dirs = _missing_ancestors(directory)
        created_script = not root.script_dir.exists()
        try:
            root.tfs_dir.mkdir(parents=True)
            root.db_dir.mkdir()
            if created_script:
                root.script_dir.mkdir()
            elif not root.script_dir.is_dir():
                raise RootError(f"{root.script_dir} exists and is not a directory")
            (config or Config()).write(root.config_path)
            _write_private(root.token_path, secrets.token_urlsafe(32))
        except BaseException:
            shutil.rmtree(root.tfs_dir, ignore_errors=True)
            if created_script:
                shutil.rmtree(root.script_dir, ignore_errors=True)
            for created in created_dirs:  # deepest first; only if empty
                try:
                    created.rmdir()
                except OSError:
                    break
            raise
        return root

    def load_config(self) -> Config:
        return Config.load(self.config_path)

    def read_token(self) -> str:
        try:
            return self.token_path.read_text(encoding="utf-8").strip()
        except OSError as e:
            raise RootError(f"Cannot read {self.token_path}: {e}") from e

    # ----------------------------------------------------------------- paths

    def relative(self, path: Path) -> PurePosixPath:
        """``path`` relative to the root, as a POSIX path (portable DB key).

        The path is resolved first (symlinks, ``..``, 8.3 names, case on
        case-insensitive filesystems) so one file always yields one key.
        """
        resolved = Path(path).resolve()
        try:
            rel = resolved.relative_to(self.path)
        except ValueError:
            raise OutsideRoot(f"{path} is not under {self.path}") from None
        if has_parent_reference(rel):  # pragma: no cover - resolve() removes them
            raise OutsideRoot(f"{path} is not under {self.path}")
        return PurePosixPath(*rel.parts)

    def absolute(self, relative: PurePosixPath | str) -> Path:
        """The host path for a root-relative key."""
        text = str(relative)
        if is_anchored(text) or has_parent_reference(PurePosixPath(text)):
            raise OutsideRoot(f"{relative!r} is not a root-relative key")
        if os.name == "nt" and PureWindowsPath(text).drive:
            # "D:foo" is drive-relative here; ":" is never in a filename.
            raise OutsideRoot(f"{relative!r} is not a root-relative key")
        return self.path.joinpath(*PurePosixPath(text).parts)

    def zone(self, path: Path) -> Zone:
        parts = self.relative(path).parts
        if not parts:
            return Zone.DATA
        if any(_same_name(part, TFS_DIR) for part in parts):
            return Zone.TFS
        if _same_name(parts[0], SCRIPT_DIR):
            return Zone.SCRIPT
        return Zone.DATA


# -------------------------------------------------------------------- lock


@dataclass(frozen=True)
class LockInfo:
    pid: int
    hostname: str
    created_at: float
    # Where that daemon's control channel actually listens, so a client can
    # reach it without (or despite) config.toml.
    port: int | None = None
    bind: str | None = None

    @property
    def age(self) -> float:
        # A lock stamped by a fast clock is young, not negative-aged.
        return max(0.0, time.time() - self.created_at)

    @property
    def created_at_text(self) -> str:
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.created_at))
        except (ValueError, OverflowError, OSError):
            return f"t={self.created_at!r}"

    def is_mine(self) -> bool:
        return self.pid == os.getpid() and self.hostname == socket.gethostname()

    def is_live_local(self) -> bool:
        """A process on *this* host that is still running: whatever it is
        doing, it is not something ``--force`` may push aside."""
        return self.hostname == socket.gethostname() and pid_alive(self.pid)

    def dumps(self) -> str:
        return json.dumps(
            {
                "pid": self.pid,
                "hostname": self.hostname,
                "created_at": self.created_at,
                "port": self.port,
                "bind": self.bind,
            }
        )


class Lock:
    """``.tfs/lock``: one daemon per root.

    ``acquire`` creates the file with ``O_EXCL``, so two processes racing for
    a free root cannot both win. A lock is *stale* when it was written on
    this host by a pid that is gone, or written on another host longer than
    ``stale_after`` ago (its pid cannot be checked from here). A stale or
    unreadable lock is taken over by renaming it away first — only one
    contender's rename succeeds — and then creating afresh. ``force`` takes
    over unconditionally.
    """

    def __init__(
        self, root: Root, stale_after: float = DEFAULT_STALE_AFTER_SECONDS
    ) -> None:
        self.path = root.lock_path
        self.stale_after = stale_after
        self._held: LockInfo | None = None

    def read(self) -> LockInfo | None:
        """The lock on disk, or ``None`` when absent or unreadable (corrupt
        lock files count as stale)."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
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
        if (
            isinstance(created_at, bool)
            or not isinstance(created_at, (int, float))
            or not math.isfinite(created_at)
            or not 0 < created_at < time.time() + _ABSURD_FUTURE
        ):
            return None
        port = data.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 < port <= 65535:
            port = None
        bind = data.get("bind")
        if not isinstance(bind, str) or not bind.strip():
            bind = None
        return LockInfo(
            pid=pid,
            hostname=hostname,
            created_at=float(created_at),
            port=port,
            bind=bind,
        )

    def is_stale(self, info: LockInfo) -> bool:
        if info.hostname == socket.gethostname():
            return not pid_alive(info.pid)
        return info.age > self.stale_after

    def holder(self) -> LockInfo | None:
        """The live holder, or ``None`` when the root is free."""
        info = self.read()
        if info is None or self.is_stale(info):
            return None
        return info

    def acquire(
        self, force: bool = False, port: int | None = None, bind: str | None = None
    ) -> LockInfo:
        """Take the lock. ``force`` takes over a lock this host cannot judge
        (another host's, or one whose pid is gone) — never one held by a
        process that is still running here."""
        if not self.path.parent.is_dir():
            raise NotARoot(f"{self.path.parent} does not exist")

        mine = LockInfo(
            pid=os.getpid(),
            hostname=socket.gethostname(),
            created_at=time.time(),
            port=port,
            bind=bind,
        )
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        last_error: str | None = None
        while time.monotonic() < deadline:
            try:
                self._create(mine)
            except FileExistsError:
                if self.path.is_dir():
                    raise RootError(f"{self.path} is a directory")
                info = self.read()
                if info is None:
                    # Unreadable: corrupt, or another contender is still
                    # writing it. Give a young file time to become readable
                    # (a future mtime — a fast file server clock — is "old").
                    if 0 <= self._age_on_disk() < _LOCK_GRACE_SECONDS and not force:
                        last_error = "lock file is being written by another process"
                        time.sleep(0.01)
                        continue
                elif not info.is_mine():
                    if force:
                        if info.is_live_local():
                            raise LockHeld(info)  # force never displaces a live daemon
                    elif not self.is_stale(info):
                        raise LockHeld(info)
                last_error = self._evict(force) or last_error
                continue
            except PermissionError as e:
                # Windows: the file is mid-replace/mid-write by another
                # contender, or the path is a directory.
                if self.path.is_dir():
                    raise RootError(f"{self.path} is a directory") from e
                last_error = str(e)
                time.sleep(0.01)
                continue

            self._held = mine
            return mine

        info = self.read()
        if info is not None and not info.is_mine():
            raise LockHeld(info)
        raise RootError(f"Could not acquire {self.path}: {last_error or 'timed out'}")

    def _create(self, mine: LockInfo) -> None:
        """Create the lock *with its content* in one step, so no contender
        can ever observe an empty lock: write a private temp file, then hard
        link it into place (atomic, fails if the lock exists). Filesystems
        without hard links fall back to ``O_EXCL`` create + write."""
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(mine.dumps())
            try:
                os.link(tmp, self.path)
                return
            except FileExistsError:
                raise
            except OSError:
                pass  # no hard links here: fall back
            fd2 = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd2, "w", encoding="utf-8") as handle:
                handle.write(mine.dumps())
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _age_on_disk(self) -> float:
        try:
            return time.time() - self.path.stat().st_mtime
        except OSError:
            return float("inf")

    def _evict(self, force: bool = False) -> str | None:
        """Remove a stale/foreign lock so creation can win. Renaming first
        makes the takeover atomic: whoever renames it owns the removal. If the
        renamed file turns out to be live after all (and this is not a forced
        takeover), it is put back."""
        graveyard = self.path.with_name(f"{self.path.name}.stale.{os.getpid()}")
        try:
            os.replace(self.path, graveyard)
        except FileNotFoundError:
            return None
        except PermissionError as e:
            time.sleep(0.01)
            return str(e)
        moved = Lock.__new__(Lock)
        moved.path, moved.stale_after, moved._held = graveyard, self.stale_after, None
        info = moved.read()
        if (
            not force
            and info is not None
            and not info.is_mine()
            and not self.is_stale(info)
        ):
            # It became live between our read and the rename: give it back —
            # without overwriting a lock a third contender created meanwhile.
            try:
                os.link(graveyard, self.path)
            except FileExistsError:
                pass
            except OSError:  # pragma: no cover - no hard links: best effort
                try:
                    os.replace(graveyard, self.path)
                except OSError:
                    pass
            try:
                os.unlink(graveyard)
            except OSError:  # pragma: no cover - best effort
                pass
            return "lock became live during takeover"
        try:
            os.unlink(graveyard)
        except OSError:  # pragma: no cover - best effort
            pass
        return None

    def release(self) -> None:
        """Remove the lock, but only if this process wrote it."""
        if self._held is None:
            return
        current = self.read()
        if current is not None and current.is_mine():
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self._held = None

    def __enter__(self) -> LockInfo:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


def pid_alive(pid: int) -> bool:
    """Whether ``pid`` is a running process on this host.

    A process we are not allowed to inspect counts as alive. On Windows
    ``os.kill(pid, 0)`` would *terminate* the process, so the check goes
    through ``OpenProcess`` instead.
    """
    if pid <= 0 or pid > _MAX_PID:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_ACCESS_DENIED = 5
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _write_private(path: Path, text: str) -> None:
    """Atomically write ``text`` to a file that is owner-only from creation
    (where the OS has POSIX modes; Windows ACLs are not restricted)."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
