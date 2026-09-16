# Code by AkinoAlice@TyrantRey

"""One ``.tfsfunctions.yaml`` (DESIGN/v0-4-0.md §3–§4).

    version: 1
    functions:
      photo:                  # script/photo.py
        resize:               # def resize(...) inside it
          width: 800          # parameters, by name, in YAML's own types
          exclude:
            tags: [draft]
            filename: ["*.tmp"]

This module reads and validates one file and applies an entry's ``exclude``
to a file; it knows nothing about the tree, the loader or the daemon
(``store.py`` does). Parameter values keep their YAML types — ``canonical_json``
encodes them, so they are run-key material as they are.

Two kinds of failure (§8 and the implementation notes):

- the document's *skeleton* is wrong (unreadable, a YAML syntax error, not a
  mapping, an unknown top-level key, ``functions`` or a script's value not a
  mapping) → ``FunctionsError`` — the whole file is refused;
- one *handler block* is wrong (not a mapping, a malformed ``exclude``, a
  parameter name that is not a string) → that entry lands in
  ``FunctionsFile.invalid`` and the rest of the file still applies.

``version`` missing, not an integer, or newer than ``SUPPORTED_VERSION`` is a
``FunctionsError`` of kind ``functions.version``: the rule the database already
applies to itself.
"""

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from fnmatch import fnmatchcase

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from tag_file_system.addons.binding import RESERVED_PARAMETERS
from tag_file_system.core.interface.action import canonical_json
from tag_file_system.core.interface.tag import normalize_tag
from tag_file_system.root import FUNCTIONS_FILE

SUPPORTED_VERSION = 1

ProblemKind = Literal["functions.parse", "functions.version"]


def file_label(folder: str) -> str:
    """How a folder's file is named in messages: ``a/b/.tfsfunctions.yaml``,
    or just the file name for the root."""
    return f"{folder}/{FUNCTIONS_FILE}" if folder else FUNCTIONS_FILE


def display(script: str, handler: str, args: dict[str, Any]) -> str:
    """The run's ``slug``: ``photo.resize(width=800)``. Display only, never parsed."""
    inner = ", ".join(f"{name}={canonical_json(value)}" for name, value in args.items())
    return f"{script}.{handler}({inner})"


class FunctionsError(Exception):
    """The whole file is refused; the previously loaded version stays."""

    def __init__(self, kind: ProblemKind, folder: str, message: str) -> None:
        super().__init__(message)
        self.kind: ProblemKind = kind
        self.folder = folder
        self.message = message


# ------------------------------------------------------------- predicates


@dataclass(frozen=True)
class Subject:
    """What a predicate sees of a file. A new kind of leaf adds a field here
    and every caller keeps working."""

    name: str  # the file name alone
    tags: frozenset[str]  # its full tag set: inherited, its own, ctx.tag


class Predicate:
    """A node of an entry's ``exclude`` tree (§4). ``reason`` is ``text`` of
    the branch that held — what ``tfs explain`` prints."""

    def matches(self, subject: Subject) -> bool:
        return self.reason(subject) is not None

    def reason(self, subject: Subject) -> str | None:
        raise NotImplementedError

    def text(self) -> str:
        raise NotImplementedError


def _values(values: tuple[str, ...]) -> str:
    return values[0] if len(values) == 1 else "(" + ", ".join(values) + ")"


@dataclass(frozen=True)
class TagIs(Predicate):
    """The file carries one of ``names`` (normalized)."""

    names: tuple[str, ...]

    def reason(self, subject: Subject) -> str | None:
        for name in self.names:
            if name in subject.tags:
                return f"tag {name}"
        return None

    def text(self) -> str:
        return f"tag {_values(self.names)}"


@dataclass(frozen=True)
class FilenameMatches(Predicate):
    """The file's name matches one of ``patterns`` — ``fnmatch`` globs,
    case-insensitively: one root is read from NTFS and ext4 both."""

    patterns: tuple[str, ...]

    def reason(self, subject: Subject) -> str | None:
        lowered = subject.name.lower()
        for pattern in self.patterns:
            if fnmatchcase(lowered, pattern.lower()):
                return f"filename {pattern}"
        return None

    def text(self) -> str:
        return f"filename {_values(self.patterns)}"


