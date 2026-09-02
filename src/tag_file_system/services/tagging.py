# Code by AkinoAlice@TyrantRey

import re
from dataclasses import dataclass
from pathlib import PurePath, PurePosixPath
from typing import Callable

from pydantic import BaseModel, ValidationError

from tag_file_system.core.interface.tag import (
    ARG_SEPARATOR,
    MARKER_PREFIXES,
    ActionCall,
    ParsedPath,
    ParseProblem,
    Tag,
    TagParserOutput,
    illegal_chars,
)
from tag_file_system.core.logger import logger

MarkerFactory = Callable[[str], BaseModel]

# Output fields a marker may be routed to, with the model each one holds.
# ``problems`` is the parser's own.
_TARGET_FIELDS: dict[str, type[BaseModel]] = {"tags": Tag, "actions": ActionCall}


@dataclass(frozen=True)
class Marker:
    prefix: str  # e.g. "--"
    field: str  # TagParserOutput field the built objects are collected into
    factory: MarkerFactory  # raw marker text -> model


def split_extension(filename: str) -> str:
    """The filename without its extension, for parsing.

    The extension is what follows the last ``.`` (``Path.suffix``), *unless*
    that text contains marker syntax — then it is part of a marker, not an
    extension (``@@make_copy__.jpg__photos`` keeps ``.jpg__photos``). A
    marker value that itself ends in a dotted part (``@@resize__1.5``) is
    still cut: markers in filenames should precede the extension.
    """
    suffix = PurePath(filename).suffix
    if not suffix:
        return filename
    if any(token in suffix for token in (*MARKER_PREFIXES, ARG_SEPARATOR)):
        return filename
    return filename[: -len(suffix)]


class TaggingParser:
    """Parse the name grammar of DESIGN/v0-1-0.md §3.

    ``parse`` handles one segment (a directory name or a filename stem);
    ``parse_path`` walks every segment of a root-relative path and merges the
    results parent-first.

    Markers are registered, not hard-coded: ``register(prefix, field)`` maps a
    prefix to the ``TagParserOutput`` field its results land in and to a
    factory that turns the raw text into a model. ``--`` (tags) and ``@@``
    (actions) are registered by default; re-registering a prefix replaces its
    factory. A marker whose text contains an illegal character, or whose
    factory rejects it (``ValidationError``/``ValueError``) or returns the
    wrong model, is recorded as a ``ParseProblem`` and skipped; the rest of
    the segment still parses. Duplicate values within a segment are dropped.
    """

    def __init__(self) -> None:
        self.logger = logger
        self._markers: dict[str, Marker] = {}
        self._pattern: re.Pattern[str] | None = None

        self.register("--", "tags")(lambda value: Tag(name=value))
        self.register("@@", "actions")(ActionCall.from_marker)

    def register(
        self, prefix: str, field: str
    ) -> Callable[[MarkerFactory], MarkerFactory]:
        if not prefix:
            raise ValueError("Marker prefix cannot be empty")
        if field not in _TARGET_FIELDS:
            raise ValueError(
                f"Unknown output field {field!r}; expected one of {list(_TARGET_FIELDS)}"
            )

        def decorator(factory: MarkerFactory) -> MarkerFactory:
            self._markers[prefix] = Marker(prefix, field, factory)
            self._pattern = None
            return factory

        return decorator

    @property
    def markers(self) -> dict[str, Marker]:
        return dict(self._markers)

    @property
    def pattern(self) -> str:
        prefixes = "|".join(
            re.escape(prefix) for prefix in sorted(self._markers, key=lambda p: -len(p))
        )
        # DOTALL + \Z so a marker containing a line break is captured whole
        # (and then rejected as illegal) instead of being cut at the newline.
        return rf"({prefixes})(.*?)(?={prefixes}|\Z)"

    def _compiled(self) -> re.Pattern[str]:
        if self._pattern is None:
            self._pattern = re.compile(self.pattern, re.DOTALL)
        return self._pattern

    # ---------------------------------------------------------------- segment

    def parse(self, segment: str) -> TagParserOutput:
        """Parse one segment. Text before the first marker is the label."""
        results: dict[str, dict[BaseModel, None]] = {f: {} for f in _TARGET_FIELDS}
        problems: list[ParseProblem] = []

        for prefix, value in self._compiled().findall(segment):
            marker = self._markers[prefix]
            raw = f"{prefix}{value}"
            expected = _TARGET_FIELDS[marker.field]

            bad = illegal_chars(value)
            if bad:
                message = f"illegal characters {bad!r} in marker"
            else:
                try:
                    built = marker.factory(value)
                except (ValidationError, ValueError) as e:
                    message = self._describe(e)
                else:
                    if isinstance(built, expected):
                        results[marker.field].setdefault(built)
                        continue
                    message = (
                        f"factory for {prefix!r} returned "
                        f"{type(built).__name__}, expected {expected.__name__}"
                    )

            # One malformed marker must not lose the rest of the segment.
            problem = ParseProblem(segment=segment, marker=raw, message=message)
            self.logger.warning("Skipping marker %r: %s", raw, message)
            problems.append(problem)

        return TagParserOutput.model_validate(
            {field: list(built) for field, built in results.items()}
            | {"problems": problems}
        )

    @staticmethod
    def _describe(error: Exception) -> str:
        if isinstance(error, ValidationError):
            errors = error.errors()
            if errors:
                return str(errors[0].get("msg", error)).removeprefix("Value error, ")
        return str(error)

    # ------------------------------------------------------------------- path

    def parse_path(self, path: PurePath | str, is_file: bool = True) -> ParsedPath:
        """Parse every segment of a root-relative path and merge parent-first.

        For a file the last segment is parsed without its extension (see
        ``split_extension``). A string is split by the host's path flavour;
        pass a ``PurePath`` to be explicit. Anchored paths (a root or a
        drive, in either flavour) and ``..`` components are rejected: callers
        relativize to the root first so the result is portable.
        """
        pure = PurePath(path) if isinstance(path, str) else path
        if pure.anchor or str(pure).startswith(("/", "\\")):
            raise ValueError(f"parse_path expects a root-relative path, got {pure}")
        parts = list(pure.parts)
        if ".." in parts:
            raise ValueError(f"parse_path does not accept '..' components: {pure}")

        posix = PurePosixPath(*parts) if parts else PurePosixPath()
        if not parts:
            return ParsedPath(path=posix)

        last = split_extension(parts[-1]) if is_file else parts[-1]
        stripped = parts[-1][len(last) :]
        segments = parts[:-1] + [last]

        tags: dict[str, Tag] = {}
        actions: dict[ActionCall, None] = {}
        problems: list[ParseProblem] = []
        for index, segment in enumerate(segments):
            parsed = self.parse(segment)
            for tag in parsed.tags:
                tags.setdefault(tag.name, tag)
            for call in parsed.actions:
                actions.setdefault(call)
            if stripped and index == len(segments) - 1 and parsed.problems:
                # A marker cut by the extension rule looks like a user typo;
                # say what actually happened.
                hint = f" (extension {stripped!r} was stripped; markers must precede the extension)"
                parsed.problems = [
                    p.model_copy(update={"message": p.message + hint})
                    for p in parsed.problems
                ]
            problems.extend(parsed.problems)

        return ParsedPath(
            path=posix,
            tags=list(tags.values()),
            actions=list(actions),
            problems=problems,
        )
