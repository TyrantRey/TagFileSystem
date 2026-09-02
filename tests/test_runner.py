# Code by AkinoAlice@TyrantRey

import sys
import textwrap
import time
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from tag_file_system.addons.loader import MODULE_PREFIX, AddonLoader
from tag_file_system.addons.runner import ActionRunner
from tag_file_system.config import Config, DaemonConfig
from tag_file_system.core.interface.action import (
    Hook,
    RunSource,
    RunStatus,
    Severity,
)
from tag_file_system.database.action_store import ActionStore
from tag_file_system.database.sqlite import SQLiteBackend
from tag_file_system.root import Root


@pytest.fixture(autouse=True)
def clean_modules():
    yield
    for name in [m for m in sys.modules if m.startswith(MODULE_PREFIX)]:
        del sys.modules[name]


@pytest.fixture
def env(tmp_path: Path):
    root = Root.init(tmp_path / "vault")
    backend = SQLiteBackend()
    backend.init_database(root.db_path, root_dir=root.path)
    store = ActionStore(backend)
    loader = AddonLoader(root, store=store)
    config = Config(
        daemon=DaemonConfig(run_warn_after_seconds=0.05, stop_timeout_seconds=0.2),
        remotes={"backup": str(tmp_path / "backup")},
    )
    runner = ActionRunner(
        root, backend, store, loader, config=config, max_chain_depth=3
    )
    loader.report = runner.problem
    (tmp_path / "backup").mkdir()
    yield SimpleNamespace(
        root=root,
        backend=backend,
        store=store,
        loader=loader,
        runner=runner,
        tmp=tmp_path,
    )
    backend.close()


def script(env, name: str, source: str) -> None:
    (env.root.script_dir / f"{name}.py").write_text(
        textwrap.dedent(source), encoding="utf-8"
    )
    env.loader.load_all()


