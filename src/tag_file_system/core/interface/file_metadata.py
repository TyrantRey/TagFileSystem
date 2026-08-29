# Code by AkinoAlice@TyrantRey

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel


class FileMetadata(BaseModel):
    file_size: int
    time_added: datetime
    file_format: str | None
    file_type: str | None
    mime_type: str | None = None


class Tag(BaseModel):
    name: str
    tag_id: str  # uuid
    time_added: datetime


class TaggedFile(BaseModel):
    file_hash: str
    original_path: Path
    file_id: str | None = None
    status: str = "active"
    tags: list[Tag] = []
    metadata: FileMetadata | None = None
