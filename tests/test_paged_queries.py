# Code by AkinoAlice@TyrantRey

"""The read queries the API pages with (DESIGN/v0-5-0.md §5): ``limit`` /
``offset`` and the sibling counts, tag counts, and paths by file id."""

from pathlib import Path

from tag_file_system.core.interface.action import (
    Hook,
    RunKey,
    RunSource,
    RunStatus,
    Severity,
)
from tag_file_system.database.action_store import ActionStore
from tag_file_system.database.sqlite import SQLiteBackend


def add(backend: SQLiteBackend, tmp_path: Path, name: str, file_hash: str) -> str:
    result = backend.insert(
        filename=name,
        file_path=tmp_path / name,
        file_hash=file_hash,
        file_size=1,
        file_format=Path(name).suffix,
        file_mime_type="text/plain",
    )
    assert result.record_id is not None
    return result.record_id


def names(files) -> list[str]:
    return [f.path.name for f in files]


# ------------------------------------------------------------------- files


def test_query_files_pages_and_counts(backend: SQLiteBackend, tmp_path: Path):
    for name in ("a.txt", "b.txt", "c.txt", "d.txt", "e.txt"):
        add(backend, tmp_path, name, f"h-{name}")
    backend.set_file_tags(tmp_path / "b.txt", ["x"])

    assert names(backend.query_files(limit=2)) == ["a.txt", "b.txt"]
    assert names(backend.query_files(limit=2, offset=2)) == ["c.txt", "d.txt"]
    assert names(backend.query_files(offset=4)) == ["e.txt"]
    assert backend.query_files(offset=9) == []
    # created within one second: newest-first falls back to the path, reversed
    assert names(backend.query_files(newest_first=True, limit=2)) == ["e.txt", "d.txt"]
    assert backend.count_files() == 5
    assert backend.count_files(filename="a") == 1
    assert backend.count_files(tags=["x"]) == 1
    assert names(backend.query_files(tags=["x"], limit=1)) == ["b.txt"]
    # a tag nobody carries: nothing can match, and the count says so
    assert backend.query_files(tags=["nope"], limit=1) == []
    assert backend.count_files(tags=["nope"]) == 0

    backend.delete(tmp_path / "e.txt")
    assert backend.count_files() == 4
    assert backend.count_files(include_deleted=True) == 5


def test_tag_counts(backend: SQLiteBackend, tmp_path: Path):
    for name in ("a.txt", "b.txt", "c.txt"):
        add(backend, tmp_path, name, f"h-{name}")
    backend.set_file_tags(tmp_path / "a.txt", ["work", "finance"])
    backend.set_file_tags(tmp_path / "b.txt", ["work"])
    backend.set_file_tags(tmp_path / "c.txt", ["personal"])
    backend.delete(tmp_path / "c.txt")

    counted = backend.tag_counts()
    assert [(tag.name, n) for tag, n in counted] == [
        ("finance", 1),
        ("personal", 0),
        ("work", 2),
    ]
    assert all(tag.tag_id and tag.time_added for tag, _ in counted)
    assert [(t.name, n) for t, n in backend.tag_counts(include_deleted=True)] == [
        ("finance", 1),
        ("personal", 1),
        ("work", 2),
    ]
    assert SQLiteBackend.tag_counts(backend) == counted  # no hidden state


def test_query_paths(backend: SQLiteBackend, tmp_path: Path):
    a = add(backend, tmp_path, "a.txt", "ha")
    b = add(backend, tmp_path, "b.txt", "hb")
    backend.delete(tmp_path / "b.txt")

    assert backend.query_paths([a, b, "nope", a]) == {a: "a.txt", b: "b.txt"}
    assert backend.query_paths([]) == {}
    assert backend.query_paths([""]) == {}
    # more ids than one IN clause takes: chunked, nothing lost
    many = [f"missing-{i}" for i in range(1500)] + [a]
    assert backend.query_paths(many) == {a: "a.txt"}


