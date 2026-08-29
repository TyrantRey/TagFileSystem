# Code by AkinoAlice@TyrantRey

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tag_file_system.core.interface.database import (
    OperationResultEnum,
    SQLOperationType,
)
from tag_file_system.database.sqlite import SQLiteBackend, transactional
from tests.helpers import fetch, metadata_of, tag_of


def add(
    backend: SQLiteBackend,
    path: Path,
    *,
    file_hash: str = "hash",
    size: int = 10,
    fmt: str | None = ".txt",
    mime: str | None = "text/plain",
):
    return backend.insert(
        filename=path.name,
        file_path=path,
        file_hash=file_hash,
        file_size=size,
        file_format=fmt,
        file_mime_type=mime,
    )


def event_names(backend: SQLiteBackend, file_id: str | None = None) -> list[str]:
    sql = "SELECT name FROM events"
    params: tuple = ()
    if file_id is not None:
        sql += " WHERE file_id = ?"
        params = (file_id,)
    sql += " ORDER BY rowid"
    return [row["name"] for row in backend.connection.execute(sql, params)]


# ------------------------------------------------------------------- insert


def test_insert_creates_row_and_event(backend: SQLiteBackend, tmp_path: Path):
    path = tmp_path / "a.txt"
    result = add(backend, path)

    assert result.status is OperationResultEnum.SUCCESS
    assert result.type is SQLOperationType.File
    assert result.record_id

    stored = backend.query_file(path)
    assert stored is not None
    assert stored.file_id == result.record_id
    assert stored.file_hash == "hash"
    assert stored.original_path == path
    assert stored.status == "active"
    assert stored.metadata is not None
    assert stored.metadata.file_size == 10
    assert stored.metadata.file_format == ".txt"
    assert stored.metadata.file_type == "txt"
    assert stored.metadata.mime_type == "text/plain"
    assert stored.metadata.time_added.tzinfo is not None

    # The single event recorded by insert reuses the operation id.
    assert event_names(backend, result.record_id) == ["file.insert"]
    row = backend.connection.execute(
        "SELECT id FROM events WHERE file_id = ?", (result.record_id,)
    ).fetchone()
    assert row["id"] == result.operation_id


def test_insert_duplicate_path_is_reported(backend: SQLiteBackend, tmp_path: Path):
    path = tmp_path / "a.txt"
    first = add(backend, path)
    second = add(backend, path, file_hash="other")

    assert second.status is OperationResultEnum.ALREADY_EXISTS
    assert second.record_id == first.record_id
    assert fetch(backend, path).file_hash == "hash"  # untouched
    assert event_names(backend, first.record_id) == [
        "file.insert",
        "file.insert.duplicate",
    ]


def test_insert_revives_soft_deleted_row(backend: SQLiteBackend, tmp_path: Path):
    path = tmp_path / "a.txt"
    first = add(backend, path)
    backend.delete(path)

    revived = add(backend, path, file_hash="new", size=99)

    assert revived.status is OperationResultEnum.SUCCESS
    assert revived.record_id == first.record_id  # same identity, tags kept
    stored = backend.query_file(path)
    assert stored is not None
    assert stored.file_hash == "new"
    assert metadata_of(stored).file_size == 99
    assert event_names(backend, first.record_id)[-1] == "file.insert.restore"


# ------------------------------------------------------------------- update


def test_update_refreshes_metadata(backend: SQLiteBackend, tmp_path: Path):
    path = tmp_path / "a.txt"
    inserted = add(backend, path)

    result = backend.update(
        file_path=path,
        file_hash="changed",
        file_size=123,
        file_format=".md",
        file_mime_type="text/markdown",
    )

    assert result.status is OperationResultEnum.SUCCESS
    assert result.record_id == inserted.record_id
    stored = fetch(backend, path)
    assert stored.file_hash == "changed"
    assert metadata_of(stored).file_size == 123
    assert metadata_of(stored).file_format == ".md"
    assert metadata_of(stored).mime_type == "text/markdown"
    assert event_names(backend, inserted.record_id) == ["file.insert", "file.update"]


def test_update_unknown_path_is_not_found(backend: SQLiteBackend, tmp_path: Path):
    result = backend.update(tmp_path / "ghost.txt", file_hash="x", file_size=1)

    assert result.status is OperationResultEnum.NOT_FOUND
    assert result.record_id is None
    assert event_names(backend) == []


# ------------------------------------------------------------------- delete


