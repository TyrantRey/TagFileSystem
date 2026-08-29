# Code by AkinoAlice@TyrantRey

import sqlite3
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from tag_file_system.core.interface.database import (
    FileStatus,
    OperationResultEnum,
    SQLOperationType,
    SQLResult,
)
from tag_file_system.core.interface.file_metadata import (
    FileMetadata,
    Tag,
    TaggedFile,
)
from tag_file_system.core.logger import logger

# SQLite caps the number of bound parameters per statement; stay well under it.
_IN_CHUNK = 500


def transactional(method: Callable) -> Callable:
    """Run ``method`` inside a single ``BEGIN IMMEDIATE`` transaction.

    Commits on success, rolls back and re-raises on failure. Calls made while
    a transaction is already open (a transactional method calling another one)
    simply join the outer transaction.
    """

    @wraps(method)
    def wrapper(self, *args, **kwargs):
        connection: sqlite3.Connection = self.connection
        if connection.in_transaction:
            return method(self, *args, **kwargs)

        connection.execute("BEGIN IMMEDIATE")
        try:
            result = method(self, *args, **kwargs)
            connection.commit()
            return result
        except Exception as e:
            connection.rollback()
            name = getattr(method, "__name__", repr(method))
            self.logger.error(f"Transaction failed in {name}: {e}")
            raise

    return wrapper


def _to_datetime(epoch: int | float | None) -> datetime:
    if epoch is None:
        return datetime.now(UTC)
    return datetime.fromtimestamp(epoch, UTC)


def _to_epoch(value: datetime) -> int:
    return int(value.timestamp())


def _chunks(items: list[str], size: int = _IN_CHUNK) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _placeholders(count: int) -> str:
    return ",".join("?" * count)


