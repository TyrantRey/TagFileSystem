# Code by AkinoAlice@TyrantRey

import json
import os
import socket
import sys
import textwrap
import time
from pathlib import Path

import pytest
from watchfiles import Change

from tag_file_system.addons.loader import MODULE_PREFIX
from tag_file_system.config import Config, DaemonConfig
from tag_file_system.core.interface.action import Hook, RunSource, RunStatus, Severity
from tag_file_system.database.action_store import ActionStore
from tag_file_system.database.sqlite import SQLiteBackend
from tag_file_system.root import FUNCTIONS_FILE, Lock, LockHeld, Root
from tag_file_system.services.daemon import Daemon

COPY = """
    from tag_file_system import action
    calls = []

    @action.added()
    def run(path, metadata, ctx, suffix: str = ".bak"):
        calls.append(("added", path.name))
        ctx.copy(path, ctx.root / "out" / (path.name + suffix))
        return path.name

    @action.modified()
    def changed(path, metadata, ctx, suffix: str = ".bak"):
        calls.append(("modified", path.name))

    @action.removed(on_move=True)
    def gone(path, metadata, ctx, suffix: str = ".bak"):
        calls.append(("removed", path.name))

    @action.tagged("hot")
    def on_hot(path, metadata, ctx):
        calls.append(("hot", path.name))
"""

# copy/ is where the copy add-on applies (DESIGN/v0-4-0.md §3): its file
# names every file handler; on_hot stays a global default.
COPY_FUNCTIONS = """
    version: 1
    functions:
      copy:
        run: {}
        changed: {}
        gone: {}
"""


@pytest.fixture(autouse=True)
def clean_modules():
    yield
    for name in [m for m in sys.modules if m.startswith(MODULE_PREFIX)]:
        del sys.modules[name]


def write_functions(root: Root, folder: str, text: str) -> Path:
    directory = root.path.joinpath(*folder.split("/")) if folder else root.path
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / FUNCTIONS_FILE
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def enable(root: Root, folder: str, script: str, *handlers: str) -> Path:
    """A folder file switching ``script``'s ``handlers`` on, no parameters."""
    lines = [f"    {h}: {{}}" for h in handlers]
    return write_functions(
        root, folder, "version: 1\nfunctions:\n  " + script + ":\n" + "\n".join(lines)
    )


@pytest.fixture
def root(tmp_path: Path) -> Root:
    root = Root.init(tmp_path / "vault")
    Config(
        daemon=DaemonConfig(run_warn_after_seconds=0.05, stop_timeout_seconds=0.2)
    ).write(root.config_path)
    (root.script_dir / "copy.py").write_text(textwrap.dedent(COPY), encoding="utf-8")
    write_functions(root, "copy", COPY_FUNCTIONS)
    return root


@pytest.fixture
def daemon(root: Root):
    d = Daemon(root)
    yield d
    d.shutdown()