@dataclass(frozen=True)
class AnyOf(Predicate):
    children: tuple[Predicate, ...]

    def reason(self, subject: Subject) -> str | None:
        for child in self.children:
            found = child.reason(subject)
            if found is not None:
                return found
        return None

    def text(self) -> str:
        return "any(" + ", ".join(c.text() for c in self.children) + ")"


@dataclass(frozen=True)
class AllOf(Predicate):
    children: tuple[Predicate, ...]

    def reason(self, subject: Subject) -> str | None:
        reasons = [child.reason(subject) for child in self.children]
        if any(r is None for r in reasons):
            return None
        return "all(" + ", ".join(r for r in reasons if r is not None) + ")"

    def text(self) -> str:
        return "all(" + ", ".join(c.text() for c in self.children) + ")"


@dataclass(frozen=True)
class Not(Predicate):
    child: Predicate

    def reason(self, subject: Subject) -> str | None:
        return None if self.child.matches(subject) else f"not({self.child.text()})"

    def text(self) -> str:
        return f"not({self.child.text()})"


PREDICATE_KEYS = ("any", "all", "not", "tag", "filename")


def _strings(value: Any, where: str) -> tuple[str, ...]:
    """A value, or a list of values, that must all be strings: a list where a
    single value goes means "any of them"."""
    values = value if isinstance(value, list) else [value]
    if not values:
        raise ValueError(f"{where}: needs at least one value")
    for item in values:
        if not isinstance(item, str):
            raise ValueError(f"{where}: expected a string, got {type(item).__name__}")
    return tuple(values)


