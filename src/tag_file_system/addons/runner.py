# Code by AkinoAlice@TyrantRey

"""The runner: turns file events into runs of add-on handlers and owns the
run lifecycle (DESIGN/v0-1-0.md §4.3, §4.5, §6).

Entry points the engine calls: ``on_file(hook, ...)`` for a file under one
or more ``@@`` directories, ``on_tag(...)`` when a file acquires a tag,
``on_lifecycle(hook)`` when the daemon starts or stops, ``retry(run_id)``,
``replay_undelivered()`` at start, ``check_overdue()`` on every loop tick,
``stop(timeout)`` at shutdown. The runner is also the ``Runtime`` behind
``ActionContext``.

Output capture: ``sys.stdout``/``sys.stderr`` are wrapped once per process
by a dispatcher and a logging handler sits on the root logger; both look up
the run *the current thread* is working for (a thread-local stack pushed
around handler calls and for the lifetime of spawned threads) and write
its trace — otherwise they pass through. So threads never contaminate each
other's traces and nothing is left behind when a run ends.
"""

import io
import logging
import os
import sys
import threading
import time
import traceback
from pathlib import Path, PurePosixPath
from typing import Any, Callable, ClassVar
from uuid import uuid4

from tag_file_system.addons.binding import (
    BindingError,
    SignatureError,
    bind,
    raw_by_name,
)
from tag_file_system.addons.context import ActionContext, ProblemContext, RunHandle
from tag_file_system.addons.loader import AddonLoader, FileHandler
from tag_file_system.config import Config
from tag_file_system.core.interface.action import (
    ActionRecord,
    Hook,
    ProblemRecord,
    ProvenanceKind,
    RunKey,
    RunRecord,
    RunSource,
    RunStatus,
    Severity,
    TraceKind,
)
from tag_file_system.core.interface.database import OperationResultEnum
from tag_file_system.core.interface.file_metadata import TaggedFile
from tag_file_system.core.interface.tag import ActionCall, ParsedPath, normalize_tag
from tag_file_system.core.logger import logger
from tag_file_system.database.action_store import (
    ActionStore,
    RunAlreadyFinal,
    RunExists,
)
from tag_file_system.database.sqlite import SQLiteBackend
from tag_file_system.root import TFS_DIR, SCRIPT_DIR, OutsideRoot, Root, Zone
from tag_file_system.services.indexer import Indexer
from tag_file_system.services.tagging import TaggingParser

MAX_CHAIN_DEPTH = 8
MAX_RETRIES = 5  # a problem handler retrying in a loop stops here

_current = threading.local()  # .stack: list[RunHandle] this thread works for


def _current_handle() -> RunHandle | None:
    stack = getattr(_current, "stack", None)
    return stack[-1] if stack else None


def _push(handle: RunHandle) -> None:
    stack = getattr(_current, "stack", None)
    if stack is None:
        stack = _current.stack = []
    stack.append(handle)


def _pop() -> None:
    stack = getattr(_current, "stack", None)
    if stack:
        stack.pop()


class _Dispatcher(io.TextIOBase):
    """``sys.stdout``/``sys.stderr`` replacement: lines written by a thread
    that works for a run go to that run's trace, everything else passes
    through to the original stream."""

    def __init__(self, original: Any, stream: str) -> None:
        super().__init__()
        self.original = original
        self._stream = stream
        self._buffers: dict[str, str] = {}

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        handle = _current_handle()
        if handle is None:
            return self.original.write(text)
        buffer = self._buffers.get(handle.id, "") + text
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            handle.sink(f"{self._stream}: {line}")
        self._buffers[handle.id] = buffer
        return len(text)

    def flush(self) -> None:
        handle = _current_handle()
        if handle is None:
            self.original.flush()
            return
        pending = self._buffers.pop(handle.id, "")
        if pending:
            handle.sink(f"{self._stream}: {pending}")

    def release(self, handle: RunHandle) -> None:
        """Flush what a finished run still had buffered."""
        pending = self._buffers.pop(handle.id, "")
        if pending:
            handle.sink(f"{self._stream}: {pending}")

    def close(self) -> None:
        """An add-on closing ``sys.stdout`` must not close the process's."""
        self.flush()

    @property
    def closed(self) -> bool:
        return False

    def __getattr__(self, name: str) -> Any:  # encoding, isatty, fileno, ...
        return getattr(self.original, name)


