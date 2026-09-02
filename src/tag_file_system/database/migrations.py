# Code by AkinoAlice@TyrantRey

"""Schema versioning (DESIGN/v0-1-0.md §7, "Migrations").

``PRAGMA user_version`` holds the schema version. ``apply_migrations`` runs
every migration above the stored version, each in its own transaction, and
refuses a database newer than the code. Old databases must keep loading, so
migrations only ever *add* to what is there.

Version 1 is the whole v1 design: it creates the legacy tables if they are
missing (a pre-versioning database already has them), adds the action /
run / trace / provenance / problem tables, adds ``files.mtime_ns`` and
rewrites ``files.path`` from absolute native to root-relative POSIX.
"""

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Callable
from uuid import uuid4

from tag_file_system.core.logger import logger
from tag_file_system.core.paths import is_anchored, posix_key

SCHEMA_VERSION = 1


class SchemaTooNew(RuntimeError):
    """The database was written by a newer version of the code."""


class MigrationError(RuntimeError):
    """A migration cannot run as asked (e.g. no root for absolute paths)."""


@dataclass
class MigrationReport:
    from_version: int
    to_version: int
    relativized: int = 0  # files.path rows rewritten to root-relative
    outside_root: list[str] = field(default_factory=list)  # soft-deleted
    conflicts: list[str] = field(default_factory=list)  # soft-deleted (dup key)
    parked: list[str] = field(
        default_factory=list
    )  # dead rows moved under .tfs/duplicates

    @property
    def applied(self) -> bool:
        return self.to_version > self.from_version


# ------------------------------------------------------------------ schema