def add_file(env, key: str, content: bytes = b"data"):
    path = env.root.absolute(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    file = env.runner.index(path)
    assert file is not None
    return path, file, env.runner.parser.parse_path(PurePosixPath(key))


def problems(env, kind: str | None = None):
    return [p for p in env.store.query_problems() if kind is None or p.kind == kind]


MAKE_COPY = """
    import logging
    from tag_file_system import action

    @action.added()
    def run(path, metadata, ctx, suffix: str = ".txt", dst: action.Remote = None):
        ctx.log("starting")
        print("hello from stdout")
        logging.getLogger("make_copy").warning("a warning")
        out = ctx.copy(path, ctx.root / "out" / (path.stem + "--copy" + suffix))
        ctx.record(copied=str(out.name))
        return {"copied": out.name, "size": metadata.file_size}
"""


# ------------------------------------------------------------------ basics


def test_added_handler_runs_once_with_trace_and_provenance(env):
    script(env, "make_copy", MAKE_COPY)
    path, file, parsed = add_file(env, "@@make_copy__.md/a.txt")

    runs = env.runner.on_file(Hook.ADDED, path, file, parsed)

    assert len(runs) == 1
    run = env.store.get_run(runs[0].id)
    assert run is not None
    assert run.status is RunStatus.OK
    assert run.result == {"copied": "a--copy.md", "size": 4}
    assert run.args == {"suffix": ".md"}
    assert run.slug == "make_copy__.md"
    assert run.source is RunSource.WATCH
    assert run.file_id == file.file_id

    kinds = [t.kind for t in env.store.query_trace(run.id)]
    assert kinds == ["log", "log", "log", "fs.copy", "emit", "record"]
    payloads = [t.payload for t in env.store.query_trace(run.id)]
    assert payloads[0] == "starting"
    assert payloads[1] == "stdout: hello from stdout"
    assert payloads[2] == "warning: a warning"

    produced = env.store.produced_by(run.id)
    assert produced == ["out/a--copy.md"]
    copy = env.backend.query_file("out/a--copy.md")
    assert copy is not None and [t.name for t in copy.tags] == ["copy"]
    assert [p.kind for p in env.store.query_provenance(file_path="out/a--copy.md")] == [
        "emitted"
    ]
    assert [p.kind for p in problems(env)] == ["run.ok"]

    # the same event again: the key exists, nothing re-runs
    assert env.runner.on_file(Hook.ADDED, path, file, parsed) == []
    assert len(env.store.query_runs()) == 1
    assert env.runner.in_flight == {}


def test_generated_file_never_retriggers_its_producer(env):
    script(env, "make_copy", MAKE_COPY.replace('ctx.root / "out" /', "path.parent /"))
    path, file, parsed = add_file(env, "@@make_copy/a.txt")
    (run,) = env.runner.on_file(Hook.ADDED, path, file, parsed)
    assert env.store.produced_by(run.id) == ["@@make_copy/a--copy.txt"]

    copy_path = env.root.absolute("@@make_copy/a--copy.txt")
    copy = env.backend.query_file(copy_path)
    assert copy is not None
    again = env.runner.on_file(
        Hook.ADDED,
        copy_path,
        copy,
        env.runner.parser.parse_path(PurePosixPath("@@make_copy/a--copy.txt")),
    )

    assert again == []
    assert len(env.store.query_runs()) == 1


def test_unbound_and_binding_problems(env):
    script(
        env,
        "resize",
        "from tag_file_system import action\n@action.added()\ndef run(p, m, c, width: int): pass\n",
    )
    path, file, parsed = add_file(env, "@@nosuch/@@resize__wide/a.txt")

    runs = env.runner.on_file(Hook.ADDED, path, file, parsed)

    assert [p.kind for p in problems(env)] == ["action.unbound", "action.binding"]
    assert len(runs) == 1 and runs[0].status is RunStatus.FAILED
    assert "binding error" in (runs[0].error or "")
    unbound = problems(env, "action.unbound")[0]
    assert unbound.action_name == "nosuch" and unbound.severity is Severity.ERR
    assert unbound.file_id == file.file_id
    # the failed binding is a dead key: no re-run on the next event
    assert env.runner.on_file(Hook.ADDED, path, file, parsed) == []


def test_handler_exception_is_a_failed_run_and_delivered_to_err_handlers(env):
    script(
        env,
        "boom",
        """
        from tag_file_system import action
        seen = []

        @action.added()
        def run(path, metadata, ctx):
            raise RuntimeError("kaboom")

        @action.err()
        def notify(problem, ctx):
            seen.append((problem.kind, problem.severity, ctx.root))
            if problem.kind == "run.failed":
                ctx.problem("err", "nested")  # logged only, never re-dispatched
        """,
    )
    path, file, parsed = add_file(env, "@@boom/a.txt")

    (run,) = env.runner.on_file(Hook.ADDED, path, file, parsed)

    assert run.status is RunStatus.FAILED
    assert run.error is not None and run.error.startswith("RuntimeError: kaboom")
    assert "Traceback" in run.error
    failed = problems(env, "run.failed")[0]
    assert failed.run_id == run.id and failed.delivered_at is not None
    addon = env.loader.addon_for("boom")
    assert addon is not None
    assert addon.module.seen == [("run.failed", Severity.ERR, env.root.path)]
    assert problems(env, "addon.problem") == []  # the nested one was not recorded


def test_problem_handler_failure_is_logged_not_redispatched(env):
    script(
        env,
        "watchdog",
        """
        from tag_file_system import action
        calls = []

        @action.warn()
        def bad(problem, ctx):
            calls.append(problem.kind)
            raise ValueError("handler broke")
        """,
    )

    record = env.runner.problem(Severity.WARN, "test.kind", "hello")

    assert env.loader.addon_for("watchdog").module.calls == ["test.kind"]
    assert env.store.get_problem(record.id).delivered_at is None  # nobody handled it
    assert (
        env.runner.problem(Severity.INFO, "quiet", "x").delivered_at is None
    )  # warn does not cover info


def test_replay_undelivered_problems_at_start(env):
    early = env.runner.problem(Severity.CRIT, "daemon.died", "before any add-on")
    assert early.delivered_at is None
    script(
        env,
        "notify",
        """
        from tag_file_system import action
        got = []

        @action.crit()
        def on_crit(problem, ctx):
            got.append(problem.kind)
        """,
    )

    assert env.runner.replay_undelivered() == 1

    assert env.loader.addon_for("notify").module.got == ["daemon.died"]
    assert env.store.get_problem(early.id).delivered_at is not None
    assert env.runner.replay_undelivered() == 0


# ------------------------------------------------------------------ chains


def test_ctx_tag_chains_into_tagged_handlers_with_depth_limit(env):
    script(
        env,
        "classify",
        """
        from tag_file_system import action

        @action.added()
        def run(path, metadata, ctx):
            ctx.tag(path, "photo")

        @action.tagged("photo")
        def on_photo(path, metadata, ctx):
            ctx.tag(path, "ping")

        @action.tagged("ping")
        def on_ping(path, metadata, ctx):
            ctx.untag(path, "pong")
            ctx.tag(path, "pong")

        @action.tagged("pong")
        def on_pong(path, metadata, ctx):
            ctx.untag(path, "ping")
            ctx.tag(path, "ping")
        """,
    )
    path, file, parsed = add_file(env, "@@classify/a.txt")

    env.runner.on_file(Hook.ADDED, path, file, parsed)

    runs = env.store.query_runs()
    by_hook = [(r.hook, r.args.get("tag"), r.source) for r in reversed(runs)]
    assert by_hook[0] == (Hook.ADDED, None, RunSource.WATCH)
    assert by_hook[1] == (Hook.TAGGED, "photo", RunSource.CHAIN)
    assert runs[-2].parent_run_id == runs[-1].id
    depth_problem = problems(env, "chain.depth")
    assert len(depth_problem) == 1
    assert all(r.status is RunStatus.OK for r in runs)
    final = env.backend.query_file(path)
    assert final is not None and "photo" in [t.name for t in final.tags]


def test_removed_on_move_only_fires_on_move_handlers(env):
    script(
        env,
        "cleanup",
        """
        from tag_file_system import action
        calls = []

        @action.removed()
        def on_delete(path, metadata, ctx):
            calls.append("delete")
        """,
    )
    script(
        env,
        "cleanup_move",
        """
        from tag_file_system import action
        calls = []

        @action.removed(on_move=True)
        def on_gone(path, metadata, ctx):
            calls.append("gone")
        """,
    )
    path, file, parsed = add_file(env, "@@cleanup/@@cleanup_move/a.txt")

    env.runner.on_file(Hook.REMOVED, path, file, parsed, moved=True)
    assert env.loader.addon_for("cleanup").module.calls == []
    assert env.loader.addon_for("cleanup_move").module.calls == ["gone"]

    # different content: identical content would share a's run key (§6.1)
    path_b, file_b, parsed_b = add_file(env, "@@cleanup/@@cleanup_move/b.txt", b"other")
    env.runner.on_file(Hook.REMOVED, path_b, file_b, parsed_b, moved=False)
    assert env.loader.addon_for("cleanup").module.calls == ["delete"]
    assert env.loader.addon_for("cleanup_move").module.calls == ["gone", "gone"]


# ------------------------------------------------------------------ threads


def test_spawn_keeps_the_run_open_until_done(env):
    script(
        env,
        "bg",
        """
        import threading
        from tag_file_system import action
        gate = threading.Event()

        @action.added()
        def run(path, metadata, ctx):
            def work():
                gate.wait(5)
                ctx.record(worker="finished")
                ctx.done({"bg": True})
            ctx.spawn(work)
            return "ignored-until-done"
        """,
    )
    path, file, parsed = add_file(env, "@@bg/a.txt")

    (run,) = env.runner.on_file(Hook.ADDED, path, file, parsed)

    assert env.store.get_run(run.id).status is RunStatus.RUNNING
    assert run.id in env.runner.in_flight
    time.sleep(0.1)
    assert env.runner.check_overdue() and problems(env, "run.overdue")
    assert env.runner.check_overdue() == []  # warned once

    env.loader.addon_for("bg").module.gate.set()
    deadline = time.time() + 5
    while (
        env.store.get_run(run.id).status is RunStatus.RUNNING and time.time() < deadline
    ):
        time.sleep(0.01)

    finished = env.store.get_run(run.id)
    assert finished.status is RunStatus.OK
    assert finished.result == {"bg": True}
    assert run.id not in env.runner.in_flight
    assert [t.payload for t in env.store.query_trace(run.id) if t.kind == "record"] == [
        {"worker": "finished"}
    ]


def test_stop_interrupts_runs_that_never_finish(env):
    script(
        env,
        "stuck",
        """
        import threading
        from tag_file_system import action

        @action.added()
        def run(path, metadata, ctx):
            ctx.spawn(lambda: threading.Event().wait(30))
        """,
    )
    path, file, parsed = add_file(env, "@@stuck/a.txt")
    (run,) = env.runner.on_file(Hook.ADDED, path, file, parsed)

    interrupted = env.runner.stop(timeout=0.1)

    assert [r.id for r in interrupted] == [run.id]
    assert env.store.get_run(run.id).status is RunStatus.INTERRUPTED
    assert env.runner.in_flight == {}
    assert problems(env, "run.interrupted")[0].severity is Severity.CRIT


def test_thread_exceptions_become_problems(env):
    script(
        env,
        "bad_thread",
        """
        from tag_file_system import action

        @action.added()
        def run(path, metadata, ctx):
            def work():
                raise RuntimeError("in thread")
            t = ctx.spawn(work)
            t.join()
            ctx.done()
        """,
    )
    path, file, parsed = add_file(env, "@@bad_thread/a.txt")

    (run,) = env.runner.on_file(Hook.ADDED, path, file, parsed)

    # a tracked thread that dies fails the run: nothing would ever call done()
    assert env.store.get_run(run.id).status is RunStatus.FAILED
    assert problems(env, "thread.failed")[0].run_id == run.id


# --------------------------------------------------------------- lifecycle


LIFECYCLE = """
    from tag_file_system import action
    calls = []

    @action.on_start()
    def up(ctx):
        ctx.log("service up")
        ctx.write("out/started.txt", ctx.action_name)
        calls.append(("start", ctx.run_id))
        return "up"

    @action.on_stop()
    def down(ctx):
        calls.append(("stop", ctx.run_id))
"""


def test_lifecycle_hooks_run_once_per_session(env):
    script(env, "service", LIFECYCLE)

    (started,) = env.runner.on_lifecycle(Hook.ON_START)

    assert started.hook is Hook.ON_START
    assert started.status is RunStatus.OK and started.result == "up"
    assert started.source is RunSource.LIFECYCLE
    assert started.file_hash == "" and started.file_id is None
    assert started.args == {"session": env.runner.session}
    assert started.slug == "on_start"
    # ctx works without a file: the written file is indexed and attributed.
    written = env.backend.query_file("out/started.txt")
    assert written is not None
    assert [
        p.run_id for p in env.store.query_provenance(file_path="out/started.txt")
    ] == [started.id]
    assert "service up" in str(env.store.query_trace(started.id))

    assert env.runner.on_lifecycle(Hook.ON_START) == []  # once per session

    (stopped,) = env.runner.on_lifecycle(Hook.ON_STOP)
    assert stopped.hook is Hook.ON_STOP and stopped.status is RunStatus.OK
    module = env.loader.addon_for("service").module
    assert [kind for kind, _ in module.calls] == ["start", "stop"]


def test_lifecycle_hooks_of_a_second_session_run_again(env):
    script(env, "service", LIFECYCLE)
    env.runner.on_lifecycle(Hook.ON_START)

    env.runner.session = "another-session"

    (again,) = env.runner.on_lifecycle(Hook.ON_START)
    assert again.args == {"session": "another-session"}
    assert len([r for r in env.store.query_runs() if r.hook is Hook.ON_START]) == 2


def test_lifecycle_failure_is_a_failed_run_and_is_not_retried(env):
    script(
        env,
        "service",
        """
        from tag_file_system import action

        @action.on_start()
        def up(ctx):
            raise RuntimeError("no connection")
        """,
    )

    (failed,) = env.runner.on_lifecycle(Hook.ON_START)

    assert failed.status is RunStatus.FAILED
    (reported,) = problems(env, "run.failed")
    assert "the on_start hook" in reported.message and reported.file_id is None

    assert env.runner.retry(failed.id) is None
    assert problems(env, "retry.lifecycle")


def test_lifecycle_signature_and_duplicate_rules(env):
    script(
        env,
        "bad",
        """
        from tag_file_system import action

        @action.on_start()
        def up(path, metadata, ctx):
            pass

        @action.on_stop()
        def down(ctx):
            pass

        @action.on_stop()
        def down_again(ctx):
            pass
        """,
    )
    addon = env.loader.addon_for("bad")

    assert [h.hook for h in addon.lifecycle_handlers] == [Hook.ON_STOP]
    assert addon.describe()["hooks"] == ["on_stop"]
    assert "on_start handlers take (ctx)" in problems(env, "addon.signature")[0].message
    assert "down_again" in problems(env, "addon.duplicate_handler")[0].message
    assert len(env.runner.on_lifecycle(Hook.ON_STOP)) == 1


def test_lifecycle_handler_can_run_a_service_thread(env):
    script(
        env,
        "service",
        """
        import threading
        from tag_file_system import action

        stop = threading.Event()
        seen = []

        @action.on_start()
        def up(ctx):
            def loop():
                stop.wait(5)
                seen.append("left")
                ctx.done("served")
            ctx.spawn(loop)

        @action.on_stop()
        def down(ctx):
            stop.set()
        """,
    )
    (started,) = env.runner.on_lifecycle(Hook.ON_START)
    assert started.status is RunStatus.RUNNING

    # a service is meant to outlive run_warn_after_seconds (0.05s here)
    time.sleep(0.1)
    assert env.runner.check_overdue() == []
    assert problems(env, "run.overdue") == []

    env.runner.on_lifecycle(Hook.ON_STOP)
    env.runner.stop(timeout=5)

    finished = env.store.get_run(started.id)
    assert finished is not None
    assert finished.status is RunStatus.OK and finished.result == "served"
    assert env.loader.addon_for("service").module.seen == ["left"]


# ------------------------------------------------------------------- retry


def test_retry_starts_a_fresh_run_for_a_failed_one(env):
    script(
        env,
        "flaky",
        """
        from tag_file_system import action
        attempts = []

        @action.added()
        def run(path, metadata, ctx, n: int = 1):
            attempts.append(n)
            if len(attempts) == 1:
                raise RuntimeError("first time fails")
            return "ok now"
        """,
    )
    path, file, parsed = add_file(env, "@@flaky__2/a.txt")
    (failed,) = env.runner.on_file(Hook.ADDED, path, file, parsed)
    assert failed.status is RunStatus.FAILED

    retried = env.runner.retry(failed.id)

    assert retried is not None
    assert retried.status is RunStatus.OK and retried.result == "ok now"
    assert retried.retry_of == failed.id and retried.source is RunSource.RETRY
    assert env.loader.addon_for("flaky").module.attempts == [2, 2]
    assert env.runner.retry("nope") is None


# ----------------------------------------------------------------- resolve


def test_resolve_remote_and_tagdir(env):
    script(
        env,
        "ship",
        """
        from tag_file_system import action

        @action.added()
        def run(path, metadata, ctx, dst: action.TagDir, remote: action.Remote):
            ctx.copy(path, dst / path.name)
            ctx.copy(path, remote / path.name)
            return {"dst": ctx.resolve("tagdir", "archive").name}
        """,
    )
    (env.root.path / "2024--archive").mkdir()
    path, file, parsed = add_file(env, "@@ship__archive__backup/a.txt")

    (run,) = env.runner.on_file(Hook.ADDED, path, file, parsed)

    assert run.status is RunStatus.OK, run.error
    assert (env.root.path / "2024--archive" / "a.txt").exists()
    assert (env.tmp / "backup" / "a.txt").exists()
    assert env.store.produced_by(run.id) == [
        "2024--archive/a.txt"
    ]  # remote output is outside the root
    emits = [t.payload for t in env.store.query_trace(run.id) if t.kind == "emit"]
    assert emits[1]["indexed"] is False

    path3, file3, parsed3 = add_file(env, "@@ship__nowhere__backup/c.txt", b"c")
    (run3,) = env.runner.on_file(Hook.ADDED, path3, file3, parsed3)
    assert "no directory carries the tag" in (run3.error or "")
    path4, file4, parsed4 = add_file(env, "@@ship__archive__nas/d.txt", b"d")
    (run4,) = env.runner.on_file(Hook.ADDED, path4, file4, parsed4)
    assert "unknown remote" in (run4.error or "")

    (env.root.path / "old--archive").mkdir()
    path2, file2, parsed2 = add_file(env, "@@ship__archive__backup/b.txt", b"b")
    (run2,) = env.runner.on_file(Hook.ADDED, path2, file2, parsed2)
    assert run2.status is RunStatus.FAILED and "ambiguous" in (run2.error or "")


def test_parse_problems_are_reported_once_per_marker(env):
    path, file, parsed = add_file(env, "x--ok@@bad_/a.txt")  # "@@bad_": trailing "_"
    assert parsed.problems

    env.runner.on_file(Hook.ADDED, path, file, parsed)
    env.runner.on_file(Hook.MODIFIED, path, file, parsed)

    assert len(problems(env, "name.parse")) == 1
    assert problems(env, "name.parse")[0].severity is Severity.WARN


def test_capture_is_per_thread_and_leaves_nothing_behind(env):
    import sys

    script(
        env,
        "loud",
        """
        import threading, logging
        from tag_file_system import action
        gate = threading.Event()

        @action.added()
        def run(path, metadata, ctx):
            def work():
                gate.wait(5)
                print("late print")
                logging.getLogger("loud.thread").warning("late log")
                ctx.done()
            ctx.spawn(work)
            print("early print")

        @action.modified()
        def quick(path, metadata, ctx):
            print("quick print")
        """,
    )
    path, file, parsed = add_file(env, "@@loud/a.txt")
    (slow,) = env.runner.on_file(Hook.ADDED, path, file, parsed)
    (fast,) = env.runner.on_file(
        Hook.MODIFIED, path, file, parsed
    )  # while slow is in flight
    env.loader.addon_for("loud").module.gate.set()
    deadline = time.time() + 5
    while (
        env.store.get_run(slow.id).status is RunStatus.RUNNING
        and time.time() < deadline
    ):
        time.sleep(0.01)

    assert [t.payload for t in env.store.query_trace(fast.id)] == [
        "stdout: quick print"
    ]
    assert [t.payload for t in env.store.query_trace(slow.id)] == [
        "stdout: early print",
        "stdout: late print",
        "warning: late log",
    ]
    # nothing captured outside a run, and the streams still work
    print("outside")
    assert not any(
        "outside" in str(t.payload)
        for run in (slow, fast)
        for t in env.store.query_trace(run.id)
    )
    assert sys.stdout.writable()


def test_ctx_tag_normalizes_and_index_keeps_ctx_tags(env):
    script(
        env,
        "tagger",
        """
        from tag_file_system import action

        @action.added()
        def run(path, metadata, ctx):
            ctx.tag(path, "Photo")
            ctx.tag(path, "photo")
            out = ctx.write(ctx.root / "out" / "r--fromname.txt", "v1")
            ctx.tag(out, "fromctx")
            ctx.write(out, "v2")  # re-index must keep fromctx

        @action.tagged("photo")
        def on_photo(path, metadata, ctx):
            pass
        """,
    )
    path, file, parsed = add_file(env, "@@tagger/a.txt")

    env.runner.on_file(Hook.ADDED, path, file, parsed)

    tagged = env.backend.query_file(path)
    assert [t.name for t in tagged.tags] == ["photo"]
    assert (
        len(env.store.query_runs(action_name="tagger", status=RunStatus.OK)) == 2
    )  # added + one tagged
    out = env.backend.query_file("out/r--fromname.txt")
    assert sorted(t.name for t in out.tags) == ["fromctx", "fromname"]


def test_ctx_delete_and_move_keep_the_database_in_step(env):
    script(
        env,
        "mover",
        """
        from tag_file_system import action

        @action.added()
        def run(path, metadata, ctx):
            tmp = ctx.write(ctx.root / "out" / "tmp.txt", "x")
            final = ctx.move(tmp, ctx.root / "out" / "final.txt")
            gone = ctx.write(ctx.root / "out" / "gone.txt", "y")
            ctx.delete(gone)
            return final.name
        """,
    )
    path, file, parsed = add_file(env, "@@mover/a.txt")

    (run,) = env.runner.on_file(Hook.ADDED, path, file, parsed)

    assert run.status is RunStatus.OK
    assert env.backend.query_file("out/tmp.txt") is None
    assert env.backend.query_file("out/final.txt") is not None
    assert env.backend.query_file("out/gone.txt") is None
    assert env.backend.query_file("out/gone.txt", include_deleted=True) is not None
    assert env.store.produced_by(run.id) == ["out/final.txt"]


def test_index_refuses_tfs_and_script_zones(env):
    (env.root.tfs_dir / "junk.txt").write_text("x")
    (env.root.script_dir / "_data.txt").write_text("x")

    assert env.runner.index(env.root.tfs_dir / "junk.txt") is None
    assert env.runner.index(env.root.script_dir / "_data.txt") is None
    assert env.backend.query_files(include_deleted=True) == []


def test_binding_failure_key_matches_a_later_success(env):
    script(
        env,
        "resize",
        "from tag_file_system import action\n@action.added()\ndef run(p, m, c, width: int): return width\n",
    )
    path, file, parsed = add_file(env, "@@resize__wide/a.txt")
    (failed,) = env.runner.on_file(Hook.ADDED, path, file, parsed)
    assert failed.status is RunStatus.FAILED and failed.args == {"width": "wide"}

    # editing the script does not re-run a dead key (DESIGN §6.1)
    script(
        env,
        "resize",
        "from tag_file_system import action\n@action.added()\ndef run(p, m, c, width: str): return width\n",
    )
    assert env.runner.on_file(Hook.ADDED, path, file, parsed) == []

    retried = env.runner.retry(failed.id)
    assert (
        retried is not None
        and retried.status is RunStatus.OK
        and retried.result == "wide"
    )


def test_retry_rules(env):
    script(
        env,
        "flaky2",
        "from tag_file_system import action\n@action.added()\ndef run(p, m, c, n: int = 1):\n    raise RuntimeError('x')\n",
    )
    path, file, parsed = add_file(env, "@@flaky2__3/a.txt")
    (failed,) = env.runner.on_file(Hook.ADDED, path, file, parsed)

    # signature changed since: the old key cannot be retried
    script(
        env,
        "flaky2",
        "from tag_file_system import action\n@action.added()\ndef run(p, m, c, count: int = 1):\n    return count\n",
    )
    assert env.runner.retry(failed.id) is None
    assert problems(env, "retry.key_changed")

    # ok runs are not retried; a deleted file is reported
    ok_path, ok_file, ok_parsed = add_file(env, "@@flaky2__4/b.txt", b"b")
    (ok,) = env.runner.on_file(Hook.ADDED, ok_path, ok_file, ok_parsed)
    assert ok.status is RunStatus.OK
    assert env.runner.retry(ok.id) is None
    path.unlink()
    env.backend.delete(path)
    assert env.runner.retry(failed.id) is None
    assert problems(env, "retry.file_missing")


def test_sys_exit_in_a_handler_is_a_failed_run(env):
    script(
        env,
        "quit",
        "import sys\nfrom tag_file_system import action\n@action.added()\ndef run(p, m, c):\n    sys.exit(3)\n",
    )
    path, file, parsed = add_file(env, "@@quit/a.txt")

    (run,) = env.runner.on_file(Hook.ADDED, path, file, parsed)

    assert run.status is RunStatus.FAILED and "SystemExit" in (run.error or "")
    assert env.runner.in_flight == {}


def test_emitted_file_in_another_action_dir_runs_as_a_chain(env):
    script(
        env,
        "producer",
        "from tag_file_system import action\n@action.added()\ndef run(p, m, c):\n    c.write(c.root / '@@consumer' / 'made.txt', 'x')\n",
    )
    script(
        env,
        "consumer",
        "from tag_file_system import action\n@action.added()\ndef run(p, m, c):\n    return 'consumed'\n",
    )
    path, file, parsed = add_file(env, "@@producer/a.txt")

    (produced,) = env.runner.on_file(Hook.ADDED, path, file, parsed)

    (consumed,) = env.store.query_runs(action_name="consumer")
    assert consumed.source is RunSource.CHAIN and consumed.parent_run_id == produced.id
    assert consumed.status is RunStatus.OK
    # the watcher's later event for made.txt finds the key and does nothing
    made = env.root.absolute("@@consumer/made.txt")
    made_file = env.backend.query_file(made)
    assert (
        env.runner.on_file(
            Hook.ADDED,
            made,
            made_file,
            env.runner.parser.parse_path(PurePosixPath("@@consumer/made.txt")),
        )
        == []
    )


def test_problems_from_runs_started_in_a_handler_are_delivered_after_it(env):
    script(
        env,
        "retrier",
        """
        from tag_file_system import action
        seen = []

        @action.added()
        def run(path, metadata, ctx):
            raise RuntimeError("always")

        @action.err()
        def on_err(problem, ctx):
            seen.append(problem.kind)
            if problem.kind == "run.failed" and len(seen) == 1:
                ctx.retry(problem.run_id)
        """,
    )
    path, file, parsed = add_file(env, "@@retrier/a.txt")

    env.runner.on_file(Hook.ADDED, path, file, parsed)

    seen = env.loader.addon_for("retrier").module.seen
    assert seen.count("run.failed") == 2  # the retried run's failure was delivered too
    assert all(p.delivered_at is not None for p in problems(env, "run.failed"))


def test_thread_failure_without_done_fails_the_run(env):
    script(
        env,
        "crashy",
        "from tag_file_system import action\n@action.added()\ndef run(p, m, c):\n    def w():\n        raise RuntimeError('in thread')\n    t = c.spawn(w)\n    t.join()\n",
    )
    path, file, parsed = add_file(env, "@@crashy/a.txt")

    (run,) = env.runner.on_file(Hook.ADDED, path, file, parsed)

    final = env.store.get_run(run.id)
    assert final.status is RunStatus.FAILED and "thread failed" in (final.error or "")
    assert env.runner.in_flight == {}


def test_addon_stream_swaps_do_not_outlive_the_run(env):
    import io
    import logging
    import sys

    script(
        env,
        "swapper",
        """
        import io, sys, logging
        from tag_file_system import action

        @action.added()
        def run(path, metadata, ctx):
            sys.stdout = io.StringIO()
            sys.stderr.close()
            logging.getLogger().setLevel(logging.WARNING)
            print("swallowed by the add-on's own buffer")

        @action.modified()
        def again(path, metadata, ctx):
            logging.getLogger("swapper").info("still captured")
        """,
    )
    path, file, parsed = add_file(env, "@@swapper/a.txt")
    before = sys.stdout

    env.runner.on_file(Hook.ADDED, path, file, parsed)

    after = (
        sys.stdout
    )  # the dispatcher wrapping what was there before, not the add-on's buffer
    assert after is before or getattr(after, "original", None) is before
    assert not isinstance(getattr(after, "original", after), io.StringIO)
    assert not sys.stdout.closed and not sys.stderr.closed
    (again,) = env.runner.on_file(Hook.MODIFIED, path, file, parsed)
    assert [t.payload for t in env.store.query_trace(again.id)] == [
        "info: still captured"
    ]
    assert logging.getLogger().level <= logging.INFO


def test_move_onto_an_existing_managed_file(env):
    script(
        env,
        "clobber",
        """
        from tag_file_system import action

        @action.added()
        def run(path, metadata, ctx):
            target = ctx.write(ctx.root / "out" / "target.txt", "old")
            ctx.move(path, target)
        """,
    )
    path, file, parsed = add_file(env, "@@clobber/x.txt", b"new")

    (run,) = env.runner.on_file(Hook.ADDED, path, file, parsed)

    assert run.status is RunStatus.OK
    assert env.backend.query_file(path) is None  # source row gone
    target = env.backend.query_file("out/target.txt")
    assert target is not None and target.file_hash == file.file_hash


def test_retry_storms_are_braked(env):
    from tag_file_system.addons.runner import MAX_RETRIES

    script(
        env,
        "loop",
        """
        from tag_file_system import action
        attempts = []

        @action.added()
        def run(path, metadata, ctx):
            attempts.append(1)
            raise RuntimeError("always")

        @action.err()
        def on_err(problem, ctx):
            if problem.kind == "run.failed":
                ctx.retry(problem.run_id)
        """,
    )
    path, file, parsed = add_file(env, "@@loop/a.txt")

    env.runner.on_file(Hook.ADDED, path, file, parsed)

    assert len(env.loader.addon_for("loop").module.attempts) == MAX_RETRIES + 1
    assert problems(env, "retry.limit")


def test_non_serializable_result_fails_the_run(env):
    script(
        env,
        "weird",
        """
        from tag_file_system import action

        @action.added()
        def run(path, metadata, ctx):
            return object()
        """,
    )
    path, file, parsed = add_file(env, "@@weird/a.txt")

    (run,) = env.runner.on_file(Hook.ADDED, path, file, parsed)

    assert run.status is RunStatus.FAILED
    assert "not JSON-serializable" in (run.error or "")
    assert env.runner.in_flight == {}
