# Code by AkinoAlice@TyrantRey

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from tag_file_system.core.interface.action import (
    ActionRecord,
    Hook,
    ProvenanceKind,
    RunKey,
    RunSource,
    RunStatus,
    Severity,
    TraceKind,
    canonical_json,
)
from tag_file_system.database.action_store import (
    ActionStore,
    RunAlreadyFinal,
    RunExists,
)
from tag_file_system.database.sqlite import SQLiteBackend


@pytest.fixture
def store(backend: SQLiteBackend) -> ActionStore:
    return ActionStore(backend)


@pytest.fixture
def action(store: ActionStore) -> ActionRecord:
    return store.register_action(
        name="make_copy",
        script_path="script/make_copy.py",
        script_hash="abc",
        signature={"properties": {"suffix": {"type": "string"}}},
        hooks=[Hook.ADDED, Hook.REMOVED],
    )


def add_file(backend: SQLiteBackend, key: str, file_hash: str = "h") -> str:
    result = backend.insert(
        filename=PurePosixPath(key).name,
        file_path=key,
        file_hash=file_hash,
        file_size=1,
    )
    assert result.record_id is not None
    return result.record_id


def key_for(file_hash: str = "h", **args) -> RunKey:
    return RunKey(
        file_hash=file_hash, action_name="make_copy", hook=Hook.ADDED, args=args
    )


# ---------------------------------------------------------------- severity


def test_severity_levels_cover_themselves_and_above():
    assert Severity.WARN.covers(Severity.CRIT)
    assert Severity.WARN.covers(Severity.ERR)
    assert Severity.WARN.covers(Severity.WARN)
    assert not Severity.WARN.covers(Severity.INFO)
    assert Severity.INFO.covers(Severity.INFO)
    assert not Severity.CRIT.covers(Severity.ERR)


# ----------------------------------------------------------------- actions


def test_register_action_upserts_by_name_and_hash(
    store: ActionStore, action: ActionRecord
):
    assert action.name == "make_copy"
    assert action.script_path == PurePosixPath("script/make_copy.py")
    assert action.hooks == [Hook.ADDED, Hook.REMOVED]
    assert action.signature == {"properties": {"suffix": {"type": "string"}}}

    same = store.register_action(
        "make_copy", "script/make_copy.py", "abc", {"changed": True}, [Hook.ADDED]
    )
    assert same.id == action.id
    assert same.signature == {"changed": True}
    assert same.hooks == [Hook.ADDED]
    assert same.loaded_at >= action.loaded_at

    newer = store.register_action("make_copy", "script/make_copy.py", "def", {}, [])
    assert newer.id != action.id
    assert store.latest_action("make_copy") is not None
    assert store.latest_action("make_copy").id == newer.id
    assert [a.script_hash for a in store.query_actions("make_copy")] == ["def", "abc"]
    assert store.latest_action("nope") is None
    assert store.get_action("nope") is None
    assert len(store.query_actions()) == 2


# -------------------------------------------------------------------- runs


def test_run_lifecycle(
    store: ActionStore, action: ActionRecord, backend: SQLiteBackend
):
    file_id = add_file(backend, "@@make_copy__.jpg/a.jpg")
    key = key_for(suffix=".jpg", dst="photos")

    assert store.find_run(key) is None
    run = store.start_run(
        action,
        key,
        slug="make_copy__.jpg__photos",
        file_path="@@make_copy__.jpg/a.jpg",
        source=RunSource.WATCH,
    )

    assert run.status is RunStatus.RUNNING
    assert run.file_id == file_id
    assert run.args == {"suffix": ".jpg", "dst": "photos"}
    assert run.slug == "make_copy__.jpg__photos"
    assert run.finished_at is None
    assert run.key == key
    assert store.find_run(key) == run

    done = store.finish_run(run.id, RunStatus.OK, result={"copied_to": "photos/a.jpg"})
    assert done.status is RunStatus.OK
    assert done.result == {"copied_to": "photos/a.jpg"}
    assert done.finished_at is not None
    assert store.find_run(key) == done
    row = backend.connection.execute(
        "SELECT args_json, result_json FROM action_runs WHERE id = ?", (run.id,)
    ).fetchone()
    assert row["args_json"] == '{"dst":"photos","suffix":".jpg"}'  # canonical
    assert json.loads(row["result_json"]) == {"copied_to": "photos/a.jpg"}


