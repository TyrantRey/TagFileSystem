# Code by AkinoAlice@TyrantRey

"""The ``tfs`` command (DESIGN.md §8): exactly six commands.

    tfs init [dir]      tfs list      tfs query -t a -t b ...
    tfs reload          tfs start [-d] [--force]      tfs stop

The CLI is client-first: it talks to the running daemon over the control
channel; when no daemon answers and the database is on a local disk it
reads the database directly; over a network mount with no daemon it
refuses (WAL over SMB/NFS is unsafe).
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated, Any

import typer

from tag_file_system.addons.loader import AddonLoader
from tag_file_system.config import ConfigError
from tag_file_system.core.interface.action import Severity
from tag_file_system.core.logger import configure_logging
from tag_file_system.root import Lock, LockHeld, NotARoot, Root, RootError, RootExists, pid_alive
from tag_file_system.services.control import (
    ControlClient,
    ControlError,
    ControlUnavailable,
    file_payload,
)

app = typer.Typer(
    help="TagFileSystem: tags and add-on functions driven by file names.",
    no_args_is_help=True,
    add_completion=False,
)

RootOption = Annotated[
    Path | None,
    typer.Option("--root", "-r", help="The managed root (default: discovered from CWD)."),
]


def _fail(message: str, code: int = 1) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code)


def _root(root: Path | None) -> Root:
    try:
        return Root.discover(root) if root is not None else Root.discover()
    except NotARoot as e:
        raise _fail(f"{e} (run `tfs init` first)")


def _client(root: Root) -> ControlClient:
    try:
        config = root.load_config()
        token = root.read_token()
    except (ConfigError, RootError) as e:
        raise _fail(str(e))
    return ControlClient(config.daemon.bind, config.daemon.port, token)


def is_network_path(path: Path) -> bool:
    """Best effort: is ``path`` on an SMB/NFS-style mount?"""
    text = str(path)
    if os.name == "nt":
        if text.startswith("\\\\"):
            return True
        try:
            import ctypes

            drive = os.path.splitdrive(os.path.abspath(text))[0] + "\\"
            return ctypes.windll.kernel32.GetDriveTypeW(drive) == 4  # DRIVE_REMOTE
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
    return best is not None and best[1] in {"nfs", "nfs4", "cifs", "smb3", "smbfs", "afpfs", "fuse.sshfs"}


def _open_backend(root: Root):
    """Direct database access for when no daemon answers."""
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


# ------------------------------------------------------------------ init


@app.command()
def init(
    directory: Annotated[Path, typer.Argument(help="Folder to turn into a root.")] = Path("."),
) -> None:
    """Turn a folder into a managed root (creates .tfs/ and script/)."""
    try:
        root = Root.init(directory)
    except (RootExists, RootError) as e:
        raise _fail(str(e))
    from tag_file_system.database.sqlite import SQLiteBackend

    backend = SQLiteBackend()
    backend.init_database(root.db_path, root_dir=root.path)
    backend.close()
    typer.echo(f"Initialized TagFileSystem root at {root.path}")
    typer.echo(f"  add-ons: {root.script_dir}")
    typer.echo(f"  config:  {root.config_path}")


# ------------------------------------------------------------------ list


@app.command("list")
def list_addons(
    root: RootOption = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """List the loaded add-ons with their hooks and arguments."""
    where = _root(root)
    problems: list[tuple[str, str, str]] = []
    try:
        actions = _client(where).actions()
        source = "daemon"
    except ControlUnavailable:
        loader = AddonLoader(
            where,
            report=lambda severity, kind, message, *, action_name=None: problems.append(
                (Severity(severity).value, kind, message)
            ),
        )
        loader.load_all()
        actions = [
            {
                "name": name,
                "script": addon.key.as_posix(),
                "script_hash": addon.script_hash,
                "hooks": [h.spec.describe() for h in addon.file_handlers],
                "problem_hooks": [h.severity.value for h in addon.problem_handlers],
                "signature": addon.signature,
            }
            for name, addon in sorted(loader.addons.items())
        ]
        source = "script/ (no daemon running)"
    except ControlError as e:
        raise _fail(str(e))

    if as_json:
        typer.echo(json.dumps({"source": source, "actions": actions, "problems": problems}, indent=2))
        return
    typer.echo(f"add-ons from {source}:")
    if not actions:
        typer.echo("  (none)")
    for action in actions:
        hooks = ", ".join([*action["hooks"], *(f"on {p}" for p in action["problem_hooks"])]) or "-"
        typer.echo(f"  {action['name']:<20} {hooks}")
        signature = action.get("signature") or {}
        if not isinstance(signature, dict):
            continue
        for hook, schema in signature.items():
            params = ", ".join(
                f"{name}: {spec.get('type', spec.get('x-tfs-path', 'any'))}"
                + (f" = {spec['default']!r}" if "default" in spec else "")
                for name, spec in schema.get("properties", {}).items()
            )
            if params:
                typer.echo(f"      {hook}({params})")
    for severity, kind, message in problems:
        typer.echo(f"  [{severity}] {kind}: {message}")


# ----------------------------------------------------------------- query


@app.command()
def query(
    root: RootOption = None,
    tag: Annotated[list[str] | None, typer.Option("--tag", "-t", help="Required tag (repeatable, ANDed).")] = None,
    name: Annotated[str | None, typer.Option("--name", help="Filename substring (case-insensitive).")] = None,
    file_format: Annotated[str | None, typer.Option("--format", help="Extension with its dot, e.g. .jpg")] = None,
    mime: Annotated[str | None, typer.Option("--mime", help="Exact MIME type, e.g. image/jpeg")] = None,
    prefix: Annotated[str | None, typer.Option("--under", help="Root-relative directory, e.g. @@make_copy")] = None,
    deleted: Annotated[bool, typer.Option("--deleted", help="Include soft-deleted rows.")] = False,
    runs: Annotated[bool, typer.Option("--runs", help="Include each file's run history.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Find files by tag and attributes."""
    where = _root(root)
    try:
        files = _client(where).files(
            tags=tag, name=name, format=file_format, mime=mime, deleted=deleted, prefix=prefix, runs=runs
        )
    except ControlUnavailable:
        backend, store = _open_backend(where)
        try:
            rows = backend.query_files(
                tags=tag or None,
                filename=name,
                file_format=file_format,
                mime_type=mime,
                include_deleted=deleted,
                path_prefix=prefix,
            )
            files = [
                file_payload(f, store.query_runs(file_path=f.path) if runs else None) for f in rows
            ]
        finally:
            backend.close()
    except ControlError as e:
        raise _fail(str(e))

    if as_json:
        typer.echo(json.dumps(files, indent=2))
        return
    if not files:
        typer.echo("(no files)")
        return
    for entry in files:
        tags = ", ".join(entry["tags"]) or "-"
        status = "" if entry["status"] == "active" else f" [{entry['status']}]"
        typer.echo(f"{entry['path']}{status}    tags: {tags}")
        for run in entry.get("runs", []):
            typer.echo(
                f"    {run['action_name']} {run['hook']} {run['status']} {run['started_at']}"
                + (f"  {run['error'].splitlines()[0]}" if run.get("error") else "")
            )


