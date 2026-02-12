# Code by AkinoAlice@TyrantRey

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel


class TaggedFile(BaseModel):
    file_hash: str
    original_path: Path
    tags: list[Tag] = []
    metadata: FileMetadata | None = None


class FileMetadata(BaseModel):
    file_size: int
    time_added: datetime


class Tag(BaseModel):
    name: str
    tag_hash: int
    time_added: datetime