def test_run_key_is_canonical_and_hook_aware(store: ActionStore, action: ActionRecord):
    a = RunKey(
        file_hash="h", action_name="make_copy", hook=Hook.ADDED, args={"x": 1, "y": 2}
    )
    b = RunKey(
        file_hash="h", action_name="make_copy", hook=Hook.ADDED, args={"y": 2, "x": 1}
    )
    assert a.args_json == b.args_json == '{"x":1,"y":2}'

    store.start_run(action, a, "make_copy__1__2", None, RunSource.RECONCILE)
    assert store.find_run(b) is not None
    removed = RunKey(
        file_hash="h", action_name="make_copy", hook=Hook.REMOVED, args={"x": 1, "y": 2}
    )
    assert store.find_run(removed) is None  # same hash+args, different hook


def test_failed_and_retried_runs(store: ActionStore, action: ActionRecord):
    key = key_for()
    first = store.start_run(action, key, "make_copy", None, RunSource.WATCH)
    with pytest.raises(RunExists):  # a running key cannot be started again
        store.start_run(action, key, "make_copy", None, RunSource.WATCH)
    failed = store.finish_run(first.id, RunStatus.FAILED, error="boom")
    assert failed.error == "boom"
    assert store.find_run(key) is not None  # dead key stays dead
    with pytest.raises(RunExists) as exc:
        store.start_run(action, key, "make_copy", None, RunSource.WATCH)
    assert exc.value.existing.id == first.id

    retry = store.start_run(
        action, key, "make_copy", None, RunSource.RETRY, retry_of=first.id
    )
    assert retry.retry_of == first.id
    assert store.find_run(key) == retry  # newest wins
    with pytest.raises(RunExists):  # the retry is running: no second retry yet
        store.start_run(
            action, key, "make_copy", None, RunSource.RETRY, retry_of=first.id
        )

    chained = store.start_run(
        action,
        key_for(file_hash="child"),
        "make_copy",
        None,
        RunSource.CHAIN,
        parent_run_id=retry.id,
    )
    assert chained.parent_run_id == retry.id

    with pytest.raises(ValueError):  # retry_of must share the key
        store.start_run(
            action, key_for("other"), "x", None, RunSource.RETRY, retry_of=first.id
        )
    with pytest.raises(KeyError):
        store.start_run(
            action, key_for("o2"), "x", None, RunSource.RETRY, retry_of="nope"
        )
    with pytest.raises(KeyError):
        store.start_run(
            action, key_for("o3"), "x", None, RunSource.CHAIN, parent_run_id="nope"
        )
    with pytest.raises(ValueError):
        store.start_run(
            action, key_for("o4"), "x", None, RunSource.WATCH, status=RunStatus.OK
        )


def test_finish_run_validates(store: ActionStore, action: ActionRecord):
    run = store.start_run(action, key_for(), "make_copy", None, RunSource.WATCH)
    with pytest.raises(ValueError):
        store.finish_run(run.id, RunStatus.RUNNING)
    with pytest.raises(KeyError):
        store.finish_run("nope", RunStatus.OK)
    with pytest.raises(KeyError):
        store.add_trace("nope", TraceKind.LOG, "x")
    with pytest.raises(KeyError):
        store.record_problem(Severity.ERR, "k", "m", run_id="nope")


def test_finished_runs_are_immutable(store: ActionStore, action: ActionRecord):
    run = store.start_run(action, key_for(), "make_copy", None, RunSource.WATCH)
    store.finish_run(run.id, RunStatus.OK, result={"a": 1})

    with pytest.raises(RunAlreadyFinal):
        store.finish_run(run.id, RunStatus.FAILED, error="late")
    final = store.get_run(run.id)
    assert (
        final is not None and final.status is RunStatus.OK and final.result == {"a": 1}
    )

    other = store.start_run(action, key_for("2"), "make_copy", None, RunSource.WATCH)
    store.mark_interrupted([other.id])
    with pytest.raises(RunAlreadyFinal):
        store.finish_run(other.id, RunStatus.OK)