# ---------------------------------------------------------------- reload


@app.command()
def reload(root: RootOption = None) -> None:
    """Re-import the add-ons and re-read config.toml in the running daemon."""
    where = _root(root)
    try:
        result = _client(where).reload()
    except ControlUnavailable as e:
        raise _fail(f"{e}; is the daemon running? (`tfs start`)")
    except ControlError as e:
        raise _fail(str(e))
    typer.echo(f"config {result['config']}; add-ons: {', '.join(result['addons']) or '(none)'}")


# ----------------------------------------------------------------- start


@app.command()
def start(
    root: RootOption = None,
    detach: Annotated[bool, typer.Option("--detach", "-d", help="Run in the background.")] = False,
    force: Annotated[bool, typer.Option("--force", help="Take over a lock held by a dead daemon.")] = False,
) -> None:
    """Reconcile the root and watch it (foreground unless -d)."""
    where = _root(root)
    try:
        config = where.load_config()
    except ConfigError as e:
        raise _fail(str(e))

    if detach:
        log = where.tfs_dir / "daemon.out"
        args = [sys.executable, "-m", "tag_file_system.cli", "--root", str(where.path), "start"]
        if force:
            args.append("--force")
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0x8)
            )
        else:
            kwargs["start_new_session"] = True
        with log.open("ab") as out:
            process = subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kwargs)
        typer.echo(f"daemon started in the background (pid {process.pid}); output in {log}")
        return

    configure_logging(config.logging.level, where.path / config.logging.file)
    from tag_file_system.services.daemon import Daemon

    daemon = Daemon(where, config=config, control=True)
    try:
        daemon.startup(force=force)
    except LockHeld as e:
        raise _fail(f"{e}; use --force if that daemon is gone")
    except OSError as e:
        daemon.shutdown()
        raise _fail(f"cannot open the control channel on {config.daemon.bind}:{config.daemon.port}: {e}")
    typer.echo(
        f"watching {where.path} (control on http://{config.daemon.bind}:{config.daemon.port}); Ctrl+C to stop"
    )
    daemon.run_forever()
    typer.echo("stopped")


# ------------------------------------------------------------------ stop


@app.command()
def stop(
    root: RootOption = None,
    timeout: Annotated[float, typer.Option("--timeout", help="Seconds to wait for the daemon to exit.")] = 30.0,
) -> None:
    """Stop the running daemon gracefully."""
    where = _root(root)
    lock = Lock(where)
    holder = lock.holder()
    try:
        _client(where).stop()
    except ControlUnavailable:
        if holder is None:
            raise _fail("no daemon is running for this root")
        typer.echo(f"control channel unresponsive; signalling pid {holder.pid}")
        if not _signal_stop(holder.pid):
            raise _fail(f"could not stop pid {holder.pid}")
    except ControlError as e:
        raise _fail(str(e))

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = lock.holder()
        if current is None or not pid_alive(current.pid):
            typer.echo("stopped")
            return
        time.sleep(0.2)
    raise _fail(f"the daemon did not stop within {timeout:.0f}s")


def _signal_stop(pid: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        return result.returncode == 0
    import signal

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True


# The `--root` global option lives on the callback so `python -m ... --root X start` works.
@app.callback()
def main(
    ctx: typer.Context,
    root: RootOption = None,
) -> None:
    ctx.obj = {"root": root}
    if root is not None and ctx.invoked_subcommand not in (None, "init"):
        # A global --root applies to every command that did not get its own.
        ctx.default_map = {ctx.invoked_subcommand: {"root": root}}


if __name__ == "__main__":  # pragma: no cover - `python -m tag_file_system.cli`
    app()
