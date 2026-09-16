# Code by AkinoAlice@TyrantRey

import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tag_file_system.addons.loader import MODULE_PREFIX, AddonLoader
from tag_file_system.addons.runner import ActionRunner
from tag_file_system.config import Config, DaemonConfig
from tag_file_system.core.interface.action import (
    Hook,
    RunKey,
    RunSource,
    RunStatus,
    Severity,
)
from tag_file_system.database.action_store import ActionStore
from tag_file_system.database.sqlite import SQLiteBackend
from tag_file_system.functions import FunctionsStore
from tag_file_system.root import FUNCTIONS_FILE, Root


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
    functions = FunctionsStore(root, loader)
    config = Config(
        daemon=DaemonConfig(run_warn_after_seconds=0.05, stop_timeout_seconds=0.2),
        remotes={"backup": str(tmp_path / "backup")},
    )
    runner = ActionRunner(
        root,
        backend,
        store,
        loader,
        config=config,
        max_chain_depth=3,
        functions=functions,
    )
    loader.report = runner.problem
    functions.report = runner.problem
    (tmp_path / "backup").mkdir()
    yield SimpleNamespace(
        root=root,
        backend=backend,
        store=store,
        loader=loader,
        functions=functions,
        runner=runner,
        tmp=tmp_path,
    )
    backend.close()


def script(env, name: str, source: str) -> None:
    (env.root.script_dir / f"{name}.py").write_text(
        textwrap.dedent(source), encoding="utf-8"
    )
    env.loader.load_all()
    env.functions.rebind()


def functions(env, folder: str, text: str) -> None:
    """Write ``folder``'s ``.tfsfunctions.yaml`` and read the tree again."""
    directory = env.root.path.joinpath(*folder.split("/")) if folder else env.root.path
    directory.mkdir(parents=True, exist_ok=True)
    (directory / FUNCTIONS_FILE).write_text(textwrap.dedent(text), encoding="utf-8")
    env.functions.load_tree()


def enable(env, folder: str, *refs: str, **params) -> None:
    """``enable(env, "job", "make_copy.run", suffix=".md")``: one entry per
    ``script.handler`` reference, all with the same parameters."""
    entries: dict[str, dict[str, dict]] = {}
    for ref in refs:
        name, handler = ref.split(".")
        entries.setdefault(name, {})[handler] = dict(params)
    functions(env, folder, yaml.safe_dump({"version": 1, "functions": entries}))


