# Code by AkinoAlice@TyrantRey

"""The daemon: one watcher per root (DESIGN.md §5, §6.3, §8).

``startup()`` takes the lock, closes runs a crash left open, loads the
add-ons, replays undelivered problems and reconciles the data tree;
``run_forever()`` then feeds watcher batches to ``process_changes()`` until
``request_stop()``; ``shutdown()`` waits for in-flight runs, releases the
lock and closes the database. ``process_changes`` and ``reconcile`` are
callable directly (tests, ``tfs`` commands).
"""

import os
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable

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
    Severity,
)
from tag_file_system.core.interface.database import OperationResultEnum
from tag_file_system.core.interface.file_metadata import TaggedFile
from tag_file_system.core.interface.tag import ParsedPath
from tag_file_system.core.logger import logger
from tag_file_system.database.action_store import ActionStore
from tag_file_system.database.sqlite import SQLiteBackend
from tag_file_system.root import SCRIPT_DIR, TFS_DIR, Lock, LockInfo, OutsideRoot, Root, Zone
from tag_file_system.services.control import ControlServer
from tag_file_system.services.file_info import compute_file_hash
from tag_file_system.services.indexer import Indexed, Indexer
from tag_file_system.services.tagging import TaggingParser

watchfiles.main.logger.setLevel("WARNING")

_PRIORITY = {Change.deleted: 0, Change.added: 1, Change.modified: 2}
_CASE_INSENSITIVE = os.path.normcase("A") == os.path.normcase("a")


def _exists_exactly(path: Path) -> bool:
    """``exists()`` that, on a case-insensitive filesystem, also wants the
    on-disk spelling to match: after ``a.txt`` → ``A.txt`` the old name must
    still count as deleted."""
    if not path.exists():
        return False
    if not _CASE_INSENSITIVE:
        return True
    try:
        return str(path.resolve()) == str(path.absolute())
    except OSError:
        return True


