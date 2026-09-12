# Code by AkinoAlice@TyrantRey

"""``tfs migrate``: turn a v1 root's ``@@func__arg`` directories into
``.tfsfunctions.yaml`` files (DESIGN/v0-4-0.md §10).

The conversion is exact: the directory names the script and its positional
arguments, the loaded handler's signature names the parameters those
positions fill, and its annotations give the values their types (``"800"``
becomes ``800``). What cannot be converted is listed, never guessed:

- a ``@@`` marker on a *file* name — there is no folder to hold the entry;
- a script that is not loaded, or a call with more arguments than a handler
  takes;
- a directory that already has a ``.tfsfunctions.yaml`` — merge by hand;
- a second call of the same script in one folder — one entry per handler
  per folder; nest a folder for the other.

Renaming the directories is a separate step (``--rename``): it moves every
file underneath, which a running daemon sees as a whole subtree leaving and
re-appearing. The markers are stripped and the ``--tags`` kept; a name that
would come out empty (``@@resize__800``) becomes the call spelled with
hyphens (``resize-800``).
"""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import TypeAdapter, ValidationError

from tag_file_system.addons.binding import (
    Parameter,
    SignatureError,
    _coerce_literal,
    parameters_of,
)
from tag_file_system.addons.loader import AddonLoader
from tag_file_system.core.interface.action import canonical_json
from tag_file_system.core.interface.tag import (
    FUNCTION_PREFIX,
    TAG_PREFIX,
    normalize_tag,
)
from tag_file_system.root import FUNCTIONS_FILE, SCRIPT_DIR, TFS_DIR, Root, same_name
from tag_file_system.services.tagging import split_extension

# The v1 grammar (DESIGN/v0-1-0.md §3), kept here and nowhere else.
_LEGACY_MARKER = re.compile(r"(--|@@)(.*?)(?=--|@@|\Z)", re.DOTALL)
_LEGACY_SEPARATOR = "__"
_HEADER = (
    "# Written by `tfs migrate` from this folder's v1 name (DESIGN/v0-4-0.md §10).\n"
    "# Read at `tfs start` and `tfs reload`; edits are not applied live.\n"
)


@dataclass(frozen=True)
class LegacyCall:
    name: str
    args: tuple[str, ...]

    @property
    def slug(self) -> str:
        return _LEGACY_SEPARATOR.join((self.name, *self.args))


def legacy_calls(segment: str) -> list[LegacyCall]:
    """The ``@@`` calls a v1 segment spelled, in order, without repeats."""
    calls: dict[LegacyCall, None] = {}
    for prefix, value in _LEGACY_MARKER.findall(segment):
        if prefix != FUNCTION_PREFIX or not value:
            continue
        name, *args = value.split(_LEGACY_SEPARATOR)
        calls.setdefault(LegacyCall(name=name.lower(), args=tuple(args)))
    return list(calls)


def legacy_tags(segment: str) -> list[str]:
    tags: list[str] = []
    for prefix, value in _LEGACY_MARKER.findall(segment):
        if prefix == TAG_PREFIX:
            name = normalize_tag(value)
            if name and name not in tags:
                tags.append(name)
    return tags


def stripped_name(segment: str) -> str:
    """The segment without its ``@@`` markers: label plus ``--tags``."""
    first = _LEGACY_MARKER.search(segment)
    label = segment[: first.start()] if first else segment
    name = label + "".join(f"{TAG_PREFIX}{tag}" for tag in legacy_tags(segment))
    if not name:
        calls = legacy_calls(segment)
        name = "-".join((calls[0].name, *calls[0].args)) if calls else segment
    return name


@dataclass
class Write:
    directory: Path
    folder: str  # root-relative POSIX key
    entries: dict[str, dict[str, dict[str, Any]]]  # script -> handler -> params

    @property
    def text(self) -> str:
        body = yaml.safe_dump(
            {"version": 1, "functions": self.entries},
            sort_keys=False,
            allow_unicode=True,
        )
        return _HEADER + body


@dataclass
class Rename:
    source: Path
    target: Path


