# Code by AkinoAlice@TyrantRey

"""Models produced by the name grammar (DESIGN/v0-1-0.md §3).

segment := label? marker*
marker  := '@@' func ('__' arg)*     -> ActionCall
         | '--' tag                  -> Tag
"""

import re
import unicodedata
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator

ARG_SEPARATOR = "__"
MARKER_PREFIXES = ("--", "@@")

# Characters that can never appear inside a marker: path separators, the
# characters NTFS refuses (so a name that parses here is valid on every OS)
# and line breaks.
ILLEGAL_MARKER_CHARS = frozenset(':/\\<>|?*"\n\r')

_FUNC_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


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


class ActionCall(BaseModel):
    """One ``@@func__arg1__arg2`` marker: a function slug plus positional args.

    Frozen so calls can be de-duplicated by value. ``name`` is normalized to
    lowercase; args are kept verbatim apart from Unicode NFC normalization
    (the add-on's annotations coerce them). Every value is checked so that
    ``slug`` always re-parses to the same call.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    args: tuple[str, ...] = ()

    @classmethod
    def from_marker(cls, raw: str) -> "ActionCall":
        name, *args = raw.split(ARG_SEPARATOR)
        return cls(name=name, args=tuple(args))

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        name = unicodedata.normalize("NFC", v).lower()
        if not name:
            raise ValueError("Function name cannot be empty")
        _check_marker_text(name, "Function name", ARG_SEPARATOR)
        if not _FUNC_NAME.match(name) or name.endswith("_"):
            raise ValueError(
                f"Function name '{v}' must match [a-z][a-z0-9_]* and not end with '_'"
            )
        return name

    @field_validator("args")
    @classmethod
    def validate_args(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for arg in v:
            if not arg:
                raise ValueError("Empty argument (check for a trailing '__')")
            arg = unicodedata.normalize("NFC", arg)
            _check_marker_text(arg, f"Argument '{arg}'", ARG_SEPARATOR)
            if arg.startswith("_") or arg.endswith("_"):
                raise ValueError(f"Argument '{arg}' may not start or end with '_'")
            normalized.append(arg)
        return tuple(normalized)

    @property
    def slug(self) -> str:
        """Exactly what the marker looks like on disk, without the ``@@``."""
        return ARG_SEPARATOR.join((self.name, *self.args))

    def __str__(self) -> str:
        return f"@@{self.slug}"

    def __repr__(self) -> str:
        return f"ActionCall({self.name!r}, {list(self.args)!r})"


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
    actions: list[ActionCall] = Field(default_factory=list)
    problems: list[ParseProblem] = Field(default_factory=list)


class ParsedPath(BaseModel):
    """Everything a root-relative path says about the file at its end.

    ``tags`` is the union over every segment (first occurrence wins);
    ``actions`` is every distinct ``(name, args)`` in parent-first order, the
    filename's own markers last.
    """

    path: PurePosixPath
    tags: list[Tag] = Field(default_factory=list)
    actions: list[ActionCall] = Field(default_factory=list)
    problems: list[ParseProblem] = Field(default_factory=list)

    @property
    def tag_names(self) -> list[str]:
        return [tag.name for tag in self.tags]
