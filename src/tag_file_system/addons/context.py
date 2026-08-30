# Code by AkinoAlice@TyrantRey

"""``ActionContext``: what a handler gets as ``ctx`` (DESIGN.md §4.5).

The context is a thin facade; everything with side effects on the daemon
goes through the ``Runtime`` protocol, which the runner implements. That
keeps the add-on-facing surface small and lets tests run handlers against a
fake runtime.
"""

import logging
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Protocol

from tag_file_system.core.interface.action import (
    ProblemRecord,
    RunRecord,
    Severity,
    TraceKind,
)
from tag_file_system.core.interface.file_metadata import TaggedFile
from tag_file_system.root import OutsideRoot, Root


@dataclass
class RunHandle:
    """Mutable state of one in-flight run, owned by the runner."""

    run: RunRecord
    depth: int = 0  # chain depth (DESIGN.md §4.5: > 8 stops)
    started: float = field(default_factory=time.monotonic)
    threads: list[threading.Thread] = field(default_factory=list)
    spawned: bool = False  # once True the run ends on ctx.done()
    result: Any = None
    finished: bool = False
    warned: bool = False  # the "running too long" P2 was raised
    lock: threading.Lock = field(default_factory=threading.Lock)
    sink: Callable[[str], None] = lambda line: None  # captured output → trace

    @property
    def id(self) -> str:
        return self.run.id

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started


class Runtime(Protocol):
    """What ``ActionContext`` needs from the daemon."""

    root: Root

    def trace(self, handle: RunHandle, kind: str, payload: Any) -> None: ...

    def emit(self, handle: RunHandle, path: Path) -> None: ...

    def moved(self, handle: RunHandle, src: Path, dst: Path) -> None: ...

    def deleted(self, handle: RunHandle, path: Path) -> None: ...

    def tag(self, handle: RunHandle, path: Path, names: list[str], add: bool) -> None: ...

    def problem(
        self,
        severity: Severity,
        kind: str,
        message: str,
        *,
        action_name: str | None = None,
        file_path: Path | PurePosixPath | str | None = None,
        run_id: str | None = None,
    ) -> ProblemRecord: ...

    def retry(self, run_id: str) -> RunRecord | None: ...

    def resolve(self, kind: str, raw: str) -> Path: ...

    def spawn(self, handle: RunHandle, fn: Callable, args: tuple, kwargs: dict) -> threading.Thread: ...

    def done(self, handle: RunHandle) -> None: ...

    def query(self, **criteria: Any) -> list[TaggedFile]: ...


class ProblemContext:
    """``ctx`` for problem handlers: the daemon-facing helpers that make
    sense without a run of their own. Problems raised here are logged, never
    re-dispatched (DESIGN.md §6.4)."""

    def __init__(self, runtime: Runtime, problem: ProblemRecord) -> None:
        self._runtime = runtime
        self.problem_record = problem
        self.logger = logging.getLogger("tfs.problem")

    @property
    def root(self) -> Path:
        return self._runtime.root.path

    def log(self, message: str, level: int = logging.INFO) -> None:
        self.logger.log(level, message)

    def query(self, **criteria: Any) -> list[TaggedFile]:
        return self._runtime.query(**criteria)

    def retry(self, run_id: str) -> RunRecord | None:
        return self._runtime.retry(run_id)

    def resolve(self, kind: str, raw: str) -> Path:
        return self._runtime.resolve(kind, raw)

    def problem(self, severity: Severity | str, message: str, kind: str = "addon.problem") -> None:
        self.logger.log(
            logging.ERROR if Severity(severity).rank <= Severity.ERR.rank else logging.WARNING,
            f"[{severity}] {kind}: {message} (raised inside a problem handler; not dispatched)",
        )