class _TraceLogHandler(logging.Handler):
    """Logging records raised by a thread working for a run become ``log``
    trace entries (the daemon's own loggers and ``ctx.log``, which is
    traced explicitly, are left out)."""

    def emit(self, record: logging.LogRecord) -> None:
        handle = _current_handle()
        if handle is None or record.name.startswith(("tag_file_system", "tfs.")):
            return
        try:
            handle.sink(f"{record.levelname.lower()}: {record.getMessage()}")
        except Exception:  # pragma: no cover - never let tracing break a run
            pass


class ActionRunner:
    _capture_installed: ClassVar[bool] = False

    def __init__(
        self,
        root: Root,
        backend: SQLiteBackend,
        store: ActionStore,
        loader: AddonLoader,
        config: Config | None = None,
        parser: TaggingParser | None = None,
        max_chain_depth: int = MAX_CHAIN_DEPTH,
    ) -> None:
        self.root = root
        self.backend = backend
        self.store = store
        self.loader = loader
        self.config = config if config is not None else Config()
        self.parser = parser if parser is not None else TaggingParser()
        self.indexer = Indexer(root, backend, self.parser)
        self.max_chain_depth = max_chain_depth
        # This daemon session: the run key of the lifecycle hooks, which have
        # no file to be keyed by (DESIGN/v0-3-0.md §2).
        self.session = uuid4().hex
        self.logger = logger
        self.in_flight: dict[str, RunHandle] = {}
        self._lock = threading.RLock()
        self._dispatching = threading.local()
        self._reported_parse_problems: set[tuple[str, str]] = set()
        self._install_capture()

    # ---------------------------------------------------------- entry points

    def on_file(
        self,
        hook: Hook,
        path: Path,
        file: TaggedFile,
        parsed: ParsedPath,
        source: RunSource = RunSource.WATCH,
        parent: RunHandle | None = None,
        moved: bool = False,
    ) -> list[RunRecord]:
        """Run every handler of ``hook`` for every ``@@`` call on the path,
        parent first. ``moved`` marks a REMOVED event caused by the file
        leaving the directory rather than being deleted."""
        hook = Hook(hook)
        self._report_parse_problems(parsed, path)
        if parent is not None and not self._chain_allowed(
            parent, f"@@ functions of {self._key_text(path)}", path
        ):
            return []
        runs: list[RunRecord] = []
        for call in parsed.actions:
            addon = self.loader.addon_for(call.name)
            if addon is None:
                self.problem(
                    Severity.ERR,
                    "action.unbound",
                    f"@@{call.slug}: no add-on script/{call.name}.py is loaded",
                    action_name=call.name,
                    file_path=path,
                )
                continue
            for handler in addon.handlers(hook):
                if hook is Hook.REMOVED and moved and not handler.spec.on_move:
                    continue
                run = self._execute(
                    handler,
                    hook,
                    raw_args=call.args,
                    key_args=None,
                    slug=call.slug,
                    path=path,
                    file=file,
                    source=source,
                    parent=parent,
                )
                if run is not None:
                    runs.append(run)
        return runs

    def on_tag(
        self,
        path: Path,
        file: TaggedFile,
        tag: str,
        source: RunSource = RunSource.WATCH,
        parent: RunHandle | None = None,
    ) -> list[RunRecord]:
        """Run every ``@action.tagged(tag)`` handler for ``path``."""
        tag = normalize_tag(tag)
        if parent is not None and not self._chain_allowed(
            parent, f"tag {tag!r} on {self._key_text(path)}", path
        ):
            return []
        runs: list[RunRecord] = []
        for handler in self.loader.tag_handlers(tag):
            run = self._execute(
                handler,
                Hook.TAGGED,
                raw_args=(),
                key_args={"tag": tag},
                slug=f"--{tag}",
                path=path,
                file=file,
                source=source,
                parent=parent,
            )
            if run is not None:
                runs.append(run)
        return runs

    def on_lifecycle(self, hook: Hook) -> list[RunRecord]:
        """Run every loaded add-on's ``on_start`` / ``on_stop`` handler for
        this daemon session (DESIGN/v0-3-0.md §2).

        The session is the run key, so calling this again — after a reload,
        or when a script appears while the daemon runs — only gives a turn to
        the add-ons that have not had one yet.
        """
        hook = Hook(hook)
        if not hook.is_lifecycle:
            raise ValueError(f"{hook.value} is not a lifecycle hook")
        runs: list[RunRecord] = []
        for handler in self.loader.lifecycle_handlers(hook):
            addon = handler.addon
            key = RunKey(
                file_hash="",  # no file: the session is what makes it unique
                action_name=addon.name,
                hook=hook,
                args={"session": self.session},
            )
            if self.store.find_run(key) is not None:
                continue
            run = self._invoke(
                self._action_record(addon),
                key,
                handler.func,
                slug=hook.value,
                path=None,
                file=None,
                args={},
                source=RunSource.LIFECYCLE,
            )
            if run is not None:
                runs.append(run)
        return runs

    def _chain_allowed(self, parent: RunHandle, what: str, path: Path) -> bool:
        if parent.depth + 1 <= self.max_chain_depth:
            return True
        self.problem(
            Severity.ERR,
            "chain.depth",
            f"{what} would start a chain deeper than {self.max_chain_depth}; stopped",
            file_path=path,
            run_id=parent.id,
        )
        return False

    def retry(self, run_id: str) -> RunRecord | None:
        """Start a fresh run for a failed/interrupted run (DESIGN §4.5)."""
        previous = self.store.get_run(run_id)
        if previous is None or previous.status not in (
            RunStatus.FAILED,
            RunStatus.INTERRUPTED,
        ):
            return None
        if previous.hook.is_lifecycle:
            self.problem(
                Severity.WARN,
                "retry.lifecycle",
                f"run {run_id} is the {previous.hook.value} run of "
                f"{previous.action_name}: a lifecycle hook happens once per "
                f"daemon session and is not retried",
                action_name=previous.action_name,
                run_id=run_id,
            )
            return None
        if self._retry_depth(previous) >= MAX_RETRIES:
            self.problem(
                Severity.WARN,
                "retry.limit",
                f"run {run_id} of {previous.action_name} has already been retried "
                f"{MAX_RETRIES} times; not retrying again",
                action_name=previous.action_name,
                run_id=run_id,
            )
            return None
        file = self._file_for(previous)
        if file is None or not file.original_path.is_file():
            self.problem(
                Severity.ERR,
                "retry.file_missing",
                f"cannot retry {run_id}: its file is no longer in the root",
                action_name=previous.action_name,
                run_id=run_id,
            )
            return None
        addon = self.loader.addon_for(previous.action_name)
        if addon is None:
            self.problem(
                Severity.ERR,
                "action.unbound",
                f"cannot retry {run_id}: add-on {previous.action_name} is not loaded",
                action_name=previous.action_name,
                run_id=run_id,
            )
            return None
        path = file.original_path
        if previous.hook is Hook.TAGGED:
            tag = str(previous.args.get("tag", ""))
            handlers = [h for h in addon.handlers(Hook.TAGGED) if h.spec.tag == tag]
            raw_args: tuple[str, ...] = ()
            key_args: dict[str, Any] | None = {"tag": tag}
        else:
            handlers = addon.handlers(previous.hook)
            raw_args = ActionCall.from_marker(previous.slug).args
            key_args = None
        for handler in handlers:
            try:
                run = self._execute(
                    handler,
                    previous.hook,
                    raw_args=raw_args,
                    key_args=key_args,
                    slug=previous.slug,
                    path=path,
                    file=file,
                    source=RunSource.RETRY,
                    parent=None,
                    retry_of=run_id,
                )
            except (ValueError, KeyError) as e:
                # The handler's parameters changed: the old key no longer
                # matches, the next file event will run the new signature.
                self.problem(
                    Severity.ERR,
                    "retry.key_changed",
                    f"cannot retry {run_id}: {e}",
                    action_name=previous.action_name,
                    run_id=run_id,
                )
                return None
            if run is not None:
                return run
        return None

    def replay_undelivered(self) -> int:
        """Hand every never-delivered problem to the handlers loaded now."""
        delivered = 0
        for problem in self.store.query_problems(undelivered_only=True):
            if self._dispatch_problem(problem):
                delivered += 1
        return delivered

    def check_overdue(self) -> list[RunHandle]:
        """Raise the P2 for runs older than ``run_warn_after_seconds``."""
        threshold = self.config.daemon.run_warn_after_seconds
        overdue: list[RunHandle] = []
        with self._lock:
            handles = list(self.in_flight.values())
        for handle in handles:
            # A service started in on_start is meant to outlive the threshold:
            # it ends with the session, not late (DESIGN/v0-3-0.md §5).
            if (
                handle.warned
                or handle.run.hook.is_lifecycle
                or handle.elapsed <= threshold
            ):
                continue
            handle.warned = True
            overdue.append(handle)
            self.problem(
                Severity.WARN,
                "run.overdue",
                f"run {handle.id} of {handle.run.action_name} has been running for "
                f"{handle.elapsed:.0f}s (threshold {threshold:.0f}s)",
                action_name=handle.run.action_name,
                run_id=handle.id,
            )
        return overdue

    def stop(self, timeout: float | None = None) -> list[RunRecord]:
        """Wait up to ``timeout`` for tracked threads, then interrupt
        whatever is still running."""
        if timeout is None:
            timeout = self.config.daemon.stop_timeout_seconds
        deadline = time.monotonic() + timeout
        with self._lock:
            handles = list(self.in_flight.values())
        for handle in handles:
            for thread in handle.threads:
                thread.join(max(0.0, deadline - time.monotonic()))
        with self._lock:
            leftover = [h for h in self.in_flight.values() if not h.finished]
            for handle in leftover:
                handle.finished = True
                self.in_flight.pop(handle.id, None)
        interrupted = self.store.mark_interrupted([h.id for h in leftover])
        for run in interrupted:
            self.problem(
                Severity.CRIT,
                "run.interrupted",
                f"run {run.id} of {run.action_name} was still running at shutdown",
                action_name=run.action_name,
                run_id=run.id,
            )
        return interrupted

    # ------------------------------------------------------------ execution

    def _execute(
        self,
        handler: FileHandler,
        hook: Hook,
        *,
        raw_args: tuple[str, ...],
        key_args: dict[str, Any] | None,
        slug: str,
        path: Path,
        file: TaggedFile,
        source: RunSource,
        parent: RunHandle | None,
        retry_of: str | None = None,
    ) -> RunRecord | None:
        addon = handler.addon
        record = self._action_record(addon)
        abs_path = self._abs(path)

        # The key first: it needs no resolver, and most events find their
        # run already there (DESIGN §6.1).
        try:
            args = (
                key_args
                if key_args is not None
                else raw_by_name(handler.func, raw_args)
            )
        except SignatureError as e:
            args = {"_args": list(raw_args)}
            binding_failure: BindingError | None = BindingError(str(e))
        else:
            binding_failure = None
        key = RunKey(
            file_hash=file.file_hash, action_name=addon.name, hook=hook, args=args
        )
        if retry_of is None:
            if self.store.find_run(key) is not None:
                return None
            if hook in (Hook.ADDED, Hook.MODIFIED) and self._produced_by(
                path, addon.name
            ):
                return None  # a generated file never re-triggers its producer

        if binding_failure is None:
            try:
                bound = bind(handler.func, raw_args, self.resolve)
            except BindingError as e:
                binding_failure = e
        if binding_failure is not None:
            try:
                run = self.store.start_run(
                    record,
                    key,
                    slug,
                    path,
                    source,
                    parent_run_id=parent.id if parent else None,
                    retry_of=retry_of,
                )
            except RunExists:
                return None
            run = self.store.finish_run(
                run.id, RunStatus.FAILED, error=f"binding error: {binding_failure}"
            )
            self.problem(
                Severity.ERR,
                "action.binding",
                f"@@{slug} on {self._key_text(path)}: {binding_failure}",
                action_name=addon.name,
                file_path=path,
                run_id=run.id,
            )
            return run

        return self._invoke(
            record,
            key,
            lambda ctx: handler.func(abs_path, file.metadata, ctx, **bound.kwargs),
            slug=slug,
            path=abs_path,
            file=file,
            args=bound.kwargs,
            source=source,
            parent=parent,
            retry_of=retry_of,
        )

    def _invoke(
        self,
        record: ActionRecord,
        key: RunKey,
        call: Callable[[ActionContext], Any],
        *,
        slug: str,
        path: Path | None,
        file: TaggedFile | None,
        args: dict[str, Any],
        source: RunSource,
        parent: RunHandle | None = None,
        retry_of: str | None = None,
    ) -> RunRecord | None:
        """Start the run, call the handler with its output captured into the
        trace, and finish it. Everything a lifecycle run shares with a file
        run is here; only ``call`` knows the handler's shape."""
        try:
            run = self.store.start_run(
                record,
                key,
                slug,
                path,
                source,
                parent_run_id=parent.id if parent else None,
                retry_of=retry_of,
            )
        except RunExists:
            return None

        handle = RunHandle(run=run, depth=parent.depth + 1 if parent else 0)
        handle.sink = lambda line, h=handle: self.trace(h, TraceKind.LOG, line)
        with self._lock:
            self.in_flight[run.id] = handle
        ctx = ActionContext(self, handle, file, path, args)
        self._ensure_capture()
        streams = (sys.stdout, sys.stderr)
        _push(handle)
        try:
            result = call(ctx)
        except KeyboardInterrupt:
            self._finish(
                handle, RunStatus.INTERRUPTED, error="interrupted by the operator"
            )
            raise
        except (
            BaseException
        ) as e:  # SystemExit included: an add-on must not stop the daemon
            error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
            return self._finish(handle, RunStatus.FAILED, error=error) or handle.run
        finally:
            _pop()
            # An add-on that swapped the process streams or lowered the root
            # logger must not affect the daemon or the next run.
            sys.stdout, sys.stderr = streams
            self._ensure_capture()
            self._release_output(handle)
        if handle.finished:
            return self.store.get_run(handle.id) or handle.run  # ctx.done() ran already
        if handle.spawned:
            if result is not None:
                handle.result = result
            return handle.run  # still running until ctx.done()
        return self._finish(handle, RunStatus.OK, result=result) or handle.run

    def _finish(
        self,
        handle: RunHandle,
        status: RunStatus,
        result: Any = None,
        error: str | None = None,
    ) -> RunRecord | None:
        with handle.lock:
            if handle.finished:
                return None
            handle.finished = True
        self._release_output(handle)
        already_final = False
        try:
            run = self.store.finish_run(handle.id, status, result=result, error=error)
        except RunAlreadyFinal:
            run = self.store.get_run(handle.id)
            already_final = True
        except TypeError as e:  # result not JSON-serializable
            run = self.store.finish_run(
                handle.id,
                RunStatus.FAILED,
                error=f"result is not JSON-serializable: {e}",
            )
            status = RunStatus.FAILED
            error = str(e)
        with self._lock:
            self.in_flight.pop(handle.id, None)
        if run is None:
            return None
        handle.run = run
        if already_final:
            return run  # interrupted/failed meanwhile: that problem was raised then
        file_path = self._path_of_run(run)
        subject = self._subject(run, file_path)
        if status is RunStatus.OK:
            self.problem(
                Severity.INFO,
                "run.ok",
                f"{run.action_name} finished on {subject}",
                action_name=run.action_name,
                file_path=file_path,
                run_id=run.id,
            )
        elif status is RunStatus.FAILED:
            self.problem(
                Severity.ERR,
                "run.failed",
                f"{run.action_name} failed on {subject}: "
                f"{(error or '').splitlines()[0] if error else status}",
                action_name=run.action_name,
                file_path=file_path,
                run_id=run.id,
            )
        elif status is RunStatus.INTERRUPTED:
            self.problem(
                Severity.CRIT,
                "run.interrupted",
                f"{run.action_name} on {subject} was interrupted: {error}",
                action_name=run.action_name,
                file_path=file_path,
                run_id=run.id,
            )
        return run

    # ------------------------------------------------------------- capture

    @classmethod
    def _install_capture(cls) -> None:
        """Wrap the process streams and the root logger once."""
        if cls._capture_installed:
            return
        cls._capture_installed = True
        root_logger = logging.getLogger()
        root_logger.addHandler(_TraceLogHandler())
        if root_logger.level == logging.NOTSET or root_logger.level > logging.INFO:
            root_logger.setLevel(logging.INFO)  # add-on INFO records reach the trace
        cls._ensure_capture()

    @staticmethod
    def _ensure_capture() -> None:
        """Re-wrap the streams if something (a test harness, an add-on)
        replaced them since, and keep the root logger at INFO so add-on
        records still reach traces."""
        if not isinstance(sys.stdout, _Dispatcher):
            sys.stdout = _Dispatcher(sys.stdout, "stdout")
        if not isinstance(sys.stderr, _Dispatcher):
            sys.stderr = _Dispatcher(sys.stderr, "stderr")
        root_logger = logging.getLogger()
        if root_logger.level == logging.NOTSET or root_logger.level > logging.INFO:
            root_logger.setLevel(logging.INFO)

    @staticmethod
    def _release_output(handle: RunHandle) -> None:
        for stream in (sys.stdout, sys.stderr):
            if isinstance(stream, _Dispatcher):
                stream.release(handle)

    # ------------------------------------------------------------- problems

    def problem(
        self,
        severity: Severity,
        kind: str,
        message: str,
        *,
        action_name: str | None = None,
        file_path: Path | PurePosixPath | str | None = None,
        run_id: str | None = None,
    ) -> ProblemRecord:
        """Record a problem and hand it to the subscribed handlers. A problem
        raised while a handler is being dispatched (a run it started failed)
        is delivered right after that handler returns."""
        severity = Severity(severity)
        level = {
            Severity.CRIT: logging.CRITICAL,
            Severity.ERR: logging.ERROR,
            Severity.WARN: logging.WARNING,
            Severity.INFO: logging.INFO,
        }[severity]
        self.logger.log(level, f"[{severity}] {kind}: {message}")
        record = self.store.record_problem(
            severity,
            kind,
            message,
            action_name=action_name,
            file_path=self._key_or_none(file_path),
            run_id=run_id,
        )
        if getattr(self._dispatching, "active", False):
            self._dispatching.queue.append(record)
        else:
            self._dispatch_problem(record)
        return self.store.get_problem(record.id) or record

    def _dispatch_problem(self, record: ProblemRecord) -> bool:
        if getattr(self._dispatching, "active", False):
            return False  # raised inside a handler: never re-dispatched
        handlers = self.loader.problem_handlers(record.severity)
        if not handlers:
            return False
        self._dispatching.active = True
        self._dispatching.queue = []
        delivered = False
        try:
            for handler in handlers:
                try:
                    handler.func(record, ProblemContext(self, record))
                    delivered = True
                except Exception:
                    self.logger.exception(
                        f"Problem handler {handler.name} ({handler.addon.name}) failed "
                        f"on {record.kind}"
                    )
        finally:
            queued = list(self._dispatching.queue)
            self._dispatching.active = False
            self._dispatching.queue = []
        if delivered:
            self.store.mark_delivered([record.id])
        for later in queued:
            self._dispatch_problem(later)
        return delivered

    # -------------------------------------------------------------- runtime

    def trace(self, handle: RunHandle, kind: str, payload: Any) -> None:
        try:
            self.store.add_trace(handle.id, kind, payload)
        except (KeyError, TypeError) as e:
            self.logger.warning(f"trace dropped for run {handle.id}: {e}")

    def emit(self, handle: RunHandle, path: Path) -> None:
        """Index an output file, record it as produced by this run, and give
        its own name (tags and ``@@`` functions) its turn — as a chain."""
        indexed = self._index(path)
        key = self._key_or_none(path)
        if indexed is None or key is None:
            reason = "outside the root" if key is None else "not a data file"
            self.trace(
                handle,
                TraceKind.EMIT,
                {"path": key or str(path), "indexed": False, "reason": reason},
            )
            return
        self.store.add_provenance(key, handle.id, ProvenanceKind.EMITTED)
        self.trace(handle, TraceKind.EMIT, {"path": key})
        abs_path = self._abs(path)
        for tag in indexed.new_tags:
            self.on_tag(
                abs_path, indexed.file, tag, source=RunSource.CHAIN, parent=handle
            )
        if indexed.parsed.actions:
            self.on_file(
                Hook.ADDED,
                abs_path,
                indexed.file,
                indexed.parsed,
                source=RunSource.CHAIN,
                parent=handle,
            )

    def moved(self, handle: RunHandle, src: Path, dst: Path) -> None:
        old_key, new_key = self._key_or_none(src), self._key_or_none(dst)
        if old_key is not None and self.backend.query_file(old_key) is not None:
            renamed = False
            if new_key is not None:
                renamed = (
                    self.backend.modify(old_key, new_key).status
                    is OperationResultEnum.SUCCESS
                )
            if not renamed:
                # Moved outside the root, or onto a file that already has a
                # row: the source is gone either way.
                self.backend.delete(old_key)
        self.emit(handle, dst)

    def deleted(self, handle: RunHandle, path: Path) -> None:
        key = self._key_or_none(path)
        if key is not None and self.backend.query_file(key) is not None:
            self.backend.delete(key)

    def tag(self, handle: RunHandle, path: Path, names: list[str], add: bool) -> None:
        names = [normalize_tag(n) for n in names if normalize_tag(n)]
        try:
            file = self.backend.query_file(path)
        except ValueError:
            file = None
        if file is None:
            indexed = self._index(path)
            file = indexed.file if indexed is not None else None
        if file is None:
            raise FileNotFoundError(f"{path} is not a managed file")
        current = [t.name for t in file.tags]
        if add:
            wanted = list(dict.fromkeys([*current, *names]))
        else:
            wanted = [t for t in current if t not in set(names)]
        self.backend.set_file_tags(path, wanted)
        self.trace(
            handle, TraceKind.RECORD, {"tags": wanted, "path": self._key_text(path)}
        )
        if add:
            fresh = self.backend.query_file(path)
            for name in names:
                if name not in current and fresh is not None:
                    self.on_tag(
                        path, fresh, name, source=RunSource.CHAIN, parent=handle
                    )

    def resolve(self, kind: str, raw: str) -> Path:
        if kind == "remote":
            target = self.config.remotes.get(raw)
            if target is None:
                raise LookupError(
                    f"unknown remote {raw!r} (add it to [remotes] in config.toml)"
                )
            resolved = Path(target)
            if not resolved.is_absolute():
                raise LookupError(
                    f"remote {raw!r} = {target!r} is not an absolute path on this host"
                )
            return resolved
        if kind == "tagdir":
            tag = normalize_tag(raw)
            candidates = self._directories_tagged(tag)
            if not candidates:
                raise LookupError(f"no directory carries the tag {tag!r}")
            if len(candidates) > 1:
                listed = ", ".join(self._key_text(c) for c in candidates)
                raise LookupError(f"tag {tag!r} is ambiguous: {listed}")
            return candidates[0]
        raise ValueError(f"unknown path argument kind {kind!r}")

    def spawn(
        self, handle: RunHandle, fn: Callable, args: tuple, kwargs: dict
    ) -> threading.Thread:
        def target() -> None:
            _push(handle)
            try:
                fn(*args, **kwargs)
            except Exception as e:
                message = f"{type(e).__name__}: {e}"
                self.trace(handle, TraceKind.LOG, f"thread failed: {message}")
                self.problem(
                    Severity.ERR,
                    "thread.failed",
                    f"a thread of run {handle.id} raised {message}",
                    action_name=handle.run.action_name,
                    run_id=handle.id,
                )
                # A thread that dies without ctx.done() would leave the run
                # open until shutdown: that is a failed run.
                self._finish(
                    handle,
                    RunStatus.FAILED,
                    error=f"thread failed: {message}\n{traceback.format_exc()}",
                )
            finally:
                _pop()

        thread = threading.Thread(
            target=target, name=f"tfs-run-{handle.id[:8]}", daemon=True
        )
        handle.spawned = True
        handle.threads.append(thread)
        thread.start()
        return thread

    def done(self, handle: RunHandle) -> None:
        self._finish(handle, RunStatus.OK, result=handle.result)

    def query(self, **criteria: Any) -> list[TaggedFile]:
        return self.backend.query_files(**criteria)

    # -------------------------------------------------------------- helpers

    def index(self, path: Path) -> TaggedFile | None:
        """Insert/refresh the DB row of a data file (hash, mime, tags from its
        name). Used for files that add-ons produce, ahead of the watcher."""
        indexed = self._index(path)
        return indexed.file if indexed is not None else None

    def _index(self, path: Path):
        """Index a *data* file; ``.tfs/`` and ``script/`` are never data."""
        abs_path = self._abs(path)
        try:
            if self.root.zone(abs_path) is not Zone.DATA:
                return None
        except OutsideRoot:
            return None
        return self.indexer.index(abs_path)

    def _action_record(self, addon) -> ActionRecord:
        if addon.record is None:
            addon.record = self.store.register_action(
                name=addon.name,
                script_path=addon.key,
                script_hash=addon.script_hash,
                signature=addon.signature,
                hooks=addon.hooks,
            )
        return addon.record

    def _produced_by(self, path: Path, action_name: str) -> bool:
        for edge in self.store.query_provenance(file_path=path, include_deleted=True):
            run = self.store.get_run(edge.run_id)
            if run is not None and run.action_name == action_name:
                return True
        return False

    def _retry_depth(self, run: RunRecord) -> int:
        """How many retries lead up to ``run`` (its ``retry_of`` chain)."""
        depth = 0
        current: RunRecord | None = run
        while current is not None and current.retry_of and depth <= MAX_RETRIES:
            depth += 1
            current = self.store.get_run(current.retry_of)
        return depth

    def _file_for(self, run: RunRecord) -> TaggedFile | None:
        if not run.file_hash:
            return None  # a lifecycle run is about the daemon, not a file
        for candidate in self.backend.query_files(file_hash=run.file_hash):
            if run.file_id is None or candidate.file_id == run.file_id:
                return candidate
        return None

    def _path_of_run(self, run: RunRecord) -> Path | None:
        file = self._file_for(run)
        return file.original_path if file is not None else None

    def _subject(self, run: RunRecord, file_path: Path | None) -> str:
        """What the run was about, for a problem message: its file when there
        is one, its hash when the file is gone, else the hook itself."""
        if file_path is not None:
            return str(file_path)
        return run.file_hash or f"the {run.hook.value} hook"

    def _directories_tagged(self, tag: str) -> list[Path]:
        found: list[Path] = []
        for current, dirs, _files in os.walk(self.root.path):
            dirs[:] = [
                d
                for d in dirs
                if not (Path(current) == self.root.path and d in (TFS_DIR, SCRIPT_DIR))
            ]
            for name in dirs:
                if tag in {t.name for t in self.parser.parse(name).tags}:
                    found.append(Path(current) / name)
        return sorted(found)

    def _report_parse_problems(self, parsed: ParsedPath, path: Path) -> None:
        for problem in parsed.problems:
            marker = (parsed.path.as_posix(), problem.marker)
            if marker in self._reported_parse_problems:
                continue
            self._reported_parse_problems.add(marker)
            self.problem(Severity.WARN, "name.parse", str(problem), file_path=path)

    def _abs(self, path: Path | PurePosixPath | str) -> Path:
        p = Path(path)
        return p if p.is_absolute() else self.root.absolute(PurePosixPath(p.as_posix()))

    def _key_or_none(self, path: Path | PurePosixPath | str | None) -> str | None:
        if path is None:
            return None
        p = Path(path)
        if not p.is_absolute():
            return PurePosixPath(p.as_posix()).as_posix()
        try:
            return self.root.relative(p).as_posix()
        except OutsideRoot:
            return None

    def _key_text(self, path: Path | PurePosixPath | str) -> str:
        return self._key_or_none(path) or str(path)
