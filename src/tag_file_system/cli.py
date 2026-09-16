# Code by AkinoAlice@TyrantRey

"""The ``tfs`` command (DESIGN/v0-1-0.md §8, DESIGN/v0-2-0.md §4,
DESIGN/v0-4-0.md §9–§10).

    tfs init [dir]      tfs list      tfs query -t a -t b ...
    tfs explain PATH    tfs migrate [--apply] [--rename]   tfs ui [--open]
    tfs reload [--yes]  tfs start [-d] [--force]      tfs stop
    tfs plan [PATH]     tfs status    tfs doctor         (DESIGN/v0-5-0.md §11)
    tfs pause | resume  tfs retry RUN_ID | cancel RUN_ID | rerun --handler S.H
    tfs touch | cp | mv | rm | mkdir   (data files, through the daemon)
    tfs update [--json]                tfs upgrade [--to TAG] [--dry-run] ...
    tfs backup list | prune [--keep N] tfs --version

The CLI is client-first: it talks to the running daemon over the control
channel; when no daemon answers and the database is on a local disk it
reads the database directly; over a network mount with no daemon it
refuses (WAL over SMB/NFS is unsafe). While ``tfs upgrade`` holds a root
(its marker in ``.tfs/lock``) nothing opens the database directly.

``tfs upgrade`` itself is dispatched by ``tag_file_system.__main__`` to the
standard-library ``updater`` before this module (and pydantic) is imported;
the command below exists for ``--help`` and ``python -m tag_file_system.cli``.
Every root-scoped command records its root in the registry (§5).
"""

import json
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path, PurePosixPath
from typing import Annotated, Any

import typer

from tag_file_system.addons.loader import AddonLoader
from tag_file_system.config import Config, ConfigError
from tag_file_system.core.interface.action import Severity
from tag_file_system.core.logger import configure_logging
from tag_file_system.core.paths import has_parent_reference, is_anchored, posix_key
from tag_file_system.root import (
    FUNCTIONS_FILE,
    Lock,
    LockHeld,
    LockInfo,
    NotARoot,
    OutsideRoot,
    Root,
    RootError,
    RootExists,
    Zone,
    pid_alive,
    same_name,
)
from tag_file_system.services.api import API_PREFIX
from tag_file_system.services.control import (
    ControlClient,
    ControlError,
    ControlUnavailable,
    file_payload,
)
from tag_file_system.services.ui import UI_PREFIX
from tag_file_system.services.views import (
    DAEMON,
    DISK,
    actions_view,
    explain_view,
    load_root,
)
from tag_file_system.updater import (
    KEEP_DEFAULT,
    UpdateError,
    backups_dir,
    check,
    human_size,
    list_backups,
    prune_backups,
    register_root,
    registry_path,
)
from tag_file_system.version import COMMIT, VERSION, describe, short

app = typer.Typer(
    help="TagFileSystem: tags and add-on functions driven by file names.",
    no_args_is_help=True,
    add_completion=False,
)
backup_app = typer.Typer(
    help="Database snapshots taken by `tfs upgrade` (.tfs/backups/).",
    no_args_is_help=True,
)
app.add_typer(backup_app, name="backup")

RootOption = Annotated[
    Path | None,
    typer.Option(
        "--root", "-r", help="The managed root (default: discovered from CWD)."
    ),
]

STOP_GRACE_SECONDS = 5.0  # on top of the daemon's own stop_timeout_seconds

# What `tfs init` writes at the root: valid as it is, and says how to go on.
FUNCTIONS_SKELETON = f"""# {FUNCTIONS_FILE} — the handlers that apply to this folder and everything
# below it (DESIGN/v0-4-0.md). Read at `tfs start` and `tfs reload`; edits are
# not applied live. Any folder may carry one; they add up, parent first.
version: 1
functions: {{}}
# functions:
#   photo:              # script/photo.py
#     resize:           # def resize(path, metadata, ctx, width: int)
#       width: 800
#       exclude:            # skip a file when any of these holds (any/all/not nest)
#         - tag: draft
#         - filename: "*.tmp"
"""


def _fail(message: str, code: int = 1) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code)


def _root(root: Path | None) -> Root:
    try:
        found = Root.discover(root) if root is not None else Root.discover()
    except NotARoot as e:
        raise _fail(f"{e} (run `tfs init` first)")
    register_root(found.path)  # so `tfs upgrade` knows every root on this machine
    return found


def _upgrade_in_progress(holder: LockInfo | None) -> str | None:
    """A ``tfs upgrade`` holds the root (DESIGN/v0-2-0.md §7 step 3)."""
    if holder is None or not holder.upgrade:
        return None
    return (
        f"an upgrade is in progress (pid {holder.pid} on {holder.hostname}, "
        f"since {holder.created_at_text})"
    )


def _config(root: Root) -> Config:
    try:
        return root.load_config()
    except ConfigError as e:
        raise _fail(str(e))


def _one_line(error: Exception) -> str:
    text = " ".join(str(error).split())
    return text if len(text) <= 300 else text[:297] + "..."


def _client(
    root: Root, holder: LockInfo | None = None, timeout: float = 5.0
) -> ControlClient:
    """The daemon's client.

    A running daemon records the address it opened in ``.tfs/lock``, so it
    stays reachable even when ``config.toml`` is edited (or broken) while it
    runs; otherwise the configured address is used. A root whose token or
    config cannot be read has no reachable daemon: that is
    ``ControlUnavailable`` (with the reason), so the direct fallbacks run.
    """
    try:
        token = root.read_token()
    except RootError as e:
        raise ControlUnavailable(_one_line(e))
    if holder is not None and holder.port:
        return ControlClient(
            holder.bind or "127.0.0.1", holder.port, token, timeout=timeout
        )
    try:
        config = root.load_config()
    except ConfigError as e:
        raise ControlUnavailable(_one_line(e))
    return ControlClient(config.daemon.bind, config.daemon.port, token, timeout=timeout)


def _no_daemon_here(error: ControlError, holder: LockInfo | None) -> bool:
    """A 401 means something else owns that address — another root's daemon
    on a shared port, say. Unless this root's lock says a daemon of ours is
    running, this root simply has none."""
    return error.status == 401 and (holder is None or not holder.is_live_local())


def _unreachable(error: ControlError, holder: LockInfo | None) -> str | None:
    """Why the daemon could not be reached, or ``None`` when the error is a
    real answer from this root's daemon and must be shown as such."""
    if isinstance(error, ControlUnavailable):
        return error.message
    if _no_daemon_here(error, holder):
        return "another program answers on the configured port"
    return None


def is_network_path(path: Path) -> bool:
    """Best effort: is ``path`` on an SMB/NFS-style mount?"""
    text = str(path)
    if os.name == "nt":
        if text.startswith("\\\\"):
            return True
        try:
            import ctypes

            drive = os.path.splitdrive(os.path.abspath(text))[0] + "\\"
            # getattr with a default: `windll` does not exist off Windows,
            # and the type check runs on Linux too.
            windll = getattr(ctypes, "windll", None)
            if windll is None:  # pragma: no cover - Windows-only branch
                return False
            return windll.kernel32.GetDriveTypeW(drive) == 4  # DRIVE_REMOTE
        except Exception:
            return False
    try:
        mounts = Path("/proc/mounts").read_text().splitlines()
    except OSError:
        return False
    best: tuple[int, str] | None = None
    for line in mounts:
        parts = line.split()
        if len(parts) < 3:
            continue
        mount, fstype = parts[1], parts[2]
        if text == mount or text.startswith(mount.rstrip("/") + "/"):
            if best is None or len(mount) > best[0]:
                best = (len(mount), fstype)
    return best is not None and best[1] in {
        "nfs",
        "nfs4",
        "cifs",
        "smb3",
        "smbfs",
        "afpfs",
        "fuse.sshfs",
    }


def _open_backend(root: Root):
    """Direct database access for when no daemon answers. Never while an
    upgrade holds the root: opening the database would migrate it out from
    under the upgrade (DESIGN/v0-2-0.md §11)."""
    busy = _upgrade_in_progress(Lock(root).holder())
    if busy is not None:
        raise _fail(f"{busy}; the database is not available until it finishes")
    if is_network_path(root.path):
        raise _fail(
            "no daemon answers and the root is on a network mount; start the daemon "
            "on the machine that owns the root (reading a WAL database over SMB/NFS is unsafe)"
        )
    from tag_file_system.database.action_store import ActionStore
    from tag_file_system.database.sqlite import SQLiteBackend

    backend = SQLiteBackend()
    backend.init_database(root.db_path, root_dir=root.path)
    return backend, ActionStore(backend)


def validate_prefix(prefix: str | None) -> str | None:
    """``--under`` must be a root-relative directory key."""
    if prefix is None:
        return None
    text = prefix.strip()
    if not text or text in (".", "./"):
        raise ValueError("--under must name a directory below the root")
    if is_anchored(text) or has_parent_reference(Path(text)):
        raise ValueError(f"--under must be a root-relative directory, got {prefix!r}")
    try:
        key = posix_key(text)
    except ValueError as e:
        raise ValueError(f"--under: {e}") from None
    if key == ".":
        raise ValueError("--under must name a directory below the root")
    return key