def test_query_runs_filters(
    store: ActionStore, action: ActionRecord, backend: SQLiteBackend
):
    add_file(backend, "a.txt", "ha")
    add_file(backend, "b.txt", "hb")
    ra = store.start_run(action, key_for("ha"), "make_copy", "a.txt", RunSource.WATCH)
    rb = store.start_run(action, key_for("hb"), "make_copy", "b.txt", RunSource.WATCH)
    store.finish_run(ra.id, RunStatus.OK)
    store.finish_run(rb.id, RunStatus.FAILED, error="x")

    assert [r.id for r in store.query_runs()] == [rb.id, ra.id]  # newest first
    assert [r.id for r in store.query_runs(file_path="a.txt")] == [ra.id]
    assert [r.id for r in store.query_runs(file_hash="hb")] == [rb.id]
    assert [r.id for r in store.query_runs(status=RunStatus.FAILED)] == [rb.id]
    assert len(store.query_runs(status=[RunStatus.OK, RunStatus.FAILED])) == 2
    assert store.query_runs(action_name="other") == []
    assert store.query_runs(file_path="missing.txt") == []
    assert len(store.query_runs(limit=1)) == 1
    future = datetime.now(UTC) + timedelta(days=1)
    assert store.query_runs(since=future) == []
    assert len(store.query_runs(since=datetime.now(UTC) - timedelta(days=1))) == 2


def test_mark_interrupted(store: ActionStore, action: ActionRecord):
    running = store.start_run(action, key_for("1"), "make_copy", None, RunSource.WATCH)
    queued = store.start_run(
        action,
        key_for("2"),
        "make_copy",
        None,
        RunSource.WATCH,
        status=RunStatus.QUEUED,
    )
    done = store.start_run(action, key_for("3"), "make_copy", None, RunSource.WATCH)
    store.finish_run(done.id, RunStatus.OK)

    interrupted = store.mark_interrupted()

    assert {r.id for r in interrupted} == {running.id, queued.id}
    assert all(r.status is RunStatus.INTERRUPTED and r.finished_at for r in interrupted)
    assert all("interrupted" in (r.error or "") for r in interrupted)
    assert store.get_run(done.id).status is RunStatus.OK
    assert store.mark_interrupted() == []

    again = store.start_run(action, key_for("4"), "make_copy", None, RunSource.WATCH)
    other = store.start_run(action, key_for("5"), "make_copy", None, RunSource.WATCH)
    assert [r.id for r in store.mark_interrupted([again.id])] == [again.id]
    assert store.get_run(other.id).status is RunStatus.RUNNING
    assert store.mark_interrupted([]) == []


# ------------------------------------------------------------------- trace


def test_trace_is_ordered_per_run(store: ActionStore, action: ActionRecord):
    run = store.start_run(action, key_for(), "make_copy", None, RunSource.WATCH)
    other = store.start_run(action, key_for("o"), "make_copy", None, RunSource.WATCH)

    first = store.add_trace(run.id, TraceKind.LOG, "starting")
    store.add_trace(other.id, TraceKind.LOG, "elsewhere")
    second = store.add_trace(run.id, TraceKind.FS_COPY, {"src": "a", "dst": "b"})
    third = store.add_trace(run.id, "custom.kind", ["x", 1, None])

    assert (first.seq, second.seq, third.seq) == (1, 2, 3)
    entries = store.query_trace(run.id)
    assert [e.kind for e in entries] == ["log", "fs.copy", "custom.kind"]
    assert [e.payload for e in entries] == [
        "starting",
        {"src": "a", "dst": "b"},
        ["x", 1, None],
    ]
    assert [e.seq for e in store.query_trace(other.id)] == [1]
    assert store.query_trace("nope") == []


# -------------------------------------------------------------- provenance


def test_provenance(store: ActionStore, action: ActionRecord, backend: SQLiteBackend):
    add_file(backend, "src.txt", "s")
    add_file(backend, "out/copy.txt", "c")
    run = store.start_run(action, key_for("s"), "make_copy", "src.txt", RunSource.WATCH)

    seen = store.add_provenance(
        "out/copy.txt", run.id, ProvenanceKind.OBSERVED, ambiguous=True
    )
    assert seen.kind is ProvenanceKind.OBSERVED and seen.ambiguous

    # emitted overrides observed, never the reverse
    emitted = store.add_provenance("out/copy.txt", run.id, ProvenanceKind.EMITTED)
    assert emitted.kind is ProvenanceKind.EMITTED and not emitted.ambiguous
    still = store.add_provenance(
        "out/copy.txt", run.id, ProvenanceKind.OBSERVED, ambiguous=True
    )
    assert still.kind is ProvenanceKind.EMITTED and not still.ambiguous

    assert store.produced_by(run.id) == ["out/copy.txt"]
    assert [p.run_id for p in store.query_provenance(file_path="out/copy.txt")] == [
        run.id
    ]
    assert len(store.query_provenance(run_id=run.id)) == 1
    assert store.query_provenance(file_path="nope") == []
    with pytest.raises(KeyError):
        store.add_provenance("nope.txt", run.id, ProvenanceKind.EMITTED)


