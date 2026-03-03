# Code by AkinoAlice@TyrantRey

from pathlib import Path
from enum import StrEnum
from typing import Protocol
from tab_file_system.core.interface.file_metadata import Tag, FileMetadata

from datetime import datetime


class DatabaseOperation(StrEnum):
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"


class DatabaseEngineProtocol(Protocol):
    def insert(self, path: str) -> int: ...

    def update(self, path: str) -> None: ...

    def delete(self, path: str) -> None: ...

    def modify(self, path: str) -> None: ...

    def init_database(self, database_path: Path) -> bool: ...

    def query_tag(self, tag_name: str | None, tag_id: int | None) -> Tag | None: ...

    def query_files(
        self,
        tags: list[Tag] | None = None,
        tag_ids: list[int] | None = None,
        filename: str | None = None,
        file_hash: str | None = None,
        file_format: str | None = None,
        file_type: str | None = None,
        file_size_range: tuple[int, int] | None = None,
        file_added_range: tuple[datetime, datetime] | None = None,
    ) -> list[FileMetadata] | None: ...