@dataclass
class Plan:
    writes: list[Write] = field(default_factory=list)
    renames: list[Rename] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _typed(parameter: Parameter, raw: str) -> Any:
    """The positional string as the annotation would read it, JSON-native so
    it dumps as plain YAML; the string itself when it does not fit (the file
    then fails validation at load, which says exactly why)."""
    if parameter.path_kind:
        return raw
    try:
        value = TypeAdapter(parameter.annotation).validate_python(
            _coerce_literal(parameter.annotation, raw)
        )
        return json.loads(canonical_json(value))
    except (ValidationError, TypeError, ValueError):
        return raw


def plan(root: Root, loader: AddonLoader) -> Plan:
    """What ``tfs migrate`` would do to ``root`` with the add-ons ``loader``
    has loaded. Nothing is touched."""
    result = Plan()
    for current, dirs, files in os.walk(root.path):
        here = Path(current)
        dirs[:] = sorted(
            d
            for d in dirs
            if not (here == root.path and same_name(d, SCRIPT_DIR))
            and not same_name(d, TFS_DIR)
        )
        for name in sorted(files):
            if legacy_calls(split_extension(name)):
                key = _key(root, here / name)
                result.skipped.append(
                    f"{key}: a function on a file name has no equivalent; "
                    "use the folder's file (or an exclusion) instead"
                )
        for name in dirs:
            calls = legacy_calls(name)
            if not calls:
                continue
            directory = here / name
            folder = _key(root, directory)
            existing = directory / FUNCTIONS_FILE
            if existing.exists():
                # Ours from an earlier --apply: done. Someone else's: the v1
                # name is theirs to merge. Either way the rename still stands.
                if not _written_by_migrate(existing):
                    result.skipped.append(
                        f"{folder}: {FUNCTIONS_FILE} exists; merge the v1 name by hand"
                    )
            else:
                entries: dict[str, dict[str, dict[str, Any]]] = {}
                for call in calls:
                    _convert(loader, folder, call, entries, result.skipped)
                if entries:
                    result.writes.append(Write(directory, folder, entries))
            target = directory.with_name(stripped_name(name))
            if target.exists():
                result.skipped.append(
                    f"{folder}: cannot rename to {target.name}: it exists"
                )
            else:
                result.renames.append(Rename(directory, target))
    return result


def _convert(
    loader: AddonLoader,
    folder: str,
    call: LegacyCall,
    entries: dict[str, dict[str, dict[str, Any]]],
    skipped: list[str],
) -> None:
    label = f"{folder}: @@{call.slug}"
    addon = loader.addon_for(call.name)
    if addon is None:
        skipped.append(f"{label}: script/{call.name}.py is not loaded")
        return
    if call.name in entries:
        skipped.append(
            f"{label}: a second call of {call.name} in one folder; nest a folder for it"
        )
        return
    handlers = {h.name: h for h in addon.file_handlers}
    if not handlers:
        skipped.append(f"{label}: script/{call.name}.py has no file handler")
        return
    converted: dict[str, dict[str, Any]] = {}
    for handler_name, handler in handlers.items():
        try:
            params = parameters_of(handler.func)
        except SignatureError as e:
            skipped.append(f"{label}: {call.name}.{handler_name}: {e}")
            continue
        if len(call.args) > len(params):
            skipped.append(
                f"{label}: {call.name}.{handler_name} takes {len(params)} "
                f"argument(s), the name gives {len(call.args)}"
            )
            continue
        converted[handler_name] = {
            p.name: _typed(p, raw) for p, raw in zip(params, call.args)
        }
    if converted:
        entries[call.name] = converted


def apply_writes(migration: Plan) -> list[Path]:
    written: list[Path] = []
    for item in migration.writes:
        path = item.directory / FUNCTIONS_FILE
        path.write_text(item.text, encoding="utf-8")
        written.append(path)
    return written


def apply_renames(migration: Plan) -> list[Rename]:
    """Deepest first, so a child is renamed before its parent's path changes."""
    done: list[Rename] = []
    for item in sorted(
        migration.renames, key=lambda r: len(r.source.parts), reverse=True
    ):
        item.source.rename(item.target)
        done.append(item)
    return done


def _written_by_migrate(path: Path) -> bool:
    try:
        with path.open(encoding="utf-8") as handle:
            return handle.readline() == _HEADER.splitlines(keepends=True)[0]
    except OSError:
        return False


def _key(root: Root, path: Path) -> str:
    return "/".join(path.relative_to(root.path).parts)