def validate_tags(tags: list[str] | None) -> list[str] | None:
    if not tags:
        return None
    if any(not t.strip() for t in tags):
        raise ValueError("--tag cannot be blank")
    return [t.strip() for t in tags]


# ------------------------------------------------------------------ init


@app.command()
def init(
    directory: Annotated[
        Path, typer.Argument(help="Folder to turn into a root.")
    ] = Path("."),
) -> None:
    """Turn a folder into a managed root (creates .tfs/, script/ and a
    .tfsfunctions.yaml skeleton)."""
    try:
        root = Root.init(directory)
    except (RootExists, RootError) as e:
        raise _fail(str(e))
    from tag_file_system.database.sqlite import SQLiteBackend

    backend = SQLiteBackend()
    backend.init_database(root.db_path, root_dir=root.path)
    backend.close()
    functions = root.path / FUNCTIONS_FILE
    if not functions.exists():  # a data file, not layout: never overwritten
        functions.write_text(FUNCTIONS_SKELETON, encoding="utf-8")
    register_root(root.path)
    typer.echo(f"Initialized TagFileSystem root at {root.path}")
    typer.echo(f"  add-ons:   {root.script_dir}")
    typer.echo(f"  config:    {root.config_path}")
    typer.echo(f"  functions: {functions}")


# ------------------------------------------------------------------ list


@app.command("list")
def list_addons(
    root: RootOption = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable output.")
    ] = False,
) -> None:
    """List the loaded add-ons with their hooks, arguments and load problems."""
    where = _root(root)
    problems: list[dict[str, Any]] = []
    holder = Lock(where).holder()
    version, commit = VERSION, COMMIT
    try:
        payload = _client(where, holder).actions()
        actions = payload["actions"]
        problems = payload.get("problems", [])
        # The daemon's own identity: what actually runs, not what the CLI is.
        version, commit = payload.get("version", version), payload.get("hash", commit)
        source = "daemon"
    except ControlError as e:
        reason = _unreachable(e, holder)
        if reason is None:
            raise _fail(str(e))
        busy = _upgrade_in_progress(holder)
        if busy is not None:
            raise _fail(f"{busy}; the root is not available until it finishes")

        script_problems: list[dict[str, Any]] = []

        def report(severity, kind, message, *, action_name=None):
            script_problems.append(
                {"severity": Severity(severity).value, "kind": kind, "message": message}
            )

        loader, functions = load_root(where, report)
        view = actions_view(loader, functions, script_problems, DISK)
        actions, problems = view["actions"], view["problems"]
        source = f"script/ and the {FUNCTIONS_FILE} files on disk (no daemon running: {reason})"

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "source": source,
                    "version": version,
                    "hash": commit,
                    "actions": actions,
                    "problems": problems,
                },
                indent=2,
            )
        )
        return
    typer.echo(f"tfs {version} ({short(commit)})")
    typer.echo(f"add-ons from {source}:")
    if not actions:
        typer.echo("  (none)")
    for action in actions:
        typer.echo(f"  {action['name']:<20} {action.get('script', '')}")
        for handler in action.get("handlers", []):
            hooks = ", ".join(handler.get("hooks", [])) or "-"
            schema = handler.get("signature") or {}
            params = ", ".join(
                f"{name}: {spec.get('x-tfs-path') or spec.get('type', 'any')}"
                + (f" = {spec['default']!r}" if "default" in spec else "")
                for name, spec in schema.get("properties", {}).items()
            )
            lifecycle_only = all(h in ("on_start", "on_stop") for h in handler["hooks"])
            call = "" if lifecycle_only else f" ({params})"
            typer.echo(f"      {handler['name']:<16} {hooks}{call}")
        for severity in action.get("problem_hooks", []):
            typer.echo(f"      on {severity}")
    for problem in problems:
        typer.echo(f"  [{problem['severity']}] {problem['kind']}: {problem['message']}")


# ----------------------------------------------------------------- query


@app.command()
def query(
    root: RootOption = None,
    tag: Annotated[
        list[str] | None,
        typer.Option("--tag", "-t", help="Required tag (repeatable, ANDed)."),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option("--name", help="Filename substring (case-insensitive)."),
    ] = None,
    file_format: Annotated[
        str | None, typer.Option("--format", help="Extension with its dot, e.g. .jpg")
    ] = None,
    mime: Annotated[
        str | None,
        typer.Option(
            "--mime", help="MIME type, exact (image/jpeg) or a family (image/*)."
        ),
    ] = None,
    prefix: Annotated[
        str | None,
        typer.Option("--under", help="Root-relative directory, e.g. 2024--trip"),
    ] = None,
    deleted: Annotated[
        bool, typer.Option("--deleted", help="Include soft-deleted rows.")
    ] = False,
    runs: Annotated[
        bool, typer.Option("--runs", help="Include each file's run history.")
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable output.")
    ] = False,
) -> None:
    """Find files by tag and attributes."""
    where = _root(root)
    try:
        tags = validate_tags(tag)
        under = validate_prefix(prefix)
    except ValueError as e:
        raise _fail(str(e), code=2)
    holder = Lock(where).holder()
    try:
        files = _client(where, holder).files(
            tags=tags,
            name=name,
            format=file_format,
            mime=mime,
            deleted=deleted,
            prefix=under,
            runs=runs,
        )
    except ControlError as e:
        if _unreachable(e, holder) is None:
            raise _fail(str(e))
        backend, store = _open_backend(where)
        try:
            rows = backend.query_files(
                tags=tags,
                filename=name,
                file_format=file_format,
                mime_type=mime,
                include_deleted=deleted,
                path_prefix=under,
            )
            files = [
                file_payload(f, store.query_runs(file_path=f.path) if runs else None)
                for f in rows
            ]
        finally:
            backend.close()

    if as_json:
        typer.echo(json.dumps(files, indent=2))
        return
    if not files:
        typer.echo("(no files)")
        return
    for entry in files:
        tags_text = ", ".join(entry["tags"]) or "-"
        status = "" if entry["status"] == "active" else f" [{entry['status']}]"
        typer.echo(f"{entry['path']}{status}    tags: {tags_text}")
        for run in entry.get("runs", []):
            typer.echo(
                f"    {run['action_name']} {run['hook']} {run['status']} {run['started_at']}"
                + (f"  {run['error'].splitlines()[0]}" if run.get("error") else "")
            )


# --------------------------------------------------------------- explain


def _explain_key(where: Root, text: str) -> str:
    """The root-relative key ``tfs explain`` was given, or exit 2."""
    if is_anchored(text) or Path(text).is_absolute():
        try:
            key = where.relative(Path(text)).as_posix()
        except OutsideRoot as e:
            raise _fail(str(e), code=2)
    else:
        if has_parent_reference(text):
            raise _fail(f"{text!r} escapes the root", code=2)
        try:
            key = posix_key(text)
        except ValueError as e:
            raise _fail(str(e), code=2)
    if not key or key == ".":
        raise _fail("explain takes one file, not the root", code=2)
    if same_name(PurePosixPath(key).name, FUNCTIONS_FILE):
        raise _fail(f"{key} is a configuration file, not a data file", code=2)
    return key


def _explain_offline(where: Root, key: str) -> dict[str, Any]:
    """No daemon: the same view, from script/, the .tfsfunctions.yaml files
    and the database as they are on disk (``services.views``)."""
    loader, functions = load_root(where)
    row = None
    try:
        backend, _store = _open_backend(where)
    except typer.Exit:
        backend = None  # a network mount: the name still says what it can
    if backend is not None:
        try:
            row = backend.query_file(key)
        finally:
            backend.close()
    return explain_view(loader, functions, key, row, DISK)


@app.command()
def explain(
    path: Annotated[str, typer.Argument(help="One file, root-relative or absolute.")],
    root: RootOption = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable output.")
    ] = False,
) -> None:
    """Which handlers apply to a file, from which folder's .tfsfunctions.yaml,
    and what excluded the rest (DESIGN/v0-4-0.md §9)."""
    where = _root(root)
    key = _explain_key(where, path)
    holder = Lock(where).holder()
    reason = None
    try:
        payload = _client(where, holder).explain(key)
    except ControlError as e:
        reason = _unreachable(e, holder)
        if reason is None:
            raise _fail(str(e))
        busy = _upgrade_in_progress(holder)
        if busy is not None:
            raise _fail(f"{busy}; the root is not available until it finishes")
        payload = _explain_offline(where, key)

    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    if payload.get("source") != DAEMON:
        typer.echo(
            f"(no daemon running: {reason}; this is what script/ and the "
            f"{FUNCTIONS_FILE} files on disk say — what the next start would load)"
        )
    tags = ", ".join(payload["tags"]) or "-"
    origin = "" if payload["known"] else " (from the name; not indexed yet)"
    typer.echo(f"{payload['path']}    tags{origin}: {tags}")
    if not payload["applied"]:
        typer.echo("  (nothing applies)")
    for entry in payload["applied"]:
        hooks = ", ".join(entry["hooks"]) or "-"
        typer.echo(f"  {entry['display']:<44} {hooks:<22} from {entry['file']}")
    if payload["suppressed"]:
        typer.echo("suppressed:")
        for entry in payload["suppressed"]:
            typer.echo(
                f"  {entry['display']:<44} from {entry['file']}: excluded by {entry['reason']}"
            )
    if payload["defaults"]:
        typer.echo("tagged defaults:")
        for entry in payload["defaults"]:
            name = f"{entry['script']}.{entry['handler']}"
            by = entry["suppressed_by"]
            note = f"suppressed by {by}" if by else ""
            typer.echo(f"  {name:<44} tagged:{entry['tag']:<15} {note}".rstrip())
    if payload["problems"]:
        typer.echo("problems:")
        for problem in payload["problems"]:
            typer.echo(
                f"  [{problem['severity']}] {problem['kind']}: {problem['message']}"
            )