# ---------------------------------------------------------------- problems


def test_problems_and_delivery(
    store: ActionStore, action: ActionRecord, backend: SQLiteBackend
):
    add_file(backend, "a.txt")
    run = store.start_run(action, key_for(), "make_copy", "a.txt", RunSource.WATCH)

    p_info = store.record_problem(
        Severity.INFO, "run.ok", "done", action_name="make_copy", run_id=run.id
    )
    p_err = store.record_problem(
        Severity.ERR, "run.failed", "boom", file_path="a.txt", run_id=run.id
    )
    p_crit = store.record_problem(Severity.CRIT, "daemon.died", "bye")
    p_warn = store.record_problem(
        Severity.WARN, "observed", "raw fs", file_path="missing.txt"
    )

    assert p_err.file_id is not None and p_warn.file_id is None
    assert [p.id for p in store.query_problems()] == [
        p_info.id,
        p_err.id,
        p_crit.id,
        p_warn.id,
    ]
    assert [p.kind for p in store.query_problems(at_least=Severity.ERR)] == [
        "run.failed",
        "daemon.died",
    ]
    assert [p.kind for p in store.query_problems(at_least=Severity.CRIT)] == [
        "daemon.died"
    ]
    assert len(store.query_problems(at_least=Severity.INFO)) == 4
    assert len(store.query_problems(limit=2)) == 2

    assert store.mark_delivered([p_info.id, p_err.id]) == 2
    assert store.mark_delivered([p_info.id]) == 0  # already delivered
    assert store.mark_delivered([]) == 0
    undelivered = store.query_problems(undelivered_only=True)
    assert [p.id for p in undelivered] == [p_crit.id, p_warn.id]
    assert store.get_problem(p_err.id).delivered_at is not None
    assert store.query_problems(since=datetime.now(UTC) + timedelta(hours=1)) == []


# ----------------------------------------------------------------- history


def test_file_history(store: ActionStore, action: ActionRecord, backend: SQLiteBackend):
    add_file(backend, "a.txt", "s")
    backend.set_file_tags("a.txt", ["photo"])
    run = store.start_run(action, key_for("s"), "make_copy", "a.txt", RunSource.WATCH)
    store.finish_run(run.id, RunStatus.FAILED, error="x")
    store.record_problem(
        Severity.ERR, "run.failed", "x", file_path="a.txt", run_id=run.id
    )
    add_file(backend, "copy.txt", "c")
    store.add_provenance("copy.txt", run.id, ProvenanceKind.EMITTED)

    history = store.file_history("a.txt")

    assert history is not None
    assert [e.name for e in history.events] == ["file.insert", "tag.assign"]
    assert [r.id for r in history.runs] == [run.id]
    assert history.provenance == []  # a.txt was not produced by anything
    assert [p.kind for p in history.problems] == ["run.failed"]

    produced = store.file_history("copy.txt")
    assert produced is not None
    assert [p.run_id for p in produced.provenance] == [run.id]
    assert store.file_history("nope.txt") is None


def test_canonical_json_is_deterministic_and_portable():
    assert canonical_json({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}'
    assert canonical_json({"s": {"c", "a", "b"}}) == '{"s":["a","b","c"]}'
    assert canonical_json({"p": PureWindowsPath("a\\b")}) == '{"p":"a/b"}'
    assert canonical_json({"p": Path("a/b")}) == '{"p":"a/b"}'
    assert (
        canonical_json(datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC))
        == '"2024-01-02T03:04:05+00:00"'
    )
    assert canonical_json(Decimal("1.5")) == '"1.5"'
    assert canonical_json(b"\x00x") == '{"__bytes__":"AHg="}'
    assert canonical_json(Hook.ADDED) == '"added"'
    assert canonical_json((1, "é")) == '[1,"é"]'
    with pytest.raises(TypeError):
        canonical_json(object())
    with pytest.raises(ValueError):
        canonical_json(float("nan"))


def test_run_key_is_hashable_and_compares_canonically():
    a = RunKey(file_hash="h", action_name="f", hook=Hook.ADDED, args={"x": 1, "y": 2})
    b = RunKey(file_hash="h", action_name="f", hook="added", args={"y": 2, "x": 1})  # type: ignore[arg-type]
    c = RunKey(file_hash="h", action_name="f", hook=Hook.REMOVED, args={"x": 1, "y": 2})

    assert a == b and hash(a) == hash(b)
    assert a != c
    assert len({a, b, c}) == 2


