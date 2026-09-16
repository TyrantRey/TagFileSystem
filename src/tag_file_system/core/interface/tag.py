# Code by AkinoAlice@TyrantRey

"""Models produced by the name grammar (DESIGN/v0-1-0.md §3, DESIGN/v0-4-0.md §2).

segment := label? marker*
marker  := '--' tag                  -> Tag

``@@`` is still a marker boundary: it once introduced a function call, and a
name that carries one is reported (the function moved to ``.tfsfunctions.yaml``)
rather than silently read as label text.
"""

import re
import unicodedata
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator

TAG_PREFIX = "--"
FUNCTION_PREFIX = "@@"  # rejected, never built (DESIGN/v0-4-0.md §2)
MARKER_PREFIXES = (TAG_PREFIX, FUNCTION_PREFIX)

# Characters that can never appear inside a marker: path separators, the
# characters NTFS refuses (so a name that parses here is valid on every OS)
# and line breaks.
ILLEGAL_MARKER_CHARS = frozenset(':/\\<>|?*"\n\r')


def normalize_tag(raw: str) -> str:
    tag = unicodedata.normalize("NFC", raw).lower()
    tag = re.sub(r"[^\w\-]", "", tag, flags=re.UNICODE)
    tag = re.sub(r"-+", "-", tag)
    tag = tag.strip("-")
    return tag


def illegal_chars(text: str) -> str:
    """The illegal characters found in ``text``, sorted, as one string."""
    return "".join(sorted(set(text) & ILLEGAL_MARKER_CHARS))


def _check_marker_text(text: str, what: str, *tokens: str) -> None:
    """Reject text that could not have come from a single marker: illegal
    characters, a marker prefix, or any extra ``tokens``."""
    bad = illegal_chars(text)
    if bad:
        raise ValueError(f"{what} contains illegal characters {bad!r}")
    for token in (*MARKER_PREFIXES, *tokens):
        if token in text:
            raise ValueError(f"{what} may not contain {token!r}")


class Tag(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Tag name cannot be empty")
        _check_marker_text(v, "Tag name")

        normalized = normalize_tag(v)
        if not normalized:
            raise ValueError(f"Tag name '{v}' is invalid after normalization")
        return normalized

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"Tag('{self.name}')"


class ParseProblem(BaseModel):
    """A marker that was skipped; surfaced by the pipeline as a P2 warn."""

    segment: str
    marker: str
    message: str

    def __str__(self) -> str:
        return f"{self.marker!r} in {self.segment!r}: {self.message}"


class TagParserOutput(BaseModel):
    """Result of parsing one path segment."""

    tags: list[Tag] = Field(default_factory=list)
    problems: list[ParseProblem] = Field(default_factory=list)


class ParsedPath(BaseModel):
    """Everything a root-relative path says about the file at its end:
    ``tags`` is the union over every segment (first occurrence wins)."""

    path: PurePosixPath
    tags: list[Tag] = Field(default_factory=list)
    problems: list[ParseProblem] = Field(default_factory=list)

    @property
    def tag_names(self) -> list[str]:
        return [tag.name for tag in self.tags]