def test_delete_is_soft_and_keeps_tags(backend: SQLiteBackend, tmp_path: Path):
    path = tmp_path / "a.txt"
    inserted = add(backend, path)
    backend.set_file_tags(path, ["work"])

    result = backend.delete(path)

    assert result.status is OperationResultEnum.SUCCESS
    assert backend.query_file(path) is None
    deleted = backend.query_file(path, include_deleted=True)
    assert deleted is not None
    assert deleted.status == "deleted"
    assert [t.name for t in deleted.tags] == ["work"]
    row = backend.connection.execute(
        "SELECT deleted_at FROM files WHERE id = ?", (inserted.record_id,)
    ).fetchone()
    assert row["deleted_at"] is not None
    assert event_names(backend, inserted.record_id)[-1] == "file.delete"


def test_delete_twice_and_unknown(backend: SQLiteBackend, tmp_path: Path):
    path = tmp_path / "a.txt"
    add(backend, path)
    backend.delete(path)

    assert backend.delete(path).status is OperationResultEnum.ALREADY_EXISTS
    assert backend.delete(tmp_path / "nope").status is OperationResultEnum.NOT_FOUND


# ------------------------------------------------------------------- modify


def test_modify_moves_path(backend: SQLiteBackend, tmp_path: Path):
    old, new = tmp_path / "old.txt", tmp_path / "sub" / "new.txt"
    inserted = add(backend, old)
    backend.set_file_tags(old, ["keep"])

    result = backend.modify(old, new)

    assert result.status is OperationResultEnum.SUCCESS
    assert result.record_id == inserted.record_id
    assert backend.query_file(old) is None
    moved = backend.query_file(new)
    assert moved is not None
    assert moved.file_id == inserted.record_id
    assert moved.original_path == new
    assert [t.name for t in moved.tags] == ["keep"]
    row = backend.connection.execute(
        "SELECT filename FROM files WHERE id = ?", (inserted.record_id,)
    ).fetchone()
    assert row["filename"] == "new.txt"
    assert event_names(backend, inserted.record_id)[-1] == "file.move"