# -------------------------------------------------------------------- ui


@app.command()
def ui(
    root: RootOption = None,
    open_browser: Annotated[
        bool, typer.Option("--open", help="Open the address in the default browser.")
    ] = False,
) -> None:
    """Print the web UI's address, token included (DESIGN/v0-5-0.md §3.2).

    The token rides in the URL fragment: the browser never sends a fragment,
    so the daemon's log never sees it; the page keeps it for its API calls.
    """
    where = _root(root)
    holder = Lock(where).holder()
    busy = _upgrade_in_progress(holder)
    if busy is not None:
        raise _fail(f"{busy}; the root is not available until it finishes")
    try:
        client = _client(where, holder)
        status = client.get(f"{API_PREFIX}/status")
    except ControlError as e:
        reason = _unreachable(e, holder)
        if reason is not None:
            raise _fail(
                f"no daemon answers ({reason}); the web UI needs the daemon: "
                "run `tfs start -d` first"
            )
        if e.status == 404:
            raise _fail(
                "the running daemon predates the API (0.4.x): stop and start it"
            )
        raise _fail(str(e))
    url = f"{client.base}{UI_PREFIX}/#token={where.read_token()}"
    built = status.get("ui") or {}
    if not built.get("built"):
        typer.echo(
            f"warning: the web UI is not built ({built.get('dist') or 'Frontend/dist'}); "
            "the daemon answers 503 at /ui/ until `npm ci && npm run build` "
            "has run in Frontend/",
            err=True,
        )
    typer.echo(url)
    if open_browser:
        webbrowser.open(url)


# --------------------------------------------------------------- migrate


@app.command()
def migrate(
    root: RootOption = None,
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Write the files (with --rename: rename the folders). Default: show the plan.",
        ),
    ] = False,
    rename: Annotated[
        bool,
        typer.Option(
            "--rename",
            help="Strip the @@ markers from the folder names instead — a separate step, after the files are in place.",
        ),
    ] = False,
) -> None:
    """Turn v1 `@@func__arg` folder names into .tfsfunctions.yaml files
    (DESIGN/v0-4-0.md §10). A dry run unless --apply."""
    from tag_file_system import migrate as migration_module

    where = _root(root)
    loader = AddonLoader(where)
    loader.load_all()
    migration = migration_module.plan(where, loader)
    holder = Lock(where).holder()
    if holder is not None and holder.is_live_local():
        typer.echo(
            "note: a daemon is running; it reports functions.drift until `tfs reload`",
            err=True,
        )

    if rename:
        if not migration.renames:
            typer.echo("nothing to rename")
            _echo_skipped(migration.skipped)
            raise typer.Exit(1 if apply else 0)
        typer.echo(
            f"{'renaming' if apply else 'would rename'} {len(migration.renames)} folder(s):"
        )
        for item in migration.renames:
            typer.echo(f"  {_relative_text(where, item.source)} -> {item.target.name}")
        _echo_skipped(migration.skipped)
        if not apply:
            typer.echo("(dry run: pass --apply to rename)")
            return
        done = migration_module.apply_renames(migration)
        typer.echo(f"renamed {len(done)} folder(s)")
        return

    if not migration.writes:
        typer.echo("nothing to migrate: no folder name carries a @@ function")
        _echo_skipped(migration.skipped)
        raise typer.Exit(1 if apply else 0)
    typer.echo(
        f"{'writing' if apply else 'would write'} {len(migration.writes)} file(s):"
    )
    for item in migration.writes:
        typer.echo(f"  {item.folder}/{FUNCTIONS_FILE}")
        for line in item.text.splitlines():
            typer.echo(f"    {line}")
    _echo_skipped(migration.skipped)
    if not apply:
        typer.echo("(dry run: pass --apply to write)")
        return
    written = migration_module.apply_writes(migration)
    typer.echo(
        f"wrote {len(written)} file(s); `tfs reload` (or `tfs start`) applies them, "
        "then `tfs migrate --rename` cleans up the names"
    )


def _echo_skipped(skipped: list[str]) -> None:
    if skipped:
        typer.echo("skipped:")
        for line in skipped:
            typer.echo(f"  {line}")


def _relative_text(where: Root, path: Path) -> str:
    try:
        return where.relative(path).as_posix()
    except OutsideRoot:
        return str(path)


# ---------------------------------------------------------------- reload


@app.command()
def reload(
    root: RootOption = None,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Consent to a reload that would start more runs than [daemon] confirm_above.",
        ),
    ] = False,
) -> None:
    """Re-import the add-ons, re-read config.toml and every .tfsfunctions.yaml
    in the running daemon. Refused above `confirm_above` unless --yes
    (DESIGN/v0-5-0.md §11.3): `tfs plan` shows what it would start."""
    where = _root(root)
    holder = Lock(where).holder()
    client = _need_daemon(where, holder, "the daemon is down until it finishes")
    try:
        result = client.reload(yes=yes)
    except ControlError as e:
        raise _fail(str(e))
    functions = result.get("functions", {"files": 0, "problems": 0})
    refused = result.get("refused")
    if refused:
        typer.echo(
            f"config {result['config']}; reload refused: it would start "
            f"{refused['would_run']} run(s), more than confirm_above = "
            f"{refused['threshold']}. Review with `tfs plan`, then `tfs reload --yes`."
        )
        raise typer.Exit(1)
    typer.echo(
        f"config {result['config']}; add-ons: {', '.join(result['addons']) or '(none)'}; "
        f"functions: {functions['files']} file(s), {functions['problems']} problem(s)"
    )


def _need_daemon(where: Root, holder: LockInfo | None, busy_hint: str) -> ControlClient:
    """A client for a command that has no offline form: exit 1 with the
    reason when no daemon answers."""
    try:
        client = _client(where, holder)
        client.health()
        return client
    except ControlError as e:
        reason = _unreachable(e, holder)
        if reason is None:
            raise _fail(str(e))
        busy = _upgrade_in_progress(holder)
        if busy is not None:
            raise _fail(f"{busy}; {busy_hint}")
        if holder is not None and holder.is_live_local():
            raise _fail(
                f"cannot reach the daemon holding this root (pid {holder.pid}): {reason}"
            )
        raise _fail(f"{reason}; is the daemon running? (`tfs start`)")


def _daemon_or_reason(
    where: Root, holder: LockInfo | None
) -> tuple[ControlClient | None, str | None]:
    """A client when a daemon answers, else why not (the offline fallback
    runs). A real error from this root's daemon exits."""
    try:
        client = _client(where, holder)
        client.health()
        return client, None
    except ControlError as e:
        reason = _unreachable(e, holder)
        if reason is None:
            raise _fail(str(e))
        busy = _upgrade_in_progress(holder)
        if busy is not None:
            raise _fail(f"{busy}; the root is not available until it finishes")
        return None, reason


# ----------------------------------------------------------------- start


@app.command()
def start(
    root: RootOption = None,
    detach: Annotated[
        bool, typer.Option("--detach", "-d", help="Run in the background.")
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Take over a lock that looks live (a lock left by a dead daemon is taken over anyway).",
        ),
    ] = False,
    log_console: Annotated[
        bool,
        typer.Option(
            "--log-console",
            envvar="TFS_LOG_CONSOLE",
            help="Also write the log to the console (stdout), for Docker or a service manager that collects it. Foreground only.",
        ),
    ] = False,
) -> None:
    """Reconcile the root and watch it (foreground unless -d)."""
    if detach and log_console:
        raise _fail("--log-console is for a foreground daemon; drop -d", code=2)
    where = _root(root)
    busy = _upgrade_in_progress(Lock(where).holder())
    if busy is not None:
        raise _fail(f"{busy}; the daemon restarts when it finishes")
    config = _config(where)
    try:
        where.read_token()
    except RootError as e:
        raise _fail(
            f"{e}; the root is damaged, re-run `tfs init` on a fresh folder or restore .tfs/token"
        )

    if detach:
        _start_detached(where, config, force)
        return

    log_file = where.path / config.logging.file
    configure_logging(config.logging.level, log_file, stream=log_console)
    # The log goes to the file, not to this console (unless asked): say so
    # where someone will look — which, detached, is .tfs/daemon.out.
    typer.echo(f"logging to {log_file}" + (" and the console" if log_console else ""))
    from tag_file_system.services.daemon import Daemon

    daemon = Daemon(
        where,
        config=config,
        control=True,
        apply_logging=True,
        log_console=log_console,
    )
    try:
        daemon.startup(force=force)
    except LockHeld as e:
        if e.info.upgrade:
            raise _fail(f"{e}; the daemon restarts when the upgrade finishes")
        if e.info.is_live_local():
            raise _fail(
                f"{e}; that process is running on this machine — `tfs stop` it first"
            )
        raise _fail(f"{e}; use --force if that daemon is gone")
    except (RootError, ConfigError) as e:
        daemon.shutdown()
        raise _fail(str(e))
    except OSError as e:
        daemon.shutdown()
        raise _fail(
            f"cannot open the control channel on {config.daemon.bind}:{config.daemon.port}: {e}"
        )
    typer.echo(
        f"watching {where.path} (control on http://{config.daemon.bind}:{config.daemon.port}); Ctrl+C to stop"
    )
    daemon.run_forever()
    typer.echo("stopped")


