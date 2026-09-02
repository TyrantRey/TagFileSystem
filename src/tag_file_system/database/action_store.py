# Code by AkinoAlice@TyrantRey

"""Persistence of the action layer (DESIGN/v0-1-0.md §6–§7): ``actions``,
``action_runs``, ``action_trace``, ``provenance`` and ``problems`` — and the
``upgrades`` log of DESIGN/v0-2-0.md §9.

``ActionStore`` shares the ``SQLiteBackend``'s connection and lock, so a
call here joins a transaction opened by the backend (and vice versa) and
add-on threads are serialized the same way.
"""

import json
import math
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import PurePath, PurePosixPath
from typing import Any
from uuid import uuid4

from tag_file_system.core.interface.action import (
    ActionRecord,
    EventRecord,
    FileHistory,
    Hook,
    ProblemRecord,
    ProvenanceKind,
    ProvenanceRecord,
    RunKey,
    RunRecord,
    RunSource,
    RunStatus,
    Severity,
    TraceEntry,
    UpgradeRecord,
    canonical_json,
)
from tag_file_system.core.interface.database import PathLike
from tag_file_system.core.logger import logger
from tag_file_system.core.paths import posix_key
from tag_file_system.database.sqlite import (
    SQLiteBackend,
    _chunks,
    _placeholders,
    locked,
    transactional,
)
from tag_file_system.version import COMMIT, VERSION


class RunExists(ValueError):
    """A run with this key already exists (DESIGN/v0-1-0.md §6.1); retry instead."""

    def __init__(self, existing: RunRecord) -> None:
        super().__init__(
            f"run {existing.id} ({existing.status}) already has key "
            f"{existing.action_name}/{existing.hook}/{existing.file_hash}"
        )
        self.existing = existing


class RunAlreadyFinal(ValueError):
    """``finish_run`` on a run that already reached a final status."""


def _to_datetime(epoch: int | float | None) -> datetime | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, UTC)


def _ceil_epoch(value: datetime) -> int:
    return int(math.ceil(value.timestamp()))


def _loads(text: str | None) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


_LIKE_ESCAPE = "\\"


def _like_prefix(prefix: str) -> str:
    escaped = (
        prefix.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )
    return escaped + "%"