@dataclass
class ReconcileReport:
    indexed: list[Indexed] = field(default_factory=list)
    removed: list[TaggedFile] = field(default_factory=list)
    moved: list[TaggedFile] = field(default_factory=list)  # vanished rows matched by hash
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
    ) -> None:
        self.root = root
        self.config = config if config is not None else root.load_config()
        self.parser = parser if parser is not None else TaggingParser()
        self.poll_ms = poll_ms
        self.logger = logger
        self.control_enabled = control
        self.control: ControlServer | None = None

        self.backend = SQLiteBackend()
        self.backend.init_database(root.db_path, root_dir=root.path)
        self.store = ActionStore(self.backend)
        self.indexer = Indexer(root, self.backend, self.parser)
        self.loader = AddonLoader(root, store=self.store)
        self.runner = ActionRunner(
            root, self.backend, self.store, self.loader, self.config, self.parser
        )
        self.loader.report = self.runner.problem
        self.lock = Lock(root)
        self._stop = threading.Event()
        self.started = False

    # ------------------------------------------------------------ lifecycle

    def startup(self, force: bool = False) -> None:
        """Lock, recover, load, replay, reconcile. Raises ``LockHeld``."""
        if self.started:
            raise RuntimeError("the daemon is already started")
        previous: LockInfo | None = self.lock.read()
        self.lock.acquire(force=force)
        if previous is not None and not previous.is_mine():
            stale = self.lock.is_stale(previous)
            self.runner.problem(
                Severity.WARN if stale else Severity.CRIT,
                "lock.stale" if stale else "lock.overridden",
                f"took over the lock held by pid {previous.pid} on {previous.hostname} "
                f"since {previous.created_at_text}"
                + ("" if stale else " — that daemon may still be running"),
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
                )
                self.control.start()
            self.loader.load_all()
            self.runner.replay_undelivered()
            self.reconcile()
        except BaseException:
            if self.control is not None:
                self.control.stop()
                self.control = None
            try:
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
        self.runner.check_overdue()

    def shutdown(self) -> None:
        if not self.backend.is_open:
            return
        self.logger.info("Stopping daemon")
        try:
            self.runner.stop(self.config.daemon.stop_timeout_seconds)
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
        return {
            "status": "ok",
            "root": str(self.root.path),
            "pid": os.getpid(),
            "started": self.started,
            "addons": sorted(self.loader.addons),
            "in_flight": in_flight,
        }

    def reload(self) -> dict:
        """``tfs reload``: re-read ``config.toml`` and re-import every add-on.
        ``[logging]`` and ``[daemon] bind/port`` take effect at the next
        ``start``."""
        try:
            self.config = self.root.load_config()
            self.runner.config = self.config
            config_ok = True
        except ConfigError as e:
            config_ok = False
            self.runner.problem(
                Severity.ERR, "config.invalid", f"config.toml not reloaded: {e}"
            )
        loaded = self.loader.load_all()
        return {
            "config": "reloaded" if config_ok else "kept",
            "addons": sorted(a.name for a in loaded),
        }

    def describe_addons(self) -> list[dict]:
        """``tfs list``: every loaded add-on with its hooks and signature."""
        result = []
        for name, addon in sorted(self.loader.addons.items()):
            result.append(
                {
                    "name": name,
                    "script": addon.key.as_posix(),
                    "script_hash": addon.script_hash,
                    "hooks": [h.spec.describe() for h in addon.file_handlers],
                    "problem_hooks": [h.severity.value for h in addon.problem_handlers],
                    "signature": addon.signature,
                }
            )
        return result

    # -------------------------------------------------------------- events

    def process_changes(
        self,
        changes: Iterable[tuple[Change, str]],
        source: RunSource = RunSource.WATCH,
    ) -> None:
        """One watcher batch: consolidate, route by zone, apply."""
        consolidated: dict[str, Change] = {}
        for change, raw in changes:
            if raw not in consolidated or _PRIORITY[change] < _PRIORITY[consolidated[raw]]:
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
            if change is Change.deleted and _exists_exactly(path):
                change = Change.added  # an editor's atomic save: the disk decides
            (script_events if zone is Zone.SCRIPT else data_events).append((change, path))

        for change, path in script_events:
            if change is Change.deleted:
                self.loader.unload(path)
            else:
                self.loader.load(path)
        if data_events:
            self._handle_data(data_events, source)

    def _handle_data(self, events: list[tuple[Change, Path]], source: RunSource) -> None:
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
                self._moved(moved_from, indexed, source)
            else:
                self._fire(indexed, Hook.ADDED, source)
            self._observe(indexed.file.path.as_posix(), in_flight_before)

        for path in modified:
            if path.is_dir() or not path.is_file():
                continue
            indexed = self._index(path)
            if indexed is None:
                continue
            hook = Hook.ADDED if indexed.previous is None else Hook.MODIFIED
            if hook is Hook.MODIFIED and not indexed.content_changed and not indexed.new_tags:
                continue  # touched, unchanged: nothing to do
            self._fire(indexed, hook, source)
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
            return self.indexer.index(path, **kwargs)
        except OSError as e:
            self.runner.problem(
                Severity.ERR,
                "file.unreadable",
                f"{self._key_text(path)} could not be read: {e}",
                file_path=path,
            )
            return None

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
        if _CASE_INSENSITIVE and key.parts:
            # A case-only rename: the resolved key already spells the new
            # case, so find the row by case-folded comparison.
            wanted = os.path.normcase(key.as_posix())
            parent = key.parent.as_posix()
            siblings = (
                self.backend.query_files(path_prefix=parent) if parent != "." else self.backend.query_files()
            )
            return [
                r for r in siblings
                if os.path.normcase(r.path.as_posix()) == wanted and r.path != key
            ]
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

    def _fire(self, indexed: Indexed, hook: Hook, source: RunSource) -> None:
        path = indexed.file.original_path
        for tag in indexed.new_tags:
            self.runner.on_tag(path, indexed.file, tag, source=source)
        self.runner.on_file(hook, path, indexed.file, indexed.parsed, source=source)

    def _moved(self, old: TaggedFile, indexed: Indexed, source: RunSource) -> None:
        old_parsed = self.parser.parse_path(old.path)
        left = [c for c in old_parsed.actions if c not in indexed.parsed.actions]
        if left:
            self.runner.on_file(
                Hook.REMOVED,
                old.original_path,
                old,
                ParsedPath(path=old.path, tags=old_parsed.tags, actions=left),
                source=source,
                moved=True,
            )
        before = {t.name for t in old.tags}
        for tag in indexed.file.tags:
            if tag.name not in before:
                self.runner.on_tag(indexed.file.original_path, indexed.file, tag.name, source=source)
        self.runner.on_file(
            Hook.ADDED, indexed.file.original_path, indexed.file, indexed.parsed, source=source
        )

    def _removed(self, row: TaggedFile, moved: bool, source: RunSource) -> None:
        parsed = self.parser.parse_path(row.path)
        if parsed.actions:
            self.runner.on_file(
                Hook.REMOVED, row.original_path, row, parsed, source=source, moved=moved
            )

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
        handles = [h for h in handles if h.id not in emitted and not h.finished]
        if not handles:
            return
        ambiguous = len(handles) > 1
        for handle in handles:
            self.store.add_provenance(key, handle.id, ProvenanceKind.OBSERVED, ambiguous=ambiguous)
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
                return report
            prefix = self.root.relative(base).as_posix()
        except OutsideRoot:
            return report
        if in_flight is None:
            in_flight = self._in_flight()

        seen: set[str] = set()
        for current, dirs, files in os.walk(base):
            here = Path(current)
            dirs[:] = sorted(
                d for d in dirs
                if not (here == self.root.path and d == SCRIPT_DIR)
                and os.path.normcase(d) != os.path.normcase(TFS_DIR)
            )
            for name in sorted(files):
                path = here / name
                indexed = self._index(path)
                if indexed is None:
                    if path.is_file():
                        report.unreadable.append(self._key_text(path))
                    continue
                seen.add(indexed.file.path.as_posix())
                report.indexed.append(indexed)
                hook = Hook.MODIFIED if indexed.previous is not None and indexed.content_changed else Hook.ADDED
                self._fire(indexed, hook, source)
                if indexed.previous is None or indexed.content_changed:
                    self._observe(indexed.file.path.as_posix(), in_flight)

        rows = self.backend.query_files(path_prefix=prefix) if prefix != "." else self.backend.query_files()
        fresh_hashes = {i.file.file_hash for i in report.indexed if i.previous is None}
        for row in rows:
            key = row.path.as_posix()
            if key in seen or key in report.unreadable:
                continue
            if row.path.parts and os.path.normcase(row.path.parts[0]) in (
                os.path.normcase(TFS_DIR), os.path.normcase(SCRIPT_DIR)
            ):
                continue
            if any(os.path.normcase(p) == os.path.normcase(TFS_DIR) for p in row.path.parts):
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