def _start_detached(where: Root, config: Config, force: bool) -> None:
    log = where.tfs_dir / "daemon.out"
    client = ControlClient(
        config.daemon.bind, config.daemon.port, where.read_token(), timeout=1.0
    )
    try:
        running = client.health()
    except ControlError:
        running = None
    if running is not None and not force:
        raise _fail(
            f"a daemon already answers on {config.daemon.bind}:{config.daemon.port} "
            f"(pid {running.get('pid')}); use `tfs stop` first"
        )
    args = [
        sys.executable,
        "-m",
        "tag_file_system.cli",
        "--root",
        str(where.path),
        "start",
    ]
    if force:
        args.append("--force")
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = (
            # Both constants are Windows-only; getattr keeps the Linux type
            # check (and any non-Windows import of this module) quiet.
            # CREATE_NO_WINDOW, not DETACHED_PROCESS: the venv's python.exe is
            # a launcher whose child is the interpreter. Detached, the launcher
            # has no console, so the interpreter allocates a visible one — a
            # console window on every `start -d` (verified). A hidden console
            # is inherited by the interpreter and by whatever an add-on spawns.
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
    else:
        kwargs["start_new_session"] = True
    with log.open("ab") as out:
        process = subprocess.Popen(
            args,
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            **kwargs,
        )

    # The child may re-exec and may die at once (lock, port): report what
    # actually happened, not that a process was spawned. The control channel
    # opens before the root is reconciled, so answering is "up" — a big
    # first reconcile must not look like a failure.
    previous_pid = running.get("pid") if running else None
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            health = client.health()
        except ControlError:
            time.sleep(0.25)
            continue
        pid = health.get("pid")
        if pid is not None and pid != previous_pid:
            state = (
                "watching"
                if health.get("started")
                else "starting: reconciling the root"
            )
            typer.echo(
                f"daemon started in the background (pid {pid}, {state}); "
                f"log in {where.path / config.logging.file}, "
                f"anything it prints (a crash) in {log}"
            )
            return
        time.sleep(0.25)

    tail = ""
    try:
        tail = "\n".join(log.read_text(errors="replace").splitlines()[-5:])
    except OSError:
        pass
    if process.poll() is not None:
        raise _fail(f"the daemon exited with code {process.returncode}:\n{tail}")
    # Alive but never opened its channel: do not leave it running.
    _kill_silently(process)
    holder = Lock(where).holder()
    if holder is not None and holder.pid != previous_pid and holder.is_live_local():
        _signal_stop(holder.pid)
    raise _fail(
        f"the daemon did not come up within 15s (pid {process.pid}); output so far:\n{tail}"
    )


def _kill_silently(process: subprocess.Popen) -> None:
    try:
        process.kill()
        process.wait(5)
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        pass


# ------------------------------------------------------------------ stop


@app.command()
def stop(
    root: RootOption = None,
    timeout: Annotated[
        float | None,
        typer.Option(
            "--timeout",
            help="Seconds to wait for the daemon to exit (default: its stop_timeout_seconds + 5).",
        ),
    ] = None,
) -> None:
    """Stop the running daemon gracefully."""
    where = _root(root)
    lock = Lock(where)
    holder = lock.holder()
    busy = _upgrade_in_progress(holder)
    if busy is not None:
        raise _fail(f"{busy}; it stops and restarts the daemon itself")
    if timeout is None:
        try:
            timeout = (
                where.load_config().daemon.stop_timeout_seconds + STOP_GRACE_SECONDS
            )
        except ConfigError:
            timeout = 30.0 + STOP_GRACE_SECONDS
    forced = False
    try:
        client = _client(where, holder)
        health = client.health()
        answering = health.get("pid")
        if holder is not None and answering is not None and answering != holder.pid:
            raise _fail(
                f"the daemon answering on that port is pid {answering}, but this root's lock "
                f"is held by pid {holder.pid}; that is a different daemon — check [daemon] port"
            )
        client.stop()
    except ControlUnavailable as e:
        if holder is None:
            raise _fail("no daemon is running for this root")
        if holder.hostname != socket.gethostname():
            raise _fail(
                f"the daemon runs on {holder.hostname} (pid {holder.pid}) and its control channel "
                f"does not answer ({e.message}); stop it on that machine"
            )
        typer.echo(
            f"control channel unresponsive ({e.message}); signalling pid {holder.pid}"
        )
        sent = _signal_stop(holder.pid)
        if sent is None:
            raise _fail(f"could not stop pid {holder.pid}")
        forced = sent is False
    except ControlError as e:
        if _no_daemon_here(e, holder):
            raise _fail("no daemon is running for this root")
        if e.status == 401 and holder is not None:
            raise _fail(
                f"the process answering on that port rejected this root's token: it is not this "
                f"root's daemon (the lock holds pid {holder.pid} on {holder.hostname})"
            )
        raise _fail(str(e))

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = lock.holder()
        if current is None or not pid_alive(current.pid):
            if current is not None or (holder is not None and where.lock_path.exists()):
                try:
                    where.lock_path.unlink()  # the daemon could not clean up after itself
                except OSError:
                    pass
            typer.echo(
                "stopped (forced: the daemon was killed, the next start recovers its runs)"
                if forced
                else "stopped"
            )
            return
        time.sleep(0.2)
    raise _fail(f"the daemon did not stop within {timeout:.0f}s")


def _signal_stop(pid: int) -> bool | None:
    """Ask ``pid`` to stop. ``True`` = graceful signal sent, ``False`` = it had
    to be killed, ``None`` = neither worked."""
    if os.name == "nt":
        # The daemon has its own hidden console: our CTRL_BREAK cannot reach
        # it, and taskkill
        # without /F is ignored by console applications. Kill it.
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"], capture_output=True
        )
        return False if result.returncode == 0 else None
    import signal

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return None
    return True


# ---------------------------------------------------------------- update


@app.command()
def update(
    root: RootOption = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable output.")
    ] = False,
) -> None:
    """Fetch the release tags from origin and report; change nothing.

    Exit status is non-zero only when the check itself fails (no git, an
    unreachable origin, a dirty checkout): "an update is available" is exit
    0 with `available: true` in --json (DESIGN/v0-2-0.md §4).
    """
    where = _root(root)
    try:
        report = check(where.path)
    except UpdateError as e:
        raise _fail(str(e))
    if as_json:
        typer.echo(json.dumps(report.as_dict(), indent=2))
        return
    typer.echo(f"current   {report.head.describe(report.current_version)}")
    if report.target is None:
        typer.echo("latest    (origin has no release tags)")
    else:
        state = "available" if report.available else "nothing newer"
        typer.echo(
            f"latest    {report.target} = {report.to_version} "
            f"({short(report.to_hash)}), {state}"
        )
        if report.to_schema != report.current_schema:
            typer.echo(f"schema    {report.current_schema} -> {report.to_schema}")
    typer.echo(f"roots     {len(report.roots)} registered in {registry_path()}")
    for state in report.roots:
        daemon = f"daemon pid {state.pid}" if state.running else "no daemon"
        schema = (
            "no database"
            if state.user_version is None
            else f"schema {state.user_version}"
        )
        line = f"  {state.path}: {daemon}, {schema}"
        if state.skipped:
            line += f", skipped: {state.skipped}"
        typer.echo(line)
    if report.available:
        typer.echo("run `tfs upgrade` to apply it")


@app.command()
def upgrade(
    root: RootOption = None,
    to: Annotated[
        str | None,
        typer.Option(
            "--to", metavar="TAG", help="A release tag on origin (default: the newest)."
        ),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the plan and change nothing.")
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes", "-y", help="Consent to a schema change without a prompt."
        ),
    ] = False,
    skip_tests: Annotated[
        bool,
        typer.Option("--skip-tests", help="Skip the test suite (and nothing else)."),
    ] = False,
    wait: Annotated[
        float | None,
        typer.Option(
            "--wait",
            metavar="SEC",
            help="Seconds to wait for in-flight runs (default: stop_timeout_seconds).",
        ),
    ] = None,
) -> None:
    """Move the checkout to a release tag and restart the daemons.

    Every registered root is snapshotted first; a failed upgrade is
    reverted (DESIGN/v0-2-0.md §7-8). The `tfs` command routes here before
    importing anything else; `python -m tag_file_system.cli upgrade` does
    not, which on Windows can leave `uv sync` unable to replace a
    dependency this process has loaded.
    """
    from tag_file_system.updater import main as upgrade_main

    argv: list[str] = []
    if root is not None:
        argv += ["--root", str(root)]
    if to is not None:
        argv += ["--to", to]
    if dry_run:
        argv.append("--dry-run")
    if yes:
        argv.append("--yes")
    if skip_tests:
        argv.append("--skip-tests")
    if wait is not None:
        argv += ["--wait", str(wait)]
    raise typer.Exit(upgrade_main(argv))


