# Code by AkinoAlice@TyrantRey

import hashlib
from pathlib import Path

from watchfiles import Change

from tag_file_system.core.interface.database import DatabaseOperation
from tag_file_system.core.router.database_event import DatabaseEventRouter
from tag_file_system.core.router.watch_event import WatchEventRouter
from tag_file_system.database.sqlite import SQLiteBackend
from tag_file_system.pipeline.database import register_database_pipeline
from tag_file_system.services.engine import TagFileEngine
from tests.helpers import fetch, metadata_of


def make_engine(backend: SQLiteBackend, tmp_path: Path, files_dir: Path):
    router = DatabaseEventRouter()
    register_database_pipeline(router, backend)
    return TagFileEngine(
        watch_event_router=WatchEventRouter(),
        database_event_router=router,
        database_engine=backend,
        root_dir=tmp_path,
        files_dir=files_dir,
        tags_dir=tmp_path / "tags",
        database_path=tmp_path / "engine.db",
    )


def test_added_file_is_indexed_with_tags(backend: SQLiteBackend, tmp_path, files_dir):
    engine = make_engine(backend, tmp_path, files_dir)
    path = files_dir / "report--Finance--q3@@archive:days=30.pdf"
    path.write_bytes(b"pdf-bytes")

    engine.process_changes({(Change.added, str(path))})

    stored = backend.query_file(path)
    assert stored is not None
    assert stored.file_hash == hashlib.sha256(b"pdf-bytes").hexdigest()
    assert metadata_of(stored).file_size == len(b"pdf-bytes")
    assert metadata_of(stored).file_format == ".pdf"
    assert metadata_of(stored).mime_type == "application/pdf"
    assert [t.name for t in stored.tags] == ["finance", "q3"]


def test_modified_file_is_updated_or_inserted(
    backend: SQLiteBackend, tmp_path, files_dir
):
    engine = make_engine(backend, tmp_path, files_dir)
    path = files_dir / "notes--draft.txt"
    path.write_text("v1")

    # modified before the watcher ever saw it -> falls back to insert
    engine.process_changes({(Change.modified, str(path))})
    first = backend.query_file(path)
    assert first is not None
    assert first.file_hash == hashlib.sha256(b"v1").hexdigest()

    path.write_text("v2 longer")
    path.rename(files_dir / "notes--final.txt")
    path = files_dir / "notes--final.txt"
    # simulate a rename as watchfiles reports it: delete old, add new
    engine.process_changes(
        {
            (Change.deleted, str(files_dir / "notes--draft.txt")),
            (Change.added, str(path)),
        }
    )

    assert backend.query_file(files_dir / "notes--draft.txt") is None
    renamed = backend.query_file(path)
    assert renamed is not None
    assert renamed.file_hash == hashlib.sha256(b"v2 longer").hexdigest()
    assert [t.name for t in renamed.tags] == ["final"]


def test_deleted_file_is_soft_deleted(backend: SQLiteBackend, tmp_path, files_dir):
    engine = make_engine(backend, tmp_path, files_dir)
    path = files_dir / "temp.txt"
    path.write_text("x")
    engine.process_changes({(Change.added, str(path))})
    assert backend.query_file(path) is not None

    path.unlink()
    engine.process_changes({(Change.deleted, str(path))})

    assert backend.query_file(path) is None
    assert fetch(backend, path, include_deleted=True).status == "deleted"


def test_directories_and_unknown_deletes_are_ignored(
    backend: SQLiteBackend, tmp_path, files_dir
):
    engine = make_engine(backend, tmp_path, files_dir)
    sub = files_dir / "folder--tagged"
    sub.mkdir()

    engine.process_changes({(Change.added, str(sub))})
    engine.process_changes({(Change.deleted, str(files_dir / "never-seen.txt"))})

    assert backend.query_files(include_deleted=True) == []


def test_handler_failure_does_not_stop_dispatch(
    backend: SQLiteBackend, tmp_path, files_dir
):
    router = DatabaseEventRouter()
    seen: list[Path] = []

    @router.on_insert()
    def first(path, metadata):
        raise RuntimeError("boom")

    @router.on_insert()
    def second(path, metadata):
        seen.append(path)

    path = files_dir / "a.txt"
    path.write_text("a")
    router.dispatch(DatabaseOperation.INSERT, path)

    assert seen == [path]
