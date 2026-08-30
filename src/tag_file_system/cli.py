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
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated, Any

import typer

from tag_file_system.addons.loader import AddonLoader
from tag_file_system.config import Config, ConfigError
from tag_file_system.core.interface.action import Severity
from tag_file_system.core.logger import configure_logging
from tag_file_system.core.paths import has_parent_reference, is_anchored, posix_key
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

STOP_GRACE_SECONDS = 5.0  # on top of the daemon's own stop_timeout_seconds


def _fail(message: str, code: int = 1) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code)


def _root(root: Path | None) -> Root:
    try:
        return Root.discover(root) if root is not None else Root.discover()
    except NotARoot as e:
        raise _fail(f"{e} (run `tfs init` first)")


def _config(root: Root) -> Config:
    try:
        return root.load_config()
    except ConfigError as e:
        raise _fail(str(e))


def _client(root: Root, port: int | None = None, timeout: float = 5.0) -> ControlClient:
    """The daemon's client. A root whose config or token cannot be read has
    no reachable daemon: that is ``ControlUnavailable`` (with the reason), so
    the direct fallbacks still run. ``port`` overrides the configured one
    (the running daemon records the port it opened in ``.tfs/lock``)."""
    bind = "127.0.0.1"
    try:
        token = root.read_token()
        if port is None:
            config = root.load_config()
            bind, port = config.daemon.bind, config.daemon.port
    except (ConfigError, RootError) as e:
        raise ControlUnavailable(str(e).splitlines()[0])
    return ControlClient(bind, port, token, timeout=timeout)


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
    """List the loaded add-ons with their hooks, arguments and load problems."""
    where = _root(root)
    problems: list[dict[str, Any]] = []
    try:
        payload = _client(where).actions()
        actions = payload["actions"]
        problems = payload.get("problems", [])
        source = "daemon"
    except ControlUnavailable as e:
        loader = AddonLoader(
            where,
            report=lambda severity, kind, message, *, action_name=None: problems.append(
                {"severity": Severity(severity).value, "kind": kind, "message": message}
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
        source = f"script/ (no daemon running: {e.message})"
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
                f"{name}: {spec.get('x-tfs-path') or spec.get('type', 'any')}"
                + (f" = {spec['default']!r}" if "default" in spec else "")
                for name, spec in schema.get("properties", {}).items()
            )
            if params:
                typer.echo(f"      {hook}({params})")
    for problem in problems:
        typer.echo(f"  [{problem['severity']}] {problem['kind']}: {problem['message']}")


# ----------------------------------------------------------------- query


@app.command()
def query(
    root: RootOption = None,
    tag: Annotated[list[str] | None, typer.Option("--tag", "-t", help="Required tag (repeatable, ANDed).")] = None,
    name: Annotated[str | None, typer.Option("--name", help="Filename substring (case-insensitive).")] = None,
    file_format: Annotated[str | None, typer.Option("--format", help="Extension with its dot, e.g. .jpg")] = None,
    mime: Annotated[str | None, typer.Option("--mime", help="MIME type, exact (image/jpeg) or a family (image/*).")] = None,
    prefix: Annotated[str | None, typer.Option("--under", help="Root-relative directory, e.g. @@make_copy")] = None,
    deleted: Annotated[bool, typer.Option("--deleted", help="Include soft-deleted rows.")] = False,
    runs: Annotated[bool, typer.Option("--runs", help="Include each file's run history.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Find files by tag and attributes."""
    where = _root(root)
    try:
        tags = validate_tags(tag)
        under = validate_prefix(prefix)
    except ValueError as e:
        raise _fail(str(e), code=2)
    try:
        files = _client(where).files(
            tags=tags, name=name, format=file_format, mime=mime, deleted=deleted, prefix=under, runs=runs
        )
    except ControlUnavailable:
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
        tags_text = ", ".join(entry["tags"]) or "-"
        status = "" if entry["status"] == "active" else f" [{entry['status']}]"
        typer.echo(f"{entry['path']}{status}    tags: {tags_text}")
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
    force: Annotated[
        bool,
        typer.Option("--force", help="Take over a lock that looks live (a lock left by a dead daemon is taken over anyway)."),
    ] = False,
) -> None:
    """Reconcile the root and watch it (foreground unless -d)."""
    where = _root(root)
    config = _config(where)
    try:
        where.read_token()
    except RootError as e:
        raise _fail(f"{e}; the root is damaged, re-run `tfs init` on a fresh folder or restore .tfs/token")

    if detach:
        _start_detached(where, config, force)
        return

    configure_logging(config.logging.level, where.path / config.logging.file, stream=False)
    from tag_file_system.services.daemon import Daemon

    daemon = Daemon(where, config=config, control=True, apply_logging=True)
    try:
        daemon.startup(force=force)
    except LockHeld as e:
        if e.info.is_live_local():
            raise _fail(f"{e}; that process is running on this machine — `tfs stop` it first")
        raise _fail(f"{e}; use --force if that daemon is gone")
    except (RootError, ConfigError) as e:
        daemon.shutdown()
        raise _fail(str(e))
    except OSError as e:
        daemon.shutdown()
        raise _fail(f"cannot open the control channel on {config.daemon.bind}:{config.daemon.port}: {e}")
    typer.echo(
        f"watching {where.path} (control on http://{config.daemon.bind}:{config.daemon.port}); Ctrl+C to stop"
    )
    daemon.run_forever()
    typer.echo("stopped")


def _start_detached(where: Root, config: Config, force: bool) -> None:
    log = where.tfs_dir / "daemon.out"
    client = ControlClient(config.daemon.bind, config.daemon.port, where.read_token(), timeout=1.0)
    try:
        running = client.health()
    except ControlError:
        running = None
    if running is not None and not force:
        raise _fail(
            f"a daemon already answers on {config.daemon.bind}:{config.daemon.port} "
            f"(pid {running.get('pid')}); use `tfs stop` first"
        )
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
        process = subprocess.Popen(
            args, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kwargs
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
            state = "watching" if health.get("started") else "starting: reconciling the root"
            typer.echo(f"daemon started in the background (pid {pid}, {state}); output in {log}")
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
    raise _fail(f"the daemon did not come up within 15s (pid {process.pid}); output so far:\n{tail}")


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
        typer.Option("--timeout", help="Seconds to wait for the daemon to exit (default: its stop_timeout_seconds + 5)."),
    ] = None,
) -> None:
    """Stop the running daemon gracefully."""
    where = _root(root)
    lock = Lock(where)
    holder = lock.holder()
    if timeout is None:
        try:
            timeout = where.load_config().daemon.stop_timeout_seconds + STOP_GRACE_SECONDS
        except ConfigError:
            timeout = 30.0 + STOP_GRACE_SECONDS
    forced = False
    try:
        client = _client(where, port=holder.port if holder is not None else None)
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
        typer.echo(f"control channel unresponsive ({e.message}); signalling pid {holder.pid}")
        sent = _signal_stop(holder.pid)
        if sent is None:
            raise _fail(f"could not stop pid {holder.pid}")
        forced = sent is False
    except ControlError as e:
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
        # A detached console-less daemon cannot receive CTRL_BREAK; taskkill
        # without /F is ignored by console applications. Kill it.
        result = subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        return False if result.returncode == 0 else None
    import signal

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return None
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
