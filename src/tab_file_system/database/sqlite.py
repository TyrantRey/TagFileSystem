# Code by AkinoAlice@TyrantRey

from typing import Callable
import sqlite3
from pathlib import Path
from functools import wraps
from tab_file_system.core.logger import logger
from tab_file_system.core.interface.file_metadata import Tag, FileMetadata
from tab_file_system.core.interface.database import (
    SQLResult,
    OperationResultEnum,
    SQLOperationType,
)
from uuid import uuid4

from datetime import datetime


def transactional(method: Callable) -> Callable:
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            result = method(self, *args, **kwargs)
            self.connection.commit()
            return result
        except Exception as e:
            self.connection.rollback()
            self.logger.error(f"Transaction failed: {e}")
            raise

    return wrapper


class SQLiteBackend:
    def __init__(self) -> None:
        self.logger = logger

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

    @transactional
    def insert(
        self,
        filename: str,
        file_path: Path,
        file_hash: str,
        file_size: int,
        file_format: str,
        file_mime_type: str,
    ) -> SQLResult:
        self.logger.info(f"Inserting file into database: {file_path}")
        cursor = self.connection.cursor()

        file_id = str(uuid4())
        event_id = str(uuid4())
        cursor = self.connection.cursor()

        cursor.execute(
            """
            INSERT OR IGNORE INTO files (id, filename, path, hash, size, format, mime_type)
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

        if cursor.rowcount == 0:
            cursor.execute("SELECT id FROM files WHERE path = ?", (str(file_path),))
            file_id = cursor.fetchone()[0]
            status = OperationResultEnum.ALREADY_EXISTS
            event_name = "file.insert.duplicate"
        else:
            status = OperationResultEnum.SUCCESS
            event_name = "file.insert"

        cursor.execute(
            """
            INSERT INTO events (id, name, file_id)
            VALUES (?, ?, ?)
            """,
            (event_id, event_name, file_id),
        )

        return SQLResult(
            operation_id=event_id,
            status=status,
            record_id=file_id,
            type=SQLOperationType.File,
        )

    def update(self, path: str) -> None: ...

    def delete(self, path: str) -> None: ...

    def modify(self, path: str) -> None: ...

    def query_tag(self, tag_name: str | None, tag_id: int | None) -> Tag | None: ...

    def query_files(
        self,
        tags: list[Tag] | None = None,
        tag_ids: list[int] | None = None,
        filename: str | None = None,
        file_hash: str | None = None,
        file_format: str | None = None,
        file_type: str | None = None,
        file_size_range: tuple[int, int] | None = None,
        file_added_range: tuple[datetime, datetime] | None = None,
    ) -> list[FileMetadata] | None: ...
