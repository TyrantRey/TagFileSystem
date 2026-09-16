# Code by AkinoAlice@TyrantRey

"""The daemon: one watcher per root (DESIGN/v0-1-0.md §5, §6.3, §8).

``startup()`` takes the lock, closes runs a crash left open, loads the
add-ons, gives them their ``on_start``, replays undelivered problems and
reconciles the data tree; ``run_forever()`` then feeds watcher batches to
``process_changes()`` until ``request_stop()``; ``shutdown()`` runs
``on_stop``, waits for in-flight runs, releases the lock and closes the
database. ``process_changes`` and ``reconcile`` are callable directly
(tests, ``tfs`` commands).
"""

import os
import shutil
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from uuid import uuid4

import watchfiles
from watchfiles import Change, watch

from tag_file_system.addons.context import RunHandle
from tag_file_system.addons.loader import AddonLoader
from tag_file_system.addons.runner import ActionRunner
from tag_file_system.config import Config, ConfigError
from tag_file_system.core.interface.action import (
    Hook,
    ProvenanceKind,
    RunSource,
    RunStatus,
    Severity,
)
from tag_file_system.core.interface.database import OperationResultEnum
from tag_file_system.core.interface.file_metadata import TaggedFile
from tag_file_system.core.interface.tag import Tag
from tag_file_system.core.logger import logger
from tag_file_system.database.action_store import ActionStore
from tag_file_system.database.migrations import user_version
from tag_file_system.database.sqlite import SQLiteBackend
from tag_file_system.functions import FunctionsStore, file_label
from tag_file_system.root import (
    FUNCTIONS_FILE,
    SCRIPT_DIR,
    TFS_DIR,
    Lock,
    LockInfo,
    OutsideRoot,
    Root,
    Zone,
    same_name,
)
from tag_file_system.services.api import (
    BadRequest,
    ByteReader,
    Conflict,
    FileResponse,
    NotFound,
    file_payload,
)
from tag_file_system.services.control import ControlServer
from tag_file_system.services.file_info import compute_file_hash
from tag_file_system.services.indexer import Indexed, Indexer
from tag_file_system.services.plan import (
    PLAN_LIMIT,
    candidates as plan_candidates,
    count_would_run,
    plan_view,
)
from tag_file_system.services.tagging import TaggingParser
from tag_file_system.services.ui import default_ui_dir
from tag_file_system.services.views import DAEMON, actions_view, explain_view
from tag_file_system.services.work import WorkItem, WorkQueue
from tag_file_system.version import COMMIT, VERSION

watchfiles.main.logger.setLevel("WARNING")

_PRIORITY = {Change.deleted: 0, Change.added: 1, Change.modified: 2}

UPLOAD_MAX = 4 * 1024**3  # bytes one upload may carry (DESIGN/v0-5-0.md §12.2)
UPLOAD_DIR = "uploads"  # under .tfs/: staged uploads the watcher never sees


def _first_line(error: BaseException) -> str:
    return str(error).splitlines()[0] if str(error) else type(error).__name__


def _case_insensitive(directory: Path) -> bool:
    """Whether names in ``directory`` are case-insensitive — probed, because
    ``os.path.normcase`` says "no" on macOS where APFS says "yes"."""
    probe = directory / f".case-probe-{os.getpid()}"
    try:
        probe.write_text("")
        return probe.with_name(probe.name.upper()).exists()
    except OSError:
        return os.path.normcase("A") == os.path.normcase("a")
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


def _exists_exactly(path: Path, case_insensitive: bool) -> bool:
    """``exists()`` that, on a case-insensitive filesystem, also wants the
    on-disk spelling to match: after ``a.txt`` → ``A.txt`` the old name must
    still count as deleted."""
    if not path.exists():
        return False
    if not case_insensitive:
        return True
    try:
        return str(path.resolve()) == str(path.absolute())
    except OSError:
        return True


@dataclass
class ReconcileReport:
    indexed: list[Indexed] = field(default_factory=list)
    removed: list[TaggedFile] = field(default_factory=list)
    moved: list[TaggedFile] = field(
        default_factory=list
    )  # vanished rows matched by hash
    unreadable: list[str] = field(default_factory=list)

    @property
    def hashed(self) -> int:
        return sum(1 for i in self.indexed if i.hashed)