LEGACY_SCHEMA: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS files (
        id          TEXT    PRIMARY KEY,
        filename    TEXT    NOT NULL,
        path        TEXT    NOT NULL UNIQUE,
        hash        TEXT    NOT NULL,
        size        INTEGER NOT NULL DEFAULT 0,
        format      TEXT    DEFAULT NULL,
        mime_type   TEXT    DEFAULT NULL,
        status      TEXT    NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active', 'deleted', 'archived')),
        created_at  INTEGER NOT NULL DEFAULT (unixepoch()),
        modified_at INTEGER NOT NULL DEFAULT (unixepoch()),
        deleted_at  INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_files_hash       ON files(hash) WHERE hash IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_files_status     ON files(status)",
    "CREATE INDEX IF NOT EXISTS idx_files_created_at ON files(created_at)",
    """
    CREATE TABLE IF NOT EXISTS tags (
        id          TEXT    PRIMARY KEY,
        name        TEXT    NOT NULL UNIQUE,
        category    TEXT,
        description TEXT,
        created_at  INTEGER NOT NULL DEFAULT (unixepoch()),
        updated_at  INTEGER NOT NULL DEFAULT (unixepoch())
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tags_category ON tags(category)",
    """
    CREATE TABLE IF NOT EXISTS tagged_files (
        tag_id      TEXT    NOT NULL REFERENCES tags(id)  ON DELETE CASCADE,
        file_id     TEXT    NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        assigned_at INTEGER NOT NULL DEFAULT (unixepoch()),
        PRIMARY KEY (tag_id, file_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tagged_files_file_id ON tagged_files(file_id)",
    """
    CREATE TABLE IF NOT EXISTS events (
        id          TEXT    PRIMARY KEY,
        name        TEXT    NOT NULL,
        description TEXT,
        file_id     TEXT    REFERENCES files(id) ON DELETE SET NULL,
        tag_id      TEXT    REFERENCES tags(id)  ON DELETE SET NULL,
        occurred_at INTEGER NOT NULL DEFAULT (unixepoch()),
        CHECK (file_id IS NOT NULL OR tag_id IS NOT NULL)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_occurred_at ON events(occurred_at)",
    "CREATE INDEX IF NOT EXISTS idx_events_file_id     ON events(file_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_tag_id      ON events(tag_id)",
]

V1_SCHEMA: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS actions (
        id             TEXT    PRIMARY KEY,
        name           TEXT    NOT NULL,
        script_path    TEXT    NOT NULL,
        script_hash    TEXT    NOT NULL,
        signature_json TEXT    NOT NULL,
        hooks_json     TEXT    NOT NULL,
        loaded_at      INTEGER NOT NULL DEFAULT (unixepoch()),
        UNIQUE (name, script_hash)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_actions_name ON actions(name)",
    """
    CREATE TABLE IF NOT EXISTS action_runs (
        id             TEXT    PRIMARY KEY,
        action_id      TEXT    NOT NULL REFERENCES actions(id),
        action_name    TEXT    NOT NULL,
        hook           TEXT    NOT NULL,
        file_id        TEXT    REFERENCES files(id) ON DELETE SET NULL,
        file_hash      TEXT    NOT NULL,
        slug           TEXT    NOT NULL,
        args_json      TEXT    NOT NULL,
        result_json    TEXT,
        status         TEXT    NOT NULL
                       CHECK (status IN ('queued','running','ok','failed','skipped','interrupted')),
        error          TEXT,
        source         TEXT    NOT NULL,
        parent_run_id  TEXT    REFERENCES action_runs(id),
        retry_of       TEXT    REFERENCES action_runs(id),
        started_at     INTEGER NOT NULL DEFAULT (unixepoch()),
        finished_at    INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_action_runs_key ON action_runs(file_hash, action_name, hook, args_json)",
    "CREATE INDEX IF NOT EXISTS idx_action_runs_file_id ON action_runs(file_id)",
    "CREATE INDEX IF NOT EXISTS idx_action_runs_status ON action_runs(status)",
    "CREATE INDEX IF NOT EXISTS idx_action_runs_started_at ON action_runs(started_at)",
    """
    CREATE TABLE IF NOT EXISTS action_trace (
        run_id       TEXT    NOT NULL REFERENCES action_runs(id) ON DELETE CASCADE,
        seq          INTEGER NOT NULL,
        ts           INTEGER NOT NULL DEFAULT (unixepoch()),
        kind         TEXT    NOT NULL,
        payload_json TEXT    NOT NULL,
        PRIMARY KEY (run_id, seq)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS provenance (
        file_id     TEXT    NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        run_id      TEXT    NOT NULL REFERENCES action_runs(id) ON DELETE CASCADE,
        kind        TEXT    NOT NULL,
        ambiguous   INTEGER NOT NULL DEFAULT 0,
        created_at  INTEGER NOT NULL DEFAULT (unixepoch()),
        PRIMARY KEY (file_id, run_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_provenance_run_id ON provenance(run_id)",
    """
    CREATE TABLE IF NOT EXISTS problems (
        id           TEXT    PRIMARY KEY,
        severity     TEXT    NOT NULL CHECK (severity IN ('crit','err','warn','info')),
        kind         TEXT    NOT NULL,
        message      TEXT    NOT NULL,
        action_name  TEXT,
        file_id      TEXT    REFERENCES files(id) ON DELETE SET NULL,
        run_id       TEXT    REFERENCES action_runs(id) ON DELETE SET NULL,
        occurred_at  INTEGER NOT NULL DEFAULT (unixepoch()),
        delivered_at INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_problems_delivered_at ON problems(delivered_at)",
    "CREATE INDEX IF NOT EXISTS idx_problems_occurred_at ON problems(occurred_at)",
]


# ------------------------------------------------------------------- paths


def relative_key(path: PurePath | str, root_dir: Path | None) -> str | None:
    """Root-relative POSIX key for ``path``.

    A relative path is normalized as is (``a/../b`` → ``b``). An absolute
    path is resolved and relativized to ``root_dir``. ``None`` when the key
    cannot be formed: the path is outside the root, no root is known, or
    the result escapes upward / is the root itself.
    """
    text = str(path)
    try:
        if not is_anchored(text):
            key = posix_key(path)
        else:
            if root_dir is None:
                return None
            rel = Path(text).resolve().relative_to(Path(root_dir).resolve())
            key = posix_key(rel)
    except ValueError:
        return None
    return None if key == "." else key


# -------------------------------------------------------------- migrations


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _migrate_1(
    connection: sqlite3.Connection, root_dir: Path | None, report: MigrationReport
) -> None:
    for statement in (*LEGACY_SCHEMA, *V1_SCHEMA):
        connection.execute(statement)

    if "mtime_ns" not in _columns(connection, "files"):
        connection.execute("ALTER TABLE files ADD COLUMN mtime_ns INTEGER")

    _relativize_paths(connection, root_dir, report)


def _relativize_paths(
    connection: sqlite3.Connection, root_dir: Path | None, report: MigrationReport
) -> None:
    rows = connection.execute(
        "SELECT id, path, status FROM files ORDER BY rowid"
    ).fetchall()
    if root_dir is None and any(is_anchored(row[1]) for row in rows):
        raise MigrationError(
            "the database holds absolute file paths; open it with a root_dir "
            "so they can be made root-relative"
        )

    for file_id, stored, status in rows:
        key = relative_key(stored, root_dir)
        if key == stored:
            continue
        if key is None:
            if status != "deleted":
                _soft_delete(connection, file_id)
                report.outside_root.append(stored)
            continue
        try:
            connection.execute("UPDATE files SET path = ? WHERE id = ?", (key, file_id))
        except sqlite3.IntegrityError:
            if not _take_over_key(connection, key, file_id, status, report):
                if status != "deleted":
                    _soft_delete(connection, file_id)
                    report.conflicts.append(stored)
                continue
        report.relativized += 1

    if report.outside_root or report.conflicts or report.parked:
        listed = "\n".join(
            [
                *(f"outside root, marked deleted: {p}" for p in report.outside_root),
                *(f"duplicate key, marked deleted: {p}" for p in report.conflicts),
                *(f"already-deleted duplicate parked at: {p}" for p in report.parked),
            ]
        )
        connection.execute(
            """
            INSERT INTO problems (id, severity, kind, message)
            VALUES (?, 'warn', 'migration.path_unresolved', ?)
            """,
            (
                str(uuid4()),
                f"{len(report.outside_root) + len(report.conflicts) + len(report.parked)} "
                f"file row(s) could not be made root-relative as they were:\n{listed}",
            ),
        )


def _take_over_key(
    connection: sqlite3.Connection,
    key: str,
    file_id: str,
    status: str,
    report: MigrationReport,
) -> bool:
    """Two rows want ``key``. Prefer the active one: if the current owner is
    soft-deleted and this row is active, park the dead row under ``.tfs/``
    (ignored by the watcher) and give the key to this row."""
    owner = connection.execute(
        "SELECT id, status FROM files WHERE path = ?", (key,)
    ).fetchone()
    if owner is None or owner[1] != "deleted" or status == "deleted":
        return False
    parked = f".tfs/duplicates/{owner[0]}/{key}"
    connection.execute("UPDATE files SET path = ? WHERE id = ?", (parked, owner[0]))
    connection.execute("UPDATE files SET path = ? WHERE id = ?", (key, file_id))
    report.parked.append(parked)
    return True


def _soft_delete(connection: sqlite3.Connection, file_id: str) -> None:
    connection.execute(
        """
        UPDATE files
        SET status = 'deleted', deleted_at = unixepoch(), modified_at = unixepoch()
        WHERE id = ? AND status <> 'deleted'
        """,
        (file_id,),
    )


Migration = Callable[[sqlite3.Connection, Path | None, MigrationReport], None]
MIGRATIONS: list[tuple[int, Migration]] = [(1, _migrate_1)]


def user_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def apply_migrations(
    connection: sqlite3.Connection, root_dir: Path | None = None
) -> MigrationReport:
    """Bring ``connection`` to ``SCHEMA_VERSION``; each step is transactional."""
    if connection.in_transaction:
        raise MigrationError("apply_migrations must not be called inside a transaction")
    root = Path(root_dir).resolve() if root_dir is not None else None

    current = user_version(connection)
    if current > SCHEMA_VERSION:
        raise SchemaTooNew(
            f"database schema is version {current}, this build understands up to "
            f"{SCHEMA_VERSION}; upgrade TagFileSystem"
        )

    report = MigrationReport(from_version=current, to_version=current)
    for version, migrate in MIGRATIONS:
        if version <= current:
            continue
        logger.info(f"Applying schema migration {version}")
        connection.execute("BEGIN IMMEDIATE")
        try:
            migrate(connection, root, report)
            connection.execute(f"PRAGMA user_version = {int(version)}")
            connection.commit()
        except Exception:
            connection.rollback()
            logger.exception(f"Schema migration {version} failed; rolled back")
            raise
        report.to_version = version

    return report
