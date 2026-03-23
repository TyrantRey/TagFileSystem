# Code by AkinoAlice@TyrantRey

from pathlib import Path

from tag_file_system.config import DatabaseSetting, FolderSetting
from tag_file_system.core.interface.file_metadata import FileMetadata
from tag_file_system.core.router.database_event import database_event_router
from tag_file_system.core.router.watch_event import watchfile_router
from tag_file_system.database.sqlite import SQLiteBackend
from tag_file_system.services.engine import TagFileEngine

database_setting = DatabaseSetting()
folder_setting = FolderSetting()

tag_file_engine = TagFileEngine(
    watch_event_router=watchfile_router,
    database_event_router=database_event_router,
    database_engine=SQLiteBackend(),
)


@watchfile_router.on_file_added()
def handle_added(path: Path, file_metadata: FileMetadata) -> None:
    print(f"File added: {path}, metadata: {file_metadata}")


@watchfile_router.on_file_modified()
def handle_modified(path: Path, file_metadata: FileMetadata) -> None:
    print(f"File modified: {path}, metadata: {file_metadata}")


@watchfile_router.on_file_deleted()
def handle_deleted(path: Path, file_metadata: FileMetadata) -> None:
    print(f"File deleted: {path}, metadata: {file_metadata}")


if __name__ == "__main__":
    tag_file_engine.start()