class SQLiteBackend:
    def __init__(self) -> None:
        self.logger = logger

    # ------------------------------------------------------------------ setup

    def init_database(self, database_path: Path) -> bool:
        self.database_file = database_path

        self.connection = sqlite3.connect(self.database_file)
        self.connection.row_factory = sqlite3.Row

        sql_script = """
        PRAGMA journal_mode = WAL;
        PRAGMA foreign_keys = ON;
        PRAGMA synchronous = NORMAL;

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
        );

        CREATE INDEX IF NOT EXISTS idx_files_hash       ON files(hash) WHERE hash IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_files_status     ON files(status);
        CREATE INDEX IF NOT EXISTS idx_files_created_at ON files(created_at);

        CREATE TABLE IF NOT EXISTS tags (
            id          TEXT    PRIMARY KEY,
            name        TEXT    NOT NULL UNIQUE,
            category    TEXT,
            description TEXT,
            created_at  INTEGER NOT NULL DEFAULT (unixepoch()),
            updated_at  INTEGER NOT NULL DEFAULT (unixepoch())
        );

        CREATE INDEX IF NOT EXISTS idx_tags_category ON tags(category);

        CREATE TABLE IF NOT EXISTS tagged_files (
            tag_id      TEXT    NOT NULL REFERENCES tags(id)  ON DELETE CASCADE,
            file_id     TEXT    NOT NULL REFERENCES files(id) ON DELETE CASCADE,
            assigned_at INTEGER NOT NULL DEFAULT (unixepoch()),
            PRIMARY KEY (tag_id, file_id)
        );
        CREATE INDEX IF NOT EXISTS idx_tagged_files_file_id ON tagged_files(file_id);

        CREATE TABLE IF NOT EXISTS events (
            id          TEXT    PRIMARY KEY,
            name        TEXT    NOT NULL,
            description TEXT,
            file_id     TEXT    REFERENCES files(id) ON DELETE SET NULL,
            tag_id      TEXT    REFERENCES tags(id)  ON DELETE SET NULL,
            occurred_at INTEGER NOT NULL DEFAULT (unixepoch()),

            CHECK (file_id IS NOT NULL OR tag_id IS NOT NULL)
        );

        CREATE INDEX IF NOT EXISTS idx_events_occurred_at ON events(occurred_at);
        CREATE INDEX IF NOT EXISTS idx_events_file_id     ON events(file_id);
        CREATE INDEX IF NOT EXISTS idx_events_tag_id      ON events(tag_id);
        """

        self.connection.executescript(sql_script)
        self.logger.info("Database initialized")
        return True

    def close(self) -> None:
        connection = getattr(self, "connection", None)
        if connection is None:
            return
        connection.close()
        self.logger.info("Database connection closed")

    # -------------------------------------------------------------- internals

    def _record_event(
        self,
        cursor: sqlite3.Cursor,
        name: str,
        *,
        file_id: str | None = None,
        tag_id: str | None = None,
        description: str | None = None,
        event_id: str | None = None,
    ) -> str:
        event_id = event_id or str(uuid4())
        cursor.execute(
            """
            INSERT INTO events (id, name, description, file_id, tag_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event_id, name, description, file_id, tag_id),
        )
        return event_id

    def _find_file(self, cursor: sqlite3.Cursor, file_path: Path) -> sqlite3.Row | None:
        return cursor.execute(
            "SELECT id, status FROM files WHERE path = ?", (str(file_path),)
        ).fetchone()

    def _ensure_tag(
        self,
        cursor: sqlite3.Cursor,
        name: str,
        category: str | None = None,
        description: str | None = None,
    ) -> tuple[str, bool]:
        """Return ``(tag_id, created)`` for ``name``, inserting it if needed."""
        row = cursor.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
        if row is not None:
            tag_id = row["id"]
            if category is not None or description is not None:
                cursor.execute(
                    """
                    UPDATE tags
                    SET category    = COALESCE(?, category),
                        description = COALESCE(?, description),
                        updated_at  = unixepoch()
                    WHERE id = ?
                    """,
                    (category, description, tag_id),
                )
            return tag_id, False

        tag_id = str(uuid4())
        cursor.execute(
            "INSERT INTO tags (id, name, category, description) VALUES (?, ?, ?, ?)",
            (tag_id, name, category, description),
        )
        return tag_id, True

    def _load_tags(
        self, cursor: sqlite3.Cursor, file_ids: list[str]
    ) -> dict[str, list[Tag]]:
        tags_by_file: dict[str, list[Tag]] = {file_id: [] for file_id in file_ids}
        for chunk in _chunks(file_ids):
            rows = cursor.execute(
                f"""
                SELECT tf.file_id, t.id AS tag_id, t.name, tf.assigned_at
                FROM tagged_files tf
                JOIN tags t ON t.id = tf.tag_id
                WHERE tf.file_id IN ({_placeholders(len(chunk))})
                ORDER BY t.name
                """,
                chunk,
            ).fetchall()
            for row in rows:
                tags_by_file[row["file_id"]].append(
                    Tag(
                        name=row["name"],
                        tag_id=row["tag_id"],
                        time_added=_to_datetime(row["assigned_at"]),
                    )
                )
        return tags_by_file

    @staticmethod
    def _row_to_tagged_file(row: sqlite3.Row, tags: list[Tag]) -> TaggedFile:
        file_format: str | None = row["format"]
        file_type = file_format.lstrip(".") or None if file_format else None
        return TaggedFile(
            file_id=row["id"],
            file_hash=row["hash"],
            original_path=Path(row["path"]),
            status=row["status"],
            tags=tags,
            metadata=FileMetadata(
                file_size=row["size"],
                time_added=_to_datetime(row["created_at"]),
                file_format=file_format,
                file_type=file_type,
                mime_type=row["mime_type"],
            ),
        )

    # ------------------------------------------------------------------ files

    @transactional
    def insert(
        self,
        filename: str,
        file_path: Path,
        file_hash: str,
        file_size: int,
        file_format: str | None = None,
        file_mime_type: str | None = None,
    ) -> SQLResult:
        """Register a file. A soft-deleted row for the same path is revived."""
        self.logger.info(f"Inserting file into database: {file_path}")
        cursor = self.connection.cursor()
        operation_id = str(uuid4())

        existing = self._find_file(cursor, file_path)
        if existing is None:
            file_id = str(uuid4())
            cursor.execute(
                """
                INSERT INTO files (id, filename, path, hash, size, format, mime_type)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_id,
                    filename,
                    str(file_path),
                    file_hash,
                    file_size,
                    file_format,
                    file_mime_type,
                ),
            )
            status = OperationResultEnum.SUCCESS
            event_name = "file.insert"
        elif existing["status"] == FileStatus.DELETED:
            file_id = existing["id"]
            cursor.execute(
                """
                UPDATE files
                SET filename = ?, hash = ?, size = ?, format = ?, mime_type = ?,
                    status = 'active', deleted_at = NULL, modified_at = unixepoch()
                WHERE id = ?
                """,
                (filename, file_hash, file_size, file_format, file_mime_type, file_id),
            )
            status = OperationResultEnum.SUCCESS
            event_name = "file.insert.restore"
        else:
            file_id = existing["id"]
            status = OperationResultEnum.ALREADY_EXISTS
            event_name = "file.insert.duplicate"

        self._record_event(cursor, event_name, file_id=file_id, event_id=operation_id)

        return SQLResult(
            operation_id=operation_id,
            status=status,
            record_id=file_id,
            type=SQLOperationType.File,
        )

    @transactional
    def update(
        self,
        file_path: Path,
        file_hash: str,
        file_size: int,
        file_format: str | None = None,
        file_mime_type: str | None = None,
    ) -> SQLResult:
        """Refresh the stored metadata of an existing path."""
        self.logger.info(f"Updating file in database: {file_path}")
        cursor = self.connection.cursor()
        operation_id = str(uuid4())

        existing = self._find_file(cursor, file_path)
        if existing is None:
            self.logger.warning(f"Cannot update unknown file: {file_path}")
            return SQLResult(
                operation_id=operation_id,
                status=OperationResultEnum.NOT_FOUND,
                type=SQLOperationType.File,
            )

        file_id = existing["id"]
        cursor.execute(
            """
            UPDATE files
            SET hash = ?, size = ?, format = ?, mime_type = ?,
                status = 'active', deleted_at = NULL, modified_at = unixepoch()
            WHERE id = ?
            """,
            (file_hash, file_size, file_format, file_mime_type, file_id),
        )
        event_name = (
            "file.update.restore"
            if existing["status"] == FileStatus.DELETED
            else "file.update"
        )
        self._record_event(cursor, event_name, file_id=file_id, event_id=operation_id)

        return SQLResult(
            operation_id=operation_id,
            status=OperationResultEnum.SUCCESS,
            record_id=file_id,
            type=SQLOperationType.File,
        )

    @transactional
    def delete(self, file_path: Path) -> SQLResult:
        """Soft-delete a file: keeps the row (and its tags), sets ``status``."""
        self.logger.info(f"Deleting file from database: {file_path}")
        cursor = self.connection.cursor()
        operation_id = str(uuid4())

        existing = self._find_file(cursor, file_path)
        if existing is None:
            self.logger.warning(f"Cannot delete unknown file: {file_path}")
            return SQLResult(
                operation_id=operation_id,
                status=OperationResultEnum.NOT_FOUND,
                type=SQLOperationType.File,
            )

        file_id = existing["id"]
        if existing["status"] == FileStatus.DELETED:
            self._record_event(
                cursor, "file.delete.duplicate", file_id=file_id, event_id=operation_id
            )
            return SQLResult(
                operation_id=operation_id,
                status=OperationResultEnum.ALREADY_EXISTS,
                record_id=file_id,
                type=SQLOperationType.File,
            )

        cursor.execute(
            """
            UPDATE files
            SET status = 'deleted', deleted_at = unixepoch(), modified_at = unixepoch()
            WHERE id = ?
            """,
            (file_id,),
        )
        self._record_event(
            cursor, "file.delete", file_id=file_id, event_id=operation_id
        )

        return SQLResult(
            operation_id=operation_id,
            status=OperationResultEnum.SUCCESS,
            record_id=file_id,
            type=SQLOperationType.File,
        )

    @transactional
    def modify(self, file_path: Path, new_path: Path) -> SQLResult:
        """Record a rename/move of ``file_path`` to ``new_path``."""
        self.logger.info(f"Moving file in database: {file_path} -> {new_path}")
        cursor = self.connection.cursor()
        operation_id = str(uuid4())

        existing = self._find_file(cursor, file_path)
        if existing is None:
            self.logger.warning(f"Cannot move unknown file: {file_path}")
            return SQLResult(
                operation_id=operation_id,
                status=OperationResultEnum.NOT_FOUND,
                type=SQLOperationType.File,
            )

        file_id = existing["id"]
        if str(new_path) == str(file_path):
            return SQLResult(
                operation_id=operation_id,
                status=OperationResultEnum.SUCCESS,
                record_id=file_id,
                type=SQLOperationType.File,
            )

        if self._find_file(cursor, new_path) is not None:
            self.logger.warning(f"Cannot move {file_path}: {new_path} already exists")
            return SQLResult(
                operation_id=operation_id,
                status=OperationResultEnum.ALREADY_EXISTS,
                record_id=file_id,
                type=SQLOperationType.File,
            )

        cursor.execute(
            """
            UPDATE files
            SET path = ?, filename = ?, modified_at = unixepoch()
            WHERE id = ?
            """,
            (str(new_path), new_path.name, file_id),
        )
        self._record_event(
            cursor,
            "file.move",
            file_id=file_id,
            description=f"{file_path} -> {new_path}",
            event_id=operation_id,
        )

        return SQLResult(
            operation_id=operation_id,
            status=OperationResultEnum.SUCCESS,
            record_id=file_id,
            type=SQLOperationType.File,
        )

    # ------------------------------------------------------------------- tags

    @transactional
    def upsert_tag(
        self,
        name: str,
        category: str | None = None,
        description: str | None = None,
    ) -> SQLResult:
        """Create a tag, or refresh ``category``/``description`` of an existing one."""
        cursor = self.connection.cursor()
        operation_id = str(uuid4())

        tag_id, created = self._ensure_tag(cursor, name, category, description)
        if created:
            self.logger.info(f"Created tag: {name}")
            self._record_event(
                cursor, "tag.insert", tag_id=tag_id, event_id=operation_id
            )
            status = OperationResultEnum.SUCCESS
        else:
            status = OperationResultEnum.ALREADY_EXISTS

        return SQLResult(
            operation_id=operation_id,
            status=status,
            record_id=tag_id,
            type=SQLOperationType.Tag,
        )

    @transactional
    def set_file_tags(self, file_path: Path, tag_names: list[str]) -> SQLResult:
        """Make ``tag_names`` the exact tag set of a file, creating tags as needed."""
        cursor = self.connection.cursor()
        operation_id = str(uuid4())

        existing = self._find_file(cursor, file_path)
        if existing is None:
            self.logger.warning(f"Cannot tag unknown file: {file_path}")
            return SQLResult(
                operation_id=operation_id,
                status=OperationResultEnum.NOT_FOUND,
                type=SQLOperationType.File,
            )
        file_id = existing["id"]

        wanted: dict[str, str] = {}  # tag name -> tag id, insertion ordered
        for name in tag_names:
            if name in wanted:
                continue
            tag_id, created = self._ensure_tag(cursor, name)
            if created:
                self._record_event(cursor, "tag.insert", tag_id=tag_id)
            wanted[name] = tag_id

        current: set[str] = {
            row["tag_id"]
            for row in cursor.execute(
                "SELECT tag_id FROM tagged_files WHERE file_id = ?", (file_id,)
            )
        }
        wanted_ids = set(wanted.values())

        for tag_id in wanted_ids - current:
            cursor.execute(
                "INSERT INTO tagged_files (tag_id, file_id) VALUES (?, ?)",
                (tag_id, file_id),
            )
            self._record_event(cursor, "tag.assign", file_id=file_id, tag_id=tag_id)

        for tag_id in current - wanted_ids:
            cursor.execute(
                "DELETE FROM tagged_files WHERE tag_id = ? AND file_id = ?",
                (tag_id, file_id),
            )
            self._record_event(cursor, "tag.unassign", file_id=file_id, tag_id=tag_id)

        if wanted_ids != current:
            self.logger.info(f"Tags for {file_path}: {list(wanted)}")

        return SQLResult(
            operation_id=operation_id,
            status=OperationResultEnum.SUCCESS,
            record_id=file_id,
            type=SQLOperationType.File,
        )

    # ---------------------------------------------------------------- queries

    def query_tag(
        self, tag_name: str | None = None, tag_id: str | None = None
    ) -> Tag | None:
        if tag_name is None and tag_id is None:
            raise ValueError("query_tag requires tag_name or tag_id")

        cursor = self.connection.cursor()
        if tag_id is not None:
            row = cursor.execute(
                "SELECT id, name, created_at FROM tags WHERE id = ?", (tag_id,)
            ).fetchone()
        else:
            row = cursor.execute(
                "SELECT id, name, created_at FROM tags WHERE name = ?", (tag_name,)
            ).fetchone()

        if row is None:
            return None
        return Tag(
            name=row["name"],
            tag_id=row["id"],
            time_added=_to_datetime(row["created_at"]),
        )

    def query_file(
        self, file_path: Path, include_deleted: bool = False
    ) -> TaggedFile | None:
        cursor = self.connection.cursor()
        sql = "SELECT * FROM files WHERE path = ?"
        if not include_deleted:
            sql += " AND status <> 'deleted'"
        row = cursor.execute(sql, (str(file_path),)).fetchone()
        if row is None:
            return None
        tags = self._load_tags(cursor, [row["id"]])[row["id"]]
        return self._row_to_tagged_file(row, tags)

    def query_files(
        self,
        tags: list[str] | None = None,
        tag_ids: list[str] | None = None,
        filename: str | None = None,
        file_hash: str | None = None,
        file_format: str | None = None,
        file_type: str | None = None,
        mime_type: str | None = None,
        file_size_range: tuple[int, int] | None = None,
        file_added_range: tuple[datetime, datetime] | None = None,
        include_deleted: bool = False,
    ) -> list[TaggedFile]:
        """Find files matching every given criterion.

        ``tags`` / ``tag_ids`` require the file to carry *all* listed tags.
        ``filename`` is a case-insensitive substring match; ``file_format`` is
        the suffix with its dot (``.txt``), ``file_type`` the suffix without it.
        """
        cursor = self.connection.cursor()
        clauses: list[str] = []
        params: list[Any] = []

        if not include_deleted:
            clauses.append("f.status <> 'deleted'")
        if filename:
            clauses.append("f.filename LIKE ? ESCAPE '\\'")
            escaped = (
                filename.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            params.append(f"%{escaped}%")
        if file_hash:
            clauses.append("f.hash = ?")
            params.append(file_hash)
        if file_format:
            clauses.append("f.format = ?")
            params.append(file_format)
        if file_type:
            clauses.append("f.format = ?")
            params.append("." + file_type.lstrip("."))
        if mime_type:
            clauses.append("f.mime_type = ?")
            params.append(mime_type)
        if file_size_range is not None:
            low, high = sorted(file_size_range)
            clauses.append("f.size BETWEEN ? AND ?")
            params.extend((low, high))
        if file_added_range is not None:
            start, end = sorted(file_added_range)
            clauses.append("f.created_at BETWEEN ? AND ?")
            params.extend((_to_epoch(start), _to_epoch(end)))

        required_tag_ids = self._resolve_tag_ids(cursor, tags, tag_ids)
        if required_tag_ids is None:
            # A requested tag does not exist, so no file can carry all of them.
            return []
        if required_tag_ids:
            clauses.append(
                f"""
                f.id IN (
                    SELECT tf.file_id FROM tagged_files tf
                    WHERE tf.tag_id IN ({_placeholders(len(required_tag_ids))})
                    GROUP BY tf.file_id
                    HAVING COUNT(DISTINCT tf.tag_id) = ?
                )
                """
            )
            params.extend(required_tag_ids)
            params.append(len(required_tag_ids))

        sql = "SELECT f.* FROM files f"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY f.created_at, f.path"

        rows = cursor.execute(sql, params).fetchall()
        if not rows:
            return []

        tags_by_file = self._load_tags(cursor, [row["id"] for row in rows])
        return [self._row_to_tagged_file(row, tags_by_file[row["id"]]) for row in rows]

    def _resolve_tag_ids(
        self,
        cursor: sqlite3.Cursor,
        tag_names: list[str] | None,
        tag_ids: list[str] | None,
    ) -> list[str] | None:
        """De-duplicated id list for names + ids; ``None`` if a name is unknown."""
        resolved: dict[str, None] = {}
        for tag_id in tag_ids or []:
            resolved[tag_id] = None

        names = list(dict.fromkeys(tag_names or []))
        for chunk in _chunks(names):
            rows = cursor.execute(
                f"SELECT id, name FROM tags WHERE name IN ({_placeholders(len(chunk))})",
                chunk,
            ).fetchall()
            found = {row["name"]: row["id"] for row in rows}
            for name in chunk:
                if name not in found:
                    self.logger.debug(f"Unknown tag in query: {name}")
                    return None
                resolved[found[name]] = None

        return list(resolved)
