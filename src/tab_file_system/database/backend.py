# Code by AkinoAlice@TyrantRey

from datetime import datetime
from pathlib import Path
from typing import Protocol


class DatabaseBackend(Protocol):
    def on_file_added(self, file_path: Path, file_hash: str, date: datetime) -> bool:
        pass

    def on_file_modified(self, file_path: Path, file_hash: str, date: datetime) -> None:
        pass

    def on_file_deleted(self, file_path: Path, file_hash: str, date: datetime) -> None:
        pass

    def on_tag_added(self, tag_path: Path, tag_hash: str, date: datetime) -> None:
        pass

    def on_tag_modified(self, tag_path: Path, tag_hash: str, date: datetime) -> None:
        pass

    def on_tag_deleted(self, tag_path: Path, tag_hash: str, date: datetime) -> None:
        pass
    

class DatabaseManager:
    def __init__(self, backend: DatabaseBackend):
        self.backend = backend

    def on_file_added(self, file_path: Path, file_hash: str, date: datetime) -> bool:
        return self.backend.on_file_added(file_path, file_hash, date)

    def on_file_modified(self, file_path: Path, file_hash: str, date: datetime) -> None:
        self.backend.on_file_modified(file_path, file_hash, date)

    def on_file_deleted(self, file_path: Path, file_hash: str, date: datetime) -> None:
        self.backend.on_file_deleted(file_path, file_hash, date)

    def on_tag_added(self, tag_path: Path, tag_hash: str, date: datetime) -> None:
        self.backend.on_tag_added(tag_path, tag_hash, date)

    def on_tag_modified(self, tag_path: Path, tag_hash: str, date: datetime) -> None:
        self.backend.on_tag_modified(tag_path, tag_hash, date)

    def on_tag_deleted(self, tag_path: Path, tag_hash: str, date: datetime) -> None:
        self.backend.on_tag_deleted(tag_path, tag_hash, date)
