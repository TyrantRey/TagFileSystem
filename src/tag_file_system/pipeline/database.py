# Code by AkinoAlice@TyrantRey

from pathlib import Path

from tag_file_system.core.interface.database import (
    DatabaseEngineProtocol,
    OperationResultEnum,
)
from tag_file_system.core.interface.file_metadata import FileMetadata
from tag_file_system.core.logger import logger
from tag_file_system.core.router.database_event import DatabaseEventRouter
from tag_file_system.services.file_info import compute_file_hash, guess_mime_type
from tag_file_system.services.tagging import TaggingParser


def register_database_pipeline(
    router: DatabaseEventRouter,
    backend: DatabaseEngineProtocol,
    parser: TaggingParser | None = None,
) -> None:
    """Connect the database event router to a storage backend.

    INSERT/UPDATE record the file's hash, size, format, MIME type and mtime,
    then sync the file's tags with the ``--tag`` markers parsed from its
    filename. DELETE soft-deletes the row.
    """
    tag_parser = parser if parser is not None else TaggingParser()

    def tag_names(path: Path) -> list[str]:
        parsed = tag_parser.parse(path.stem)
        if parsed.actions:
            logger.debug(f"Ignoring actions on {path.name}: {parsed.actions}")
        return list(dict.fromkeys(tag.name for tag in parsed.tags))

    def describe(path: Path, metadata: FileMetadata) -> dict:
        mtime_ns = path.stat().st_mtime_ns
        return {
            "file_hash": file_hash(path, metadata.file_size, mtime_ns),
            "file_size": metadata.file_size,
            "file_format": metadata.file_format or None,
            "file_mime_type": metadata.mime_type or guess_mime_type(path),
            "mtime_ns": mtime_ns,
        }

    def file_hash(path: Path, size: int, mtime_ns: int) -> str:
        """sha256, reused from the stored row when ``(size, mtime_ns)`` are
        unchanged (DESIGN.md §5): a touched 20 GB video is not re-hashed."""
        stored = backend.query_file(path, include_deleted=True)
        if (
            stored is not None
            and stored.metadata is not None
            and stored.metadata.mtime_ns == mtime_ns
            and stored.metadata.file_size == size
        ):
            return stored.file_hash
        return compute_file_hash(path)

    @router.on_insert()
    def handle_insert(path: Path, metadata: FileMetadata) -> None:
        if path.is_dir():
            return
        result = backend.insert(
            filename=path.name, file_path=path, **describe(path, metadata)
        )
        logger.info(f"DB insert {result.status}: {path}")
        backend.set_file_tags(path, tag_names(path))

    @router.on_update()
    def handle_update(path: Path, metadata: FileMetadata) -> None:
        if path.is_dir():
            return
        details = describe(path, metadata)
        result = backend.update(file_path=path, **details)
        if result.status is OperationResultEnum.NOT_FOUND:
            # Modified before we ever saw it (e.g. it predates the watcher).
            result = backend.insert(filename=path.name, file_path=path, **details)
        logger.info(f"DB update {result.status}: {path}")
        backend.set_file_tags(path, tag_names(path))

    @router.on_delete()
    def handle_delete(path: Path, metadata: FileMetadata) -> None:
        result = backend.delete(path)
        if result.status is OperationResultEnum.NOT_FOUND:
            # Directories and files we never indexed land here; not an error.
            logger.debug(f"DB delete skipped, unknown path: {path}")
            return
        logger.info(f"DB delete {result.status}: {path}")