@app.command("record-upgrade", hidden=True)
def record_upgrade(
    payload: Annotated[Path, typer.Argument(help="JSON written by tfs upgrade.")],
    root: RootOption = None,
) -> None:
    """Write the `upgrades` row of this root (DESIGN/v0-2-0.md §9); only
    `tfs upgrade` calls this, as the new code, after the daemon migrated."""
    where = _root(root)
    try:
        data = json.loads(payload.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise _fail(f"cannot read {payload}: {e}")
    if not isinstance(data, dict):
        raise _fail(f"{payload} does not hold a JSON object")
    from tag_file_system.database.migrations import user_version

    backend, store = _open_backend(where)
    try:
        report = backend.migration_report
        before = data.get("schema_before")
        if before is None and report is not None:
            before = report.from_version
        record = store.record_upgrade(
            from_tag=data.get("from_tag"),
            from_hash=str(data["from_hash"]),
            to_tag=str(data["to_tag"]),
            to_hash=str(data["to_hash"]),
            schema_before=int(before) if before is not None else 0,
            schema_after=user_version(backend.connection),
            started_at=float(data.get("started_at") or time.time()),
            outcome=str(data.get("outcome") or "ok"),
            tests_run=data.get("tests_run"),
            tests_passed=data.get("tests_passed"),
            tests_skipped=data.get("tests_skipped"),
            snapshot_path=data.get("snapshot_path"),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise _fail(f"bad upgrade payload: {e}")
    finally:
        backend.close()
    typer.echo(
        f"recorded upgrade {record.id}: {record.from_tag or short(record.from_hash)} "
        f"-> {record.to_tag}, schema {record.schema_before} -> {record.schema_after}"
    )


# ---------------------------------------------------------------- backup


@backup_app.command("list")
def backup_list(
    root: RootOption = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable output.")
    ] = False,
) -> None:
    """List the snapshots of this root, newest first."""
    where = _root(root)
    backups = list_backups(where.path)
    if as_json:
        typer.echo(
            json.dumps(
                [
                    {
                        "path": str(b.path),
                        "size": b.size,
                        "tag": b.label,
                        "created": b.created.isoformat() if b.created else None,
                    }
                    for b in backups
                ],
                indent=2,
            )
        )
        return
    if not backups:
        typer.echo(f"(no snapshots in {backups_dir(where.path)})")
        return
    for backup in backups:
        typer.echo(
            f"  {backup.path.name:<44} {human_size(backup.size):>10}   from {backup.label}"
        )
    total = sum(b.size for b in backups)
    typer.echo(
        f"{len(backups)} snapshot(s), {human_size(total)} in {backups_dir(where.path)}"
    )


@backup_app.command("prune")
def backup_prune(
    root: RootOption = None,
    keep: Annotated[
        int, typer.Option("--keep", min=0, help="Snapshots to keep (the newest).")
    ] = KEEP_DEFAULT,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would go; delete nothing.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Delete without asking.")
    ] = False,
) -> None:
    """Delete all but the newest --keep snapshots (the policy `tfs upgrade`
    applies automatically)."""
    where = _root(root)
    busy = _upgrade_in_progress(Lock(where).holder())
    if busy is not None:
        raise _fail(f"{busy}; it may still need these snapshots")
    doomed = prune_backups(where.path, keep, dry_run=True)
    if not doomed:
        typer.echo(f"nothing to prune (keeping the newest {keep})")
        return
    total = sum(b.size for b in doomed)
    typer.echo(
        f"{'would delete' if dry_run else 'deleting'} {len(doomed)} snapshot(s), {human_size(total)}:"
    )
    for backup in doomed:
        typer.echo(f"  {backup.path}")
    if dry_run:
        return
    if not yes and not typer.confirm("delete them?", default=False):
        typer.echo("kept")
        return
    removed = prune_backups(where.path, keep)
    typer.echo(f"deleted {len(removed)} snapshot(s)")


# ------------------------------------------------------------------ status


def _human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours:02d}h"


def _lock_text(holder: LockInfo | None, lock: Lock) -> str:
    info = lock.read()
    if info is None:
        return "none"
    kind = "upgrade marker" if info.upgrade else "daemon"
    state = (
        "live on this host"
        if info.is_live_local()
        else "another host"
        if info.hostname != socket.gethostname()
        else "stale (pid gone)"
        if lock.is_stale(info)
        else "unknown"
    )
    return f"{kind} pid {info.pid} on {info.hostname} since {info.created_at_text}, {state}"


def _offline_counts(where: Root) -> dict[str, Any]:
    """What the database says with no daemon to ask (DESIGN/v0-5-0.md §11.5)."""
    from tag_file_system.core.interface.action import RunStatus
    from tag_file_system.database.migrations import user_version

    backend, store = _open_backend(where)
    try:
        return {
            "files": backend.count_files(),
            "runs": {
                "ok": store.count_runs(status=RunStatus.OK),
                "failed": store.count_runs(status=RunStatus.FAILED),
                "interrupted": store.count_runs(status=RunStatus.INTERRUPTED),
                "running": store.count_runs(
                    status=[RunStatus.RUNNING, RunStatus.QUEUED]
                ),
            },
            "problems": {
                "undelivered": store.count_problems(
                    at_least=Severity.WARN, undelivered_only=True
                )
            },
            "database": {
                "schema": user_version(backend.connection),
                "path": str(where.db_path),
            },
        }
    finally:
        backend.close()


@app.command()
def status(
    root: RootOption = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable output.")
    ] = False,
) -> None:
    """The daemon in one screen: state, queue, runs, limits, drift
    (DESIGN/v0-5-0.md §11.5); the lock and the database's counts when no
    daemon runs."""
    where = _root(root)
    lock = Lock(where)
    holder = lock.holder()
    client, reason = _daemon_or_reason(where, holder)
    if client is None:
        payload: dict[str, Any] = {
            "source": DISK,
            "reason": reason,
            "root": str(where.path),
            "lock": _lock_text(holder, lock),
            **_offline_counts(where),
        }
        if as_json:
            typer.echo(json.dumps(payload, indent=2))
            return
        typer.echo(f"no daemon running for {where.path} ({reason})")
        typer.echo(f"lock      {payload['lock']}")
        runs = payload["runs"]
        typer.echo(
            f"files     {payload['files']} indexed; runs: {runs['ok']} ok, "
            f"{runs['failed']} failed, {runs['interrupted']} interrupted"
            + (f", {runs['running']} left open" if runs["running"] else "")
        )
        typer.echo(f"problems  {payload['problems']['undelivered']} undelivered")
        typer.echo(f"schema    {payload['database']['schema']}")
        return
    try:
        payload = client.get(f"{API_PREFIX}/status")
    except ControlError as e:
        if e.status == 404:
            raise _fail("the running daemon predates `tfs status` (0.4.x): restart it")
        raise _fail(str(e))
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    queue = payload.get("queue") or {}
    state = "stopping" if payload.get("status") == "stopping" else "running"
    if payload.get("paused"):
        state = "paused"
        since = queue.get("since")
        if since:
            state += f" for {_human_duration(time.time() - since)}"
    typer.echo(
        f"tfs {payload.get('version')} ({short(payload.get('hash'))})   "
        f"daemon pid {payload.get('pid')} on {payload.get('root')}, "
        f"up {_human_duration(payload.get('uptime_seconds') or 0)}, {state}"
        + ("" if payload.get("started") else " (starting: reconciling the root)")
    )
    typer.echo(
        f"queue     {queue.get('depth', 0)} waiting, {queue.get('active', 0)} running, "
        f"{queue.get('workers', 0)} worker(s)"
    )
    runs = payload.get("runs") or {}
    typer.echo(
        f"runs      {runs.get('in_flight', len(payload.get('in_flight', [])))} in flight, "
        f"{runs.get('failed', 0)} failed, {runs.get('interrupted', 0)} interrupted"
    )
    typer.echo(f"files     {payload.get('files', '?')} indexed")
    typer.echo(
        f"problems  {(payload.get('problems') or {}).get('undelivered', 0)} undelivered"
    )
    functions = payload.get("functions") or {}
    drift = payload.get("drift") or []
    typer.echo(
        f"functions {functions.get('files', 0)} file(s), {functions.get('problems', 0)} problem(s)"
        + (
            f"; drift: {', '.join(drift)} (`tfs plan`, then `tfs reload`)"
            if drift
            else ""
        )
    )
    limits = payload.get("limits") or {}
    typer.echo(
        f"limits    max_concurrent_runs {limits.get('max_concurrent_runs')}"
        f", max_runs_per_minute {limits.get('max_runs_per_minute') or '0 (unlimited)'}"
        f", run_timeout_seconds {limits.get('run_timeout_seconds') or '0 (none)'}"
        f", confirm_above {limits.get('confirm_above') or '0 (off)'}"
    )
    ui_state = payload.get("ui") or {}
    typer.echo(f"ui        {'built' if ui_state.get('built') else 'not built'}")


# ------------------------------------------------------------------ doctor