class ActionStore:
    def __init__(self, backend: SQLiteBackend) -> None:
        self.backend = backend
        self.logger = logger

    # The decorators from sqlite.py look for these three attributes.
    @property
    def connection(self) -> sqlite3.Connection:
        return self.backend.connection

    @property
    def _lock(self) -> threading.RLock:
        return self.backend._lock

    def _file_id(
        self, cursor: sqlite3.Cursor, file_path: PathLike | None
    ) -> str | None:
        if file_path is None:
            return None
        try:
            key = self.backend.key(file_path)
        except ValueError as e:
            # An unusable path can never be a known file; say so, since a
            # caller passing one is usually an engine bug.
            self.logger.warning(f"unusable file path {file_path!r}: {e}")
            return None
        row = cursor.execute("SELECT id FROM files WHERE path = ?", (key,)).fetchone()
        return row["id"] if row is not None else None

    def _require_run(
        self, cursor: sqlite3.Cursor, run_id: str, what: str = "run"
    ) -> None:
        if (
            cursor.execute(
                "SELECT 1 FROM action_runs WHERE id = ?", (run_id,)
            ).fetchone()
            is None
        ):
            raise KeyError(f"unknown {what} {run_id}")

    # ---------------------------------------------------------------- actions

    @transactional
    def register_action(
        self,
        name: str,
        script_path: PurePath | str,
        script_hash: str,
        signature: dict[str, Any],
        hooks: list[Hook],
    ) -> ActionRecord:
        """Upsert the add-on version ``(name, script_hash)``; refreshes
        ``loaded_at`` (sub-second) on every load so the newest load is the
        live one even when versions flip within a second."""
        cursor = self.connection.cursor()
        row = cursor.execute(
            "SELECT id FROM actions WHERE name = ? AND script_hash = ?",
            (name, script_hash),
        ).fetchone()
        script_text = posix_key(script_path)
        hooks_json = json.dumps([Hook(h).value for h in hooks])
        signature_json = canonical_json(signature)
        # Strictly later than every earlier load of this name: the wall clock
        # alone has ~15 ms granularity on Windows and would tie.
        latest = cursor.execute(
            "SELECT MAX(loaded_at) FROM actions WHERE name = ?", (name,)
        ).fetchone()[0]
        now = max(time.time(), (float(latest) if latest is not None else 0.0) + 1e-6)
        if row is None:
            action_id = str(uuid4())
            cursor.execute(
                """
                INSERT INTO actions
                    (id, name, script_path, script_hash, signature_json, hooks_json, loaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    name,
                    script_text,
                    script_hash,
                    signature_json,
                    hooks_json,
                    now,
                ),
            )
        else:
            action_id = row["id"]
            cursor.execute(
                """
                UPDATE actions
                SET script_path = ?, signature_json = ?, hooks_json = ?, loaded_at = ?
                WHERE id = ?
                """,
                (script_text, signature_json, hooks_json, now, action_id),
            )
        record = self.get_action(action_id)
        assert record is not None
        return record

    @locked
    def get_action(self, action_id: str) -> ActionRecord | None:
        row = self.connection.execute(
            "SELECT * FROM actions WHERE id = ?", (action_id,)
        ).fetchone()
        return self._row_to_action(row) if row is not None else None

    @locked
    def latest_action(self, name: str) -> ActionRecord | None:
        row = self.connection.execute(
            "SELECT * FROM actions WHERE name = ? ORDER BY loaded_at DESC, rowid DESC LIMIT 1",
            (name,),
        ).fetchone()
        return self._row_to_action(row) if row is not None else None

    @locked
    def query_actions(self, name: str | None = None) -> list[ActionRecord]:
        sql = "SELECT * FROM actions"
        params: tuple = ()
        if name is not None:
            sql += " WHERE name = ?"
            params = (name,)
        sql += " ORDER BY name, loaded_at DESC, rowid DESC"
        return [self._row_to_action(r) for r in self.connection.execute(sql, params)]

    @staticmethod
    def _row_to_action(row: sqlite3.Row) -> ActionRecord:
        loaded = _to_datetime(row["loaded_at"])
        assert loaded is not None
        return ActionRecord(
            id=row["id"],
            name=row["name"],
            script_path=PurePosixPath(row["script_path"]),
            script_hash=row["script_hash"],
            signature=_loads(row["signature_json"]) or {},
            hooks=[Hook(h) for h in _loads(row["hooks_json"]) or []],
            loaded_at=loaded,
        )

    # ------------------------------------------------------------------- runs

    @locked
    def find_run(self, key: RunKey) -> RunRecord | None:
        """The newest run with this key, whatever its status (DESIGN §6.1)."""
        row = self.connection.execute(
            """
            SELECT * FROM action_runs
            WHERE file_hash = ? AND action_name = ? AND hook = ? AND args_json = ?
            ORDER BY started_at DESC, rowid DESC LIMIT 1
            """,
            (key.file_hash, key.action_name, key.hook.value, key.args_json),
        ).fetchone()
        return self._row_to_run(row) if row is not None else None

    @transactional
    def start_run(
        self,
        action: ActionRecord,
        key: RunKey,
        slug: str,
        file_path: PathLike | None,
        source: RunSource,
        parent_run_id: str | None = None,
        retry_of: str | None = None,
        status: RunStatus = RunStatus.RUNNING,
    ) -> RunRecord:
        """Create a run row. Refuses (``RunExists``) when the key already
        has a run of any status, unless this is a retry of that run."""
        status = RunStatus(status)
        if status.is_final:
            raise ValueError(f"start_run needs queued or running, got {status}")
        cursor = self.connection.cursor()
        existing = self.find_run(key)
        if retry_of is not None:
            previous = self.get_run(retry_of)
            if previous is None:
                raise KeyError(f"unknown run {retry_of}")
            if previous.key != key:
                raise ValueError(
                    f"run {retry_of} has a different key; cannot retry it here"
                )
            if existing is not None and not existing.status.is_final:
                raise RunExists(existing)
        elif existing is not None:
            raise RunExists(existing)
        if parent_run_id is not None:
            self._require_run(cursor, parent_run_id, "parent run")

        run_id = str(uuid4())
        cursor.execute(
            """
            INSERT INTO action_runs
                (id, action_id, action_name, hook, file_id, file_hash, slug, args_json,
                 status, source, parent_run_id, retry_of, code_version, code_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                action.id,
                key.action_name,
                key.hook.value,
                self._file_id(cursor, file_path),
                key.file_hash,
                slug,
                key.args_json,
                status.value,
                RunSource(source).value,
                parent_run_id,
                retry_of,
                VERSION,
                COMMIT,
            ),
        )
        run = self.get_run(run_id)
        assert run is not None
        return run

    @transactional
    def finish_run(
        self,
        run_id: str,
        status: RunStatus,
        result: Any = None,
        error: str | None = None,
    ) -> RunRecord:
        """Move a queued/running run to a final status. A final run is
        immutable: a second call is ``RunAlreadyFinal``."""
        status = RunStatus(status)
        if not status.is_final:
            raise ValueError(f"finish_run needs a final status, got {status}")
        cursor = self.connection.cursor()
        cursor.execute(
            """
            UPDATE action_runs
            SET status = ?, result_json = ?, error = ?, finished_at = unixepoch()
            WHERE id = ? AND status IN ('queued', 'running')
            """,
            (
                status.value,
                None if result is None else canonical_json(result),
                error,
                run_id,
            ),
        )
        if cursor.rowcount == 0:
            current = self.get_run(run_id)
            if current is None:
                raise KeyError(f"unknown run {run_id}")
            raise RunAlreadyFinal(f"run {run_id} is already {current.status}")
        run = self.get_run(run_id)
        assert run is not None
        return run

    @locked
    def get_run(self, run_id: str) -> RunRecord | None:
        row = self.connection.execute(
            "SELECT * FROM action_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return self._row_to_run(row) if row is not None else None

    @locked
    def query_runs(
        self,
        file_path: PathLike | None = None,
        file_hash: str | None = None,
        action_name: str | None = None,
        status: RunStatus | list[RunStatus] | None = None,
        since: datetime | None = None,
        path_prefix: str | None = None,
        limit: int | None = None,
    ) -> list[RunRecord]:
        """Runs matching every criterion, newest first.

        ``file_path`` also matches runs started before the file row existed
        (``file_id`` NULL, same hash). ``path_prefix`` is a root-relative
        key prefix such as ``"@@make_copy/"``.
        """
        clauses: list[str] = []
        params: list[Any] = []
        cursor = self.connection.cursor()
        if file_path is not None:
            file_row = self._file_row(cursor, file_path)
            if file_row is None:
                return []
            clauses.append("(r.file_id = ? OR (r.file_id IS NULL AND r.file_hash = ?))")
            params.extend((file_row["id"], file_row["hash"]))
        if file_hash is not None:
            clauses.append("r.file_hash = ?")
            params.append(file_hash)
        if action_name is not None:
            clauses.append("r.action_name = ?")
            params.append(action_name)
        if status is not None:
            statuses = (
                [status] if isinstance(status, (RunStatus, str)) else list(status)
            )
            clauses.append(f"r.status IN ({_placeholders(len(statuses))})")
            params.extend(RunStatus(s).value for s in statuses)
        if since is not None:
            clauses.append("r.started_at >= ?")
            params.append(_ceil_epoch(since))
        if path_prefix is not None:
            like = _like_prefix(self.backend.key(path_prefix) + "/")
            clauses.append(
                """(
                    r.file_id IN (SELECT id FROM files WHERE path LIKE ? ESCAPE '\\')
                    OR (r.file_id IS NULL AND r.file_hash IN
                        (SELECT hash FROM files WHERE path LIKE ? ESCAPE '\\'))
                )"""
            )
            params.extend((like, like))
        sql = "SELECT r.* FROM action_runs r"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY r.started_at DESC, r.rowid DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [self._row_to_run(r) for r in cursor.execute(sql, params)]

    def _file_row(
        self, cursor: sqlite3.Cursor, file_path: PathLike
    ) -> sqlite3.Row | None:
        try:
            key = self.backend.key(file_path)
        except ValueError:
            return None
        return cursor.execute(
            "SELECT id, hash FROM files WHERE path = ?", (key,)
        ).fetchone()

    @transactional
    def mark_interrupted(self, run_ids: list[str] | None = None) -> list[RunRecord]:
        """Every ``running``/``queued`` run (or just ``run_ids``) becomes
        ``interrupted``: used at startup after a crash and on ``stop``."""
        cursor = self.connection.cursor()
        ids: list[str] = []
        if run_ids is None:
            ids = [
                row["id"]
                for row in cursor.execute(
                    "SELECT id FROM action_runs WHERE status IN ('running', 'queued')"
                )
            ]
        else:
            for chunk in _chunks(list(run_ids)):
                ids.extend(
                    row["id"]
                    for row in cursor.execute(
                        f"""
                        SELECT id FROM action_runs
                        WHERE status IN ('running', 'queued')
                          AND id IN ({_placeholders(len(chunk))})
                        """,
                        chunk,
                    )
                )
        for run_id in ids:
            cursor.execute(
                """
                UPDATE action_runs
                SET status = 'interrupted', finished_at = unixepoch(),
                    error = COALESCE(error, 'interrupted: the daemon stopped before the run finished')
                WHERE id = ?
                """,
                (run_id,),
            )
        return [r for r in (self.get_run(i) for i in ids) if r is not None]

    def _row_to_run(self, row: sqlite3.Row) -> RunRecord:
        started = _to_datetime(row["started_at"])
        assert started is not None
        return RunRecord(
            id=row["id"],
            action_id=row["action_id"],
            action_name=row["action_name"],
            hook=Hook(row["hook"]),
            file_id=row["file_id"],
            file_hash=row["file_hash"],
            slug=row["slug"],
            args=_loads(row["args_json"]) or {},
            result=_loads(row["result_json"]),
            status=RunStatus(row["status"]),
            error=row["error"],
            source=RunSource(row["source"]),
            parent_run_id=row["parent_run_id"],
            retry_of=row["retry_of"],
            started_at=started,
            finished_at=_to_datetime(row["finished_at"]),
            code_version=row["code_version"],
            code_hash=row["code_hash"],
        )

    # ------------------------------------------------------------------ trace

    @transactional
    def add_trace(self, run_id: str, kind: str, payload: Any) -> TraceEntry:
        cursor = self.connection.cursor()
        self._require_run(cursor, run_id)
        seq = cursor.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM action_trace WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]
        cursor.execute(
            "INSERT INTO action_trace (run_id, seq, kind, payload_json) VALUES (?, ?, ?, ?)",
            (run_id, seq, str(kind), canonical_json(payload)),
        )
        row = cursor.execute(
            "SELECT * FROM action_trace WHERE run_id = ? AND seq = ?", (run_id, seq)
        ).fetchone()
        return self._row_to_trace(row)

    @locked
    def query_trace(self, run_id: str) -> list[TraceEntry]:
        rows = self.connection.execute(
            "SELECT * FROM action_trace WHERE run_id = ? ORDER BY seq", (run_id,)
        )
        return [self._row_to_trace(r) for r in rows]

    @staticmethod
    def _row_to_trace(row: sqlite3.Row) -> TraceEntry:
        ts = _to_datetime(row["ts"])
        assert ts is not None
        return TraceEntry(
            run_id=row["run_id"],
            seq=row["seq"],
            ts=ts,
            kind=row["kind"],
            payload=_loads(row["payload_json"]),
        )

    # ------------------------------------------------------------- provenance

    @transactional
    def add_provenance(
        self,
        file_path: PathLike,
        run_id: str,
        kind: ProvenanceKind,
        ambiguous: bool = False,
    ) -> ProvenanceRecord:
        """Record that ``file_path`` exists because of ``run_id``. An
        ``emitted`` edge overrides an earlier ``observed`` one, never the
        reverse; repeated observations accumulate ``ambiguous``."""
        cursor = self.connection.cursor()
        file_id = self._file_id(cursor, file_path)
        if file_id is None:
            raise KeyError(f"unknown file {file_path}")
        self._require_run(cursor, run_id)
        kind = ProvenanceKind(kind)
        existing = cursor.execute(
            "SELECT kind FROM provenance WHERE file_id = ? AND run_id = ?",
            (file_id, run_id),
        ).fetchone()
        if existing is None:
            cursor.execute(
                "INSERT INTO provenance (file_id, run_id, kind, ambiguous) VALUES (?, ?, ?, ?)",
                (file_id, run_id, kind.value, int(ambiguous)),
            )
        elif kind is ProvenanceKind.EMITTED:
            cursor.execute(
                "UPDATE provenance SET kind = ?, ambiguous = 0 WHERE file_id = ? AND run_id = ?",
                (kind.value, file_id, run_id),
            )
        elif existing["kind"] == ProvenanceKind.OBSERVED.value and ambiguous:
            cursor.execute(
                "UPDATE provenance SET ambiguous = 1 WHERE file_id = ? AND run_id = ?",
                (file_id, run_id),
            )
        row = cursor.execute(
            "SELECT * FROM provenance WHERE file_id = ? AND run_id = ?",
            (file_id, run_id),
        ).fetchone()
        return self._row_to_provenance(row)

    @locked
    def query_provenance(
        self,
        file_path: PathLike | None = None,
        run_id: str | None = None,
        include_deleted: bool = False,
    ) -> list[ProvenanceRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        cursor = self.connection.cursor()
        if file_path is not None:
            file_id = self._file_id(cursor, file_path)
            if file_id is None:
                return []
            clauses.append("p.file_id = ?")
            params.append(file_id)
        if run_id is not None:
            clauses.append("p.run_id = ?")
            params.append(run_id)
        if not include_deleted:
            clauses.append("f.status <> 'deleted'")
        sql = "SELECT p.* FROM provenance p JOIN files f ON f.id = p.file_id"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY p.created_at, p.rowid"
        return [self._row_to_provenance(r) for r in cursor.execute(sql, params)]

    @locked
    def produced_by(self, run_id: str, include_deleted: bool = False) -> list[str]:
        """Keys of the files that exist because of ``run_id``."""
        sql = """
            SELECT f.path FROM provenance p JOIN files f ON f.id = p.file_id
            WHERE p.run_id = ?
        """
        if not include_deleted:
            sql += " AND f.status <> 'deleted'"
        sql += " ORDER BY p.rowid"
        return [row["path"] for row in self.connection.execute(sql, (run_id,))]

    @staticmethod
    def _row_to_provenance(row: sqlite3.Row) -> ProvenanceRecord:
        created = _to_datetime(row["created_at"])
        assert created is not None
        return ProvenanceRecord(
            file_id=row["file_id"],
            run_id=row["run_id"],
            kind=ProvenanceKind(row["kind"]),
            ambiguous=bool(row["ambiguous"]),
            created_at=created,
        )

    # --------------------------------------------------------------- problems

    @transactional
    def record_problem(
        self,
        severity: Severity,
        kind: str,
        message: str,
        action_name: str | None = None,
        file_path: PathLike | None = None,
        run_id: str | None = None,
    ) -> ProblemRecord:
        cursor = self.connection.cursor()
        if run_id is not None:
            self._require_run(cursor, run_id)
        problem_id = str(uuid4())
        cursor.execute(
            """
            INSERT INTO problems (id, severity, kind, message, action_name, file_id, run_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                problem_id,
                Severity(severity).value,
                kind,
                message,
                action_name,
                self._file_id(cursor, file_path),
                run_id,
            ),
        )
        problem = self.get_problem(problem_id)
        assert problem is not None
        return problem

    @locked
    def get_problem(self, problem_id: str) -> ProblemRecord | None:
        row = self.connection.execute(
            "SELECT * FROM problems WHERE id = ?", (problem_id,)
        ).fetchone()
        return self._row_to_problem(row) if row is not None else None

    @locked
    def query_problems(
        self,
        at_least: Severity | None = None,
        since: datetime | None = None,
        undelivered_only: bool = False,
        limit: int | None = None,
    ) -> list[ProblemRecord]:
        """Problems, oldest first (delivery order). ``at_least`` keeps the
        level and everything more severe."""
        clauses: list[str] = []
        params: list[Any] = []
        if at_least is not None:
            wanted = [s.value for s in Severity if Severity(at_least).covers(s)]
            clauses.append(f"severity IN ({_placeholders(len(wanted))})")
            params.extend(wanted)
        if since is not None:
            clauses.append("occurred_at >= ?")
            params.append(_ceil_epoch(since))
        if undelivered_only:
            clauses.append("delivered_at IS NULL")
        sql = "SELECT * FROM problems"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY occurred_at, rowid"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [self._row_to_problem(r) for r in self.connection.execute(sql, params)]

    @transactional
    def mark_delivered(self, problem_ids: list[str]) -> int:
        cursor = self.connection.cursor()
        count = 0
        for chunk in _chunks(list(dict.fromkeys(problem_ids))):
            cursor.execute(
                f"""
                UPDATE problems SET delivered_at = unixepoch()
                WHERE delivered_at IS NULL AND id IN ({_placeholders(len(chunk))})
                """,
                chunk,
            )
            count += cursor.rowcount
        return count

    @staticmethod
    def _row_to_problem(row: sqlite3.Row) -> ProblemRecord:
        occurred = _to_datetime(row["occurred_at"])
        assert occurred is not None
        return ProblemRecord(
            id=row["id"],
            severity=Severity(row["severity"]),
            kind=row["kind"],
            message=row["message"],
            action_name=row["action_name"],
            file_id=row["file_id"],
            run_id=row["run_id"],
            occurred_at=occurred,
            delivered_at=_to_datetime(row["delivered_at"]),
        )

    # ---------------------------------------------------------------- history

    @locked
    def file_history(self, file_path: PathLike) -> FileHistory | None:
        """Everything recorded about one file, each list oldest first.
        Runs started before the row existed (same hash) are included."""
        cursor = self.connection.cursor()
        file_row = self._file_row(cursor, file_path)
        if file_row is None:
            return None
        file_id, file_hash = file_row["id"], file_row["hash"]
        events = [
            EventRecord(
                id=r["id"],
                name=r["name"],
                description=r["description"],
                file_id=r["file_id"],
                tag_id=r["tag_id"],
                tag_name=r["tag_name"],
                occurred_at=_to_datetime(r["occurred_at"]) or datetime.now(UTC),
            )
            for r in cursor.execute(
                """
                SELECT e.*, t.name AS tag_name FROM events e
                LEFT JOIN tags t ON t.id = e.tag_id
                WHERE e.file_id = ? ORDER BY e.occurred_at, e.rowid
                """,
                (file_id,),
            )
        ]
        runs = [
            self._row_to_run(r)
            for r in cursor.execute(
                """
                SELECT * FROM action_runs
                WHERE file_id = ? OR (file_id IS NULL AND file_hash = ?)
                ORDER BY started_at, rowid
                """,
                (file_id, file_hash),
            )
        ]
        provenance = [
            self._row_to_provenance(r)
            for r in cursor.execute(
                "SELECT * FROM provenance WHERE file_id = ? ORDER BY created_at, rowid",
                (file_id,),
            )
        ]
        problems = [
            self._row_to_problem(r)
            for r in cursor.execute(
                "SELECT * FROM problems WHERE file_id = ? ORDER BY occurred_at, rowid",
                (file_id,),
            )
        ]
        return FileHistory(
            file_id=file_id,
            events=events,
            runs=runs,
            provenance=provenance,
            problems=problems,
        )

    # --------------------------------------------------------------- upgrades

    @transactional
    def record_upgrade(
        self,
        from_tag: str | None,
        from_hash: str,
        to_tag: str,
        to_hash: str,
        schema_before: int,
        schema_after: int,
        started_at: datetime | float,
        outcome: str = "ok",
        tests_run: int | None = None,
        tests_passed: int | None = None,
        tests_skipped: int | None = None,
        snapshot_path: str | None = None,
    ) -> UpgradeRecord:
        """Write the ``upgrades`` row of DESIGN/v0-2-0.md §9. Only the new
        code can write it, after migrating: a failed upgrade leaves its
        evidence in the log and the snapshot, not here."""
        cursor = self.connection.cursor()
        upgrade_id = str(uuid4())
        started = (
            started_at.timestamp()
            if isinstance(started_at, datetime)
            else float(started_at)
        )
        cursor.execute(
            """
            INSERT INTO upgrades
                (id, from_tag, from_hash, to_tag, to_hash, schema_before, schema_after,
                 tests_run, tests_passed, tests_skipped, snapshot_path, outcome,
                 started_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                upgrade_id,
                from_tag,
                from_hash,
                to_tag,
                to_hash,
                int(schema_before),
                int(schema_after),
                tests_run,
                tests_passed,
                tests_skipped,
                snapshot_path,
                outcome,
                int(started),
            ),
        )
        row = cursor.execute(
            "SELECT * FROM upgrades WHERE id = ?", (upgrade_id,)
        ).fetchone()
        return self._row_to_upgrade(row)

    @locked
    def query_upgrades(self, limit: int | None = None) -> list[UpgradeRecord]:
        """Every recorded upgrade of this root, newest first."""
        sql = "SELECT * FROM upgrades ORDER BY finished_at DESC, rowid DESC"
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (int(limit),)
        return [self._row_to_upgrade(r) for r in self.connection.execute(sql, params)]

    @staticmethod
    def _row_to_upgrade(row: sqlite3.Row) -> UpgradeRecord:
        started = _to_datetime(row["started_at"])
        finished = _to_datetime(row["finished_at"])
        assert started is not None and finished is not None
        return UpgradeRecord(
            id=row["id"],
            from_tag=row["from_tag"],
            from_hash=row["from_hash"],
            to_tag=row["to_tag"],
            to_hash=row["to_hash"],
            schema_before=row["schema_before"],
            schema_after=row["schema_after"],
            tests_run=row["tests_run"],
            tests_passed=row["tests_passed"],
            tests_skipped=row["tests_skipped"],
            snapshot_path=row["snapshot_path"],
            outcome=row["outcome"],
            started_at=started,
            finished_at=finished,
        )
