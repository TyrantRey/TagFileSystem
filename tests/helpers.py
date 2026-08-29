# Code by AkinoAlice@TyrantRey

from pathlib import Path

from tag_file_system.core.interface.database import DatabaseEngineProtocol
from tag_file_system.core.interface.file_metadata import FileMetadata, Tag, TaggedFile


def fetch(
    backend: DatabaseEngineProtocol, path: Path, include_deleted: bool = False
) -> TaggedFile:
    """query_file that fails the test instead of returning None."""
    stored = backend.query_file(path, include_deleted=include_deleted)
    assert stored is not None, f"{path} not found in database"
    return stored


def metadata_of(stored: TaggedFile) -> FileMetadata:
    assert stored.metadata is not None
    return stored.metadata


def tag_of(backend: DatabaseEngineProtocol, name: str) -> Tag:
    tag = backend.query_tag(tag_name=name)
    assert tag is not None, f"tag {name!r} not found in database"
    return tag