def _local_checks(where: Root, holder: LockInfo | None) -> list[dict[str, str]]:
    """The checks the CLI makes on its own (DESIGN/v0-5-0.md §11.5)."""
    checks: list[dict[str, str]] = []

    def add(check: str, status: str, detail: str) -> None:
        checks.append({"check": check, "status": status, "detail": detail})

    missing = [
        str(p.relative_to(where.path))
        for p in (where.tfs_dir, where.config_path, where.token_path, where.db_dir)
        if not p.exists()
    ]
    if missing:
        add("layout", "fail", "missing: " + ", ".join(missing))
    elif not where.script_dir.is_dir():
        add("layout", "warn", f"{where.script_dir.name}/ is missing: no add-ons load")
    else:
        add("layout", "ok", f"{where.path}")
    try:
        where.load_config()
        add("config", "ok", str(where.config_path))
    except ConfigError as e:
        add("config", "fail", _one_line(e))
    try:
        token = where.read_token()
        add("token", "ok" if token else "fail", "present" if token else "empty")
    except RootError as e:
        add("token", "fail", _one_line(e))
    lock = Lock(where)
    info = lock.read()
    if info is None:
        add("lock", "ok", "none")
    elif info.upgrade:
        add("lock", "warn", f"an upgrade marker: {_lock_text(holder, lock)}")
    elif info.is_live_local():
        add("lock", "ok", _lock_text(holder, lock))
    elif info.hostname != socket.gethostname():
        add("lock", "warn", f"another host's: {_lock_text(holder, lock)}")
    else:
        add(
            "lock",
            "warn",
            f"stale: {_lock_text(holder, lock)}; the next start takes it over",
        )
    if is_network_path(where.path):
        add(
            "mount",
            "warn",
            "the root is on a network mount: only the daemon may open the database",
        )
    return checks


def _offline_db_checks(where: Root) -> list[dict[str, str]]:
    from tag_file_system.core.interface.action import RunStatus
    from tag_file_system.database.migrations import (
        SCHEMA_VERSION,
        SchemaTooNew,
        user_version,
    )

    checks: list[dict[str, str]] = []

    def add(check: str, status: str, detail: str) -> None:
        checks.append({"check": check, "status": status, "detail": detail})

    if is_network_path(where.path):
        add("database", "warn", "not checked: the root is on a network mount")
        return checks
    try:
        backend, store = _open_backend(where)
    except typer.Exit:
        add("database", "fail", "cannot be opened now (an upgrade holds the root)")
        return checks
    except SchemaTooNew as e:
        add("database", "fail", f"schema newer than this code: {e}")
        return checks
    except Exception as e:
        add("database", "fail", f"cannot open: {type(e).__name__}: {e}")
        return checks
    try:
        verdict = backend.connection.execute("PRAGMA quick_check").fetchone()[0]
        schema = user_version(backend.connection)
        add(
            "database",
            "ok" if verdict == "ok" else "fail",
            f"schema {schema} (code {SCHEMA_VERSION}), quick_check {verdict}",
        )
        failed = store.count_runs(status=[RunStatus.FAILED, RunStatus.INTERRUPTED])
        add(
            "runs",
            "warn" if failed else "ok",
            f"{failed} failed or interrupted run(s): `tfs retry RUN_ID` or `tfs rerun --failed`"
            if failed
            else "no failed run",
        )
        undelivered = store.count_problems(
            at_least=Severity.WARN, undelivered_only=True
        )
        add(
            "problems",
            "warn" if undelivered else "ok",
            f"{undelivered} problem(s) of P2 or worse no handler has seen"
            if undelivered
            else "all delivered",
        )
    finally:
        backend.close()
    return checks


@app.command()
def doctor(
    root: RootOption = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable output.")
    ] = False,
) -> None:
    """Check the root, the configuration, the database, the lock, the daemon,
    the add-ons and the functions files; exit 1 when a check fails
    (DESIGN/v0-5-0.md §11.5)."""
    where = _root(root)
    holder = Lock(where).holder()
    checks = _local_checks(where, holder)
    client, reason = _daemon_or_reason(where, holder)
    if client is None:
        checks.append(
            {"check": "daemon", "status": "warn", "detail": f"not running ({reason})"}
        )
        checks.extend(_offline_db_checks(where))
        script_problems: list[dict[str, Any]] = []

        def report(severity, kind, message, *, action_name=None):
            script_problems.append(
                {"severity": Severity(severity).value, "kind": kind, "message": message}
            )

        try:
            loader, functions = load_root(where, report)
        except Exception as e:  # a root too broken to load: said above
            checks.append({"check": "scripts", "status": "warn", "detail": str(e)})
        else:
            checks.append(
                {
                    "check": "scripts",
                    "status": "warn" if script_problems else "ok",
                    "detail": f"{len(loader.addons)} add-on(s) load"
                    + (
                        "; "
                        + "; ".join(
                            f"{p['kind']}: {p['message']}" for p in script_problems
                        )
                        if script_problems
                        else ""
                    ),
                }
            )
            checks.append(
                {
                    "check": "functions",
                    "status": "warn" if functions.problems else "ok",
                    "detail": f"{len(functions.files)} file(s) load"
                    + (
                        "; "
                        + "; ".join(
                            f"{p['kind']}: {p['message']}" for p in functions.problems
                        )
                        if functions.problems
                        else ""
                    ),
                }
            )
    else:
        try:
            health = client.health()
            remote = client.get(f"{API_PREFIX}/doctor")
            status_payload = client.get(f"{API_PREFIX}/status")
        except ControlError as e:
            if e.status == 404:
                raise _fail(
                    "the running daemon predates `tfs doctor` (0.4.x): restart it"
                )
            raise _fail(str(e))
        same = health.get("version") == VERSION and health.get("hash") == COMMIT
        checks.append(
            {
                "check": "daemon",
                "status": "ok" if same else "warn",
                "detail": f"pid {health.get('pid')}, {health.get('version')} ({short(health.get('hash'))})"
                + (
                    ""
                    if same
                    else f"; this CLI is {describe()}: restart the daemon to run the new code"
                ),
            }
        )
        checks.extend(remote.get("checks", []))
        ui_state = status_payload.get("ui") or {}
        checks.append(
            {
                "check": "ui",
                "status": "ok" if ui_state.get("built") else "warn",
                "detail": "built"
                if ui_state.get("built")
                else "not built (`npm ci && npm run build` in Frontend/)",
            }
        )
    failed = any(c["status"] == "fail" for c in checks)
    if as_json:
        typer.echo(json.dumps({"checks": checks, "ok": not failed}, indent=2))
    else:
        for check in checks:
            typer.echo(f"{check['status']:<5} {check['check']:<10} {check['detail']}")
        warned = sum(1 for c in checks if c["status"] == "warn")
        failures = sum(1 for c in checks if c["status"] == "fail")
        typer.echo(
            f"{len(checks)} check(s): {failures} failed, {warned} warning(s)"
            if failed or warned
            else f"{len(checks)} check(s), all ok"
        )
    if failed:
        raise typer.Exit(1)


# -------------------------------------------------------------------- plan


def _scope_of(where: Root, text: str | None) -> tuple[str | None, str | None]:
    """``PATH`` as ``(file key, directory prefix)``: a directory on disk is
    a prefix, anything else names one file. Exit 2 for a bad path."""
    if text is None:
        return None, None
    if is_anchored(text) or Path(text).is_absolute():
        try:
            key = where.relative(Path(text)).as_posix()
        except OutsideRoot as e:
            raise _fail(str(e), code=2)
    else:
        if has_parent_reference(text):
            raise _fail(f"{text!r} escapes the root", code=2)
        try:
            key = posix_key(text)
        except ValueError as e:
            raise _fail(str(e), code=2)
    if not key or key == ".":
        return None, None
    if same_name(PurePosixPath(key).name, FUNCTIONS_FILE):
        raise _fail(f"{key} is a configuration file, not a data file", code=2)
    if where.absolute(PurePosixPath(key)).is_dir():
        return None, key
    return key, None


def _plan_offline(
    where: Root, key: str | None, prefix: str | None, limit: int
) -> dict[str, Any]:
    from tag_file_system.services.plan import plan_view

    loader, functions = load_root(where)
    try:
        threshold = where.load_config().daemon.confirm_above
    except ConfigError:
        threshold = 0
    backend, store = _open_backend(where)
    try:
        return plan_view(
            loader,
            functions,
            None,
            backend,
            store,
            source=DISK,
            key=key,
            prefix=prefix,
            limit=limit,
            threshold=threshold,
        )
    finally:
        backend.close()


@app.command()
def plan(
    path: Annotated[
        str | None,
        typer.Argument(
            help="One file, or a directory as a prefix (default: the root)."
        ),
    ] = None,
    root: RootOption = None,
    all_files: Annotated[
        bool, typer.Option("--all", help="List every file, not the first 200.")
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable output.")
    ] = False,
) -> None:
    """What `tfs reload` would start, and why: per file, the handlers that
    would run, the ones that already ran, the entries it leaves; how every
    .tfsfunctions.yaml and script differs from what the daemon loaded
    (DESIGN/v0-5-0.md §11.2). Offline: what the next `tfs start` runs."""
    from tag_file_system.services.plan import PLAN_LIMIT, PLAN_MAX

    where = _root(root)
    key, prefix = _scope_of(where, path)
    limit = PLAN_MAX if all_files else PLAN_LIMIT
    holder = Lock(where).holder()
    client, reason = _daemon_or_reason(where, holder)
    if client is None:
        payload = _plan_offline(where, key, prefix, limit)
    else:
        try:
            payload = client.get(
                f"{API_PREFIX}/plan",
                {"path": key, "prefix": prefix, "limit": limit},
                timeout=600.0,
            )
        except ControlError as e:
            if e.status == 404:
                raise _fail(
                    "the running daemon predates `tfs plan` (0.4.x): restart it"
                )
            raise _fail(str(e))
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    _print_plan(where, payload, reason)


