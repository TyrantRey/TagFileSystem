# Code by AkinoAlice@TyrantRey

from tab_file_system.core.interface.file_metadata import FileMetadata
from tab_file_system.core.router.watch_event import watchfile_router
from pathlib import Path


@watchfile_router.on_file_added()
def handle_added(path: Path, file_metadata: FileMetadata) -> None:
    print(f"File added: {path}, metadata: {file_metadata}")
