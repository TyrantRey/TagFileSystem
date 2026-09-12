# Code by AkinoAlice@TyrantRey

"""Every ``.tfsfunctions.yaml`` of a root, and what applies to one file
(DESIGN/v0-4-0.md §3.2–§5).

``FunctionsStore`` holds the last-good parse of each folder's file, validates
every entry against the loaded add-ons, and answers ``effective(key, tags)``:
the entries that apply to a file (parent first, document order, identical
entries collapsed), the ones its exclusions suppressed and why, and every
``(script, handler)`` the chain *names* — which is what suppresses a
``@action.tagged`` default. Files are read on ``load_tree()`` only (start and
explicit reload); ``rebind()`` re-validates after a script changes.

A folder is keyed by its root-relative POSIX path, ``""`` for the root.
"""

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from tag_file_system.addons.binding import check
from tag_file_system.addons.loader import AddonLoader, ProblemReporter
from tag_file_system.core.interface.action import Severity, canonical_json
from tag_file_system.core.logger import logger
from tag_file_system.functions.model import (
    Entry,
    FunctionsError,
    FunctionsFile,
    Subject,
    file_label,
    load_file,
)
from tag_file_system.root import FUNCTIONS_FILE, SCRIPT_DIR, TFS_DIR, Root, same_name


def _silent(*_args: Any, **_kwargs: Any) -> None:
    return None


def folder_of(key: str) -> str:
    """The folder a file key lives in (``""`` for the root)."""
    parent = PurePosixPath(key).parent.as_posix()
    return "" if parent == "." else parent


def chain(folder: str) -> list[str]:
    """The root down to ``folder``: ``["", "a", "a/b"]`` for ``"a/b"``."""
    if not folder:
        return [""]
    parts = folder.split("/")
    return ["", *("/".join(parts[:depth]) for depth in range(1, len(parts) + 1))]


def _folder_order(folder: str) -> tuple[int, str]:
    return (folder.count("/") + 1 if folder else 0, folder)


@dataclass(frozen=True)
class Applied:
    entry: Entry
    folder: str  # the folder whose file contributed it (provenance for `explain`)


@dataclass(frozen=True)
class Suppressed:
    entry: Entry
    folder: str
    reason: str  # "tag draft" / "filename *.tmp"


@dataclass
class Effective:
    """What one file's configuration comes to."""

    key: str
    tags: list[str]
    applied: list[Applied] = field(default_factory=list)
    suppressed: list[Suppressed] = field(default_factory=list)
    named: set[tuple[str, str]] = field(default_factory=set)
    """Every ``(script, handler)`` a valid entry in the chain names, excluded
    or not: a folder that names a ``tagged`` handler takes over its default."""


@dataclass
class LoadReport:
    folders: list[str]  # every folder with a file, parent first
    problems: int


