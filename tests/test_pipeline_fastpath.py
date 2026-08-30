# Code by AkinoAlice@TyrantRey

import hashlib
import os
from pathlib import Path

import pytest
from watchfiles import Change

from tag_file_system.core.router.database_event import DatabaseEventRouter
from tag_file_system.core.router.watch_event import WatchEventRouter
from tag_file_system.database.sqlite import SQLiteBackend
from tag_file_system.pipeline import database as pipeline
from tag_file_system.services.engine import TagFileEngine
from tests.helpers import fetch, metadata_of


def test_unchanged_size_and_mtime_skip_rehashing(
    backend: SQLiteBackend, tmp_path: Path, files_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: list[Path] = []
    real = pipeline.compute_file_hash

    def counting(path: Path, *args, **kwargs) -> str:
        calls.append(path)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(pipeline, "compute_file_hash", counting)
    router = DatabaseEventRouter()
    pipeline.register_database_pipeline(router, backend)
    engine = TagFileEngine(
        watch_event_router=WatchEventRouter(),
        database_event_router=router,
        database_engine=backend,
        root_dir=tmp_path,
        files_dir=files_dir,
        tags_dir=tmp_path / "tags",
        database_path=tmp_path / "engine.db",
    )
    path = files_dir / "video.bin"
    path.write_bytes(b"x" * 1000)

    engine.process_changes({(Change.added, str(path))})
    engine.process_changes({(Change.modified, str(path))})  # touched, unchanged
    engine.process_changes({(Change.modified, str(path))})
    assert len(calls) == 1
    assert metadata_of(fetch(backend, path)).mtime_ns == path.stat().st_mtime_ns

    # same size, new mtime -> hashed again
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 5_000_000_000))
    engine.process_changes({(Change.modified, str(path))})
    assert len(calls) == 2

    # different content, different size -> hashed again and stored
    path.write_bytes(b"y" * 999)
    engine.process_changes({(Change.modified, str(path))})
    assert len(calls) == 3
    assert fetch(backend, path).file_hash == hashlib.sha256(b"y" * 999).hexdigest()
