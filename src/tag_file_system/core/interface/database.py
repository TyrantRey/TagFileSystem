# Code by AkinoAlice@TyrantRey

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from tag_file_system.core.interface.file_metadata import FileMetadata, Tag


class OperationResultEnum(StrEnum):
    SUCCESS = "Success"
    FAILURE = "Failure"
    NOT_FOUND = "Not Found"
    ALREADY_EXISTS = "Already Exists"


class DatabaseOperation(StrEnum):
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"


class SQLOperationType(StrEnum):
    File = "File"
    Tag = "Tag"
    Event = "Event"


class SQLResult(BaseModel):
    operation_id: str
    status: OperationResultEnum
    record_id: str | None = None
    type: SQLOperationType


# class InsertResult(SQLResult):
#     id: str
#     path: Path
#     hash: str
#     size: int
#     format: str
#     mime_type: str


class DatabaseEngineProtocol(Protocol):
    def insert(
        self,
        filename: str,
        file_path: Path,
        file_hash: str,
        file_size: int,
        file_format: str,
        file_mime_type: str,
    ) -> SQLResult: ...

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
