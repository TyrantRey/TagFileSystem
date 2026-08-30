# Code by AkinoAlice@TyrantRey

from datetime import datetime
from pathlib import Path, PurePosixPath

from pydantic import BaseModel


class FileMetadata(BaseModel):
    file_size: int
    time_added: datetime
    file_format: str | None
    file_type: str | None
    mime_type: str | None = None
    mtime_ns: int | None = None  # st_mtime_ns at the last hash (DESIGN.md §5)


class Tag(BaseModel):
    name: str
    tag_id: str  # uuid
    time_added: datetime


class TaggedFile(BaseModel):
    """A ``files`` row.

    ``path`` is the stored key: root-relative, POSIX separators, portable
    between hosts. ``original_path`` is that key resolved against the root
    the backend was opened with (or the bare key when no root is known).
    """

    file_hash: str
    path: PurePosixPath
    original_path: Path
    file_id: str | None = None
    status: str = "active"
    tags: list[Tag] = []
    metadata: FileMetadata | None = None
