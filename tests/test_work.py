# Code by AkinoAlice@TyrantRey

"""The work queue and the operator's controls: pause, workers, the rate
limit, timeouts, cancel, the races (DESIGN/v0-5-0.md §11.3)."""

import os
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
from watchfiles import Change

from tag_file_system.addons.loader import MODULE_PREFIX
from tag_file_system.addons.runner import RateLimiter
from tag_file_system.config import Config, DaemonConfig
from tag_file_system.core.interface.action import (
    ProvenanceKind,
    RunStatus,
    Severity,
    TraceKind,
)
from tag_file_system.root import FUNCTIONS_FILE, Root
from tag_file_system.services.api import Conflict, NotFound
from tag_file_system.services.daemon import Daemon
from tag_file_system.services.work import WorkItem, WorkQueue


@pytest.fixture(autouse=True)
def clean_modules():
    yield
    for name in [m for m in sys.modules if m.startswith(MODULE_PREFIX)]:
        del sys.modules[name]


def wait_for(condition, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def write(root: Root, key: str, content: str = "data") -> Path:
    path = root.absolute(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def added(*paths: Path) -> set:
    return {(Change.added, str(p)) for p in paths}


def enable(root: Root, folder: str, script: str, *handlers: str) -> Path:
    directory = root.path / folder
    directory.mkdir(parents=True, exist_ok=True)
    lines = "\n".join(f"    {h}: {{}}" for h in handlers)
    path = directory / FUNCTIONS_FILE
    path.write_text(f"version: 1\nfunctions:\n  {script}:\n{lines}\n", encoding="utf-8")
    return path


def make_root(tmp_path: Path, script: str, source: str, **daemon_config) -> Root:
    root = Root.init(tmp_path / "vault")
    Config(
        daemon=DaemonConfig(
            run_warn_after_seconds=0.05, stop_timeout_seconds=0.5, **daemon_config
        )
    ).write(root.config_path)
    (root.script_dir / f"{script}.py").write_text(
        textwrap.dedent(source), encoding="utf-8"
    )
    enable(root, script, script, "run")
    return root


def module(daemon: Daemon, name: str):
    addon = daemon.loader.addon_for(name)
    assert addon is not None
    return addon.module


def problems(daemon: Daemon, kind: str) -> list:
    return [p for p in daemon.store.query_problems() if p.kind == kind]


SLOW = """
    import threading
    from tag_file_system import action
    started = threading.Event()
    release = threading.Event()
    calls = []
    outcomes = []

    @action.added()
    def run(path, metadata, ctx):
        calls.append(path.name)
        started.set()
        release.wait(10)
        try:
            ctx.check()
        except action.Cancelled as e:
            outcomes.append(("cancelled", ctx.cancelled))
            raise
        outcomes.append(("finished", ctx.cancelled))
        return path.name
"""

COUNTING = """
    import threading, time
    from tag_file_system import action
    lock = threading.Lock()
    active = 0
    peak = 0

    @action.added()
    def run(path, metadata, ctx):
        global active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.3)
        with lock:
            active -= 1
        return path.name
"""

SELF_EDIT = """
    from tag_file_system import action
    calls = []

    @action.added()
    def run(path, metadata, ctx):
        calls.append(("added", path.name))
        path.write_text(path.read_text() + "!")

    @action.modified()
    def again(path, metadata, ctx):
        calls.append(("modified", path.name))
"""

PLAIN = """
    from tag_file_system import action
    calls = []

    @action.added()
    def run(path, metadata, ctx):
        calls.append(path.read_text())
        return path.name
"""


# ------------------------------------------------------------------- queue


def test_inline_queue_runs_items_in_order_and_honours_pause():
    queue = WorkQueue(0)
    done: list[int] = []
    for n in range(3):
        queue.put(WorkItem(f"item {n}", "test", lambda n=n: done.append(n)))
    assert queue.depth == 3 and queue.inline
    assert queue.run_pending() == 3
    assert done == [0, 1, 2] and queue.depth == 0

    queue.pause()
    queue.put(WorkItem("later", "test", lambda: done.append(9)))
    assert queue.run_pending() == 0 and done == [0, 1, 2]
    assert queue.describe()["paused"] and queue.describe()["since"] is not None
    queue.resume()
    assert queue.run_pending() == 1 and done[-1] == 9


def test_inline_queue_is_reentrant():
    queue = WorkQueue(0)
    done: list[str] = []

    def outer() -> None:
        done.append("outer")
        queue.put(WorkItem("inner", "test", lambda: done.append("inner")))
        queue.run_pending()  # a reconcile of a directory drains what it produced
        done.append("outer done")

    queue.put(WorkItem("outer", "test", outer))
    queue.put(WorkItem("after", "test", lambda: done.append("after")))
    queue.run_pending()
    # FIFO throughout: the nested drain takes what was queued before "inner"
    assert done == ["outer", "after", "inner", "outer done"]


def test_workers_execute_and_an_abandoned_worker_is_replaced():
    queue = WorkQueue(1)
    queue.start()
    gate = threading.Event()
    seen: list[str] = []

    def stuck() -> None:
        seen.append("stuck")
        gate.wait(10)

    queue.put(WorkItem("stuck", "test", stuck))
    assert wait_for(lambda: queue.active and queue.active[0].label == "stuck")
    worker = next(t for t in threading.enumerate() if t.name == "tfs-work-1")
    assert queue.abandon(worker) and queue.worker_count == 1
    assert not queue.abandon(threading.current_thread())

    queue.put(WorkItem("next", "test", lambda: seen.append("next")))
    assert wait_for(lambda: "next" in seen)  # the replacement took it
    gate.set()
    assert wait_for(lambda: not worker.is_alive())
    assert queue.describe()["active"] == 0
    assert queue.stop(1.0) == 0


def test_stop_drops_queued_items_and_errors_are_reported():
    errors: list[tuple[str, str]] = []
    queue = WorkQueue(1, on_error=lambda item, e: errors.append((item.label, str(e))))
    queue.start()

    def boom() -> None:
        raise RuntimeError("no")

    queue.put(WorkItem("boom", "test", boom))
    assert wait_for(lambda: errors == [("boom", "no")])
    queue.pause()
    queue.put(WorkItem("a", "test", lambda: None))
    queue.put(WorkItem("b", "test", lambda: None))
    assert queue.stop(1.0) == 2
    assert queue.depth == 0 and queue.worker_count == 0


def test_rate_limiter_is_a_token_bucket():
    now = [0.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(2, clock=lambda: now[0], sleep=sleep)
    assert (
        limiter.acquire() == 0.0 and limiter.acquire() == 0.0
    )  # the bucket starts full
    waited = limiter.acquire()  # 2 per minute: one token every 30 s
    assert 29.0 <= waited <= 31.0 and sum(slept) == waited
    now[0] += 120
    assert limiter.acquire() == 0.0  # refilled, capped at the capacity
    assert limiter.acquire() == 0.0
    assert limiter.acquire() > 0

    unlimited = RateLimiter(0, clock=lambda: now[0], sleep=sleep)
    assert unlimited.acquire() == 0.0
    limiter.configure(0)
    assert limiter.acquire() == 0.0


# ------------------------------------------------------------------ daemon


def test_pause_defers_runs_and_resume_drains_them(tmp_path: Path):
    root = make_root(tmp_path, "plain", PLAIN, max_concurrent_runs=0)
    daemon = Daemon(root)
    daemon.startup()
    try:
        assert daemon.pause()["paused"] is True
        path = write(root, "plain/a.txt", "v1")
        daemon.process_changes(added(path))
        assert daemon.backend.query_file(path) is not None  # indexing goes on
        assert module(daemon, "plain").calls == []
        assert daemon.status()["paused"] and daemon.status()["queue"]["depth"] == 1
        assert daemon.queue_view()["items"][0]["label"] == "added plain/a.txt"
        daemon.tick()  # paused: nothing
        assert module(daemon, "plain").calls == []
        state = daemon.resume()
        assert state == {"paused": False, "queued": 1, "in_flight": 0}
        daemon.tick()
        assert module(daemon, "plain").calls == ["v1"]
        assert daemon.status()["queue"]["depth"] == 0
    finally:
        daemon.shutdown()


def test_a_stale_item_is_revalidated_when_it_runs(tmp_path: Path):
    """Race 1: the file changed after it was queued; the run is keyed by the
    content the handler sees, and the watcher's own event is a no-op."""
    root = make_root(tmp_path, "plain", PLAIN, max_concurrent_runs=0)
    daemon = Daemon(root)
    daemon.startup()
    try:
        daemon.pause()
        path = write(root, "plain/a.txt", "v1")
        daemon.process_changes(added(path))
        first = daemon.backend.query_file(path)
        assert first is not None
        path.write_text("v2 is longer")  # a new size: unmistakably changed
        daemon.resume()
        daemon.tick()
        assert module(daemon, "plain").calls == ["v2 is longer"]
        (run,) = daemon.store.query_runs(action_name="plain")
        row = daemon.backend.query_file(path)
        assert row is not None and run.file_hash == row.file_hash != first.file_hash
        daemon.process_changes({(Change.modified, str(path))})
        assert len(daemon.store.query_runs(action_name="plain")) == 1
    finally:
        daemon.shutdown()


def test_workers_run_in_the_background(tmp_path: Path):
    root = make_root(tmp_path, "plain", PLAIN, max_concurrent_runs=1)
    write(root, "plain/pre.txt", "pre")
    daemon = Daemon(root)
    daemon.startup()
    try:
        assert daemon.status()["queue"]["workers"] == 1 and not daemon.queue.inline
        assert daemon.queue.drain(5)
        assert module(daemon, "plain").calls == ["pre"]
        path = write(root, "plain/a.txt", "v1")
        daemon.process_changes(added(path))
        assert wait_for(lambda: "v1" in module(daemon, "plain").calls)
        assert [r.status for r in daemon.store.query_runs(action_name="plain")] == [
            RunStatus.OK,
            RunStatus.OK,
        ]
    finally:
        daemon.shutdown()


@pytest.mark.parametrize("workers, peak", [(1, 1), (2, 2)])
def test_max_concurrent_runs_bounds_the_parallelism(
    tmp_path: Path, workers: int, peak: int
):
    root = make_root(tmp_path, "counting", COUNTING, max_concurrent_runs=workers)
    daemon = Daemon(root)
    daemon.startup()
    try:
        daemon.process_changes(
            added(
                write(root, "counting/a.txt", "a"), write(root, "counting/b.txt", "b")
            )
        )
        assert daemon.queue.drain(10)
        assert module(daemon, "counting").peak == peak
        assert daemon.store.count_runs(status=RunStatus.OK) == 2
    finally:
        daemon.shutdown()


def test_timeout_fails_the_run_and_abandons_its_worker(tmp_path: Path):
    root = make_root(
        tmp_path, "slow", SLOW, max_concurrent_runs=1, run_timeout_seconds=0.2
    )
    daemon = Daemon(root)
    daemon.startup()
    try:
        slow = module(daemon, "slow")
        daemon.process_changes(added(write(root, "slow/a.txt", "a")))
        assert slow.started.wait(5)
        time.sleep(0.3)
        daemon.tick()
        (run,) = daemon.store.query_runs(action_name="slow")
        assert run.status is RunStatus.FAILED and "timed out" in (run.error or "")
        assert problems(daemon, "run.timeout")[0].severity is Severity.ERR
        assert daemon.status()["in_flight"] == []
        assert daemon.queue.worker_count == 1  # a replacement took over

        # the replacement works while the old worker is still stuck
        slow.started.clear()
        daemon.process_changes(added(write(root, "slow/b.txt", "b")))
        assert slow.started.wait(5)
        assert slow.calls == ["a.txt", "b.txt"]
        slow.release.set()
        assert wait_for(lambda: len(slow.outcomes) == 2)
        # the first handler saw the cancel; the second finished normally
        assert sorted(slow.outcomes) == [("cancelled", True), ("finished", False)]
        assert wait_for(
            lambda: (
                daemon.store.count_runs(status=RunStatus.OK) == 1
                and daemon.store.count_runs(status=RunStatus.FAILED) == 1
            )
        )
    finally:
        module(daemon, "slow").release.set()
        daemon.shutdown()


def test_cancel_ends_a_run_in_flight(tmp_path: Path):
    root = make_root(tmp_path, "slow", SLOW, max_concurrent_runs=1)
    daemon = Daemon(root)
    daemon.startup()
    try:
        slow = module(daemon, "slow")
        daemon.process_changes(added(write(root, "slow/a.txt", "a")))
        assert slow.started.wait(5)
        (run,) = daemon.store.query_runs(action_name="slow")
        result = daemon.cancel_run(run.id)
        assert result["cancelled"] == run.id and result["worker_abandoned"] is True
        current = daemon.store.get_run(run.id)
        assert current is not None and current.status is RunStatus.INTERRUPTED
        assert "cancelled by the operator" in (current.error or "")
        assert problems(daemon, "run.cancelled")[0].severity is Severity.WARN
        assert not problems(daemon, "run.interrupted")
        with pytest.raises(Conflict):
            daemon.cancel_run(run.id)  # final already
        with pytest.raises(NotFound):
            daemon.cancel_run("nope")
        slow.release.set()
        assert wait_for(lambda: slow.outcomes == [("cancelled", True)])
        final = daemon.store.get_run(run.id)
        assert final is not None and final.status is RunStatus.INTERRUPTED
        # a cancelled run can be retried
        assert daemon.runner.retry_check(run.id) is None
    finally:
        module(daemon, "slow").release.set()
        daemon.shutdown()


def test_a_handler_that_edits_its_input_does_not_loop(tmp_path: Path):
    """Race 2: the run is recorded as the producer of the content it made,
    so the watcher's event for that edit does not re-trigger it."""
    root = make_root(tmp_path, "selfedit", SELF_EDIT, max_concurrent_runs=0)
    enable(root, "selfedit", "selfedit", "run", "again")
    daemon = Daemon(root)
    daemon.startup()
    try:
        path = write(root, "selfedit/a.txt", "v1")
        daemon.process_changes(added(path))
        assert module(daemon, "selfedit").calls == [("added", "a.txt")]
        (run,) = daemon.store.query_runs(action_name="selfedit")
        edges = daemon.store.query_provenance(file_path=path)
        assert [(e.run_id, e.kind) for e in edges] == [(run.id, ProvenanceKind.EMITTED)]
        assert [t.kind for t in daemon.store.query_trace(run.id)] == [
            TraceKind.SELF_MODIFIED.value
        ]
        assert path.read_text() == "v1!"

        daemon.process_changes({(Change.modified, str(path))})
        assert module(daemon, "selfedit").calls == [("added", "a.txt")]
        assert len(daemon.store.query_runs(action_name="selfedit")) == 1
        row = daemon.backend.query_file(path)
        assert row is not None and row.file_hash != run.file_hash  # indexed, not run
    finally:
        daemon.shutdown()


def test_lifecycle_runs_are_neither_timed_out_nor_rate_limited(tmp_path: Path):
    root = make_root(
        tmp_path,
        "service",
        """
        from tag_file_system import action

        @action.on_start()
        def up(ctx):
            ctx.spawn(lambda: None)
        """,
        max_concurrent_runs=0,
        run_timeout_seconds=0.01,
        max_runs_per_minute=1,
    )
    daemon = Daemon(root)
    daemon.startup()
    try:
        time.sleep(0.05)
        daemon.tick()
        (run,) = daemon.store.query_runs(action_name="service")
        assert run.status is RunStatus.RUNNING  # open until on_stop or shutdown
        assert not problems(daemon, "run.timeout")
        assert daemon.runner.limiter.capacity == 1
    finally:
        daemon.shutdown()


def test_status_and_queue_views(tmp_path: Path):
    root = make_root(tmp_path, "plain", PLAIN, max_concurrent_runs=0)
    daemon = Daemon(root)
    daemon.startup()
    try:
        status = daemon.status()
        assert status["paused"] is False
        assert status["queue"] == {"depth": 0, "active": 0, "workers": 0, "since": None}
        assert status["uptime_seconds"] >= 0
        assert status["limits"] == {
            "max_concurrent_runs": 0,
            "max_runs_per_minute": 0,
            "run_timeout_seconds": 0,
            "confirm_above": 500,
        }
        detail = daemon.status_detail()
        assert detail["files"] == 0 and detail["runs"]["failed"] == 0
        assert detail["database"]["schema"] == 3 and detail["drift"] == []
        checks = {c["check"]: c["status"] for c in daemon.doctor()["checks"]}
        assert checks == {
            "database": "ok",
            "scripts": "ok",
            "functions": "ok",
            "drift": "ok",
            "runs": "ok",
            "problems": "ok",
            "queue": "ok",
            "disk": "ok",
        }
        os.utime(root.path / "plain" / FUNCTIONS_FILE, None)
        (root.path / "plain" / FUNCTIONS_FILE).write_text(
            "version: 1\nfunctions:\n  plain:\n    run: {}\n# edited\n",
            encoding="utf-8",
        )
        daemon.reconcile()
        assert daemon.status_detail()["drift"] == ["plain/.tfsfunctions.yaml"]
        checks = {c["check"]: c["status"] for c in daemon.doctor()["checks"]}
        assert checks["drift"] == "warn"
    finally:
        daemon.shutdown()