class ActionContext:
    """Handed to every handler as ``ctx``.

    File operations through ``copy``/``move``/``write``/``delete`` are traced,
    keep the database in step, and — when the destination is inside the
    root — record *emitted* provenance of this run. Paths may be absolute or
    root-relative.
    """

    def __init__(
        self,
        runtime: Runtime,
        handle: RunHandle,
        file: TaggedFile | None,
        path: Path,
        args: dict[str, Any],
    ) -> None:
        self._runtime = runtime
        self._handle = handle
        self.file = file
        self.path = path
        self.args = args
        self.logger = logging.getLogger(f"tfs.addon.{handle.run.action_name}")

    # ------------------------------------------------------------ identity

    @property
    def root(self) -> Path:
        return self._runtime.root.path

    @property
    def run_id(self) -> str:
        return self._handle.id

    @property
    def action_name(self) -> str:
        return self._handle.run.action_name

    @property
    def run(self) -> RunRecord:
        return self._handle.run

    def _abs(self, path: Path | PurePosixPath | str) -> Path:
        p = Path(path)
        if p.is_absolute():
            return p
        return self._runtime.root.absolute(PurePosixPath(p.as_posix()))

    def _key(self, path: Path) -> str | None:
        try:
            return self._runtime.root.relative(path).as_posix()
        except OutsideRoot:
            return None

    # ------------------------------------------------------------ file ops

    def copy(self, src: Path | str, dst: Path | str) -> Path:
        source, target = self._abs(src), self._abs(dst)
        if target.is_dir():
            target = target / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        self._runtime.trace(
            self._handle,
            TraceKind.FS_COPY,
            {"src": self._key(source) or str(source), "dst": self._key(target) or str(target)},
        )
        self.emit(target)
        return target

    def move(self, src: Path | str, dst: Path | str) -> Path:
        source, target = self._abs(src), self._abs(dst)
        if target.is_dir():
            target = target / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        moved = Path(shutil.move(str(source), str(target)))
        self._runtime.trace(
            self._handle,
            TraceKind.FS_MOVE,
            {"src": self._key(source) or str(source), "dst": self._key(moved) or str(moved)},
        )
        self._runtime.moved(self._handle, source, moved)
        return moved

    def write(self, dst: Path | str, data: bytes | str, encoding: str = "utf-8") -> Path:
        target = self._abs(dst)
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            target.write_text(data, encoding=encoding)
        else:
            target.write_bytes(data)
        self._runtime.trace(
            self._handle,
            TraceKind.FS_WRITE,
            {"dst": self._key(target) or str(target), "bytes": target.stat().st_size},
        )
        self.emit(target)
        return target

    def delete(self, path: Path | str) -> None:
        target = self._abs(path)
        target.unlink()
        self._runtime.trace(
            self._handle, TraceKind.FS_DELETE, {"path": self._key(target) or str(target)}
        )
        self._runtime.deleted(self._handle, target)

    def emit(self, path: Path | str) -> None:
        """Declare that ``path`` exists because of this run."""
        self._runtime.emit(self._handle, self._abs(path))

    # --------------------------------------------------------------- trace

    def record(self, **facts: Any) -> None:
        self._runtime.trace(self._handle, TraceKind.RECORD, facts)

    def log(self, message: str, level: int = logging.INFO) -> None:
        self._runtime.trace(self._handle, TraceKind.LOG, str(message))
        self.logger.log(level, message)

    # ------------------------------------------------------------- threads

    def spawn(self, fn: Callable, *args: Any, **kwargs: Any) -> threading.Thread:
        """Run ``fn`` in a tracked thread; the run then ends on ``done()``."""
        return self._runtime.spawn(self._handle, fn, args, kwargs)

    def done(self, result: Any = None) -> None:
        """Finish the run (needed after ``spawn``). A value passed here is
        the run's result; a value *returned* by the handler after ``done()``
        is ignored."""
        if result is not None:
            self._handle.result = result
        self._runtime.done(self._handle)

    # ------------------------------------------------------------ problems

    def problem(
        self, severity: Severity | str, message: str, kind: str = "addon.problem"
    ) -> ProblemRecord:
        return self._runtime.problem(
            Severity(severity),
            kind,
            message,
            action_name=self.action_name,
            file_path=self.path,
            run_id=self.run_id,
        )

    # --------------------------------------------------------------- data

    def query(self, **criteria: Any) -> list[TaggedFile]:
        return self._runtime.query(**criteria)

    def tag(self, path: Path | str, *names: str) -> None:
        self._runtime.tag(self._handle, self._abs(path), list(names), add=True)

    def untag(self, path: Path | str, *names: str) -> None:
        """Remove tags added through ``tag``/the API. Tags spelled in the
        file's *name* come back on the next re-index: the name is authoritative."""
        self._runtime.tag(self._handle, self._abs(path), list(names), add=False)

    def retry(self, run_id: str) -> RunRecord | None:
        return self._runtime.retry(run_id)

    def resolve(self, kind: str, raw: str) -> Path:
        """Resolve a ``tagdir``/``remote`` name the way arguments are."""
        return self._runtime.resolve(kind, raw)