# -------------------------------------------------------------------- runs


def run_key(file_hash: str) -> RunKey:
    return RunKey(
        file_hash=file_hash, action_name="copy", handler="run", hook=Hook.ADDED
    )


def test_query_runs_offset_and_count(backend: SQLiteBackend, tmp_path: Path):
    store = ActionStore(backend)
    action = store.register_action("copy", "script/copy.py", "h", {}, {})
    other = store.register_action("other", "script/other.py", "h", {}, {})
    add(backend, tmp_path, "a.txt", "h1")
    runs = [
        store.start_run(action, run_key(f"h{i}"), "copy.run()", None, RunSource.WATCH)
        for i in (1, 2, 3)
    ]
    extra = store.start_run(
        other,
        RunKey(file_hash="h9", action_name="other", handler="run", hook=Hook.ADDED),
        "other.run()",
        None,
        RunSource.WATCH,
    )
    store.finish_run(runs[0].id, RunStatus.OK)

    newest = [r.id for r in store.query_runs(limit=2)]
    assert newest == [extra.id, runs[2].id]
    assert [r.id for r in store.query_runs(limit=1, offset=1)] == [runs[2].id]
    assert [r.id for r in store.query_runs(offset=3)] == [runs[0].id]
    assert store.query_runs(offset=99) == []
    assert store.count_runs() == 4
    assert store.count_runs(action_name="copy") == 3
    assert store.count_runs(status=RunStatus.RUNNING) == 3
    assert store.count_runs(status=[RunStatus.OK]) == 1
    assert store.count_runs(file_path="a.txt") == 1  # by hash, no file_id yet
    assert store.count_runs(file_path="missing.txt") == 0
    assert [r.id for r in store.query_runs(action_name="copy", limit=1, offset=2)] == [
        runs[0].id
    ]


# ---------------------------------------------------------------- problems


def test_query_problems_filters_offset_newest_and_count(
    backend: SQLiteBackend, tmp_path: Path
):
    store = ActionStore(backend)
    action = store.register_action("copy", "script/copy.py", "h", {}, {})
    add(backend, tmp_path, "f.txt", "hf")
    run = store.start_run(action, run_key("hf"), "copy.run()", "f.txt", RunSource.WATCH)
    first = store.record_problem(Severity.WARN, "a.kind", "one", action_name="x")
    second = store.record_problem(Severity.ERR, "b.kind", "two", file_path="f.txt")
    third = store.record_problem(Severity.INFO, "c.kind", "three", run_id=run.id)
    store.mark_delivered([first.id])

    ids = lambda problems: [p.id for p in problems]  # noqa: E731 - test shorthand
    assert ids(store.query_problems()) == [first.id, second.id, third.id]
    assert ids(store.query_problems(newest_first=True)) == [
        third.id,
        second.id,
        first.id,
    ]
    assert ids(store.query_problems(limit=1, offset=1)) == [second.id]
    assert ids(store.query_problems(newest_first=True, limit=1, offset=1)) == [
        second.id
    ]
    assert ids(store.query_problems(kind="b.kind")) == [second.id]
    assert ids(store.query_problems(action_name="x")) == [first.id]
    assert ids(store.query_problems(file_path="f.txt")) == [second.id]
    assert store.query_problems(file_path="missing.txt") == []
    assert ids(store.query_problems(run_id=run.id)) == [third.id]
    assert ids(store.query_problems(undelivered_only=True)) == [second.id, third.id]
    assert ids(store.query_problems(at_least=Severity.WARN)) == [first.id, second.id]

    assert store.count_problems() == 3
    assert store.count_problems(at_least=Severity.WARN) == 2
    assert store.count_problems(kind="c.kind") == 1
    assert store.count_problems(file_path="missing.txt") == 0
    assert store.count_problems(undelivered_only=True, run_id=run.id) == 1
