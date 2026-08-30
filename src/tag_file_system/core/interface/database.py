# Code by AkinoAlice@TyrantRey

from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePath
from typing import Protocol

from pydantic import BaseModel

from tag_file_system.core.interface.file_metadata import Tag, TaggedFile

# Any path the backend accepts: root-relative (``PurePosixPath`` preferred)
# or absolute under the root it was opened with.
PathLike = PurePath | str


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


class FileStatus(StrEnum):
    ACTIVE = "active"
    DELETED = "deleted"
    ARCHIVED = "archived"


class SQLResult(BaseModel):
    """Outcome of one backend operation.

    ``operation_id`` is a UUID generated for the call. When the operation
    records exactly one ``events`` row, that row reuses the same id.
    ``record_id`` is the id of the affected ``files`` / ``tags`` row when one
    could be resolved.
    """

    operation_id: str
    status: OperationResultEnum
    record_id: str | None = None
    type: SQLOperationType


class DatabaseEngineProtocol(Protocol):
    def init_database(
        self, database_path: Path, root_dir: Path | None = None
    ) -> bool: ...

    def close(self) -> None: ...

    # -- files ---------------------------------------------------------------

    def insert(
        self,
        filename: str,
        file_path: PathLike,
        file_hash: str,
        file_size: int,
        file_format: str | None = None,
        file_mime_type: str | None = None,
        mtime_ns: int | None = None,
    ) -> SQLResult: ...

    def update(
        self,
        file_path: PathLike,
        file_hash: str,
        file_size: int,
        file_format: str | None = None,
        file_mime_type: str | None = None,
        mtime_ns: int | None = None,
    ) -> SQLResult: ...

    def delete(self, file_path: PathLike) -> SQLResult: ...

    def modify(self, file_path: PathLike, new_path: PathLike) -> SQLResult: ...

    # -- tags ----------------------------------------------------------------

    def upsert_tag(
        self,
        name: str,
        category: str | None = None,
        description: str | None = None,
    ) -> SQLResult: ...

    def set_file_tags(self, file_path: PathLike, tag_names: list[str]) -> SQLResult: ...

    # -- queries -------------------------------------------------------------

    def query_tag(
        self, tag_name: str | None = None, tag_id: str | None = None
    ) -> Tag | None: ...

    def query_file(
        self, file_path: PathLike, include_deleted: bool = False
    ) -> TaggedFile | None: ...

    def query_files(
        self,
        tags: list[str] | None = None,
        tag_ids: list[str] | None = None,
        filename: str | None = None,
        file_hash: str | None = None,
        file_format: str | None = None,
        file_type: str | None = None,
        mime_type: str | None = None,
        file_size_range: tuple[int, int] | None = None,
        file_added_range: tuple[datetime, datetime] | None = None,
        include_deleted: bool = False,
        path_prefix: str | None = None,
    ) -> list[TaggedFile]: ...