class FunctionsStore:
    def __init__(
        self,
        root: Root,
        loader: AddonLoader,
        report: ProblemReporter | None = None,
    ) -> None:
        self.root = root
        self.loader = loader
        self.report: ProblemReporter = report if report is not None else _silent
        self.logger = logger
        self.files: dict[str, FunctionsFile] = {}  # folder -> last-good parse
        self.problems: list[dict[str, Any]] = []  # what `tfs list` shows
        self.invalid: dict[str, list[tuple[str, str, str]]] = {}
        self._file_errors: dict[str, tuple[str, str]] = {}  # folder -> (kind, message)
        self._bound: dict[str, list[Entry]] = {}  # folder -> entries that validate
        self._reported: set[tuple[str, str]] = set()

    # ---------------------------------------------------------------- reading

    def path_of(self, folder: str) -> Path:
        base = self.root.path.joinpath(*folder.split("/")) if folder else self.root.path
        return base / FUNCTIONS_FILE

    def _folder(self, directory: Path) -> str:
        return "/".join(directory.relative_to(self.root.path).parts)

    def load_tree(self) -> LoadReport:
        """Re-read every folder's file, parent first; forget folders whose
        file is gone; then ``rebind()``."""
        self._reported.clear()
        found: list[str] = []
        for current, dirs, files in os.walk(self.root.path):
            here = Path(current)
            dirs[:] = sorted(
                d
                for d in dirs
                if not (here == self.root.path and same_name(d, SCRIPT_DIR))
                and not same_name(d, TFS_DIR)
            )
            if any(same_name(name, FUNCTIONS_FILE) for name in files):
                folder = self._folder(here)
                found.append(folder)
                self.load_folder(folder)
        for gone in [f for f in {*self.files, *self._file_errors} if f not in found]:
            self.files.pop(gone, None)
            self._file_errors.pop(gone, None)
        self.rebind()
        return LoadReport(folders=found, problems=len(self.problems))

    def load_folder(self, folder: str) -> FunctionsFile | None:
        """Read one folder's file. A file that cannot be taken as a whole keeps
        the previously loaded version (§8); its problem is reported by
        ``rebind()``."""
        try:
            parsed = load_file(self.path_of(folder), folder)
        except FunctionsError as e:
            self._file_errors[folder] = (e.kind, e.message)
            return self.files.get(folder)
        self._file_errors.pop(folder, None)
        self.files[folder] = parsed
        self.logger.info(
            f"Loaded {file_label(folder)}: {len(parsed.entries)} entr(y/ies)"
            + (f", {len(parsed.invalid)} invalid" if parsed.invalid else "")
        )
        return parsed

    def is_current(self, folder: str, path: Path) -> bool:
        """Whether the file on disk is the one loaded for ``folder``."""
        parsed = self.files.get(folder)
        if parsed is None or folder in self._file_errors:
            return False
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest() == parsed.digest
        except OSError:
            return False

    # ------------------------------------------------------------- validating

    def rebind(self) -> None:
        """Validate every entry against the loaded add-ons and rebuild
        ``problems``. A problem is reported (P1) once until it goes away."""
        self.problems = []
        self._bound = {}
        self.invalid = {}
        current: set[tuple[str, str]] = set()
        for folder in sorted({*self.files, *self._file_errors}, key=_folder_order):
            label = file_label(folder)
            error = self._file_errors.get(folder)
            if error is not None:
                self._problem(error[0], f"{label}: {error[1]}", label, current)
            parsed = self.files.get(folder)
            if parsed is None:
                continue
            valid: list[Entry] = []
            invalid = list(parsed.invalid)
            for entry in parsed.entries:
                problem = self._validate(entry)
                if problem is None:
                    valid.append(entry)
                else:
                    invalid.append((entry.ref, *problem))
            self._bound[folder] = valid
            self.invalid[folder] = invalid
            for _ref, kind, message in invalid:
                self._problem(kind, f"{label}: {message}", label, current)
        # A problem that went away and comes back is news again.
        self._reported &= current

    def _problem(
        self, kind: str, message: str, label: str, current: set[tuple[str, str]]
    ) -> None:
        self.problems.append(
            {
                "severity": Severity.ERR.value,
                "kind": kind,
                "message": message,
                "file": label,
            }
        )
        current.add((kind, message))
        if (kind, message) in self._reported:
            return
        self._reported.add((kind, message))
        self.report(Severity.ERR, kind, message)

    def _validate(self, entry: Entry) -> tuple[str, str] | None:
        addon = self.loader.addon_for(entry.script)
        if addon is None:
            return (
                "functions.unbound",
                f"{entry.ref}: script/{entry.script}.py is not loaded",
            )
        marks = addon.named(entry.handler)
        if not marks:
            return (
                "functions.unbound",
                f"{entry.ref}: script/{entry.script}.py has no file handler {entry.handler}",
            )
        messages = check(marks[0].func, entry.args)
        if messages:
            return ("functions.signature", f"{entry.ref}: {'; '.join(messages)}")
        return None

    # -------------------------------------------------------------- answering

    def entries(self, folder: str) -> list[Entry]:
        """The entries of one folder's file that validate."""
        return list(self._bound.get(folder, []))

    def effective(self, key: str, tags: Iterable[str]) -> Effective:
        """What applies to the file ``key`` carrying ``tags`` (§3.3–§4)."""
        posix = PurePosixPath(key)
        subject = Subject(name=posix.name, tags=frozenset(tags))
        result = Effective(key=posix.as_posix(), tags=sorted(subject.tags))
        seen: set[tuple[str, str, str]] = set()
        for folder in chain(folder_of(result.key)):
            for entry in self._bound.get(folder, []):
                result.named.add((entry.script, entry.handler))
                reason = entry.exclude.reason(subject)
                if reason is not None:
                    result.suppressed.append(Suppressed(entry, folder, reason))
                    continue
                if entry.identity in seen:
                    continue  # the same work again, deeper down: one run (§3.3)
                seen.add(entry.identity)
                result.applied.append(Applied(entry, folder))
        return result

    def named(self, key: str) -> set[tuple[str, str]]:
        """Every ``(script, handler)`` the chain above ``key`` names."""
        return {
            (entry.script, entry.handler)
            for folder in chain(folder_of(key))
            for entry in self._bound.get(folder, [])
        }

    def suppressed_by(self, key: str, script: str, handler: str) -> str | None:
        """The nearest-to-root folder whose file names ``script.handler`` for
        ``key``, or ``None``: a ``tagged`` default gives way to it (§5)."""
        for folder in chain(folder_of(key)):
            if any(
                e.script == script and e.handler == handler
                for e in self._bound.get(folder, [])
            ):
                return folder
        return None


def _jsonable_args(args: dict[str, Any]) -> Any:
    return json.loads(canonical_json(args))


def explain_payload(
    store: FunctionsStore,
    loader: AddonLoader,
    key: str,
    tags: Iterable[str],
    known: bool,
) -> dict[str, Any]:
    """What ``tfs explain`` and ``GET /explain`` return for one file: JSON-safe
    values only (``str``/``int``/``bool``/``list``/``dict``)."""
    tag_list = list(tags)
    effective = store.effective(key, tag_list)

    def entry_dict(entry: Entry, folder: str) -> dict[str, Any]:
        return {
            "script": entry.script,
            "handler": entry.handler,
            "args": _jsonable_args(entry.args),
            "folder": folder,
            "file": file_label(folder),
            "display": entry.display(),
        }

    applied = []
    for item in effective.applied:
        addon = loader.addon_for(item.entry.script)
        hooks = addon.hooks_of(item.entry.handler) if addon is not None else []
        applied.append({**entry_dict(item.entry, item.folder), "hooks": hooks})
    suppressed = [
        {**entry_dict(item.entry, item.folder), "reason": item.reason}
        for item in effective.suppressed
    ]
    defaults = []
    for tag in tag_list:
        for handler in loader.tag_handlers(tag):
            folder = store.suppressed_by(key, handler.addon.name, handler.name)
            defaults.append(
                {
                    "script": handler.addon.name,
                    "handler": handler.name,
                    "tag": tag,
                    "suppressed_by": file_label(folder) if folder is not None else None,
                }
            )
    labels = {file_label(folder) for folder in chain(folder_of(effective.key))}
    problems = [p for p in store.problems if p["file"] in labels]
    return {
        "path": effective.key,
        "tags": effective.tags,
        "known": known,
        "applied": applied,
        "suppressed": suppressed,
        "defaults": defaults,
        "problems": problems,
    }
