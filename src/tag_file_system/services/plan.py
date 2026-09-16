# Code by AkinoAlice@TyrantRey

"""``tfs plan``: what a ``tfs reload`` would start, and why
(DESIGN/v0-5-0.md §11.2) — computed, never applied.

The inputs are the add-ons as loaded, the ``.tfsfunctions.yaml`` tree as it
is **on disk** (``disk``: a store read for the answer and thrown away), the
tree the daemon **has loaded** (``loaded``; ``None`` offline, where the next
start loads exactly what is on disk), the file rows and the runs. A reload is
a *rescope* (v0-4-0 §6): every entry in scope is offered as ``added`` again,
so the candidates are a handler's ``added`` marks and its ``tagged`` marks
whose tag the file carries, keyed exactly as the runner keys them — and the
run store says which keys already have a run.

``candidates()`` is the enumeration `tfs rerun` shares; ``plan_view()``
renders it, with the diff of the functions files and of the scripts.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator

from tag_file_system.addons.loader import AddonLoader, FileHandler, script_hash
from tag_file_system.core.interface.action import Hook, RunKey, RunRecord
from tag_file_system.core.interface.file_metadata import TaggedFile
from tag_file_system.database.action_store import ActionStore
from tag_file_system.database.sqlite import SQLiteBackend
from tag_file_system.functions.model import display, file_label
from tag_file_system.functions.store import Applied, FunctionsStore

PLAN_LIMIT = 200
PLAN_MAX = 5000


def _folder_order(folder: str) -> tuple[int, str]:
    return (folder.count("/") + 1 if folder else 0, folder)


@dataclass
class Candidate:
    """One mark of one entry on one file: what a reload would offer."""

    row: TaggedFile
    applied: Applied
    mark: FileHandler
    args: dict[str, Any]
    key: RunKey
    existing: RunRecord | None  # the run holding the key, if any
    stale: bool = False  # `existing` was made by an older script
    produced: bool = False  # the provenance guard would skip it
    reason: str = ""  # why it would run (empty when it would not)

    @property
    def display(self) -> str:
        return display(self.applied.entry.script, self.applied.entry.handler, self.args)

    @property
    def would_run(self) -> bool:
        return self.existing is None and not self.produced

    def describe(self) -> dict[str, Any]:
        entry = self.applied.entry
        item: dict[str, Any] = {
            "display": self.display,
            "script": entry.script,
            "handler": entry.handler,
            "hook": self.mark.hook.value,
            "file": file_label(self.applied.folder),
            "stale": self.stale,
        }
        if self.existing is not None:
            when = self.existing.finished_at or self.existing.started_at
            item["run"] = self.existing.id
            item["status"] = self.existing.status.value
            item["reason"] = (
                "in flight"
                if not self.existing.status.is_final
                else f"already ran: {self.existing.status.value} at {_iso(when)}"
            )
            if self.stale:
                item["reason"] += f"; with an older script/{entry.script}.py"
        elif self.produced:
            item["reason"] = f"produced by {entry.script}"
        else:
            item["reason"] = self.reason
        return item


@dataclass
class FilePlan:
    row: TaggedFile
    candidates: list[Candidate] = field(default_factory=list)
    leaving: list[dict[str, Any]] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.row.path.as_posix()

    def describe(self) -> dict[str, Any]:
        return {
            "path": self.key,
            "hash": self.row.file_hash,
            "tags": [t.name for t in self.row.tags],
            "would_run": [c.describe() for c in self.candidates if c.would_run],
            "skipped": [c.describe() for c in self.candidates if not c.would_run],
            "leaving": list(self.leaving),
        }


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _rows(
    backend: SQLiteBackend, key: str | None, prefix: str | None
) -> list[TaggedFile]:
    if key is not None:
        row = backend.query_file(key)
        return [row] if row is not None else []
    if prefix is not None:
        return backend.query_files(path_prefix=prefix)
    return backend.query_files()


def candidates(
    loader: AddonLoader,
    disk: FunctionsStore,
    loaded: FunctionsStore | None,
    backend: SQLiteBackend,
    store: ActionStore,
    *,
    key: str | None = None,
    prefix: str | None = None,
) -> Iterator[FilePlan]:
    """Every live row under the scope with what a reload would offer it."""
    for row in _rows(backend, key, prefix):
        yield plan_file(loader, disk, loaded, store, row)


def plan_file(
    loader: AddonLoader,
    disk: FunctionsStore,
    loaded: FunctionsStore | None,
    store: ActionStore,
    row: TaggedFile,
) -> FilePlan:
    file_key = row.path.as_posix()
    tags = [t.name for t in row.tags]
    tag_set = set(tags)
    after = disk.effective(file_key, tags)
    before = loaded.effective(file_key, tags) if loaded is not None else None
    before_ids = {a.entry.identity: a for a in before.applied} if before else {}
    before_handlers = (
        {(a.entry.script, a.entry.handler): a for a in before.applied} if before else {}
    )
    # Keyed by (script, handler) as well: "newly included" also covers an
    # entry whose arguments changed while its exclusion went.
    before_suppressed = (
        {
            **{(s.entry.script, s.entry.handler): s for s in before.suppressed},
            **{s.entry.identity: s for s in before.suppressed},
        }
        if before
        else {}
    )
    producers: set[str] | None = None  # add-ons that made this file (lazy)
    plan = FilePlan(row=row)

    for applied in after.applied:
        entry = applied.entry
        addon = loader.addon_for(entry.script)
        marks = [
            *loader.handler_marks(entry.script, entry.handler, Hook.ADDED),
            *(
                h
                for h in loader.handler_marks(entry.script, entry.handler, Hook.TAGGED)
                if h.spec.tag in tag_set
            ),
        ]
        for mark in marks:
            args: dict[str, Any] = dict(entry.args)
            if mark.hook is Hook.TAGGED:
                args = {"tag": mark.spec.tag, **args}
            run_key = RunKey(
                file_hash=row.file_hash,
                action_name=entry.script,
                handler=mark.name,
                hook=mark.hook,
                args=args,
            )
            existing = store.find_run(run_key)
            candidate = Candidate(
                row=row,
                applied=applied,
                mark=mark,
                args=args,
                key=run_key,
                existing=existing,
            )
            if existing is not None:
                action = store.get_action(existing.action_id)
                candidate.stale = (
                    action is not None
                    and addon is not None
                    and action.script_hash != addon.script_hash
                )
            else:
                if producers is None:
                    producers = _producers(store, file_key)
                candidate.produced = entry.script in producers
                candidate.reason = _reason(
                    store,
                    row,
                    applied,
                    mark,
                    loaded is not None,
                    before_ids,
                    before_handlers,
                    before_suppressed,
                )
            plan.candidates.append(candidate)

    if before is not None:
        after_ids = {a.entry.identity for a in after.applied}
        after_suppressed = {s.entry.identity: s for s in after.suppressed}
        for previous in before.applied:
            identity = previous.entry.identity
            if identity in after_ids:
                continue
            label = file_label(previous.folder)
            if identity in after_suppressed:
                why = f"now excluded by {after_suppressed[identity].reason}"
            elif previous.folder in disk._file_errors:
                why = f"{label} no longer loads"
            elif previous.folder not in disk.files:
                why = f"{label} is gone"
            else:
                why = f"entry removed from {label}"
            plan.leaving.append(
                {
                    "display": previous.entry.display(),
                    "script": previous.entry.script,
                    "handler": previous.entry.handler,
                    "file": label,
                    "reason": why,
                }
            )
    return plan


def _producers(store: ActionStore, file_key: str) -> set[str]:
    names: set[str] = set()
    for edge in store.query_provenance(file_path=file_key, include_deleted=True):
        run = store.get_run(edge.run_id)
        if run is not None:
            names.add(run.action_name)
    return names


def _reason(
    store: ActionStore,
    row: TaggedFile,
    applied: Applied,
    mark: FileHandler,
    compared: bool,
    before_ids: dict[Any, Applied],
    before_handlers: dict[tuple[str, str], Applied],
    before_suppressed: dict[Any, Any],
) -> str:
    entry = applied.entry
    if compared:
        if entry.identity not in before_ids:
            earlier = before_handlers.get((entry.script, entry.handler))
            if earlier is not None:
                return f"arguments changed from {earlier.entry.display()}"
            suppressed = before_suppressed.get(entry.identity) or before_suppressed.get(
                (entry.script, entry.handler)
            )
            if suppressed is not None:
                return f"newly included: was excluded by {suppressed.reason}"
            return f"new entry from {file_label(applied.folder)}"
    # The entry applied before (or nothing is loaded to compare with): the
    # run history says whether the handler ever ran on this content.
    earlier_runs = [
        r
        for r in store.query_runs(
            file_hash=row.file_hash, action_name=entry.script, handler=mark.name
        )
        if r.hook is mark.hook
    ]
    if earlier_runs:
        return f"arguments changed: last ran as {earlier_runs[0].slug}"
    return "never ran"


# ------------------------------------------------------------------- diffs


def functions_diff(
    loaded: FunctionsStore | None, disk: FunctionsStore
) -> list[dict[str, Any]]:
    """Every folder's file, loaded or on disk: ``unchanged`` / ``changed``
    (with the entries added and removed) / ``new`` / ``gone`` / ``error``;
    offline (nothing loaded) ``loaded`` for what the next start reads."""
    folders = {*disk.files, *disk._file_errors}
    if loaded is not None:
        folders |= {*loaded.files, *loaded._file_errors}
    out: list[dict[str, Any]] = []
    for folder in sorted(folders, key=_folder_order):
        on_disk = disk.files.get(folder)
        error = disk._file_errors.get(folder)
        was = loaded.files.get(folder) if loaded is not None else None
        item: dict[str, Any] = {
            "folder": folder,
            "file": file_label(folder),
            "added": [],
            "removed": [],
            "error": None,
        }
        if error is not None:
            item["state"] = "error"
            item["error"] = {"kind": error[0], "message": error[1]}
            if loaded is not None and was is not None:
                item["note"] = "the loaded version stays in force at reload"
        elif loaded is None:
            item["state"] = "loaded"
            item["added"] = [e.display() for e in disk.entries(folder)]
        elif was is None:
            item["state"] = "new"
            item["added"] = [e.display() for e in disk.entries(folder)]
        elif on_disk is None:
            item["state"] = "gone"
            item["removed"] = [e.display() for e in loaded.entries(folder)]
        elif was.digest == on_disk.digest:
            item["state"] = "unchanged"
        else:
            item["state"] = "changed"
            before = {e.identity: e for e in loaded.entries(folder)}
            after = {e.identity: e for e in disk.entries(folder)}
            item["added"] = [e.display() for i, e in after.items() if i not in before]
            item["removed"] = [e.display() for i, e in before.items() if i not in after]
        out.append(item)
    return out


def scripts_diff(loader: AddonLoader, compared: bool) -> list[dict[str, Any]]:
    """Every ``script/*.py``: loaded hash against the hash on disk."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name, addon in sorted(loader.addons.items()):
        seen.add(name)
        try:
            on_disk: str | None = script_hash(addon.path)
        except OSError:
            on_disk = None
        item: dict[str, Any] = {
            "name": name,
            "file": addon.key.as_posix(),
            "loaded_hash": addon.script_hash,
            "disk_hash": on_disk,
        }
        if compared:
            item["state"] = (
                "gone"
                if on_disk is None
                else "unchanged"
                if on_disk == addon.script_hash
                else "changed"
            )
        out.append(item)
    if compared and loader.script_dir.is_dir():
        for path in sorted(loader.script_dir.glob("*.py")):
            if path.stem in seen or not loader.is_addon_file(path):
                continue
            out.append(
                {
                    "name": path.stem,
                    "file": f"{loader.script_dir.name}/{path.name}",
                    "loaded_hash": None,
                    "disk_hash": script_hash(path),
                    "state": "not loaded",
                }
            )
    return out


# -------------------------------------------------------------------- view


def plan_view(
    loader: AddonLoader,
    disk: FunctionsStore,
    loaded: FunctionsStore | None,
    backend: SQLiteBackend,
    store: ActionStore,
    *,
    source: str,
    key: str | None = None,
    prefix: str | None = None,
    limit: int = PLAN_LIMIT,
    threshold: int = 0,
) -> dict[str, Any]:
    """The JSON-safe answer of ``tfs plan`` / ``GET /api/v1/plan``."""
    compared = loaded is not None
    files_examined = 0
    would_run = already = stale = leaving = candidates_total = 0
    by_handler: dict[tuple[str, str], dict[str, Any]] = {}
    listed: list[dict[str, Any]] = []
    truncated = False
    for plan in candidates(
        loader, disk, loaded, backend, store, key=key, prefix=prefix
    ):
        files_examined += 1
        for c in plan.candidates:
            candidates_total += 1
            bucket = by_handler.setdefault(
                (c.display, c.mark.hook.value),
                {
                    "display": c.display,
                    "hook": c.mark.hook.value,
                    "would_run": 0,
                    "already_ran": 0,
                    "stale": 0,
                },
            )
            if c.would_run:
                would_run += 1
                bucket["would_run"] += 1
            elif c.existing is not None:
                already += 1
                bucket["already_ran"] += 1
            if c.stale:
                stale += 1
                bucket["stale"] += 1
        leaving += len(plan.leaving)
        interesting = plan.leaving or any(
            c.would_run or c.stale for c in plan.candidates
        )
        if not interesting:
            continue
        if len(listed) >= limit:
            truncated = True
            continue
        listed.append(plan.describe())
    return {
        "source": source,
        "compared": compared,
        "scope": {"path": key, "prefix": prefix},
        "summary": {
            "files": files_examined,
            "candidates": candidates_total,
            "would_run": would_run,
            "already_ran": already,
            "stale": stale,
            "leaving": leaving,
            "threshold": threshold,
            "over_threshold": threshold > 0 and would_run > threshold,
            "by_handler": sorted(
                by_handler.values(), key=lambda b: (-b["would_run"], b["display"])
            ),
        },
        "functions": functions_diff(loaded, disk),
        "scripts": scripts_diff(loader, compared),
        "files": listed,
        "listed": len(listed),
        "truncated": truncated,
    }


def count_would_run(
    loader: AddonLoader,
    disk: FunctionsStore,
    loaded: FunctionsStore | None,
    backend: SQLiteBackend,
    store: ActionStore,
) -> int:
    """Just the number a reload would start (the threshold check)."""
    return sum(
        1
        for plan in candidates(loader, disk, loaded, backend, store)
        for c in plan.candidates
        if c.would_run
    )