def _print_plan(where: Root, payload: dict[str, Any], reason: str | None) -> None:
    scope = payload.get("scope") or {}
    what = scope.get("path") or (
        f"{scope['prefix']}/" if scope.get("prefix") else "the whole root"
    )
    if payload.get("compared"):
        typer.echo(f"plan for {what}: the files on disk against what the daemon loaded")
    else:
        typer.echo(
            f"plan for {what}: no daemon running ({reason}); this is what the next "
            f"`tfs start` runs from script/ and the {FUNCTIONS_FILE} files on disk"
        )
    typer.echo("functions files:")
    for item in payload.get("functions", []):
        changes = [f"+{d}" for d in item.get("added", [])] + [
            f"-{d}" for d in item.get("removed", [])
        ]
        line = f"  {item['file']:<40} {item['state']}"
        if item.get("error"):
            line += f": {item['error']['message']}"
            if item.get("note"):
                line += f" ({item['note']})"
        elif changes:
            line += ": " + ", ".join(changes)
        typer.echo(line)
    if not payload.get("functions"):
        typer.echo("  (none)")
    scripts = [
        s
        for s in payload.get("scripts", [])
        if s.get("state") not in (None, "unchanged")
    ]
    if scripts:
        typer.echo("scripts:")
        for item in scripts:
            detail = item["state"]
            if item["state"] == "changed":
                detail += (
                    f" on disk (loaded {short(item['loaded_hash'])}, disk "
                    f"{short(item['disk_hash'])}): re-imported at reload; existing runs keep their keys"
                )
            elif item["state"] == "not loaded":
                detail += " (loaded at reload)"
            typer.echo(f"  {item['file']:<40} {detail}")
    summary = payload["summary"]
    typer.echo(
        f"would run {summary['would_run']} handler(s) on {summary['files']} file(s); "
        f"{summary['already_ran']} already ran"
        + (f", {summary['stale']} with an older script" if summary["stale"] else "")
        + (
            f"; {summary['leaving']} entr(y/ies) no longer apply"
            if summary["leaving"]
            else ""
        )
    )
    for bucket in summary.get("by_handler", []):
        if not bucket["would_run"] and not bucket["stale"]:
            continue
        typer.echo(
            f"  {bucket['display']:<44} {bucket['hook']:<8} {bucket['would_run']} file(s)"
            + (f", {bucket['stale']} stale" if bucket["stale"] else "")
        )
    files = payload.get("files", [])
    if files:
        note = (
            f" (first {payload['listed']}; --all for every one, --json for the data)"
            if payload.get("truncated")
            else ""
        )
        typer.echo(f"files:{note}")
    for item in files:
        for run in item.get("would_run", []):
            typer.echo(
                f"  {item['path']:<40} {run['display']} {run['hook']}: {run['reason']}"
            )
        for run in item.get("skipped", []):
            if run.get("stale"):
                typer.echo(
                    f"  {item['path']:<40} {run['display']} {run['hook']}: {run['reason']}"
                )
        for leaving in item.get("leaving", []):
            typer.echo(
                f"  {item['path']:<40} {leaving['display']} leaves: {leaving['reason']}"
                " (no `removed` fires on reload)"
            )
    if summary.get("over_threshold"):
        typer.echo(
            f"above confirm_above = {summary['threshold']}: `tfs reload` refuses; "
            "`tfs reload --yes` consents"
        )


# ------------------------------------------------------------ work control


@app.command()
def pause(root: RootOption = None) -> None:
    """Stop starting runs; watching and indexing go on, the work queues
    until `tfs resume` (DESIGN/v0-5-0.md §11.3)."""
    where = _root(root)
    client = _need_daemon(where, Lock(where).holder(), "nothing to pause")
    try:
        state = client.post(f"{API_PREFIX}/pause")
    except ControlError as e:
        raise _fail(str(e))
    typer.echo(
        f"paused: {state['queued']} item(s) queued, {state['in_flight']} run(s) in flight"
    )


@app.command()
def resume(root: RootOption = None) -> None:
    """Start runs again after `tfs pause`."""
    where = _root(root)
    client = _need_daemon(where, Lock(where).holder(), "nothing to resume")
    try:
        state = client.post(f"{API_PREFIX}/resume")
    except ControlError as e:
        raise _fail(str(e))
    typer.echo(f"resumed: {state['queued']} item(s) queued")


@app.command()
def retry(
    run_id: Annotated[str, typer.Argument(help="A failed or interrupted run.")],
    root: RootOption = None,
) -> None:
    """Start a fresh run for a failed or interrupted run, under its own key
    (DESIGN/v0-5-0.md §11.3)."""
    where = _root(root)
    client = _need_daemon(where, Lock(where).holder(), "retry it afterwards")
    try:
        result = client.post(f"{API_PREFIX}/run/retry", {"id": run_id})
    except ControlError as e:
        raise _fail(str(e))
    typer.echo(f"queued a retry of {run_id} ({result.get('slug')})")


@app.command()
def cancel(
    run_id: Annotated[str, typer.Argument(help="A run in flight.")],
    root: RootOption = None,
) -> None:
    """End a run in flight: its record is final at once, the handler's next
    ctx call raises (DESIGN/v0-5-0.md §11.3)."""
    where = _root(root)
    client = _need_daemon(where, Lock(where).holder(), "nothing runs")
    try:
        result = client.post(f"{API_PREFIX}/run/cancel", {"id": run_id})
    except ControlError as e:
        raise _fail(str(e))
    typer.echo(
        f"cancelled {run_id} ({result.get('status')})"
        + (
            "; its worker is abandoned until the handler returns"
            if result.get("worker_abandoned")
            else ""
        )
    )


@app.command()
def rerun(
    handler: Annotated[
        str,
        typer.Option(
            "--handler", help="script.handler, as .tfsfunctions.yaml names it."
        ),
    ],
    path: Annotated[
        str | None,
        typer.Argument(
            help="One file, or a directory as a prefix (default: the root)."
        ),
    ] = None,
    root: RootOption = None,
    failed: Annotated[
        bool,
        typer.Option(
            "--failed", help="Only where the last run failed or was interrupted."
        ),
    ] = False,
    stale: Annotated[
        bool,
        typer.Option(
            "--stale",
            help="Only where the last run used an older version of the script.",
        ),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Count; start nothing.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Consent above [daemon] confirm_above.")
    ] = False,
) -> None:
    """Run a handler again on the files in its scope, finished runs included
    (DESIGN/v0-5-0.md §11.3)."""
    where = _root(root)
    key, prefix = _scope_of(where, path)
    client = _need_daemon(where, Lock(where).holder(), "rerun afterwards")
    try:
        result = client.post(
            f"{API_PREFIX}/rerun",
            {
                "handler": handler,
                "path": key,
                "prefix": prefix,
                "failed": failed,
                "stale": stale,
                "dry": dry_run,
                "yes": yes,
            },
            timeout=600.0,
        )
    except ControlError as e:
        raise _fail(str(e))
    skipped = result.get("skipped") or {}
    skipped_text = ", ".join(
        f"{n} {k.replace('_', ' ')}" for k, n in skipped.items() if n
    )
    verb = (
        "would rerun"
        if dry_run or (result.get("over_threshold") and not yes)
        else "queued"
    )
    typer.echo(
        f"{verb} {result['candidates']} run(s) of {result['handler']} on {result['files']} file(s)"
        + (f"; skipped: {skipped_text}" if skipped_text else "")
    )
    if result.get("over_threshold") and not yes and not dry_run:
        typer.echo(
            f"above confirm_above = {result['threshold']}: nothing started; "
            "`tfs rerun --yes` consents"
        )
        raise typer.Exit(1)


# ------------------------------------------------------------ file commands


def _data_key(where: Root, text: str, *, directory_ok: bool) -> str:
    """A data path inside the root as a key (DESIGN/v0-5-0.md §11.6); exit 2
    for `.tfs/`, `script/`, a functions file, the root or anything outside."""
    if is_anchored(text) or Path(text).is_absolute():
        try:
            key = where.relative(Path(text)).as_posix()
        except OutsideRoot as e:
            raise _fail(str(e), code=2)
    else:
        if has_parent_reference(text):
            raise _fail(f"{text!r} escapes the root", code=2)
        try:
            key = posix_key(text)
        except ValueError as e:
            raise _fail(str(e), code=2)
    if not key or key == ".":
        raise _fail("the root itself is not a file", code=2)
    try:
        zone = where.zone(where.absolute(PurePosixPath(key)))
    except (OutsideRoot, ValueError) as e:
        raise _fail(str(e), code=2)
    if zone is not Zone.DATA:
        raise _fail(f"{key} is not a data path ({zone.value})", code=2)
    if not directory_ok and where.absolute(PurePosixPath(key)).is_dir():
        raise _fail(f"{key} is a directory", code=2)
    return key