class Daemon:
    def __init__(
        self,
        root: Root,
        config: Config | None = None,
        parser: TaggingParser | None = None,
        poll_ms: int = 1000,
        control: bool = False,
        apply_logging: bool = False,
        log_console: bool = False,
        ui_dir: Path | None = None,
        workers: int | None = None,
    ) -> None:
        self.root = root
        self.config = config if config is not None else root.load_config()
        self.parser = parser if parser is not None else TaggingParser()
        self.poll_ms = poll_ms
        self.logger = logger
        self.control_enabled = control
        self.control: ControlServer | None = None
        # The work queue (DESIGN/v0-5-0.md §11.3): `[daemon] max_concurrent_runs`
        # worker threads, or 0 — the caller runs its own items, as 0.4.x did
        # and as the tests do — when `workers` says so.
        self.workers = (
            workers if workers is not None else self.config.daemon.max_concurrent_runs
        )
        self.queue = WorkQueue(self.workers, on_error=self._work_failed)
        self.started_at = time.time()
        # The built web UI (DESIGN/v0-5-0.md §3.3): the checkout's
        # Frontend/dist unless a test says otherwise.
        self.ui_dir = ui_dir if ui_dir is not None else default_ui_dir()
        self.apply_logging = apply_logging  # re-apply [logging] on reload
        self.log_console = log_console  # `tfs start --log-console`: stdout too
        self.case_insensitive = _case_insensitive(root.tfs_dir)
        self._load_problems: dict[str, list[dict]] = {}  # script file -> problems

        self.backend = SQLiteBackend()
        self.backend.init_database(root.db_path, root_dir=root.path)
        self.store = ActionStore(self.backend)
        self.indexer = Indexer(root, self.backend, self.parser)
        self.loader = AddonLoader(root, store=self.store)
        self.functions = FunctionsStore(
            root, self.loader, report=self._report_functions_problem
        )
        self.runner = ActionRunner(
            root,
            self.backend,
            self.store,
            self.loader,
            self.config,
            self.parser,
            functions=self.functions,
        )
        self.loader.report = self._report_load_problem
        self.lock = Lock(root)
        self._stop = threading.Event()
        self.started = False
        self._lifecycle_started = False  # on_start has had its turn this session
        self._drifted: set[str] = set()  # folders whose file changed since the load

    # ------------------------------------------------------- load problems

    def _report_load_problem(
        self,
        severity: Severity,
        kind: str,
        message: str,
        /,
        *,
        action_name: str | None = None,
    ) -> object:
        """Loader problems go to the problem log like any other and are also
        kept per script file, so ``tfs list`` can show why a script is not
        what its author expects."""
        first = message.split(" ", 1)[0].rstrip(
            ":"
        )  # loader messages lead with the file
        if first.endswith(".py"):
            script = first
        else:
            script = f"{action_name}.py" if action_name else "script/"
        self._load_problems.setdefault(script, []).append(
            {"severity": Severity(severity).value, "kind": kind, "message": message}
        )
        return self.runner.problem(severity, kind, message, action_name=action_name)

    def _report_functions_problem(
        self,
        severity: Severity,
        kind: str,
        message: str,
        /,
        *,
        action_name: str | None = None,
    ) -> object:
        """A ``.tfsfunctions.yaml`` problem: the store keeps it for ``tfs
        list``; here it reaches the problem log and the notifiers."""
        return self.runner.problem(severity, kind, message)

    def _load(self, path: Path | None = None) -> list:
        """Load one script (or all) and refresh its recorded problems.

        Once the session has started, an add-on that appears (or comes back
        from a failed import) gets its ``on_start`` here; the run key makes
        that a no-op for the add-ons that already had theirs. The
        configuration is re-validated: an entry binds or unbinds with the
        script it names.
        """
        if path is None:
            self._load_problems.clear()
            loaded = self.loader.load_all()
        else:
            self._load_problems.pop(path.name, None)
            addon = self.loader.load(path)
            loaded = [addon] if addon is not None else []
        self.functions.rebind()
        if self._lifecycle_started:
            self.runner.on_lifecycle(Hook.ON_START)
        return loaded

    def actions_view(self) -> dict:
        """``tfs list`` / ``/actions``: the add-ons and every load problem,
        built by ``services.views`` — the same way the CLI builds it offline."""
        script_problems = [
            p for problems in self._load_problems.values() for p in problems
        ]
        return actions_view(self.loader, self.functions, script_problems, DAEMON)

    def load_problems(self) -> list[dict]:
        """Every load problem ``tfs list`` shows: the scripts' and the
        configuration files'."""
        return self.actions_view()["problems"]

    def _folder_key(self, directory: Path) -> str:
        text = self.root.relative(directory).as_posix()
        return "" if text == "." else text

    def _drift(self, path: Path) -> None:
        """A ``.tfsfunctions.yaml`` changed under the watcher: say so once per
        folder, apply nothing — ``tfs reload`` is the commit (§8)."""
        folder = self._folder_key(path.parent)
        if folder in self._drifted:
            return
        self._drifted.add(folder)
        self.runner.problem(
            Severity.WARN,
            "functions.drift",
            f"{file_label(folder)} changed on disk; not applied until `tfs reload`",
        )

    def explain(self, key: str) -> dict:
        """``tfs explain``: what applies to one file and why (§9), from the
        configuration this daemon loaded — what runs."""
        return explain_view(
            self.loader,
            self.functions,
            key,
            self.backend.query_file(key),
            DAEMON,
            self.parser,
        )

    # ------------------------------------------------------------ lifecycle

    def startup(self, force: bool = False) -> None:
        """Lock, recover, load, replay, reconcile. Raises ``LockHeld``."""
        if self.started:
            raise RuntimeError("the daemon is already started")
        previous: LockInfo | None = self.lock.read()
        # The port goes into the lock: `tfs stop` must reach *this* daemon
        # even if config.toml is edited (or broken) while it runs.
        self.lock.acquire(
            force=force,
            port=self.config.daemon.port if self.control_enabled else None,
            bind=self.config.daemon.bind if self.control_enabled else None,
        )
        if previous is not None and not previous.is_mine():
            stale = self.lock.is_stale(previous)
            if previous.upgrade:
                # A crashed `tfs upgrade` (its pid is gone, or --force pushed
                # it aside): the code may be half-swapped, the snapshot is
                # the safe copy.
                what = (
                    f"took over the upgrade marker left by pid {previous.pid} on "
                    f"{previous.hostname} since {previous.created_at_text}: that "
                    f"upgrade did not finish; check the checkout and .tfs/backups/"
                )
            else:
                what = (
                    f"took over the lock held by pid {previous.pid} on {previous.hostname} "
                    f"since {previous.created_at_text}"
                    + ("" if stale else " — that daemon may still be running")
                )
            self.runner.problem(
                Severity.WARN if stale else Severity.CRIT,
                "lock.stale" if stale else "lock.overridden",
                what,
            )
        try:
            for run in self.store.mark_interrupted():
                self.runner.problem(
                    Severity.CRIT,
                    "run.interrupted",
                    f"run {run.id} of {run.action_name} was still running when the daemon last stopped",
                    action_name=run.action_name,
                    run_id=run.id,
                )
            if self.control_enabled:
                self.control = ControlServer(
                    self,
                    self.config.daemon.bind,
                    self.config.daemon.port,
                    self.root.read_token(),
                    ui_dir=self.ui_dir,
                )
                self.control.start()
            self._load()
            self.functions.load_tree()  # needs the handlers: validation binds
            # The add-ons are up and no file has been looked at yet: an
            # on_start handler prepares what the rest of the session (the
            # replayed problems included) is about to use.
            self._lifecycle_started = True
            self.runner.on_lifecycle(Hook.ON_START)
            self.runner.replay_undelivered()
            self.queue.start()  # the workers, now that on_start has run
            self.reconcile()
        except BaseException:
            if self.control is not None:
                self.control.stop()
                self.control = None
            try:
                self.queue.stop(0)
                self._stop_addons()
                self.runner.stop(self.config.daemon.stop_timeout_seconds)
            finally:
                self.lock.release()
                self.backend.close()
            raise
        self.started = True
        self.logger.info(f"Daemon ready on {self.root.path}")

    def _watch_filter(self, change: Change, path: str) -> bool:
        """Watch everything under the root except ``.tfs/`` (the DB's own
        writes); watchfiles' default filter would also hide ``.git``,
        ``node_modules``, ``*~`` and friends, which are data here."""
        try:
            return self.root.zone(Path(path)) is not Zone.TFS
        except OutsideRoot:
            return False

    def run_forever(self) -> None:
        """Watch the root until ``request_stop()``; then ``shutdown()``.

        A batch that raises is reported (P0 ``batch.failed``) and the loop
        goes on; anything that ends the loop early is P0 ``daemon.died``.
        """
        self._install_signal_handlers()
        first = True
        try:
            for changes in watch(
                self.root.path,
                stop_event=self._stop,
                rust_timeout=self.poll_ms,
                yield_on_timeout=True,
                watch_filter=self._watch_filter,
            ):
                if changes:
                    try:
                        self.process_changes(changes)
                    except Exception as e:
                        self.runner.problem(
                            Severity.CRIT,
                            "batch.failed",
                            f"a watcher batch failed: {type(e).__name__}: {e}\n{traceback.format_exc()}",
                        )
                if first:
                    # Files that appeared between reconcile and the watcher
                    # going live: cheap, the fast path skips the rest.
                    first = False
                    self.reconcile()
                self.tick()
        except KeyboardInterrupt:
            self.logger.info("Interrupted")
        except BaseException as e:
            self.runner.problem(
                Severity.CRIT,
                "daemon.died",
                f"the watch loop ended: {type(e).__name__}: {e}\n{traceback.format_exc()}",
            )
            raise
        else:
            # Events the watcher received but never yielded (queued behind a
            # long handler when stop arrived) are picked up by one last pass.
            try:
                self.reconcile()
            except Exception as e:
                self.logger.warning(f"final reconcile skipped: {e}")
        finally:
            self.shutdown()

    def _install_signal_handlers(self) -> None:
        """``tfs stop`` may fall back to a signal; only the main thread can
        register handlers."""
        if threading.current_thread() is not threading.main_thread():
            return
        import signal

        def handler(signum: int, frame: object) -> None:
            self.logger.info(f"Signal {signum}: stopping")
            self.request_stop()

        for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is not None:
                try:
                    signal.signal(sig, handler)
                except (ValueError, OSError):  # pragma: no cover
                    pass

    def request_stop(self) -> None:
        self._stop.set()

    def tick(self) -> None:
        """Once per watch-loop turn: the overdue P2s, the timeouts (their
        workers abandoned), and — with no workers — the queued work."""
        self.runner.check_overdue()
        for handle in self.runner.check_timeouts():
            self.queue.abandon(handle.thread)
        self._drain_inline()

    def _stop_addons(self) -> None:
        """``on_stop`` for every loaded add-on, before in-flight runs are
        waited for: a handler that shuts a service down must be able to end
        the run its ``on_start`` left open."""
        if not self._lifecycle_started:
            return
        self._lifecycle_started = False
        try:
            self.runner.on_lifecycle(Hook.ON_STOP)
        except Exception as e:
            self.logger.warning(f"on_stop skipped: {type(e).__name__}: {e}")

    def shutdown(self) -> None:
        if not self.backend.is_open:
            return
        self.logger.info("Stopping daemon")
        try:
            self._stop_addons()
            timeout = self.config.daemon.stop_timeout_seconds
            deadline = time.monotonic() + timeout
            dropped = self.queue.stop(timeout)
            if dropped:
                self.logger.info(
                    f"{dropped} queued item(s) dropped; the next start reconciles them"
                )
            self.runner.stop(max(0.0, deadline - time.monotonic()))
        finally:
            if self.control is not None:
                self.control.stop()
                self.control = None
            self.lock.release()
            self.backend.close()
            self.started = False

    # ------------------------------------------------------------- control

    def status(self) -> dict:
        """What ``/health`` reports."""
        with self.runner._lock:
            in_flight = [h.run.id for h in self.runner.in_flight.values()]
        queue = self.queue.describe(limit=0)
        daemon = self.config.daemon
        return {
            "status": "stopping" if self._stop.is_set() else "ok",
            "root": str(self.root.path),
            "pid": os.getpid(),
            "started": self.started,
            # The code this daemon runs (DESIGN/v0-2-0.md §3): `tfs upgrade`
            # compares the hash, the one that cannot be true-but-wrong.
            "version": VERSION,
            "hash": COMMIT,
            "addons": sorted(self.loader.addons),
            "in_flight": in_flight,
            # DESIGN/v0-5-0.md §11.5 (additive).
            "paused": queue["paused"],
            "queue": {
                "depth": queue["depth"],
                "active": queue["active"],
                "workers": queue["workers"],
                "since": queue["since"],
            },
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "limits": {
                "max_concurrent_runs": daemon.max_concurrent_runs,
                "max_runs_per_minute": daemon.max_runs_per_minute,
                "run_timeout_seconds": daemon.run_timeout_seconds,
                "confirm_above": daemon.confirm_above,
            },
        }

    def status_detail(self) -> dict:
        """``/api/v1/status``'s extra: what the database says now."""
        store = self.store
        return {
            "files": self.backend.count_files(),
            "runs": {
                "in_flight": len(self.status()["in_flight"]),
                "failed": store.count_runs(status=RunStatus.FAILED),
                "interrupted": store.count_runs(status=RunStatus.INTERRUPTED),
            },
            # P0–P2 no handler has seen; P3 (`run.ok`) would count every run.
            "problems": {
                "undelivered": store.count_problems(
                    at_least=Severity.WARN, undelivered_only=True
                )
            },
            "drift": sorted(file_label(folder) for folder in self._drifted),
            "database": {
                "schema": user_version(self.backend.connection),
                "path": str(self.root.db_path),
            },
        }

    def reload(self, yes: bool = False) -> dict:
        """``tfs reload``: re-read ``config.toml`` (``[logging]`` included when
        the daemon owns the logging setup), re-import every add-on, re-read
        every ``.tfsfunctions.yaml`` and reconcile so a new entry reaches the
        files it covers (DESIGN/v0-4-0.md §8). ``[daemon] bind/port`` take
        effect at the next ``start``.

        When the plan would start more runs than ``confirm_above`` the
        configuration is the only thing reloaded and ``refused`` says how
        many; ``yes`` consents (DESIGN/v0-5-0.md §11.3).
        """
        try:
            self.config = self.root.load_config()
            self.runner.apply_config(self.config)
            config_ok = True
            if self.apply_logging:
                from tag_file_system.core.logger import configure_logging

                configure_logging(
                    self.config.logging.level,
                    self.root.path / self.config.logging.file,
                    stream=self.log_console,
                )
        except ConfigError as e:
            config_ok = False
            self.runner.problem(
                Severity.ERR, "config.invalid", f"config.toml not reloaded: {e}"
            )
        threshold = self.config.daemon.confirm_above
        if threshold > 0 and not yes:
            would_run = count_would_run(
                self.loader,
                self._disk_functions(),
                self.functions,
                self.backend,
                self.store,
            )
            if would_run > threshold:
                self.runner.problem(
                    Severity.WARN,
                    "reload.refused",
                    f"reload would start {would_run} run(s), more than confirm_above = "
                    f"{threshold}; review with `tfs plan`, then `tfs reload --yes`",
                )
                return {
                    "config": "reloaded" if config_ok else "kept",
                    "addons": [],
                    "functions": {
                        "files": len(self.functions.files),
                        "problems": len(self.functions.problems),
                    },
                    "refused": {"would_run": would_run, "threshold": threshold},
                }
        loaded = self._load()
        self._drifted.clear()
        functions = self.functions.load_tree()
        self.reconcile()
        return {
            "config": "reloaded" if config_ok else "kept",
            "addons": sorted(a.name for a in loaded),
            "functions": {
                "files": len(functions.folders),
                "problems": functions.problems,
            },
        }

    def _disk_functions(self) -> FunctionsStore:
        """The tree as it is on disk, read for one answer (`tfs plan`, the
        threshold) and thrown away: the loaded store is untouched."""
        disk = FunctionsStore(self.root, self.loader, quiet=True)
        disk.load_tree()
        return disk

    # ------------------------------------------------------- work control

    def pause(self) -> dict:
        """``tfs pause``: the workers take no more items; runs in flight go
        on, the watcher keeps indexing (DESIGN/v0-5-0.md §11.3)."""
        self.queue.pause()
        self.logger.info("Paused: queued work waits for `tfs resume`")
        return self._pause_state()

    def resume(self) -> dict:
        self.queue.resume()
        self.logger.info("Resumed")
        return self._pause_state()

    def _pause_state(self) -> dict:
        with self.runner._lock:
            in_flight = len(self.runner.in_flight)
        return {
            "paused": self.queue.paused,
            "queued": self.queue.depth,
            "in_flight": in_flight,
        }

    def queue_view(self, limit: int = 20) -> dict:
        """``GET /api/v1/queue``."""
        view = self.queue.describe(limit=limit)
        with self.runner._lock:
            handles = list(self.runner.in_flight.values())
        view["in_flight"] = [
            {
                "run": h.id,
                "action": h.run.action_name,
                "handler": h.run.handler,
                "hook": h.run.hook.value,
                "slug": h.run.slug,
                "elapsed": round(h.elapsed, 1),
                "spawned": h.spawned,
            }
            for h in handles
        ]
        return view

    def plan(
        self,
        key: str | None = None,
        prefix: str | None = None,
        limit: int = PLAN_LIMIT,
    ) -> dict:
        """``tfs plan`` through the daemon (DESIGN/v0-5-0.md §11.2): the tree
        on disk against the tree this daemon loaded."""
        return plan_view(
            self.loader,
            self._disk_functions(),
            self.functions,
            self.backend,
            self.store,
            source=DAEMON,
            key=key,
            prefix=prefix,
            limit=limit,
            threshold=self.config.daemon.confirm_above,
        )

    def retry_run(self, run_id: str) -> dict:
        """``tfs retry``: refuse now for the reasons ``ctx.retry`` would
        (404 unknown, 409 with the reason), else queue it."""
        run = self.store.get_run(run_id)
        if run is None:
            raise NotFound(f"no such run: {run_id}")
        reason = self.runner.retry_check(run_id)
        if reason is not None:
            raise Conflict(reason)
        self._put(
            WorkItem(
                f"retry {run_id[:8]} {run.slug}",
                "retry",
                lambda: self.runner.retry(run_id),
                source=RunSource.RETRY.value,
            )
        )
        return {"queued": True, "retry_of": run_id, "slug": run.slug}

    def cancel_run(self, run_id: str) -> dict:
        """``tfs cancel``: end a run in flight and abandon its worker."""
        handle = self.runner.cancel(run_id)
        if handle is None:
            run = self.store.get_run(run_id)
            if run is None:
                raise NotFound(f"no such run: {run_id}")
            raise Conflict(f"run {run_id} is {run.status.value}, not in flight")
        abandoned = self.queue.abandon(handle.thread)
        return {
            "cancelled": run_id,
            "status": handle.run.status.value,
            "worker_abandoned": abandoned,
        }

    def rerun(
        self,
        handler: str,
        *,
        key: str | None = None,
        prefix: str | None = None,
        failed: bool = False,
        stale: bool = False,
        dry: bool = False,
        yes: bool = False,
    ) -> dict:
        """``tfs rerun --handler script.handler``: every candidate of that
        handler in scope, finished runs retried (DESIGN/v0-5-0.md §11.3)."""
        script, _, name = handler.partition(".")
        if not script or not name:
            raise BadRequest(f"handler must be script.handler, got {handler!r}")
        addon = self.loader.addon_for(script)
        if addon is None or not addon.named(name):
            raise NotFound(f"no loaded handler {handler}")
        chosen: list[tuple[Any, Any, Any]] = []
        skipped = {"in_flight": 0, "not_failed": 0, "not_stale": 0}
        files: set[str] = set()
        for plan in plan_candidates(
            self.loader,
            self.functions,
            None,
            self.backend,
            self.store,
            key=key,
            prefix=prefix,
        ):
            for c in plan.candidates:
                entry = c.applied.entry
                if entry.script != script or entry.handler != name:
                    continue
                if c.existing is not None and not c.existing.status.is_final:
                    skipped["in_flight"] += 1
                    continue
                if failed and (
                    c.existing is None
                    or c.existing.status
                    not in (RunStatus.FAILED, RunStatus.INTERRUPTED)
                ):
                    skipped["not_failed"] += 1
                    continue
                if stale and not c.stale:
                    skipped["not_stale"] += 1
                    continue
                chosen.append((plan.row, c.applied, c.mark))
                files.add(plan.key)
        threshold = self.config.daemon.confirm_above
        over = threshold > 0 and len(chosen) > threshold
        result = {
            "handler": handler,
            "files": len(files),
            "candidates": len(chosen),
            "queued": 0,
            "skipped": skipped,
            "threshold": threshold,
            "over_threshold": over,
            "dry_run": dry,
        }
        if dry or (over and not yes):
            return result
        for row, applied, mark in chosen:
            path = row.original_path
            self._put(
                WorkItem(
                    f"rerun {applied.entry.display()} on {row.path.as_posix()}",
                    "rerun",
                    lambda r=row, a=applied, m=mark, p=path: self.runner.run_mark(
                        m, a, p, r, source=RunSource.RERUN, rerun=True
                    ),
                    source=RunSource.RERUN.value,
                )
            )
        result["queued"] = len(chosen)
        return result

    # ------------------------------------------------------ file commands

    def _data_path(self, key: str) -> Path:
        """The absolute path of a data key, or ``BadRequest``
        (DESIGN/v0-5-0.md §11.6: never `.tfs/`, `script/`, a functions file)."""
        try:
            path = self.root.absolute(PurePosixPath(key))
            zone = self.root.zone(path)
        except (OutsideRoot, ValueError) as e:
            raise BadRequest(f"{key}: {e}") from None
        if zone is not Zone.DATA:
            raise BadRequest(f"{key} is not a data path ({zone.value})")
        return path

    def _applies(self, file: TaggedFile) -> list[str]:
        return [a.entry.display() for a in self.runner.scope(file).applied]

    def _file_result(self, indexed: Indexed | None, key: str, **extra: Any) -> dict:
        payload: dict[str, Any] = {"path": key, **extra}
        if indexed is None:
            payload["file"] = None
            payload["applies"] = []
        else:
            payload["file"] = file_payload(indexed.file)
            payload["applies"] = self._applies(indexed.file)
        return payload

    def touch(self, key: str, content: str | bytes | None = None) -> dict:
        path = self._data_path(key)
        if path.is_dir():
            raise Conflict(f"{key} is a directory")
        created = not path.exists()
        if created:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = content if content is not None else b""
            if isinstance(data, str):
                path.write_text(data, encoding="utf-8")
            else:
                path.write_bytes(data)
        else:
            os.utime(path, None)
        indexed = self._index(path)
        if indexed is not None:
            self._fire(indexed, RunSource.CLI)
        return self._file_result(indexed, key, created=created)

    def _destination(self, src: Path, dst_key: str) -> tuple[Path, str]:
        target = self._data_path(dst_key)
        if target.is_dir():
            target = target / src.name
        if target.exists():
            raise Conflict(f"{self._key_text(target)} exists")
        return target, self._key_text(target)

    def copy(self, src_key: str, dst_key: str) -> dict:
        src = self._data_path(src_key)
        if not src.is_file():
            raise NotFound(f"no such file: {src_key}")
        target, target_key = self._destination(src, dst_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        indexed = self._index(target)
        if indexed is not None:
            self._fire(indexed, RunSource.CLI)
        return self._file_result(indexed, target_key, **{"from": src_key})

    def move(self, src_key: str, dst_key: str) -> dict:
        """A move is recorded as one: the row keeps its identity and the
        transition sees the old row as *previous* (§11.6)."""
        src = self._data_path(src_key)
        if not src.is_file():
            raise NotFound(f"no such file: {src_key}")
        target, target_key = self._destination(src, dst_key)
        previous = self.backend.query_file(self.root.relative(src))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(target))
        if previous is not None:
            renamed = self.backend.modify(previous.path, self.root.relative(target))
            if renamed.status is not OperationResultEnum.SUCCESS:
                self.backend.delete(previous.path)
        indexed = self._index(
            target,
            previous_key=previous.path if previous is not None else None,
            known_hash=previous.file_hash if previous is not None else None,
        )
        if indexed is not None:
            if previous is not None:
                self._enqueue_transition(
                    target, indexed.file, previous, False, RunSource.CLI, False
                )
            else:
                self._fire(indexed, RunSource.CLI)
        return self._file_result(indexed, target_key, **{"from": src_key})

    def remove(self, key: str, recursive: bool = False) -> dict:
        path = self._data_path(key)
        removed: list[str] = []
        if path.is_dir():
            if not recursive:
                raise BadRequest(f"{key} is a directory; pass recursive")
            for row in self.backend.query_files(path_prefix=key):
                self.backend.delete(row.path)
                self._removed(row, moved=False, source=RunSource.CLI)
                removed.append(row.path.as_posix())
            shutil.rmtree(path)
        elif path.is_file():
            row = self.backend.query_file(self.root.relative(path))
            path.unlink()
            if row is not None:
                self.backend.delete(row.path)
                self._removed(row, moved=False, source=RunSource.CLI)
            removed.append(key)
        else:
            raise NotFound(f"no such file: {key}")
        return {"removed": removed}

    def mkdir(self, key: str) -> dict:
        path = self._data_path(key)
        if path.is_file():
            raise Conflict(f"{key} is a file")
        path.mkdir(parents=True, exist_ok=True)
        tags = self.parser.parse_path(PurePosixPath(key), is_file=False).tag_names
        return {"path": key, "tags": list(tags)}

    # ------------------------------------------- the browser's writes (§12)

    def upload(
        self, key: str, stream: ByteReader, length: int, *, overwrite: bool = False
    ) -> dict:
        """Write ``length`` bytes of ``stream`` to the data path ``key``
        (DESIGN/v0-5-0.md §12.2): staged under ``.tfs/uploads/`` — inside the
        root, invisible to the watcher — then moved into place, indexed and
        queued with source ``api``."""
        path = self._data_path(key)
        if path.is_dir():
            raise BadRequest(f"{key} is a directory; name the file to write")
        if length < 0 or length > UPLOAD_MAX:
            raise BadRequest(f"the body must be 0 to {UPLOAD_MAX} bytes, got {length}")
        if path.exists() and not overwrite:
            raise Conflict(f"{key} exists; pass overwrite to replace it")
        staging = self.root.tfs_dir / UPLOAD_DIR
        staging.mkdir(parents=True, exist_ok=True)
        part = staging / f"{uuid4().hex}.part"
        try:
            with part.open("wb") as out:
                remaining = length
                while remaining > 0:
                    chunk = stream.read(min(1 << 20, remaining))
                    if not chunk:
                        raise BadRequest(
                            f"the body ended after {length - remaining} of {length} bytes"
                        )
                    out.write(chunk)
                    remaining -= len(chunk)
            path.parent.mkdir(parents=True, exist_ok=True)
            created = not path.exists()
            os.replace(part, path)
        finally:
            part.unlink(missing_ok=True)
        indexed = self._index(path)
        if indexed is not None:
            self._fire(indexed, RunSource.API)
        return self._file_result(indexed, key, created=created, size=length)

    def content(self, key: str) -> FileResponse:
        """A download: the file of an indexed row, streamed by the server."""
        path = self._data_path(key)
        row = self.backend.query_file(key)
        if row is None or not path.is_file():
            raise NotFound(f"no such file: {key}")
        mime = row.metadata.mime_type if row.metadata is not None else None
        return FileResponse(
            path=path,
            content_type=mime or "application/octet-stream",
            filename=path.name,
            size=path.stat().st_size,
        )

    def retag(
        self,
        key: str,
        add: list[str],
        remove: list[str],
        source: RunSource = RunSource.API,
    ) -> dict:
        """Change a file's tags the way ``ctx.tag``/``ctx.untag`` do
        (DESIGN/v0-5-0.md §12.2): the wanted set is written and the
        transition — old row as *previous* — runs what a gained tag or a
        changed exclusion asks. Tags the name spells are the name's: asking
        to remove one is ``kept``."""
        path = self._data_path(key)
        row = self.backend.query_file(key)
        if row is None:
            raise NotFound(f"no such file: {key}")

        def clean(names: list[str]) -> list[str]:
            out: list[str] = []
            for raw in names:
                try:
                    name = Tag(name=raw).name
                except ValueError as e:
                    raise BadRequest(f"tag {raw!r}: {_first_line(e)}") from None
                if name not in out:
                    out.append(name)
            return out

        adding, removing = clean(add), clean(remove)
        if not adding and not removing:
            raise BadRequest("nothing to add or remove")
        spelled = set(self.parser.parse_path(row.path).tag_names)
        current = [t.name for t in row.tags]
        kept = [n for n in removing if n in spelled]
        dropping = {n for n in removing if n not in spelled}
        wanted = [t for t in current if t not in dropping]
        wanted += [n for n in adding if n not in wanted]
        added = [n for n in adding if n not in current]
        removed = [n for n in removing if n in current and n in dropping]
        fresh = row
        if wanted != current:
            self.backend.set_file_tags(key, wanted)
            fresh = self.backend.query_file(key) or row
            self._enqueue_transition(path, fresh, row, False, source, False)
        return {
            "path": key,
            "file": file_payload(fresh),
            "added": added,
            "removed": removed,
            "kept": kept,
            "applies": self._applies(fresh),
        }

    # -------------------------------------------------------------- doctor

    def doctor(self) -> dict:
        """``GET /api/v1/doctor``: the checks only the daemon can make
        (DESIGN/v0-5-0.md §11.5); the CLI adds the local ones."""
        checks: list[dict[str, str]] = []

        def add(check: str, status: str, detail: str) -> None:
            checks.append({"check": check, "status": status, "detail": detail})

        try:
            verdict = self.backend.connection.execute("PRAGMA quick_check").fetchone()[
                0
            ]
            schema = user_version(self.backend.connection)
        except Exception as e:  # pragma: no cover - a broken database
            add("database", "fail", f"cannot check: {e}")
        else:
            add(
                "database",
                "ok" if verdict == "ok" else "fail",
                f"schema {schema}, quick_check {verdict}, {self.root.db_path}",
            )
        problems = self.load_problems()
        scripts = [p for p in problems if "file" not in p]
        functions = [p for p in problems if "file" in p]
        add(
            "scripts",
            "warn" if scripts else "ok",
            f"{len(self.loader.addons)} add-on(s) loaded"
            + (
                "; " + "; ".join(f"{p['kind']}: {p['message']}" for p in scripts)
                if scripts
                else ""
            ),
        )
        add(
            "functions",
            "warn" if functions else "ok",
            f"{len(self.functions.files)} file(s) loaded"
            + (
                "; " + "; ".join(f"{p['kind']}: {p['message']}" for p in functions)
                if functions
                else ""
            ),
        )
        drift = sorted(file_label(f) for f in self._drifted)
        add(
            "drift",
            "warn" if drift else "ok",
            ", ".join(drift) + " changed on disk: `tfs plan`, then `tfs reload`"
            if drift
            else "every loaded file matches the disk",
        )
        failed = self.store.count_runs(status=[RunStatus.FAILED, RunStatus.INTERRUPTED])
        add(
            "runs",
            "warn" if failed else "ok",
            f"{failed} failed or interrupted run(s): `tfs retry RUN_ID` or `tfs rerun --failed`"
            if failed
            else "no failed run",
        )
        undelivered = self.store.count_problems(
            at_least=Severity.WARN, undelivered_only=True
        )
        has_handlers = any(a.problem_handlers for a in self.loader.addons.values())
        add(
            "problems",
            "warn" if undelivered and has_handlers else "ok",
            f"{undelivered} problem(s) of P2 or worse no handler has seen"
            if undelivered and has_handlers
            else f"{undelivered} of P2 or worse recorded; no problem handler is loaded"
            if undelivered
            else "all delivered",
        )
        state = self.queue.describe(limit=0)
        add(
            "queue",
            "warn" if state["paused"] else "ok",
            f"paused, {state['depth']} item(s) waiting: `tfs resume`"
            if state["paused"]
            else f"{state['depth']} queued, {state['active']} running, {state['workers']} worker(s)",
        )
        try:
            usage = shutil.disk_usage(self.root.path)
            free_gb = usage.free / 1e9
            add(
                "disk",
                "warn" if usage.free < 1_000_000_000 else "ok",
                f"{free_gb:.1f} GB free under the root",
            )
        except OSError as e:  # pragma: no cover
            add("disk", "warn", f"cannot measure: {e}")
        return {"checks": checks}

    # ---------------------------------------------------------- the queue

    def _put(self, item: WorkItem) -> WorkItem:
        """Queue an item; with no workers the watch loop drains it on its
        next turn, or the producer does when it is the watch loop itself."""
        return self.queue.put(item)

    def _drain_inline(self) -> None:
        if self.queue.inline:
            self.queue.run_pending()

    def _work_failed(self, item: WorkItem, error: BaseException) -> None:
        self.runner.problem(
            Severity.CRIT,
            "work.failed",
            f"{item.kind} {item.label} failed: {type(error).__name__}: {error}\n"
            + "".join(traceback.format_exception(error)),
        )

    def _enqueue_transition(
        self,
        path: Path,
        file: TaggedFile,
        previous: TaggedFile | None,
        content_changed: bool,
        source: RunSource,
        rescope: bool,
    ) -> None:
        """Queue one transition. When the worker gets to it the file is
        stat-ed again: if it moved on, it is indexed afresh and the
        transition runs against the file as it is now (DESIGN/v0-5-0.md
        §11.3, race 1)."""
        key = file.path.as_posix()
        snapshot = (
            (file.metadata.file_size, file.metadata.mtime_ns)
            if file.metadata is not None
            else None
        )

        def run() -> None:
            current = file
            changed = content_changed
            try:
                stat = path.stat()
            except OSError:
                return  # gone: its deletion is an item of its own
            if snapshot is not None and (stat.st_size, stat.st_mtime_ns) != snapshot:
                fresh = self._index(path)
                if fresh is None:
                    return
                current = fresh.file
                changed = previous is None or previous.file_hash != current.file_hash
            self.runner.on_transition(
                path,
                current,
                previous,
                content_changed=changed,
                source=source,
                rescope=rescope,
            )

        verb = "rescope" if rescope else "changed" if previous is not None else "added"
        self._put(WorkItem(f"{verb} {key}", "transition", run, source=source.value))
        self._drain_inline()

    def _enqueue_removed(self, row: TaggedFile, moved: bool, source: RunSource) -> None:
        self._put(
            WorkItem(
                f"{'moved away' if moved else 'removed'} {row.path.as_posix()}",
                "removed",
                lambda: self.runner.on_removed(row, moved=moved, source=source),
                source=source.value,
            )
        )
        self._drain_inline()

    def describe_addons(self) -> list[dict]:
        """``tfs list``: every loaded add-on with its hooks and signature."""
        return [addon.describe() for _, addon in sorted(self.loader.addons.items())]

    # -------------------------------------------------------------- events

    def process_changes(
        self,
        changes: Iterable[tuple[Change, str]],
        source: RunSource = RunSource.WATCH,
    ) -> None:
        """One watcher batch: consolidate, route by zone, apply."""
        consolidated: dict[str, Change] = {}
        for change, raw in changes:
            if (
                raw not in consolidated
                or _PRIORITY[change] < _PRIORITY[consolidated[raw]]
            ):
                consolidated[raw] = change

        script_events: list[tuple[Change, Path]] = []
        data_events: list[tuple[Change, Path]] = []
        for raw, change in consolidated.items():
            path = Path(raw)
            try:
                zone = self.root.zone(path)
            except OutsideRoot:
                continue
            if zone is Zone.TFS:
                continue
            if zone is Zone.FUNCTIONS:
                self._drift(path)
                continue
            if change is Change.deleted and _exists_exactly(
                path, self.case_insensitive
            ):
                change = Change.added  # an editor's atomic save: the disk decides
            (script_events if zone is Zone.SCRIPT else data_events).append(
                (change, path)
            )

        for change, path in script_events:
            if change is Change.deleted:
                self._load_problems.pop(path.name, None)
                self.loader.unload(path)
                self.functions.rebind()  # its entries are unbound now
            else:
                self._load(path)
        if data_events:
            self._handle_data(data_events, source)

    def _handle_data(
        self, events: list[tuple[Change, Path]], source: RunSource
    ) -> None:
        in_flight_before = self._in_flight()
        deleted = [p for c, p in events if c is Change.deleted]
        added = [p for c, p in events if c is Change.added]
        modified = [p for c, p in events if c is Change.modified]

        # Rows that vanished: candidates for a move if an added file has the same hash.
        pending: dict[str, list[TaggedFile]] = {}
        gone: dict[str, TaggedFile] = {}
        for path in deleted:
            for row in self._rows_for_deleted(path):
                if row.file_id in gone:
                    continue
                pending.setdefault(row.file_hash, []).append(row)
                gone[row.file_id or row.path.as_posix()] = row
        matched: set[str | None] = set()
        created_hashes: set[str] = set()

        for path in added:
            if path.is_dir():
                report = self.reconcile(path, source, in_flight_before)
                created_hashes.update(i.file.file_hash for i in report.indexed)
                continue
            if not path.is_file():
                continue
            moved_from, digest = self._match_move(path, pending)
            if moved_from is not None:
                matched.add(moved_from.file_id)
                renamed = self.backend.modify(moved_from.path, self.root.relative(path))
                if renamed.status is not OperationResultEnum.SUCCESS:
                    # A row already sits at the destination: it takes over.
                    self.backend.delete(moved_from.path)
            indexed = self._index(
                path,
                previous_key=moved_from.path if moved_from is not None else None,
                known_hash=digest,
            )
            if indexed is None:
                continue
            created_hashes.add(indexed.file.file_hash)
            if moved_from is not None:
                # The row as it was: old key, old tags — the scope it left.
                self.runner.on_transition(
                    path,
                    indexed.file,
                    moved_from,
                    content_changed=False,
                    source=source,
                )
            elif (
                indexed.previous is None or indexed.content_changed or indexed.new_tags
            ):
                # "added" for a path we already know (an atomic save, a
                # write-tmp-and-rename editor) is a modification; the runner
                # tells the two apart by the row that was there.
                self._fire(indexed, source)
            else:
                continue
            self._observe(indexed.file.path.as_posix(), in_flight_before)

        for path in modified:
            if path.is_dir() or not path.is_file():
                continue
            indexed = self._index(path)
            if indexed is None:
                continue
            if (
                indexed.previous is not None
                and not indexed.content_changed
                and not indexed.new_tags
            ):
                continue  # touched, unchanged: nothing to do
            self._fire(indexed, source)
            self._observe(indexed.file.path.as_posix(), in_flight_before)

        for row in gone.values():
            if row.file_id in matched:
                continue
            self.backend.delete(row.path)
            self._observe(row.path.as_posix(), in_flight_before)
            self._removed(row, moved=row.file_hash in created_hashes, source=source)

    def _index(self, path: Path, **kwargs) -> Indexed | None:
        """``Indexer.index`` that turns an unreadable file into a problem
        instead of a dead daemon (a file mid-copy is routine on Windows)."""
        try:
            indexed = self.indexer.index(path, **kwargs)
        except OSError as e:
            self.runner.problem(
                Severity.ERR,
                "file.unreadable",
                f"{self._key_text(path)} could not be read: {e}",
                file_path=path,
            )
            return None
        if indexed is not None:
            self.runner.report_parse_problems(indexed.parsed, path)
        return indexed

    def _rows_for_deleted(self, path: Path) -> list[TaggedFile]:
        try:
            key = self.root.relative(path)
        except OutsideRoot:
            return []
        row = self.backend.query_file(key)
        if row is not None:
            return [row]
        if key.parts:
            below = self.backend.query_files(path_prefix=key.as_posix())
            if below:
                return below
        if self.case_insensitive and key.parts:
            # A case-only rename: the resolved key already spells the new
            # case, so find the row by case-folded comparison.
            folded = self.backend.query_file_folded(key)
            if folded is not None and folded.path != key:
                return [folded]
        return []

    @staticmethod
    def _match_move(
        path: Path, pending: dict[str, list[TaggedFile]]
    ) -> tuple[TaggedFile | None, str | None]:
        """A vanished row with the same content as ``path``, and the hash
        computed to find it (so the indexer need not hash again)."""
        if not pending:
            return None, None
        try:
            digest = compute_file_hash(path)
        except OSError:
            return None, None
        rows = pending.get(digest)
        if not rows:
            return None, digest
        return rows.pop(0), digest

    # ------------------------------------------------------------ dispatch

    def _fire(self, indexed: Indexed, source: RunSource, rescope: bool = False) -> None:
        """Hand a freshly indexed file to the runner — through the queue —
        with the row it replaced: the runner works out added / modified /
        removed from the scope diff (DESIGN/v0-4-0.md §6)."""
        self._enqueue_transition(
            indexed.file.original_path,
            indexed.file,
            indexed.previous,
            indexed.content_changed,
            source,
            rescope,
        )

    def _removed(self, row: TaggedFile, moved: bool, source: RunSource) -> None:
        self._enqueue_removed(row, moved, source)

    def _in_flight(self) -> list[RunHandle]:
        with self.runner._lock:
            return list(self.runner.in_flight.values())

    def _observe(self, key: str, handles: list[RunHandle]) -> None:
        """A change seen while runs were already in flight (DESIGN §6.3):
        attribute it to them as ``observed`` — except to a run that emitted
        the file itself."""
        if not handles:
            return
        emitted = {
            p.run_id
            for p in self.store.query_provenance(file_path=key, include_deleted=True)
            if p.kind is ProvenanceKind.EMITTED
        }
        handles = [
            h
            for h in handles
            # A lifecycle run is in flight for the whole session, so "it was
            # running" says nothing about who changed a file: attributing to
            # it would make every change of every session ambiguous.
            if h.id not in emitted and not h.finished and not h.run.hook.is_lifecycle
        ]
        if not handles:
            return
        ambiguous = len(handles) > 1
        for handle in handles:
            self.store.add_provenance(
                key, handle.id, ProvenanceKind.OBSERVED, ambiguous=ambiguous
            )
        self.runner.problem(
            Severity.WARN,
            "observed",
            f"{key} changed while {len(handles)} run(s) were in flight: "
            + ", ".join(h.run.action_name for h in handles),
            file_path=key,
            run_id=handles[0].id if not ambiguous else None,
        )

    # ---------------------------------------------------------- reconcile

    def reconcile(
        self,
        subtree: Path | None = None,
        source: RunSource = RunSource.RECONCILE,
        in_flight: list[RunHandle] | None = None,
    ) -> ReconcileReport:
        """Walk the data tree (or ``subtree``): index every file, start the
        runs its name asks for, and soft-delete rows whose file is gone —
        as a move when a new row carries the same content."""
        base = Path(subtree) if subtree is not None else self.root.path
        report = ReconcileReport()
        try:
            if subtree is not None and self.root.zone(base) is not Zone.DATA:
                self.logger.warning(
                    f"reconcile skipped: {base} is not a data directory"
                )
                return report
            prefix = self.root.relative(base).as_posix()
        except OutsideRoot:
            self.logger.warning(
                f"reconcile skipped: {base} is outside {self.root.path}"
            )
            return report
        if in_flight is None:
            in_flight = self._in_flight()

        seen: set[str] = set()
        for current, dirs, files in os.walk(base):
            here = Path(current)
            dirs[:] = sorted(
                d
                for d in dirs
                if not (here == self.root.path and d == SCRIPT_DIR)
                and os.path.normcase(d) != os.path.normcase(TFS_DIR)
            )
            for name in sorted(files):
                path = here / name
                if same_name(name, FUNCTIONS_FILE):
                    # Configuration, not data: read at start and reload; a
                    # file that differs from what was loaded is drift.
                    if not self.functions.is_current(self._folder_key(here), path):
                        self._drift(path)
                    continue
                indexed = self._index(path)
                if indexed is None:
                    if path.is_file():
                        report.unreadable.append(self._key_text(path))
                    continue
                seen.add(indexed.file.path.as_posix())
                report.indexed.append(indexed)
                # Every entry in scope is offered again (rescope): the run key
                # makes finished work a no-op and a new entry reaches the file.
                self._fire(indexed, source, rescope=True)
                if indexed.previous is None or indexed.content_changed:
                    self._observe(indexed.file.path.as_posix(), in_flight)

        rows = (
            self.backend.query_files(path_prefix=prefix)
            if prefix != "."
            else self.backend.query_files()
        )
        fresh_hashes = {i.file.file_hash for i in report.indexed if i.previous is None}
        for row in rows:
            key = row.path.as_posix()
            if key in seen or key in report.unreadable:
                continue
            if row.path.parts and os.path.normcase(row.path.parts[0]) in (
                os.path.normcase(TFS_DIR),
                os.path.normcase(SCRIPT_DIR),
            ):
                continue
            if any(
                os.path.normcase(p) == os.path.normcase(TFS_DIR) for p in row.path.parts
            ):
                continue
            if same_name(row.path.name, FUNCTIONS_FILE):
                # A 0.3.x daemon indexed the configuration file as data;
                # retire the row quietly, no handler ever applied to it.
                self.backend.delete(row.path)
                continue
            if self.root.absolute(row.path).is_file():
                # Written while the walk was under way — a run's ctx.copy or
                # ctx.write into a folder the walk had already listed. The
                # context indexed it and chained its own functions: not gone,
                # and not a move of the file it was made from.
                continue
            self.backend.delete(row.path)
            moved = row.file_hash in fresh_hashes
            (report.moved if moved else report.removed).append(row)
            self._removed(row, moved=moved, source=source)
        return report

    # ------------------------------------------------------------- helpers

    def _key_text(self, path: Path | PurePosixPath | str) -> str:
        try:
            return self.root.relative(Path(path)).as_posix()
        except OutsideRoot:
            return str(path)
