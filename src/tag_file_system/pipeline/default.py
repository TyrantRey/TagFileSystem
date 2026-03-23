# Code by AkinoAlice@TyrantRey

from pathlib import Path

from tag_file_system.core.interface.file_metadata import FileMetadata
from tag_file_system.core.router.watch_event import watchfile_router


@watchfile_router.on_file_added()
def handle_added(path: Path, file_metadata: FileMetadata) -> None:
    print(f"File added: {path}, metadata: {file_metadata}")