def _offline_note(reason: str | None, what: str) -> None:
    typer.echo(f"(no daemon running: {reason}; {what} at the next `tfs start`)")


def _echo_file_result(result: dict[str, Any], verb: str) -> None:
    applies = result.get("applies") or []
    typer.echo(
        f"{verb} {result['path']}"
        + (
            f"    tags: {', '.join(result['file']['tags']) or '-'}"
            if result.get("file")
            else ""
        )
    )
    for display in applies:
        typer.echo(f"  runs {display}")


@app.command()
def touch(
    path: Annotated[
        str, typer.Argument(help="A data file, root-relative or absolute.")
    ],
    root: RootOption = None,
    content: Annotated[
        str | None, typer.Option("--content", help="Text to write into a new file.")
    ] = None,
) -> None:
    """Create a data file (or update its mtime) inside the root; the daemon
    indexes it at once and says what will run on it (DESIGN/v0-5-0.md §11.6)."""
    where = _root(root)
    key = _data_key(where, path, directory_ok=False)
    holder = Lock(where).holder()
    client, reason = _daemon_or_reason(where, holder)
    if client is None:
        target = where.absolute(PurePosixPath(key))
        created = not target.exists()
        if created:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content or "", encoding="utf-8")
        else:
            os.utime(target, None)
        typer.echo(f"{'created' if created else 'touched'} {key}")
        _offline_note(reason, "it is indexed")
        return
    try:
        result = client.post(
            f"{API_PREFIX}/files/touch",
            {"path": key},
            body={"content": content} if content is not None else None,
        )
    except ControlError as e:
        raise _fail(str(e))
    _echo_file_result(result, "created" if result.get("created") else "touched")


@app.command()
def cp(
    src: Annotated[str, typer.Argument(help="A data file.")],
    dst: Annotated[str, typer.Argument(help="A file name, or a directory.")],
    root: RootOption = None,
) -> None:
    """Copy a data file inside the root (DESIGN/v0-5-0.md §11.6)."""
    _transfer(root, src, dst, move=False)


@app.command()
def mv(
    src: Annotated[str, typer.Argument(help="A data file.")],
    dst: Annotated[str, typer.Argument(help="A file name, or a directory.")],
    root: RootOption = None,
) -> None:
    """Move a data file inside the root: recorded as a move, so `removed`
    fires for the entries it leaves and `added` for those it enters
    (DESIGN/v0-5-0.md §11.6)."""
    _transfer(root, src, dst, move=True)


def _transfer(root: Path | None, src: str, dst: str, *, move: bool) -> None:
    import shutil

    where = _root(root)
    src_key = _data_key(where, src, directory_ok=False)
    dst_key = _data_key(where, dst, directory_ok=True)
    holder = Lock(where).holder()
    client, reason = _daemon_or_reason(where, holder)
    verb = "moved" if move else "copied"
    if client is None:
        source = where.absolute(PurePosixPath(src_key))
        if not source.is_file():
            raise _fail(f"no such file: {src_key}")
        target = where.absolute(PurePosixPath(dst_key))
        if target.is_dir():
            target = target / source.name
        if target.exists():
            raise _fail(f"{where.relative(target).as_posix()} exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        if move:
            shutil.move(str(source), str(target))
        else:
            shutil.copy2(source, target)
        typer.echo(f"{verb} {src_key} -> {where.relative(target).as_posix()}")
        _offline_note(
            reason, "it is indexed" if not move else "the move is matched by hash"
        )
        return
    try:
        result = client.post(
            f"{API_PREFIX}/files/{'move' if move else 'copy'}",
            {"src": src_key, "dst": dst_key},
            timeout=600.0,
        )
    except ControlError as e:
        raise _fail(str(e))
    _echo_file_result(result, f"{verb} {src_key} ->")


@app.command()
def rm(
    path: Annotated[str, typer.Argument(help="A data file, or a directory with -r.")],
    # `-r` is recursive here, as everyone types it; the root is `--root`
    # (or the global `-r` before the command name).
    root: Annotated[
        Path | None,
        typer.Option("--root", help="The managed root (default: discovered from CWD)."),
    ] = None,
    recursive: Annotated[
        bool,
        typer.Option(
            "--recursive", "-r", help="Delete a directory and everything below it."
        ),
    ] = False,
) -> None:
    """Delete a data file (or a directory with -r) inside the root; the
    daemon soft-deletes the rows and fires `removed` (DESIGN/v0-5-0.md §11.6)."""
    import shutil

    where = _root(root)
    key = _data_key(where, path, directory_ok=True)
    holder = Lock(where).holder()
    client, reason = _daemon_or_reason(where, holder)
    if client is None:
        target = where.absolute(PurePosixPath(key))
        if target.is_dir():
            if not recursive:
                raise _fail(f"{key} is a directory; pass -r", code=2)
            shutil.rmtree(target)
        elif target.is_file():
            target.unlink()
        else:
            raise _fail(f"no such file: {key}")
        typer.echo(f"removed {key}")
        _offline_note(reason, "the rows are retired")
        return
    try:
        result = client.post(
            f"{API_PREFIX}/files/remove",
            {"path": key, "recursive": recursive},
            timeout=600.0,
        )
    except ControlError as e:
        raise _fail(str(e))
    removed = result.get("removed", [])
    typer.echo(
        f"removed {len(removed)} file(s)"
        if len(removed) != 1
        else f"removed {removed[0]}"
    )
    if len(removed) > 1:
        for item in removed:
            typer.echo(f"  {item}")


@app.command()
def mkdir(
    path: Annotated[
        str, typer.Argument(help="A directory, root-relative or absolute.")
    ],
    root: RootOption = None,
) -> None:
    """Create a directory inside the root; its name's tags are what files
    placed there will carry (DESIGN/v0-5-0.md §11.6)."""
    where = _root(root)
    key = _data_key(where, path, directory_ok=True)
    holder = Lock(where).holder()
    client, reason = _daemon_or_reason(where, holder)
    if client is None:
        where.absolute(PurePosixPath(key)).mkdir(parents=True, exist_ok=True)
        from tag_file_system.services.tagging import TaggingParser

        tags = TaggingParser().parse_path(PurePosixPath(key), is_file=False).tag_names
        typer.echo(f"created {key}    tags: {', '.join(tags) or '-'}")
        return
    try:
        result = client.post(f"{API_PREFIX}/files/mkdir", {"path": key})
    except ControlError as e:
        raise _fail(str(e))
    typer.echo(f"created {result['path']}    tags: {', '.join(result['tags']) or '-'}")


# ------------------------------------------------------------------- tags


def _retag(root: Path | None, path: str, add: list[str], remove: list[str]) -> None:
    where = _root(root)
    key = _data_key(where, path, directory_ok=False)
    client = _need_daemon(where, Lock(where).holder(), "tag it afterwards")
    try:
        result = client.post(
            f"{API_PREFIX}/file/tags",
            {"path": key, "add": add or None, "remove": remove or None},
        )
    except ControlError as e:
        raise _fail(str(e))
    tags = ", ".join(result["file"]["tags"]) or "-"
    typer.echo(f"{result['path']}    tags: {tags}")
    if result["added"]:
        typer.echo(f"  added: {', '.join(result['added'])}")
    if result["removed"]:
        typer.echo(f"  removed: {', '.join(result['removed'])}")
    if result["kept"]:
        typer.echo(
            f"  kept: {', '.join(result['kept'])} (spelled by the name; rename the file to drop it)"
        )
    for display in result.get("applies") or []:
        typer.echo(f"  applies: {display}")


@app.command()
def tag(
    path: Annotated[
        str, typer.Argument(help="A data file, root-relative or absolute.")
    ],
    tags: Annotated[list[str], typer.Argument(help="Tags to add.")],
    root: RootOption = None,
) -> None:
    """Add tags to a file without renaming it (DESIGN/v0-5-0.md §12.4); a
    gained tag runs its `tagged` handlers. Needs the daemon."""
    _retag(root, path, tags, [])


@app.command()
def untag(
    path: Annotated[
        str, typer.Argument(help="A data file, root-relative or absolute.")
    ],
    tags: Annotated[list[str], typer.Argument(help="Tags to remove.")],
    root: RootOption = None,
) -> None:
    """Remove tags added with `tfs tag`, `ctx.tag` or the web UI; tags the
    file's name spells stay (rename the file to drop them). Needs the daemon."""
    _retag(root, path, [], tags)


# ------------------------------------------------------------------ main


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(describe())
        raise typer.Exit()


# The `--root` global option lives on the callback so `python -m ... --root X start` works.
@app.callback()
def main(
    ctx: typer.Context,
    root: RootOption = None,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            help="Print the version and commit (0.2.0 (abc1234)), then exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    ctx.obj = {"root": root}
    if root is not None and ctx.invoked_subcommand not in (None, "init"):
        # A global --root applies to every command that did not get its own.
        if ctx.invoked_subcommand == "backup":
            ctx.default_map = {
                "backup": {"list": {"root": root}, "prune": {"root": root}}
            }
        else:
            ctx.default_map = {ctx.invoked_subcommand: {"root": root}}


if __name__ == "__main__":  # pragma: no cover - `python -m tag_file_system.cli`
    app()