def _nodes(value: Any, where: str) -> tuple[Predicate, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{where}: expected a list of predicates")
    return tuple(parse_predicate(item, f"{where}[{i}]") for i, item in enumerate(value))


def parse_predicate(raw: Any, where: str = "exclude") -> Predicate:
    """Build the tree of §4 from YAML. A list is ``any`` of its items; a
    mapping is one node, keyed by exactly one of ``PREDICATE_KEYS``.
    ``ValueError`` names the offending node (``exclude.all[1].tag``)."""
    if isinstance(raw, list):
        return AnyOf(_nodes(raw, where))
    if not isinstance(raw, dict) or len(raw) != 1:
        raise ValueError(
            f"{where}: expected a mapping with one of {', '.join(PREDICATE_KEYS)} "
            f"as its single key, or a list of them"
        )
    ((key, value),) = raw.items()
    inner = f"{where}.{key}"
    if key == "any":
        return AnyOf(_nodes(value, inner))
    if key == "all":
        children = _nodes(value, inner)
        if not children:
            raise ValueError(f"{inner}: an empty all would exclude every file")
        return AllOf(children)
    if key == "not":
        return Not(parse_predicate(value, inner))
    if key == "tag":
        names: list[str] = []
        for raw_name in _strings(value, inner):
            name = normalize_tag(raw_name)
            if not name:
                raise ValueError(f"{inner}: tag {raw_name!r} is empty once normalized")
            names.append(name)
        return TagIs(tuple(names))
    if key == "filename":
        patterns = _strings(value, inner)
        if any(not p.strip() for p in patterns):
            raise ValueError(f"{inner}: an empty pattern matches nothing")
        return FilenameMatches(patterns)
    raise ValueError(
        f"{where}: unknown predicate {key!r}; expected one of {', '.join(PREDICATE_KEYS)}"
    )


@dataclass(frozen=True)
class Exclude:
    """An entry's exclusion (§4): the file is skipped when the tree holds."""

    predicate: Predicate | None = None

    @classmethod
    def parse(cls, raw: Any) -> "Exclude":
        """``exclude:`` with nothing under it excludes nothing."""
        return cls(None) if raw is None else cls(parse_predicate(raw))

    def reason(self, subject: Subject) -> str | None:
        """Why the file is excluded, rendered (``tag draft``,
        ``all(tag raw, not(tag keep))``), or ``None``."""
        return None if self.predicate is None else self.predicate.reason(subject)

    def text(self) -> str:
        return "" if self.predicate is None else self.predicate.text()


@dataclass(frozen=True)
class Entry:
    """One handler invocation declared by one folder's file."""

    folder: str  # the folder whose file declared it ("" for the root)
    script: str  # script/<script>.py
    handler: str  # the function's __name__ inside it
    args: dict[str, Any]  # parameters by name, YAML-typed, `exclude` removed
    exclude: Exclude
    order: int  # position within the file: document order is run order

    @property
    def ref(self) -> str:
        return f"{self.script}.{self.handler}"

    @property
    def args_json(self) -> str:
        return canonical_json(self.args)

    @property
    def identity(self) -> tuple[str, str, str]:
        """What makes two entries the same work (§3.3): the run key minus the file."""
        return (self.script, self.handler, self.args_json)

    def display(self) -> str:
        return display(self.script, self.handler, self.args)


@dataclass
class FunctionsFile:
    folder: str
    path: Path
    digest: str  # sha256 of the bytes read: what "still current" means
    entries: list[Entry] = field(default_factory=list)
    invalid: list[tuple[str, str, str]] = field(default_factory=list)
    """``(handler ref, problem kind, message)`` for every block that could not
    become an entry; the message is unlabelled, the store prefixes the file."""


class _Skeleton(BaseModel):
    """The document above the handler blocks; anything wrong here refuses the file."""

    model_config = ConfigDict(extra="forbid")

    version: Any = None  # checked by hand first, so its problem has its own kind
    functions: dict[str, dict[str, Any] | None] | None = None


def _describe(error: ValidationError) -> str:
    first = error.errors()[0] if error.errors() else None
    if first is None:
        return str(error)
    where = ".".join(str(part) for part in first.get("loc", ()))
    message = str(first.get("msg", error)).removeprefix("Value error, ")
    return f"{where}: {message}" if where else message


def _check_version(folder: str, data: dict[str, Any]) -> None:
    if "version" not in data:
        raise FunctionsError(
            "functions.version",
            folder,
            f"version is required; this release reads version {SUPPORTED_VERSION}",
        )
    version = data["version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise FunctionsError(
            "functions.version", folder, f"version must be an integer, got {version!r}"
        )
    if version > SUPPORTED_VERSION:
        raise FunctionsError(
            "functions.version",
            folder,
            f"version {version} is newer than this release understands "
            f"({SUPPORTED_VERSION}); upgrade TagFileSystem",
        )


def _block(
    folder: str, script: str, handler: str, block: Any, order: int
) -> Entry | tuple[str, str, str]:
    ref = f"{script}.{handler}"
    if block is None:
        block = {}  # `thumbnail:` with nothing under it: switched on, no parameters
    if not isinstance(block, dict):
        return (
            ref,
            "functions.signature",
            f"{ref}: expected a mapping of parameters, got {type(block).__name__}",
        )
    params = dict(block)
    try:
        exclude = Exclude.parse(params.pop("exclude", None))
    except ValueError as e:
        return (ref, "functions.signature", f"{ref}: {e}")
    odd = [name for name in params if not isinstance(name, str)]
    if odd:
        return (
            ref,
            "functions.signature",
            f"{ref}: parameter names must be strings, got {odd[0]!r}",
        )
    reserved = sorted(name for name in params if name in RESERVED_PARAMETERS)
    if (
        reserved
    ):  # cannot happen today (`exclude` was popped) but keeps the rule in one place
        return (
            ref,
            "functions.signature",
            f"{ref}: reserved key(s) {', '.join(reserved)}",
        )
    return Entry(
        folder=folder,
        script=script,
        handler=handler,
        args=params,
        exclude=exclude,
        order=order,
    )


def load_file(path: Path, folder: str) -> FunctionsFile:
    """Read and validate one file. Raises ``FunctionsError`` when the whole
    file must be refused; block-level problems land in ``.invalid``."""
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise FunctionsError("functions.parse", folder, f"cannot read: {e}") from e
    digest = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise FunctionsError("functions.parse", folder, f"not UTF-8: {e}") from e
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        message = " ".join(str(e).split())
        raise FunctionsError(
            "functions.parse", folder, f"YAML syntax: {message}"
        ) from e
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise FunctionsError(
            "functions.parse",
            folder,
            f"the document must be a mapping, got {type(data).__name__}",
        )
    _check_version(folder, data)
    try:
        skeleton = _Skeleton.model_validate(data)
    except ValidationError as e:
        raise FunctionsError("functions.parse", folder, _describe(e)) from e

    parsed = FunctionsFile(folder=folder, path=path, digest=digest)
    order = 0
    for script, handlers in (skeleton.functions or {}).items():
        for handler, block in (handlers or {}).items():
            built = _block(folder, script, handler, block, order)
            if isinstance(built, Entry):
                parsed.entries.append(built)
                order += 1
            else:
                parsed.invalid.append(built)
    return parsed