def test_modify_conflicts_and_missing(backend: SQLiteBackend, tmp_path: Path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    add(backend, a)
    add(backend, b)

    assert backend.modify(a, b).status is OperationResultEnum.ALREADY_EXISTS
    assert backend.modify(a, a).status is OperationResultEnum.SUCCESS
    assert backend.modify(tmp_path / "x", b).status is OperationResultEnum.NOT_FOUND
    assert backend.query_file(a) is not None


# --------------------------------------------------------------------- tags


def test_upsert_tag(backend: SQLiteBackend):
    created = backend.upsert_tag("photo", category="media")
    assert created.status is OperationResultEnum.SUCCESS
    assert created.type is SQLOperationType.Tag

    again = backend.upsert_tag("photo", description="pictures")
    assert again.status is OperationResultEnum.ALREADY_EXISTS
    assert again.record_id == created.record_id

    row = backend.connection.execute(
        "SELECT category, description FROM tags WHERE id = ?", (created.record_id,)
    ).fetchone()
    assert (row["category"], row["description"]) == ("media", "pictures")

    tag = backend.query_tag(tag_name="photo")
    assert tag is not None
    assert tag.tag_id == created.record_id
    by_id = backend.query_tag(tag_id=created.record_id)
    assert by_id is not None and by_id.name == "photo"
    assert backend.query_tag(tag_name="missing") is None
    with pytest.raises(ValueError):
        backend.query_tag()


def test_set_file_tags_syncs_links(backend: SQLiteBackend, tmp_path: Path):
    path = tmp_path / "a.txt"
    inserted = add(backend, path)

    first = backend.set_file_tags(path, ["a", "b", "a"])
    assert first.status is OperationResultEnum.SUCCESS
    assert [t.name for t in fetch(backend, path).tags] == ["a", "b"]

    backend.set_file_tags(path, ["b", "c"])
    assert [t.name for t in fetch(backend, path).tags] == ["b", "c"]

    # tags are shared entities: "a" still exists even if no file carries it
    assert backend.query_tag(tag_name="a") is not None

    names = event_names(backend, inserted.record_id)
    assert names.count("tag.assign") == 3
    assert names.count("tag.unassign") == 1

    backend.set_file_tags(path, [])
    assert fetch(backend, path).tags == []

    missing = backend.set_file_tags(tmp_path / "ghost", ["x"])
    assert missing.status is OperationResultEnum.NOT_FOUND


# ------------------------------------------------------------------ queries


@pytest.fixture
def populated(backend: SQLiteBackend, tmp_path: Path) -> SQLiteBackend:
    add(
        backend,
        tmp_path / "report.pdf",
        file_hash="h1",
        size=100,
        fmt=".pdf",
        mime="application/pdf",
    )
    add(
        backend,
        tmp_path / "notes.txt",
        file_hash="h2",
        size=5,
        fmt=".txt",
        mime="text/plain",
    )
    add(
        backend,
        tmp_path / "photo.jpg",
        file_hash="h3",
        size=5000,
        fmt=".jpg",
        mime="image/jpeg",
    )
    add(
        backend,
        tmp_path / "gone.txt",
        file_hash="h4",
        size=7,
        fmt=".txt",
        mime="text/plain",
    )
    backend.set_file_tags(tmp_path / "report.pdf", ["work", "finance"])
    backend.set_file_tags(tmp_path / "notes.txt", ["work"])
    backend.set_file_tags(tmp_path / "photo.jpg", ["personal"])
    backend.delete(tmp_path / "gone.txt")
    return backend


def names(results) -> list[str]:
    return sorted(f.original_path.name for f in results)


def test_query_files_no_filter_excludes_deleted(populated: SQLiteBackend):
    assert names(populated.query_files()) == ["notes.txt", "photo.jpg", "report.pdf"]
    assert names(populated.query_files(include_deleted=True)) == [
        "gone.txt",
        "notes.txt",
        "photo.jpg",
        "report.pdf",
    ]


def test_query_files_by_tags_requires_all(populated: SQLiteBackend):
    assert names(populated.query_files(tags=["work"])) == ["notes.txt", "report.pdf"]
    assert names(populated.query_files(tags=["work", "finance"])) == ["report.pdf"]
    assert populated.query_files(tags=["work", "nope"]) == []

    finance = tag_of(populated, "finance")
    assert names(populated.query_files(tag_ids=[finance.tag_id])) == ["report.pdf"]
    # names and ids can be mixed and overlap without double counting
    assert names(
        populated.query_files(tags=["finance", "work"], tag_ids=[finance.tag_id])
    ) == ["report.pdf"]

    (report,) = populated.query_files(tags=["finance"])
    assert [t.name for t in report.tags] == ["finance", "work"]


def test_query_files_by_attributes(populated: SQLiteBackend):
    assert names(populated.query_files(filename="REPORT")) == ["report.pdf"]
    assert names(populated.query_files(filename="%")) == []  # wildcard escaped
    assert names(populated.query_files(file_hash="h3")) == ["photo.jpg"]
    assert names(populated.query_files(file_format=".txt")) == ["notes.txt"]
    assert names(populated.query_files(file_type="txt")) == ["notes.txt"]
    assert names(populated.query_files(mime_type="image/jpeg")) == ["photo.jpg"]
    assert names(populated.query_files(file_size_range=(50, 10_000))) == [
        "photo.jpg",
        "report.pdf",
    ]
    assert names(populated.query_files(file_size_range=(10_000, 50))) == [
        "photo.jpg",
        "report.pdf",
    ]
    assert names(populated.query_files(tags=["work"], file_size_range=(0, 10))) == [
        "notes.txt"
    ]


def test_query_files_by_added_range(populated: SQLiteBackend):
    now = datetime.now(UTC)
    window = (now - timedelta(minutes=1), now + timedelta(minutes=1))
    assert len(populated.query_files(file_added_range=window)) == 3
    past = (now - timedelta(days=2), now - timedelta(days=1))
    assert populated.query_files(file_added_range=past) == []


# ------------------------------------------------------------- transactions


def test_transaction_rolls_back_on_error(backend: SQLiteBackend, tmp_path: Path):
    path = tmp_path / "a.txt"
    add(backend, path)

    class Broken(SQLiteBackend):
        @transactional
        def explode(self, file_path: Path) -> None:
            self.connection.execute(
                "UPDATE files SET hash = 'partial' WHERE path = ?", (str(file_path),)
            )
            raise RuntimeError("boom")

    broken = Broken()
    broken.connection = backend.connection

    with pytest.raises(RuntimeError):
        broken.explode(path)

    assert fetch(backend, path).file_hash == "hash"
    assert not backend.connection.in_transaction


def test_nested_transactional_calls_join_outer(backend: SQLiteBackend, tmp_path: Path):
    path = tmp_path / "a.txt"

    class Composite(SQLiteBackend):
        @transactional
        def insert_and_tag(self, file_path: Path) -> None:
            add(self, file_path)
            self.set_file_tags(file_path, ["t"])
            raise RuntimeError("abort everything")

    composite = Composite()
    composite.connection = backend.connection

    with pytest.raises(RuntimeError):
        composite.insert_and_tag(path)

    # the inner insert/set_file_tags did not commit on their own
    assert backend.query_file(path) is None
    assert backend.query_tag(tag_name="t") is None
