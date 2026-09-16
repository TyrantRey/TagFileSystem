# Code by AkinoAlice@TyrantRey

"""The daemon's work queue (DESIGN/v0-5-0.md §11.3).

What the watcher, a reconcile, ``tfs retry``, ``tfs rerun`` and the file
commands produce is an *item* — a callable that runs the transition, the
removal, the retry — put here and executed by ``workers`` threads. With no
workers the producer drains the queue itself (``run_pending``), which is
how 0.4.x ran everything on the watch loop and how the test suite runs.

``pause`` stops items being taken (nothing running is touched); ``abandon``
gives up on a worker stuck in a cancelled or timed-out handler and starts a
replacement; ``stop`` drops what is left — the next start's reconcile
offers every file again, and the run key makes finished work a no-op.
"""

import itertools
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

_ids = itertools.count(1)


@dataclass
class WorkItem:
    label: str  # "added copy/a.txt", "retry 3f2a…": what `tfs status` lists
    kind: str  # transition | removed | retry | rerun | reconcile
    run: Callable[[], Any]
    source: str = ""
    id: int = field(default_factory=lambda: next(_ids))
    enqueued_at: float = field(default_factory=time.time)

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "source": self.source,
            "enqueued_at": self.enqueued_at,
        }


class _Worker(threading.Thread):
    def __init__(self, queue: "WorkQueue", number: int) -> None:
        super().__init__(name=f"tfs-work-{number}", daemon=True)
        self.queue = queue
        self.abandoned = False

    def run(self) -> None:
        self.queue._loop(self)


ErrorHandler = Callable[[WorkItem, BaseException], None]


class WorkQueue:
    def __init__(self, workers: int = 0, on_error: ErrorHandler | None = None) -> None:
        self.workers_wanted = max(0, int(workers))
        self.on_error = on_error
        self._items: deque[WorkItem] = deque()
        self._cv = threading.Condition()
        self._workers: list[_Worker] = []
        self._numbers = itertools.count(1)
        self._active: dict[int, WorkItem] = {}  # thread ident -> item
        self._stopping = False
        self.paused = False
        self.paused_since: float | None = None
        self.started = False

    # ------------------------------------------------------------- workers

    @property
    def inline(self) -> bool:
        """No worker threads: the producer runs its own items."""
        return self.workers_wanted == 0

    def start(self) -> None:
        with self._cv:
            if self.started:
                return
            self.started = True
            self._stopping = False
            for _ in range(self.workers_wanted):
                self._spawn_locked()

    def _spawn_locked(self) -> _Worker:
        worker = _Worker(self, next(self._numbers))
        self._workers.append(worker)
        worker.start()
        return worker

    def _loop(self, worker: _Worker) -> None:
        while True:
            with self._cv:
                while (
                    not self._stopping
                    and not worker.abandoned
                    and (self.paused or not self._items)
                ):
                    self._cv.wait(0.5)
                if self._stopping or worker.abandoned:
                    return
                item = self._items.popleft()
                self._active[worker.ident or 0] = item
            try:
                self._execute(item)
            finally:
                with self._cv:
                    self._active.pop(worker.ident or 0, None)
                    self._cv.notify_all()
            if worker.abandoned:
                return  # the handler came back after all: its result is discarded

    def _execute(self, item: WorkItem) -> None:
        try:
            item.run()
        except BaseException as e:  # a failing item must never take a worker down
            if self.on_error is None:
                raise
            self.on_error(item, e)

    def abandon(self, thread: threading.Thread | None) -> bool:
        """Give up on the worker ``thread`` (stuck in a handler that was
        cancelled or timed out) and start a replacement. ``False`` when the
        thread is not one of this queue's workers."""
        with self._cv:
            for worker in self._workers:
                if worker is thread and not worker.abandoned:
                    worker.abandoned = True
                    self._workers.remove(worker)
                    if not self._stopping and self.started:
                        self._spawn_locked()
                    return True
        return False

    @property
    def worker_count(self) -> int:
        with self._cv:
            return len(self._workers)

    # --------------------------------------------------------------- items

    def put(self, item: WorkItem) -> WorkItem:
        with self._cv:
            self._items.append(item)
            self._cv.notify()
        return item

    @property
    def depth(self) -> int:
        with self._cv:
            return len(self._items)

    @property
    def active(self) -> list[WorkItem]:
        with self._cv:
            return list(self._active.values())

    def run_pending(self) -> int:
        """Inline mode: execute the queued items on this thread until none is
        left or the queue is paused. Re-entrant (an item may reconcile a
        directory and drain what that produced)."""
        done = 0
        while True:
            with self._cv:
                if self.paused or self._stopping or not self._items:
                    return done
                item = self._items.popleft()
                self._active[threading.get_ident()] = item
            try:
                self._execute(item)
            finally:
                with self._cv:
                    self._active.pop(threading.get_ident(), None)
                    self._cv.notify_all()
            done += 1

    def drain(self, timeout: float = 10.0) -> bool:
        """Wait until nothing is queued (or the queue is paused) and nothing
        is being executed. For tests and shutdown."""
        with self._cv:
            return self._cv.wait_for(
                lambda: (self.paused or not self._items) and not self._active,
                timeout,
            )

    # ------------------------------------------------------------- control

    def pause(self) -> None:
        with self._cv:
            if not self.paused:
                self.paused = True
                self.paused_since = time.time()

    def resume(self) -> None:
        with self._cv:
            self.paused = False
            self.paused_since = None
            self._cv.notify_all()

    def stop(self, timeout: float = 0.0) -> int:
        """Stop taking items, wait up to ``timeout`` for the ones being
        executed, drop the rest. Returns how many were dropped."""
        with self._cv:
            self._stopping = True
            self._cv.notify_all()
            workers = list(self._workers)
        deadline = time.monotonic() + max(0.0, timeout)
        for worker in workers:
            worker.join(max(0.0, deadline - time.monotonic()))
        with self._cv:
            dropped = len(self._items)
            self._items.clear()
            self._workers = [w for w in self._workers if w.is_alive()]
            self.started = False
        return dropped

    def describe(self, limit: int = 20) -> dict[str, Any]:
        """What ``/health`` and ``/api/v1/queue`` report."""
        with self._cv:
            items = list(self._items)
            active = list(self._active.values())
            return {
                "paused": self.paused,
                "since": self.paused_since,
                "depth": len(items),
                "active": len(active),
                "workers": len(self._workers),
                "items": [i.describe() for i in items[:limit]],
                "running": [i.describe() for i in active],
            }
