# Code by AkinoAlice@TyrantRey
from datetime import date

from pydantic import BaseModel, Field


class FileMetadataFilter(BaseModel):
    tags: list[str] | None = Field(default=None)
    ext: str | None = Field(default=None)
    size_gt: int | None = Field(default=None, ge=0)
    size_lt: int | None = Field(default=None, ge=0)
    date_after: date | None = Field(default=None)
    date_before: date | None = Field(default=None)
    date_on: date | None = Field(default=None)