def write(root: Root, key: str, content: str = "data") -> Path:
    path = root.absolute(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def added(*paths: Path) -> set:
    return {(Change.added, str(p)) for p in paths}


def calls(daemon: Daemon) -> list:
    addon = daemon.loader.addon_for("copy")
    assert addon is not None
    return addon.module.calls


def problems(daemon: Daemon, kind: str) -> list:
    return [p for p in daemon.store.query_problems() if p.kind == kind]


# ------------------------------------------------------------------ startup


def test_startup_locks_loads_and_reconciles(root: Root, daemon: Daemon):
    existing = write(root, "copy/pre--hot.txt", "pre")

    daemon.startup()

    assert daemon.started
    holder = Lock(root).holder()
    assert holder is not None and holder.is_mine()
    row = daemon.backend.query_file(existing)
    assert row is not None and [t.name for t in row.tags] == ["hot"]
    assert calls(daemon) == [("hot", "pre--hot.txt"), ("added", "pre--hot.txt")]
    assert (root.path / "out" / "pre--hot.txt.bak").exists()
    runs = daemon.store.query_runs()
    assert {r.source for r in runs} == {RunSource.RECONCILE}
    assert ["copy"] == sorted(daemon.functions.files)  # the tree was read
    assert daemon.backend.query_file(f"copy/{FUNCTIONS_FILE}") is None  # not data
    # the copy landed in the DB with provenance, and its own added-event is harmless
    daemon.process_changes(added(root.path / "out" / "pre--hot.txt.bak"))
    assert len(daemon.store.query_runs()) == len(runs)


def test_startup_refuses_when_locked_and_recovers_interrupted_runs(
    root: Root, tmp_path: Path
):
    other = Daemon(root)
    other.startup()
    # simulate a crash: a run left running, lock left behind by a dead pid
    action = other.store.register_action("copy", "script/copy.py", "h", {}, {})
    from tag_file_system.core.interface.action import RunKey

    stuck = other.store.start_run(
        action,
        RunKey(file_hash="x", action_name="copy", hook=Hook.ADDED),
        "copy",
        None,
        RunSource.WATCH,
    )
    other.backend.close()  # not shutdown(): the lock stays

    # another live process holds the lock: refused
    import subprocess

    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        foreign = json.loads(root.lock_path.read_text())
        foreign["pid"] = sleeper.pid
        foreign["hostname"] = socket.gethostname()
        root.lock_path.write_text(json.dumps(foreign))
        with pytest.raises(LockHeld):
            Daemon(root).startup()
    finally:
        sleeper.kill()
        sleeper.wait()

    dead = json.loads(root.lock_path.read_text())
    dead["pid"] = 2**31 - 1  # certainly not running
    dead["hostname"] = socket.gethostname()
    root.lock_path.write_text(json.dumps(dead))

    recovered = Daemon(root)
    recovered.startup()
    recovered_run = recovered.store.get_run(stuck.id)
    assert recovered_run is not None and recovered_run.status is RunStatus.INTERRUPTED
    assert problems(recovered, "run.interrupted")[0].severity is Severity.CRIT
    assert problems(recovered, "lock.stale")
    recovered.shutdown()
    assert not root.lock_path.exists()
    assert not recovered.backend.is_open


# ------------------------------------------------------------------- files


def test_file_lifecycle_through_events(root: Root, daemon: Daemon):
    write_functions(
        root,
        "orig",
        """
        version: 1
        functions:
          copy:
            run: {suffix: .orig}
            changed: {suffix: .orig}
            gone: {suffix: .orig}
        """,
    )
    daemon.startup()
    path = write(root, "orig/a--hot.txt", "v1")

    daemon.process_changes(added(path))
    assert calls(daemon) == [("hot", "a--hot.txt"), ("added", "a--hot.txt")]
    assert (root.path / "out" / "a--hot.txt.orig").exists()
    run = daemon.store.query_runs(action_name="copy", status=RunStatus.OK)[0]
    assert run.args == {"suffix": ".orig"} and run.source is RunSource.WATCH
    assert run.slug == 'copy.run(suffix=".orig")'

    # touched but unchanged: nothing
    os.utime(path)
    daemon.process_changes({(Change.modified, str(path))})
    assert len(calls(daemon)) == 2

    # content changed: modified handler, once
    path.write_text("v2")
    daemon.process_changes({(Change.modified, str(path))})
    daemon.process_changes({(Change.modified, str(path))})
    assert calls(daemon)[-1] == ("modified", "a--hot.txt")
    assert len(calls(daemon)) == 3

    # deleted: removed handler, row soft-deleted
    path.unlink()
    daemon.process_changes({(Change.deleted, str(path))})
    assert calls(daemon)[-1] == ("removed", "a--hot.txt")
    assert daemon.backend.query_file(path) is None
    assert daemon.backend.query_file(path, include_deleted=True) is not None


def test_move_keeps_identity_and_fires_only_on_move_handlers(
    root: Root, daemon: Daemon
):
    daemon.startup()
    old = write(root, "copy/a.txt", "same")
    daemon.process_changes(added(old))
    row = daemon.backend.query_file(old)
    assert row is not None
    runs_before = len(daemon.store.query_runs())

    new = root.absolute("plain/a.txt")
    new.parent.mkdir()
    old.rename(new)
    daemon.process_changes({(Change.deleted, str(old)), (Change.added, str(new))})

    moved = daemon.backend.query_file(new)
    assert moved is not None and moved.file_id == row.file_id  # same row, new key
    assert daemon.backend.query_file(old, include_deleted=True) is None
    assert calls(daemon)[-1] == ("removed", "a.txt")  # left copy/'s scope
    assert len(daemon.store.query_runs()) == runs_before + 1  # only the removed run
    assert (
        daemon.backend.connection.execute(
            "SELECT COUNT(*) FROM events WHERE name = 'file.move'"
        ).fetchone()[0]
        == 1
    )


def test_directory_rename_rescans_the_subtree(root: Root, daemon: Daemon):
    daemon.startup()
    old_dir = root.absolute("2024--trip")
    old_dir.mkdir()
    a = write(root, "2024--trip/a.txt", "A")
    daemon.process_changes(added(old_dir))
    assert [t.name for t in daemon.backend.query_file(a).tags] == ["trip"]

    new_dir = root.absolute("copy/2024--trip--hot")
    old_dir.rename(new_dir)
    daemon.process_changes(
        {(Change.deleted, str(old_dir)), (Change.added, str(new_dir))}
    )

    assert daemon.backend.query_file(a) is None
    moved = daemon.backend.query_file(new_dir / "a.txt")
    assert moved is not None
    assert sorted(t.name for t in moved.tags) == ["hot", "trip"]
    assert ("hot", "a.txt") in calls(daemon) and ("added", "a.txt") in calls(daemon)
    assert ("removed", "a.txt") not in calls(daemon)  # a dir move is not a deletion


def test_reconcile_is_idempotent_and_notices_missing_files(root: Root, daemon: Daemon):
    daemon.startup()
    a = write(root, "copy/a.txt", "A")
    b = write(root, "keep/b--hot.txt", "B")

    first = daemon.reconcile()
    assert {i.file.path.as_posix() for i in first.indexed} >= {
        "copy/a.txt",
        "keep/b--hot.txt",
    }
    assert first.hashed >= 2
    runs = len(daemon.store.query_runs())

    second = daemon.reconcile()
    assert second.hashed == 0  # size+mtime fast path
    assert len(daemon.store.query_runs()) == runs

    a.unlink()
    third = daemon.reconcile()
    assert [r.path.as_posix() for r in third.removed] == ["copy/a.txt"]
    assert calls(daemon)[-1] == ("removed", "a.txt")
    assert daemon.backend.query_file(b) is not None


def test_files_produced_during_a_reconcile_are_not_retired(root: Root, daemon: Daemon):
    """``out/`` did not exist when the walk listed the root, so the copy the
    run makes is never walked: it is still not gone, nor a move of its
    source (its hash is the source's)."""
    daemon.startup()
    write(root, "copy/a.txt", "A")

    report = daemon.reconcile()

    assert report.moved == [] and report.removed == []
    assert daemon.backend.query_file("out/a.txt.bak") is not None
    again = daemon.reconcile()
    assert again.moved == [] and again.removed == []
    assert "out/a.txt.bak" in {i.file.path.as_posix() for i in again.indexed}


# ---------------------------------------------------------- configuration


def test_a_changed_functions_file_is_drift_until_reload(root: Root, daemon: Daemon):
    daemon.startup()
    a = write(root, "copy/a.txt", "A")
    daemon.process_changes(added(a))
    assert calls(daemon) == [("added", "a.txt")]

    config = write_functions(
        root,
        "copy",
        """
        version: 1
        functions:
          copy:
            run: {suffix: .new}
        """,
    )
    daemon.process_changes({(Change.modified, str(config))})
    daemon.process_changes({(Change.modified, str(config))})

    (drift,) = problems(daemon, "functions.drift")
    assert drift.severity is Severity.WARN and f"copy/{FUNCTIONS_FILE}" in drift.message
    assert daemon.backend.query_file(config) is None  # never indexed
    b = write(root, "copy/b.txt", "B")
    daemon.process_changes(added(b))
    (run,) = daemon.store.query_runs(file_path=b)
    assert run.args == {}  # the old configuration still applies
    assert daemon.reconcile().indexed  # a reconcile does not re-read it either
    assert len(problems(daemon, "functions.drift")) == 1  # said once

    result = daemon.reload()

    assert result["functions"] == {"files": 1, "problems": 0}
    # the new entry reaches the files it covers (§6): same content, new args
    assert [r.args for r in daemon.store.query_runs(file_path=b)] == [
        {"suffix": ".new"},
        {},
    ]
    assert (root.path / "out" / "b.txt.new").exists()
    # and the entries that were dropped fire nothing
    assert ("removed", "a.txt") not in calls(daemon)
    daemon.process_changes({(Change.modified, str(config))})
    assert len(problems(daemon, "functions.drift")) == 2  # news again after a reload


def test_a_broken_functions_file_keeps_the_last_good_one(root: Root, daemon: Daemon):
    daemon.startup()
    config = write_functions(
        root, "copy", "version: 1\nfunctions:\n  copy:\n    run: {\n"
    )

    result = daemon.reload()

    assert result["functions"]["problems"] == 1
    (parse,) = problems(daemon, "functions.parse")
    assert parse.severity is Severity.ERR
    assert [p["kind"] for p in daemon.load_problems()] == ["functions.parse"]
    a = write(root, "copy/a.txt", "A")
    daemon.process_changes(added(a))
    assert calls(daemon) == [("added", "a.txt")]  # the previous file still applies
    config.write_text("version: 1\nfunctions: {}\n", encoding="utf-8")
    daemon.reload()  # a fresh module: `calls` starts empty again
    assert daemon.load_problems() == []
    b = write(root, "copy/b.txt", "B")
    daemon.process_changes(added(b))
    assert calls(daemon) == []  # switched off now
    assert daemon.store.query_runs(file_path=b) == []


def test_explain_reports_the_effective_configuration(root: Root, daemon: Daemon):
    write_functions(
        root,
        "",
        """
        version: 1
        functions:
          copy:
            changed: {exclude: [{tag: hot}]}
            on_hot: {}
        """,
    )
    daemon.startup()
    a = write(root, "copy/a--hot.txt", "A")
    daemon.process_changes(added(a))

    payload = daemon.explain("copy/a--hot.txt")

    assert payload["known"] is True and payload["tags"] == ["hot"]
    assert [(e["display"], e["file"]) for e in payload["applied"]] == [
        ("copy.on_hot()", FUNCTIONS_FILE),
        ("copy.run()", f"copy/{FUNCTIONS_FILE}"),
        ("copy.changed()", f"copy/{FUNCTIONS_FILE}"),
        ("copy.gone()", f"copy/{FUNCTIONS_FILE}"),
    ]
    assert [(e["display"], e["reason"]) for e in payload["suppressed"]] == [
        ("copy.changed()", "tag hot")
    ]
    assert payload["defaults"] == [
        {
            "script": "copy",
            "handler": "on_hot",
            "tag": "hot",
            "suppressed_by": FUNCTIONS_FILE,
        }
    ]
    assert calls(daemon) == [("hot", "a--hot.txt"), ("added", "a--hot.txt")]
    (hot,) = [r for r in daemon.store.query_runs() if r.hook is Hook.TAGGED]
    assert hot.slug == 'copy.on_hot(tag="hot")'  # configured at the root, not a default

    unknown = daemon.explain("copy/new--hot.txt")
    assert unknown["known"] is False and unknown["tags"] == ["hot"]
    assert len(unknown["applied"]) == 4


# ------------------------------------------------------------------- zones


def test_tfs_changes_are_ignored_and_scripts_hot_reload(root: Root, daemon: Daemon):
    daemon.startup()
    rows = len(daemon.backend.query_files(include_deleted=True))

    daemon.process_changes(added(root.db_path, root.tfs_dir / "db" / "system.db-wal"))
    assert len(daemon.backend.query_files(include_deleted=True)) == rows

    script = root.script_dir / "copy.py"
    script.write_text(
        textwrap.dedent(COPY).replace("return path.name", 'return "v2"'),
        encoding="utf-8",
    )
    daemon.process_changes({(Change.modified, str(script))})
    path = write(root, "copy/n.txt", "n")
    daemon.process_changes(added(path))
    assert (
        daemon.store.query_runs(action_name="copy", status=RunStatus.OK)[0].result
        == "v2"
    )

    helper = root.script_dir / "_h.py"
    helper.write_text("V = 1\n")
    daemon.process_changes(added(helper))  # helpers reload everything, no crash

    script.unlink()
    daemon.process_changes({(Change.deleted, str(script))})
    assert daemon.loader.addon_for("copy") is None
    assert [p["kind"] for p in daemon.load_problems()] == ["functions.unbound"] * 3
    other = write(root, "copy/m.txt", "m")
    daemon.process_changes(added(other))
    assert daemon.store.query_runs(file_path=other) == []
    assert problems(daemon, "functions.unbound")


def test_observed_changes_are_attributed_to_in_flight_runs(root: Root, daemon: Daemon):
    (root.script_dir / "bg.py").write_text(
        textwrap.dedent(
            """
            import threading
            from tag_file_system import action
            gate = threading.Event()

            @action.added()
            def run(path, metadata, ctx):
                def wait():
                    gate.wait(5)
                    ctx.done()
                ctx.spawn(wait)
            """
        ),
        encoding="utf-8",
    )
    enable(root, "bg", "bg", "run")
    daemon.startup()
    trigger = write(root, "bg/a.txt", "a")
    daemon.process_changes(added(trigger))
    (run,) = daemon.store.query_runs(action_name="bg")
    assert run.status is RunStatus.RUNNING

    stray = write(root, "plain/user-edit.txt", "x")
    daemon.process_changes(added(stray))

    edges = daemon.store.query_provenance(file_path=stray)
    assert [(e.run_id, e.kind.value, e.ambiguous) for e in edges] == [
        (run.id, "observed", False)
    ]
    assert problems(daemon, "observed")
    time.sleep(0.1)
    daemon.tick()
    assert problems(daemon, "run.overdue")

    bg = daemon.loader.addon_for("bg")
    assert bg is not None
    bg.module.gate.set()
    deadline = time.time() + 5

    def status() -> RunStatus:
        current = daemon.store.get_run(run.id)
        assert current is not None
        return current.status

    while status() is RunStatus.RUNNING and time.time() < deadline:
        time.sleep(0.01)
    assert status() is RunStatus.OK


def test_unreadable_file_is_a_problem_not_a_crash(
    root: Root, daemon: Daemon, monkeypatch: pytest.MonkeyPatch
):
    daemon.startup()
    busy = write(root, "copy/busy.txt", "b")
    other = write(root, "copy/other.txt", "o")
    from tag_file_system.services import indexer as indexer_module

    real = indexer_module.compute_file_hash

    def flaky(path: Path, *args, **kwargs):
        if path.name == "busy.txt":
            raise PermissionError("sharing violation")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(indexer_module, "compute_file_hash", flaky)

    daemon.process_changes(added(busy, other))

    assert daemon.backend.query_file(other) is not None
    assert daemon.backend.query_file(busy) is None
    assert problems(daemon, "file.unreadable")[0].severity is Severity.ERR


def test_deleted_event_for_an_existing_file_is_an_add(root: Root, daemon: Daemon):
    daemon.startup()
    path = write(root, "copy/a.txt", "v1")
    daemon.process_changes(added(path))

    # an editor's atomic save: delete + add in one batch, the file is there
    daemon.process_changes({(Change.deleted, str(path)), (Change.added, str(path))})

    row = daemon.backend.query_file(path)
    assert row is not None and row.status == "active"
    assert ("removed", "a.txt") not in calls(daemon)


def test_added_event_for_a_known_file_is_a_modification(root: Root, daemon: Daemon):
    daemon.startup()
    path = write(root, "copy/a.txt", "v1")
    daemon.process_changes(added(path))

    daemon.process_changes(added(path))  # unchanged: nothing
    assert calls(daemon) == [("added", "a.txt")]

    path.write_text("v2")  # write-tmp-and-rename editors report "added"
    daemon.process_changes({(Change.added, str(path)), (Change.deleted, str(path))})
    assert calls(daemon) == [("added", "a.txt"), ("modified", "a.txt")]


def test_move_takes_the_new_names_tags(root: Root, daemon: Daemon):
    daemon.startup()
    old = write(root, "2024--trip/a--old.txt", "same")
    daemon.process_changes(added(old))
    new = root.absolute("plain/a--hot.txt")
    new.parent.mkdir()
    old.rename(new)

    daemon.process_changes({(Change.deleted, str(old)), (Change.added, str(new))})

    moved = daemon.backend.query_file(new)
    assert moved is not None and [t.name for t in moved.tags] == ["hot"]
    assert calls(daemon) == [("hot", "a--hot.txt")]  # the default, gained by the move


def test_own_trigger_file_is_not_observed_by_its_run(root: Root, daemon: Daemon):
    (root.script_dir / "bg.py").write_text(
        "import threading\nfrom tag_file_system import action\ngate = threading.Event()\n"
        "@action.added()\ndef run(p, m, c):\n    c.spawn(lambda: (gate.wait(5), c.done()))\n",
        encoding="utf-8",
    )
    enable(root, "bg", "bg", "run")
    daemon.startup()
    trigger = write(root, "bg/a.txt", "a")

    daemon.process_changes(added(trigger))

    assert daemon.store.query_provenance(file_path=trigger) == []
    assert problems(daemon, "observed") == []
    daemon.loader.addon_for("bg").module.gate.set()  # type: ignore[union-attr]


@pytest.mark.skipif(sys.platform != "win32", reason="case-insensitive filesystem")
def test_case_only_rename_keeps_one_row(root: Root, daemon: Daemon):
    daemon.startup()
    lower = write(root, "d/a.txt", "x")
    daemon.process_changes(added(lower))
    upper = root.absolute("d/A.txt")
    lower.rename(upper)

    daemon.process_changes({(Change.deleted, str(lower)), (Change.added, str(upper))})

    rows = daemon.backend.query_files(path_prefix="d")
    assert [r.path.as_posix() for r in rows] == ["d/A.txt"]


def test_move_onto_a_soft_deleted_row(root: Root, daemon: Daemon):
    daemon.startup()
    a = write(root, "p/a.txt", "one")
    daemon.process_changes(added(a))
    a.unlink()
    daemon.process_changes({(Change.deleted, str(a))})
    b = write(root, "p/b.txt", "two")
    daemon.process_changes(added(b))
    b.rename(a)

    daemon.process_changes({(Change.deleted, str(b)), (Change.added, str(a))})

    assert daemon.backend.query_file(b) is None
    assert daemon.backend.query_file(a) is not None
    assert [r.path.as_posix() for r in daemon.backend.query_files(path_prefix="p")] == [
        "p/a.txt"
    ]


def test_startup_twice_is_refused(root: Root, daemon: Daemon):
    daemon.startup()
    with pytest.raises(RuntimeError):
        daemon.startup()


def test_reconcile_never_indexes_tfs_script_or_functions_files(
    root: Root, daemon: Daemon
):
    daemon.startup()
    nested = root.absolute("sub/.tfs/inner.txt")
    nested.parent.mkdir(parents=True)
    nested.write_text("x")
    stray = write_functions(root, "sub", "version: 1\nfunctions: {}\n")

    report = daemon.reconcile()
    assert daemon.reconcile(root.script_dir).indexed == []
    assert daemon.reconcile(root.tfs_dir).indexed == []

    assert daemon.backend.query_file(nested) is None
    assert daemon.backend.query_file(stray) is None
    assert stray.name not in {i.file.path.name for i in report.indexed}
    assert daemon.backend.query_files(path_prefix="script") == []
    # a file that appeared since the load is drift, not data
    assert [p.kind for p in daemon.store.query_problems()][-1] == "functions.drift"


def test_watch_filter_keeps_dotfiles_and_drops_tfs(root: Root, daemon: Daemon):
    assert daemon._watch_filter(Change.added, str(root.path / ".git" / "config"))
    assert daemon._watch_filter(Change.added, str(root.path / "node_modules" / "x.txt"))
    assert daemon._watch_filter(Change.added, str(root.path / "notes.txt~"))
    assert daemon._watch_filter(Change.added, str(root.path / "copy" / FUNCTIONS_FILE))
    assert not daemon._watch_filter(Change.added, str(root.db_path))
    assert not daemon._watch_filter(Change.added, str(root.path.parent / "outside.txt"))


def test_force_never_displaces_a_live_local_daemon(root: Root):
    import subprocess

    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        first = Daemon(root)
        first.startup()
        info = json.loads(root.lock_path.read_text())
        first.backend.close()  # the lock stays, now "held" by the live sleeper
        root.lock_path.write_text(json.dumps({**info, "pid": sleeper.pid}))

        with pytest.raises(LockHeld):
            Daemon(root).startup(force=True)
        assert json.loads(root.lock_path.read_text())["pid"] == sleeper.pid

        # ...but a live lock from another host is exactly what --force is for
        root.lock_path.write_text(
            json.dumps({**info, "pid": sleeper.pid, "hostname": "other-nas"})
        )
        forced = Daemon(root)
        forced.startup(force=True)
        assert problems(forced, "lock.overridden")[0].severity is Severity.CRIT
        forced.shutdown()
    finally:
        sleeper.kill()
        sleeper.wait()


def test_the_lock_records_the_control_port(root: Root, tmp_path: Path):
    import socket as socket_module

    with socket_module.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    Config(daemon=DaemonConfig(port=port, stop_timeout_seconds=0.5)).write(
        root.config_path
    )
    daemon = Daemon(root, control=True, ui_dir=root.path.parent / "no-dist")
    daemon.startup()
    try:
        holder = Lock(root).holder()
        assert holder is not None and holder.port == port
    finally:
        daemon.shutdown()

    plain = Daemon(root)  # no control channel: nothing to record
    plain.startup()
    try:
        holder = Lock(root).holder()
        assert holder is not None and holder.port is None
    finally:
        plain.shutdown()


def test_edits_and_deletions_are_observed_while_a_run_is_in_flight(
    root: Root, daemon: Daemon
):
    (root.script_dir / "bg.py").write_text(
        "import threading\nfrom tag_file_system import action\ngate = threading.Event()\n"
        "@action.added()\ndef run(p, m, c):\n    c.write(c.root / 'out' / 'g.txt', 'g')\n"
        "    c.spawn(lambda: (gate.wait(5), c.done()))\n",
        encoding="utf-8",
    )
    enable(root, "bg", "bg", "run")
    daemon.startup()
    daemon.process_changes(added(write(root, "bg/a.txt", "a")))
    (run,) = daemon.store.query_runs(action_name="bg")
    emitted = root.absolute("out/g.txt")
    victim = write(root, "plain/v.txt", "v")
    daemon.process_changes(added(victim))

    emitted.write_text("edited by the user")
    daemon.process_changes({(Change.modified, str(emitted))})
    victim.unlink()
    daemon.process_changes({(Change.deleted, str(victim))})

    kinds = {p.kind.value for p in daemon.store.query_provenance(file_path=emitted)}
    assert kinds == {"emitted"}  # the emitter never observes its own output
    deleted_edges = daemon.store.query_provenance(
        file_path=victim, include_deleted=True
    )
    assert [(e.run_id, e.kind.value) for e in deleted_edges] == [(run.id, "observed")]
    daemon.loader.addon_for("bg").module.gate.set()  # type: ignore[union-attr]


def test_reconcile_matches_moves_by_hash(root: Root, daemon: Daemon):
    daemon.startup()
    old = write(root, "copy/moved.txt", "content")
    daemon.process_changes(added(old))
    runs_before = len(daemon.store.query_runs())
    new = root.absolute("elsewhere/moved.txt")
    new.parent.mkdir()
    old.rename(new)  # while the daemon "was down"

    report = daemon.reconcile()

    assert [r.path.as_posix() for r in report.moved] == ["copy/moved.txt"]
    assert report.removed == []
    assert calls(daemon)[-1] == ("removed", "moved.txt")  # on_move handler
    assert len(daemon.store.query_runs()) == runs_before + 1


def test_batch_failure_is_reported_and_the_loop_survives(
    root: Root, monkeypatch: pytest.MonkeyPatch
):
    import threading

    daemon = Daemon(root, poll_ms=50)
    daemon.startup()
    calls_seen: list[int] = []
    original = daemon.process_changes

    def flaky(changes, source=RunSource.WATCH):
        calls_seen.append(1)
        if len(calls_seen) == 1:
            raise RuntimeError("boom")
        return original(changes, source)

    monkeypatch.setattr(daemon, "process_changes", flaky)
    thread = threading.Thread(target=daemon.run_forever, daemon=True)
    thread.start()
    time.sleep(0.3)
    write(root, "copy/one.txt", "1")
    deadline = time.time() + 10
    while time.time() < deadline and not problems(daemon, "batch.failed"):
        time.sleep(0.05)
    write(root, "copy/two.txt", "2")
    while time.time() < deadline and ("added", "two.txt") not in calls(daemon):
        time.sleep(0.05)
    batch_failed = problems(daemon, "batch.failed")  # read before the DB closes
    survived = ("added", "two.txt") in calls(daemon)
    daemon.request_stop()
    thread.join(10)

    assert batch_failed and batch_failed[0].severity is Severity.CRIT
    assert survived


def test_shutdown_interrupts_and_releases(root: Root):
    (root.script_dir / "stuck.py").write_text(
        "import threading\nfrom tag_file_system import action\n@action.added()\ndef run(p, m, c):\n    c.spawn(lambda: threading.Event().wait(30))\n",
        encoding="utf-8",
    )
    enable(root, "stuck", "stuck", "run")
    daemon = Daemon(root)
    daemon.startup()
    daemon.process_changes(added(write(root, "stuck/a.txt", "a")))

    daemon.shutdown()

    assert not root.lock_path.exists()
    assert not daemon.backend.is_open
    backend = SQLiteBackend()
    backend.init_database(root.db_path, root_dir=root.path)
    store = ActionStore(backend)
    assert [r.status for r in store.query_runs(action_name="stuck")] == [
        RunStatus.INTERRUPTED
    ]
    assert [
        p.severity for p in store.query_problems() if p.kind == "run.interrupted"
    ] == [Severity.CRIT]
    backend.close()


SERVICE = """
    from tag_file_system import action
    events = []

    @action.on_start()
    def up(ctx):
        events.append("start")

    @action.on_stop()
    def down(ctx):
        events.append("stop")
"""


def events(daemon: Daemon, name: str = "service") -> list:
    addon = daemon.loader.addon_for(name)
    return addon.module.events if addon is not None else []


def lifecycle_runs(daemon: Daemon, hook: Hook) -> list:
    return [r for r in daemon.store.query_runs() if r.hook is hook]


def test_on_start_and_on_stop_bracket_the_session(root: Root):
    (root.script_dir / "service.py").write_text(
        textwrap.dedent(SERVICE).replace(
            'events.append("start")',
            'events.append("start"); events.append(len(ctx.query()))',
        ),
        encoding="utf-8",
    )
    write(root, "copy/a.txt", "a")
    daemon = Daemon(root)

    daemon.startup()

    # before any file was looked at, and once only — but after the
    # configuration was read (§7 of v0-4-0: an on_start may look at it)
    assert events(daemon) == ["start", 0]
    assert sorted(daemon.functions.files) == ["copy"]
    assert calls(daemon) == [("added", "a.txt")]
    daemon.reload()  # a fresh module, but the same session: no second on_start
    assert events(daemon) == [] and len(lifecycle_runs(daemon, Hook.ON_START)) == 1
    (started,) = lifecycle_runs(daemon, Hook.ON_START)
    assert started.status is RunStatus.OK and started.source is RunSource.LIFECYCLE

    daemon.shutdown()

    assert events(daemon) == ["stop"]
    backend = SQLiteBackend()
    backend.init_database(root.db_path, root_dir=root.path)
    store = ActionStore(backend)
    (stopped,) = [r for r in store.query_runs() if r.hook is Hook.ON_STOP]
    assert stopped.status is RunStatus.OK and stopped.started_at >= started.started_at
    backend.close()


def test_a_script_added_while_running_gets_its_on_start(root: Root, daemon: Daemon):
    daemon.startup()

    script = root.script_dir / "service.py"
    script.write_text(textwrap.dedent(SERVICE), encoding="utf-8")
    daemon.process_changes(added(script))

    assert events(daemon) == ["start"]
    # an edit reloads the add-on but does not start a second session for it
    script.write_text(
        textwrap.dedent(SERVICE).replace('"stop"', '"restop"'), encoding="utf-8"
    )
    daemon.process_changes({(Change.modified, str(script))})
    assert events(daemon) == [] and len(lifecycle_runs(daemon, Hook.ON_START)) == 1

    daemon.shutdown()
    assert events(daemon) == ["restop"]


def test_a_service_run_does_not_observe_every_change(root: Root):
    (root.script_dir / "service.py").write_text(
        "import threading\n"
        "from tag_file_system import action\n"
        "@action.on_start()\n"
        "def up(ctx):\n"
        "    ctx.spawn(lambda: threading.Event().wait(30))\n",
        encoding="utf-8",
    )
    daemon = Daemon(root)
    daemon.startup()
    assert daemon.runner.in_flight  # the service run is in flight all session

    daemon.process_changes(added(write(root, "copy/a.txt", "a")))

    assert not problems(daemon, "observed")
    assert not daemon.store.query_provenance(file_path="copy/a.txt")
    daemon.shutdown()


def test_a_daemon_that_never_started_runs_no_lifecycle_hooks(root: Root):
    (root.script_dir / "service.py").write_text(
        textwrap.dedent(SERVICE), encoding="utf-8"
    )
    daemon = Daemon(root)
    daemon._load()
    assert not [r for r in daemon.store.query_runs() if r.hook.is_lifecycle]

    daemon.shutdown()

    assert events(daemon) == []


def test_run_forever_stops_on_request(root: Root):
    import threading

    daemon = Daemon(root, poll_ms=50)
    daemon.startup()
    thread = threading.Thread(target=daemon.run_forever, daemon=True)
    thread.start()
    time.sleep(0.2)
    path = write(root, "copy/live.txt", "live")
    deadline = time.time() + 10
    while time.time() < deadline and not calls(daemon):
        time.sleep(0.05)

    daemon.request_stop()
    thread.join(10)

    assert not thread.is_alive()
    assert ("added", "live.txt") in calls(daemon)
    assert not root.lock_path.exists()
    assert not daemon.backend.is_open
    assert path.exists()