def test_latest_action_follows_the_most_recent_load(store: ActionStore):
    v1 = store.register_action("f", "script/f.py", "v1", {}, [])
    store.register_action("f", "script/f.py", "v2", {}, [])
    back = store.register_action("f", "script/f.py", "v1", {}, [])  # script reverted

    assert back.id == v1.id
    latest = store.latest_action("f")
    assert latest is not None and latest.script_hash == "v1"
    assert store.register_action(
        "g", PureWindowsPath("script\\g.py"), "x", {}, []
    ).script_path == PurePosixPath("script/g.py")


def test_results_and_payloads_round_trip_typed_values(
    store: ActionStore, action: ActionRecord
):
    run = store.start_run(action, key_for(), "make_copy", None, RunSource.WATCH)
    store.add_trace(
        run.id,
        TraceKind.RECORD,
        {"when": datetime(2024, 1, 1, tzinfo=UTC), "out": Path("o/x")},
    )
    done = store.finish_run(
        run.id, RunStatus.OK, result={"paths": {Path("b"), Path("a")}}
    )

    assert done.result == {"paths": ["a", "b"]}
    assert store.query_trace(run.id)[0].payload == {
        "when": "2024-01-01T00:00:00+00:00",
        "out": "o/x",
    }
    with pytest.raises(TypeError):
        store.add_trace(run.id, TraceKind.RECORD, object())


def test_observed_provenance_accumulates_ambiguity(
    store: ActionStore, action: ActionRecord, backend: SQLiteBackend
):
    add_file(backend, "out.txt", "o")
    run = store.start_run(action, key_for(), "make_copy", None, RunSource.WATCH)

    first = store.add_provenance("out.txt", run.id, ProvenanceKind.OBSERVED)
    assert not first.ambiguous
    second = store.add_provenance(
        "out.txt", run.id, ProvenanceKind.OBSERVED, ambiguous=True
    )
    assert second.ambiguous
    third = store.add_provenance(
        "out.txt", run.id, ProvenanceKind.OBSERVED, ambiguous=False
    )
    assert third.ambiguous  # once ambiguous, stays ambiguous

    backend.delete("out.txt")
    assert store.produced_by(run.id) == []
    assert store.produced_by(run.id, include_deleted=True) == ["out.txt"]
    assert store.query_provenance(run_id=run.id) == []
    assert len(store.query_provenance(run_id=run.id, include_deleted=True)) == 1
    with pytest.raises(KeyError):
        store.add_provenance("out.txt", "nope", ProvenanceKind.EMITTED)


def test_history_includes_runs_started_before_the_row_existed(
    store: ActionStore, action: ActionRecord, backend: SQLiteBackend
):
    early = store.start_run(
        action, key_for("s"), "make_copy", "late.txt", RunSource.WATCH
    )
    assert early.file_id is None
    add_file(backend, "late.txt", "s")
    backend.set_file_tags("late.txt", ["photo"])

    history = store.file_history("late.txt")
    assert history is not None
    assert [r.id for r in history.runs] == [early.id]
    assert [r.id for r in store.query_runs(file_path="late.txt")] == [early.id]
    assert [e.tag_name for e in history.events if e.name == "tag.assign"] == ["photo"]
    assert [entry.kind for entry in history.timeline] == ["event", "event", "run"] or {
        entry.kind for entry in history.timeline
    } == {"event", "run"}
    assert store.query_runs(file_path="..") == []
    assert store.file_history("..") is None


def test_path_prefix_queries(
    store: ActionStore, action: ActionRecord, backend: SQLiteBackend
):
    add_file(backend, "@@make_copy/a.txt", "a")
    add_file(backend, "@@make_copy/sub/b.txt", "b")
    add_file(backend, "@@make_copy_other/c.txt", "c")
    add_file(backend, "elsewhere/d.txt", "d")
    ra = store.start_run(
        action, key_for("a"), "make_copy", "@@make_copy/a.txt", RunSource.WATCH
    )
    store.finish_run(ra.id, RunStatus.OK)
    store.start_run(
        action, key_for("c"), "make_copy", "@@make_copy_other/c.txt", RunSource.WATCH
    )

    under = backend.query_files(path_prefix="@@make_copy")
    assert sorted(f.path.as_posix() for f in under) == [
        "@@make_copy/a.txt",
        "@@make_copy/sub/b.txt",
    ]
    assert [r.id for r in store.query_runs(path_prefix="@@make_copy/")] == [ra.id]
    # "which files under @@make_copy have no ok run yet": b.txt
    done = {
        r.file_hash
        for r in store.query_runs(path_prefix="@@make_copy", status=RunStatus.OK)
    }
    assert [f.path.as_posix() for f in under if f.file_hash not in done] == [
        "@@make_copy/sub/b.txt"
    ]