def add_file(env, key: str, content: bytes = b"data"):
    path = env.root.absolute(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    file = env.runner.index(path)
    assert file is not None
    return path, file


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
    enable(env, "job", "make_copy.run", suffix=".md")
    path, file = add_file(env, "job/a.txt")

    runs = env.runner.on_file(Hook.ADDED, path, file)

    assert len(runs) == 1
    run = env.store.get_run(runs[0].id)
    assert run is not None
    assert run.status is RunStatus.OK
    assert run.result == {"copied": "a--copy.md", "size": 4}
    assert run.args == {"suffix": ".md"}
    assert (run.action_name, run.handler) == ("make_copy", "run")
    assert run.slug == 'make_copy.run(suffix=".md")'
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
    assert env.runner.on_file(Hook.ADDED, path, file) == []
    assert len(env.store.query_runs()) == 1
    assert env.runner.in_flight == {}


def test_nothing_runs_without_an_entry(env):
    script(env, "make_copy", MAKE_COPY)
    path, file = add_file(env, "job/a.txt")

    assert env.runner.on_file(Hook.ADDED, path, file) == []
    assert env.runner.on_transition(path, file, None, content_changed=True) == []
    assert env.store.query_runs() == []


def test_generated_file_never_retriggers_its_producer(env):
    script(env, "make_copy", MAKE_COPY.replace('ctx.root / "out" /', "path.parent /"))
    enable(env, "job", "make_copy.run")
    path, file = add_file(env, "job/a.txt")
    (run,) = env.runner.on_file(Hook.ADDED, path, file)
    assert env.store.produced_by(run.id) == ["job/a--copy.txt"]

    copy_path = env.root.absolute("job/a--copy.txt")
    copy = env.backend.query_file(copy_path)
    assert copy is not None
    # the copy sits in the same scope: entering it as a chain was refused,
    # and the watcher's own event for it finds nothing to do either
    assert env.runner.on_file(Hook.ADDED, copy_path, copy) == []
    assert len(env.store.query_runs()) == 1


def test_invalid_entries_are_load_problems_and_unresolved_paths_failed_runs(env):
    script(
        env,
        "resize",
        "from tag_file_system import action\n@action.added()\ndef run(p, m, c, width: int): pass\n",
    )
    script(
        env,
        "ship",
        "from tag_file_system import action\n@action.added()\ndef run(p, m, c, remote: action.Remote): pass\n",
    )
    functions(
        env,
        "job",
        """
        version: 1
        functions:
          nosuch:
            run: {}
          resize:
            run: {width: wide}
          ship:
            run: {remote: nowhere}
        """,
    )
    path, file = add_file(env, "job/a.txt")

    runs = env.runner.on_file(Hook.ADDED, path, file)

    # the two entries that cannot bind were refused at load, once
    assert [p.kind for p in problems(env)] == [
        "functions.unbound",
        "functions.signature",
        "action.binding",
    ]
    unbound = problems(env, "functions.unbound")[0]
    assert unbound.severity is Severity.ERR and "nosuch" in unbound.message
    assert "width: Input should be a valid integer" in (
        problems(env, "functions.signature")[0].message
    )
    # a remote is resolved at run time: that failure is a run
    assert len(runs) == 1 and runs[0].status is RunStatus.FAILED
    assert "unknown remote" in (runs[0].error or "")
    assert problems(env, "action.binding")[0].file_id == file.file_id
    # the failed binding is a dead key: no re-run on the next event
    assert env.runner.on_file(Hook.ADDED, path, file) == []


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
    enable(env, "job", "boom.run")
    path, file = add_file(env, "job/a.txt")

    (run,) = env.runner.on_file(Hook.ADDED, path, file)

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


# ------------------------------------------------------------------- scope


GUARD = """
    from tag_file_system import action
    calls = []

    @action.added()
    def run(path, metadata, ctx):
        calls.append("added")

    @action.modified()
    def changed(path, metadata, ctx):
        calls.append("modified")

    @action.removed(on_move=True)
    def gone(path, metadata, ctx):
        calls.append("removed")
"""


def test_transition_follows_the_file_in_and_out_of_scope(env):
    # DESIGN/v0-4-0.md §6: gaining an excluded tag is leaving scope; losing
    # it is entering; a content change while inside is a modification.
    script(env, "guard", GUARD)
    functions(
        env,
        "job",
        """
        version: 1
        functions:
          guard:
            run: {exclude: [{tag: draft}]}
            changed: {exclude: [{tag: draft}]}
            gone: {exclude: [{tag: draft}]}
        """,
    )
    calls = env.loader.addon_for("guard").module.calls
    path, file = add_file(env, "job/a.txt", b"v1")

    env.runner.on_transition(path, file, None, content_changed=True)
    assert calls == ["added"]

    env.backend.set_file_tags(path, ["draft"])
    excluded = env.backend.query_file(path)
    env.runner.on_transition(path, excluded, file, content_changed=False)
    assert calls == ["added", "removed"]

    path.write_bytes(b"v2")  # edited while excluded: nothing applies
    edited = env.runner.index(path)
    env.runner.on_transition(path, edited, excluded, content_changed=True)
    assert calls == ["added", "removed"]

    env.backend.set_file_tags(path, [])
    back = env.backend.query_file(path)
    env.runner.on_transition(path, back, edited, content_changed=False)
    assert calls == ["added", "removed", "added"]  # a new hash: entering is new work

    path.write_bytes(b"v3")
    again = env.runner.index(path)
    env.runner.on_transition(path, again, back, content_changed=True)
    assert calls == ["added", "removed", "added", "modified"]

    removed = [r for r in env.store.query_runs() if r.hook is Hook.REMOVED]
    assert len(removed) == 1 and removed[0].slug == "guard.gone()"


def test_ctx_tag_and_untag_move_the_file_across_an_exclusion(env):
    script(env, "guard", GUARD)
    script(
        env,
        "editor",
        """
        from tag_file_system import action

        @action.added()
        def hide(path, metadata, ctx):
            ctx.tag(path, "draft")

        @action.modified()
        def show(path, metadata, ctx):
            ctx.untag(path, "draft")
        """,
    )
    functions(
        env,
        "job",
        """
        version: 1
        functions:
          guard:
            gone: {exclude: [{tag: draft}]}
            run: {exclude: [{tag: draft}]}
          editor:
            hide: {}
            show: {}
        """,
    )
    calls = env.loader.addon_for("guard").module.calls
    path, file = add_file(env, "job/a.txt")

    env.runner.on_file(Hook.ADDED, path, file)
    assert calls == ["added", "removed"]  # hide() tagged it: guard left scope
    chained = [r for r in env.store.query_runs() if r.source is RunSource.CHAIN]
    assert [(r.handler, r.hook) for r in chained] == [("gone", Hook.REMOVED)]

    tagged = env.backend.query_file(path)
    env.runner.on_file(Hook.MODIFIED, path, tagged)
    # show() untagged it: guard.run's key already exists for this hash
    assert calls == ["added", "removed"]
    assert [t.name for t in env.backend.query_file(path).tags] == []


def test_tagged_default_is_suppressed_and_configured_by_a_folder_entry(env):
    script(
        env,
        "photo",
        """
        from tag_file_system import action

        @action.tagged("photo")
        def on_photo(path, metadata, ctx, size: int = 1):
            return size
        """,
    )
    enable(env, "own", "photo.on_photo", size=9)
    inside, inside_file = add_file(env, "own/a--photo.txt")
    outside, outside_file = add_file(env, "x--photo.txt", b"other")

    env.runner.on_transition(inside, inside_file, None, content_changed=True)
    env.runner.on_transition(outside, outside_file, None, content_changed=True)

    runs = {r.file_id: r for r in env.store.query_runs()}
    assert len(runs) == 2
    configured = runs[inside_file.file_id]
    assert configured.args == {"tag": "photo", "size": 9} and configured.result == 9
    assert configured.slug == 'photo.on_photo(tag="photo", size=9)'
    default = runs[outside_file.file_id]
    assert default.args == {"tag": "photo"} and default.result == 1
    assert default.slug == 'photo.on_photo(tag="photo")'


def test_rescope_offers_every_entry_in_scope_again(env):
    for name in ("a", "b"):
        script(
            env,
            name,
            f"from tag_file_system import action\n@action.added()\ndef run(p, m, c):\n    return {name!r}\n",
        )
    enable(env, "job", "a.run")
    path, file = add_file(env, "job/x.txt")
    env.runner.on_transition(path, file, None, content_changed=True)
    assert [r.action_name for r in env.store.query_runs()] == ["a"]

    enable(env, "job", "a.run", "b.run")  # a reload added b
    assert env.runner.on_transition(path, file, file, content_changed=False) == []
    (new,) = env.runner.on_transition(
        path, file, file, content_changed=False, rescope=True
    )
    assert new.action_name == "b"
    assert sorted(r.action_name for r in env.store.query_runs()) == ["a", "b"]


def test_identical_entries_in_parent_and_child_are_one_run(env):
    script(
        env,
        "resize",
        "from tag_file_system import action\n@action.added()\ndef run(p, m, c, width: int):\n    return width\n",
    )
    enable(env, "", "resize.run", width=800)
    enable(env, "a", "resize.run", width=800)
    enable(env, "a/b", "resize.run", width=400)
    path, file = add_file(env, "a/b/x.txt")

    runs = env.runner.on_file(Hook.ADDED, path, file)

    assert [r.args for r in runs] == [{"width": 800}, {"width": 400}]
    assert [r.result for r in runs] == [800, 400]  # parent first


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
    enable(env, "job", "classify.run")
    path, file = add_file(env, "job/a.txt")

    env.runner.on_file(Hook.ADDED, path, file)

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
    enable(env, "job", "cleanup.on_delete", "cleanup_move.on_gone")
    path, file = add_file(env, "job/a.txt")

    env.runner.on_removed(file, moved=True)
    assert env.loader.addon_for("cleanup").module.calls == []
    assert env.loader.addon_for("cleanup_move").module.calls == ["gone"]

    # different content: identical content would share a's run key (§6.1)
    path_b, file_b = add_file(env, "job/b.txt", b"other")
    env.runner.on_removed(file_b, moved=False)
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
    enable(env, "job", "bg.run")
    path, file = add_file(env, "job/a.txt")

    (run,) = env.runner.on_file(Hook.ADDED, path, file)

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
    enable(env, "job", "stuck.run")
    path, file = add_file(env, "job/a.txt")
    (run,) = env.runner.on_file(Hook.ADDED, path, file)

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
    enable(env, "job", "bad_thread.run")
    path, file = add_file(env, "job/a.txt")

    (run,) = env.runner.on_file(Hook.ADDED, path, file)

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
        ctx.write("out/started.txt", ctx.action_name + "." + ctx.handler_name)
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
    assert started.slug == "on_start" and started.handler == "up"
    # ctx works without a file: the written file is indexed and attributed.
    written = env.backend.query_file("out/started.txt")
    assert written is not None
    assert env.root.absolute("out/started.txt").read_text() == "service.up"
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


def test_lifecycle_signature_rule_and_several_handlers_per_hook(env):
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

    # Handlers are addressed by name (DESIGN/v0-4-0.md §5): two on_stop
    # functions are two handlers, each with its own run.
    assert [h.hook for h in addon.lifecycle_handlers] == [Hook.ON_STOP, Hook.ON_STOP]
    assert [(h["name"], h["hooks"]) for h in addon.describe()["handlers"]] == [
        ("down", ["on_stop"]),
        ("down_again", ["on_stop"]),
    ]
    assert "on_start handlers take (ctx)" in problems(env, "addon.signature")[0].message
    runs = env.runner.on_lifecycle(Hook.ON_STOP)
    assert [(r.action_name, r.handler) for r in runs] == [
        ("bad", "down"),
        ("bad", "down_again"),
    ]
    assert env.runner.on_lifecycle(Hook.ON_STOP) == []  # once per session each


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
    enable(env, "job", "flaky.run", n=2)
    path, file = add_file(env, "job/a.txt")
    (failed,) = env.runner.on_file(Hook.ADDED, path, file)
    assert failed.status is RunStatus.FAILED and failed.args == {"n": 2}

    retried = env.runner.retry(failed.id)

    assert retried is not None
    assert retried.status is RunStatus.OK and retried.result == "ok now"
    assert retried.retry_of == failed.id and retried.source is RunSource.RETRY
    assert retried.slug == "flaky.run(n=2)"
    assert env.loader.addon_for("flaky").module.attempts == [2, 2]
    assert env.runner.retry("nope") is None


def test_retry_of_a_run_keyed_before_0_4_is_refused(env):
    script(
        env,
        "old",
        "from tag_file_system import action\n@action.added()\ndef run(p, m, c, n: int = 1):\n    return n\n",
    )
    path, file = add_file(env, "a.txt")
    record = env.runner._action_record(env.loader.addon_for("old"))
    legacy = env.store.start_run(
        record,
        RunKey(
            file_hash=file.file_hash,
            action_name="old",
            hook=Hook.ADDED,
            args={"n": "1"},
        ),
        "old__1",
        path,
        RunSource.WATCH,
    )
    env.store.finish_run(legacy.id, RunStatus.FAILED, error="x")

    assert env.runner.retry(legacy.id) is None
    (why,) = problems(env, "retry.key_changed")
    assert "old.?" in why.message and "no longer handles" in why.message


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
    enable(env, "job", "ship.run", dst="archive", remote="backup")
    path, file = add_file(env, "job/a.txt")

    (run,) = env.runner.on_file(Hook.ADDED, path, file)

    assert run.status is RunStatus.OK, run.error
    assert (env.root.path / "2024--archive" / "a.txt").exists()
    assert (env.tmp / "backup" / "a.txt").exists()
    assert env.store.produced_by(run.id) == [
        "2024--archive/a.txt"
    ]  # remote output is outside the root
    emits = [t.payload for t in env.store.query_trace(run.id) if t.kind == "emit"]
    assert emits[1]["indexed"] is False

    enable(env, "nowhere", "ship.run", dst="nowhere", remote="backup")
    path3, file3 = add_file(env, "nowhere/c.txt", b"c")
    (run3,) = env.runner.on_file(Hook.ADDED, path3, file3)
    assert "no directory carries the tag" in (run3.error or "")
    enable(env, "nas", "ship.run", dst="archive", remote="nas")
    path4, file4 = add_file(env, "nas/d.txt", b"d")
    (run4,) = env.runner.on_file(Hook.ADDED, path4, file4)
    assert "unknown remote" in (run4.error or "")

    (env.root.path / "old--archive").mkdir()
    path2, file2 = add_file(env, "job/b.txt", b"b")
    (run2,) = env.runner.on_file(Hook.ADDED, path2, file2)
    assert run2.status is RunStatus.FAILED and "ambiguous" in (run2.error or "")


def test_legacy_function_markers_are_reported_once_per_marker(env):
    path, file = add_file(env, "x--ok@@make_copy__.jpg/a.txt")
    assert [t.name for t in file.tags] == ["ok"]

    env.runner.on_file(Hook.ADDED, path, file)
    env.runner.index(path)  # indexing again reports nothing new

    (reported,) = problems(env, "name.parse")
    assert reported.severity is Severity.WARN
    assert "'@@make_copy__.jpg'" in reported.message
    assert ".tfsfunctions.yaml" in reported.message


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
    enable(env, "job", "loud.run", "loud.quick")
    path, file = add_file(env, "job/a.txt")
    (slow,) = env.runner.on_file(Hook.ADDED, path, file)
    (fast,) = env.runner.on_file(Hook.MODIFIED, path, file)  # while slow is in flight
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
    enable(env, "job", "tagger.run")
    path, file = add_file(env, "job/a.txt")

    env.runner.on_file(Hook.ADDED, path, file)

    tagged = env.backend.query_file(path)
    assert [t.name for t in tagged.tags] == ["photo"]
    assert (
        len(env.store.query_runs(action_name="tagger", status=RunStatus.OK)) == 2
    )  # added + one tagged (the default: nothing configures on_photo)
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
    enable(env, "job", "mover.run")
    path, file = add_file(env, "job/a.txt")

    (run,) = env.runner.on_file(Hook.ADDED, path, file)

    assert run.status is RunStatus.OK
    assert env.backend.query_file("out/tmp.txt") is None
    assert env.backend.query_file("out/final.txt") is not None
    assert env.backend.query_file("out/gone.txt") is None
    assert env.backend.query_file("out/gone.txt", include_deleted=True) is not None
    assert env.store.produced_by(run.id) == ["out/final.txt"]


def test_index_refuses_tfs_script_and_functions_files(env):
    (env.root.tfs_dir / "junk.txt").write_text("x")
    (env.root.script_dir / "_data.txt").write_text("x")
    (env.root.path / FUNCTIONS_FILE).write_text("version: 1\n")

    assert env.runner.index(env.root.tfs_dir / "junk.txt") is None
    assert env.runner.index(env.root.script_dir / "_data.txt") is None
    assert env.runner.index(env.root.path / FUNCTIONS_FILE) is None
    assert env.backend.query_files(include_deleted=True) == []


def test_unresolved_remote_is_retried_once_the_remote_exists(env):
    script(
        env,
        "ship",
        "from tag_file_system import action\n@action.added()\ndef run(p, m, c, remote: action.Remote): return remote.name\n",
    )
    enable(env, "job", "ship.run", remote="nas")
    path, file = add_file(env, "job/a.txt")
    (failed,) = env.runner.on_file(Hook.ADDED, path, file)
    assert failed.status is RunStatus.FAILED and failed.args == {"remote": "nas"}

    # editing the script does not re-run a dead key (DESIGN §6.1)
    script(
        env,
        "ship",
        "from tag_file_system import action\n@action.added()\ndef run(p, m, c, remote: action.Remote): return remote.name + '!'\n",
    )
    assert env.runner.on_file(Hook.ADDED, path, file) == []

    (env.tmp / "nas").mkdir()
    env.runner.config.remotes["nas"] = str(env.tmp / "nas")
    retried = env.runner.retry(failed.id)
    assert (
        retried is not None
        and retried.status is RunStatus.OK
        and retried.result == "nas!"
    )


def test_retry_rules(env):
    script(
        env,
        "flaky2",
        "from tag_file_system import action\n@action.added()\ndef run(p, m, c, n: int = 1):\n    raise RuntimeError('x')\n",
    )
    enable(env, "job", "flaky2.run", n=3)
    path, file = add_file(env, "job/a.txt")
    (failed,) = env.runner.on_file(Hook.ADDED, path, file)

    # signature changed since: the run's own arguments no longer fit
    script(
        env,
        "flaky2",
        "from tag_file_system import action\n@action.added()\ndef run(p, m, c, count: int = 1):\n    return count\n",
    )
    assert env.runner.retry(failed.id) is None
    (why,) = problems(env, "retry.key_changed")
    assert "unknown parameter(s) n" in why.message

    # ok runs are not retried; a deleted file is reported
    enable(env, "job2", "flaky2.run", count=4)
    ok_path, ok_file = add_file(env, "job2/b.txt", b"b")
    (ok,) = env.runner.on_file(Hook.ADDED, ok_path, ok_file)
    assert ok.status is RunStatus.OK and ok.result == 4
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
    enable(env, "job", "quit.run")
    path, file = add_file(env, "job/a.txt")

    (run,) = env.runner.on_file(Hook.ADDED, path, file)

    assert run.status is RunStatus.FAILED and "SystemExit" in (run.error or "")
    assert env.runner.in_flight == {}


def test_emitted_file_in_another_folder_runs_as_a_chain(env):
    script(
        env,
        "producer",
        "from tag_file_system import action\n@action.added()\ndef run(p, m, c):\n    c.write(c.root / 'consumer' / 'made.txt', 'x')\n",
    )
    script(
        env,
        "consumer",
        "from tag_file_system import action\n@action.added()\ndef run(p, m, c):\n    return 'consumed'\n",
    )
    enable(env, "job", "producer.run")
    enable(env, "consumer", "consumer.run")
    path, file = add_file(env, "job/a.txt")

    (produced,) = env.runner.on_file(Hook.ADDED, path, file)

    (consumed,) = env.store.query_runs(action_name="consumer")
    assert consumed.source is RunSource.CHAIN and consumed.parent_run_id == produced.id
    assert consumed.status is RunStatus.OK
    # the watcher's later event for made.txt finds the key and does nothing
    made = env.root.absolute("consumer/made.txt")
    made_file = env.backend.query_file(made)
    assert env.runner.on_file(Hook.ADDED, made, made_file) == []


def test_ctx_write_over_a_known_file_is_a_modification(env):
    script(
        env,
        "writer",
        "from tag_file_system import action\n@action.added()\ndef run(p, m, c):\n    c.write(c.root / 'consumer' / 'made.txt', str(p.name))\n",
    )
    script(
        env,
        "consumer",
        """
        from tag_file_system import action
        calls = []

        @action.added()
        def run(p, m, c):
            calls.append("added")

        @action.modified()
        def changed(p, m, c):
            calls.append("modified")
        """,
    )
    enable(env, "job", "writer.run")
    enable(env, "consumer", "consumer.run", "consumer.changed")
    first, first_file = add_file(env, "job/a.txt")
    second, second_file = add_file(env, "job/b.txt", b"b")

    env.runner.on_file(Hook.ADDED, first, first_file)
    env.runner.on_file(Hook.ADDED, second, second_file)

    assert env.loader.addon_for("consumer").module.calls == ["added", "modified"]


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
    enable(env, "job", "retrier.run")
    path, file = add_file(env, "job/a.txt")

    env.runner.on_file(Hook.ADDED, path, file)

    seen = env.loader.addon_for("retrier").module.seen
    assert seen.count("run.failed") == 2  # the retried run's failure was delivered too
    assert all(p.delivered_at is not None for p in problems(env, "run.failed"))


def test_thread_failure_without_done_fails_the_run(env):
    script(
        env,
        "crashy",
        "from tag_file_system import action\n@action.added()\ndef run(p, m, c):\n    def w():\n        raise RuntimeError('in thread')\n    t = c.spawn(w)\n    t.join()\n",
    )
    enable(env, "job", "crashy.run")
    path, file = add_file(env, "job/a.txt")

    (run,) = env.runner.on_file(Hook.ADDED, path, file)

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
    enable(env, "job", "swapper.run", "swapper.again")
    path, file = add_file(env, "job/a.txt")
    before = sys.stdout

    env.runner.on_file(Hook.ADDED, path, file)

    after = (
        sys.stdout
    )  # the dispatcher wrapping what was there before, not the add-on's buffer
    assert after is before or getattr(after, "original", None) is before
    assert not isinstance(getattr(after, "original", after), io.StringIO)
    assert not sys.stdout.closed and not sys.stderr.closed
    (again,) = env.runner.on_file(Hook.MODIFIED, path, file)
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
    enable(env, "job", "clobber.run")
    path, file = add_file(env, "job/x.txt", b"new")

    (run,) = env.runner.on_file(Hook.ADDED, path, file)

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
    enable(env, "job", "loop.run")
    path, file = add_file(env, "job/a.txt")

    env.runner.on_file(Hook.ADDED, path, file)

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
    enable(env, "job", "weird.run")
    path, file = add_file(env, "job/a.txt")

    (run,) = env.runner.on_file(Hook.ADDED, path, file)

    assert run.status is RunStatus.FAILED
    assert "not JSON-serializable" in (run.error or "")
    assert env.runner.in_flight == {}