def test_path_prefix_runs_include_rows_started_before_the_file_existed(
    store: ActionStore, action: ActionRecord, backend: SQLiteBackend
):
    early = store.start_run(
        action, key_for("b"), "make_copy", "@@f/b.txt", RunSource.WATCH
    )
    assert early.file_id is None
    add_file(backend, "@@f/b.txt", "b")

    assert [r.id for r in store.query_runs(path_prefix="@@f")] == [early.id]
    assert store.query_runs(path_prefix="@@f/") == [early]
    with pytest.raises(ValueError):
        store.query_runs(path_prefix="..")


def test_run_key_rejects_values_that_cannot_form_a_key():
    with pytest.raises(ValueError):
        RunKey(
            file_hash="h", action_name="f", hook=Hook.ADDED, args={"x": float("nan")}
        )
    with pytest.raises((ValueError, TypeError)):
        RunKey(file_hash="h", action_name="f", hook=Hook.ADDED, args={"x": object()})


def test_delivery_and_interrupt_handle_large_id_lists(
    store: ActionStore, action: ActionRecord
):
    ids = [store.record_problem(Severity.INFO, "k", "m").id for _ in range(5)]
    padding = [f"missing-{i}" for i in range(40_000)]

    assert store.mark_delivered(ids + padding) == 5
    runs = [
        store.start_run(action, key_for(str(i)), "x", None, RunSource.WATCH).id
        for i in range(3)
    ]
    assert len(store.mark_interrupted(runs + padding)) == 3


def test_since_rounds_up_to_the_next_second(store: ActionStore):
    problem = store.record_problem(Severity.INFO, "k", "m")
    just_after = problem.occurred_at + timedelta(microseconds=1)

    assert store.query_problems(since=problem.occurred_at) == [problem]
    assert store.query_problems(since=just_after) == []


def test_store_joins_backend_transactions(
    store: ActionStore, action: ActionRecord, backend: SQLiteBackend
):
    from tag_file_system.database.sqlite import transactional

    class Composite(ActionStore):
        @transactional
        def run_and_fail(self) -> None:
            self.backend.insert(
                filename="a.txt", file_path="a.txt", file_hash="h", file_size=1
            )
            self.start_run(action, key_for(), "make_copy", "a.txt", RunSource.WATCH)
            raise RuntimeError("abort")

    with pytest.raises(RuntimeError):
        Composite(backend).run_and_fail()

    assert backend.query_file("a.txt") is None
    assert store.query_runs() == []
    assert not backend.connection.in_transaction


# ---------------------------------------------------------------- upgrades


def test_runs_are_stamped_with_the_code_that_produced_them(
    store: ActionStore, action: ActionRecord
):
    from tag_file_system.version import COMMIT, VERSION

    run = store.start_run(action, key_for(), "copy", None, RunSource.WATCH)
    assert (run.code_version, run.code_hash) == (VERSION, COMMIT)
    assert store.get_run(run.id) == run


def test_record_and_query_upgrades(store: ActionStore):
    assert store.query_upgrades() == []
    first = store.record_upgrade(
        from_tag=None,
        from_hash="a" * 40,
        to_tag="v0.2.0",
        to_hash="b" * 40,
        schema_before=1,
        schema_after=2,
        started_at=datetime(2026, 9, 2, 4, 0, tzinfo=UTC),
        tests_run=10,
        tests_passed=9,
        tests_skipped=1,
        snapshot_path=".tfs/backups/x.db",
    )
    second = store.record_upgrade(
        from_tag="v0.2.0",
        from_hash="b" * 40,
        to_tag="v0.3.0",
        to_hash="c" * 40,
        schema_before=2,
        schema_after=2,
        started_at=1700000000.0,
    )
    assert first.from_tag is None and first.tests_passed == 9
    assert second.tests_run is None and second.started_at == datetime.fromtimestamp(
        1700000000, UTC
    )
    assert [u.to_tag for u in store.query_upgrades()] == ["v0.3.0", "v0.2.0"]
    assert store.query_upgrades(limit=1) == [second]
