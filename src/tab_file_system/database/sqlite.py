# Code by AkinoAlice@TyrantRey

import logging
import sqlite3
from pathlib import Path


class SQLiteBackend:
    def __init__(self, database_file: Path) -> None:
        self.logger = logging.getLogger(__name__)

        self.database_file = database_file
        self.connection = sqlite3.connect(self.database_file)
        self.cursor = self.connection.cursor()
        self.connection.row_factory = sqlite3.Row

        self.init_database()

    def init_database(self) -> None:
        sql_script = """
        PRAGMA journal_mode = WAL;
        PRAGMA foreign_keys = ON;
        PRAGMA synchronous = NORMAL;

        CREATE TABLE IF NOT EXISTS files (
            id          TEXT    PRIMARY KEY,
            path        TEXT    NOT NULL UNIQUE,
            hash        TEXT,
            size        INTEGER NOT NULL DEFAULT 0,
            format      TEXT,
            mime_type   TEXT,
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

        CREATE TABLE IF NOT EXISTS actions (
            id          TEXT    PRIMARY KEY,
            name        TEXT    NOT NULL UNIQUE,
            description TEXT,
            icon        TEXT,
            category    TEXT,
            is_active   INTEGER NOT NULL DEFAULT 1,
            created_at  INTEGER NOT NULL DEFAULT (unixepoch())
        );

        CREATE INDEX IF NOT EXISTS idx_actions_is_active ON actions(is_active);

        CREATE TABLE IF NOT EXISTS tagged_files (
            tag_id      TEXT    NOT NULL REFERENCES tags(id)  ON DELETE CASCADE,
            file_id     TEXT    NOT NULL REFERENCES files(id) ON DELETE CASCADE,
            assigned_at INTEGER NOT NULL DEFAULT (unixepoch()),
            PRIMARY KEY (tag_id, file_id)
        );
        CREATE INDEX IF NOT EXISTS idx_tagged_files_file_id ON tagged_files(file_id);

        CREATE TABLE IF NOT EXISTS tag_actions (
            tag_id      TEXT    NOT NULL REFERENCES tags(id)    ON DELETE CASCADE,
            action_id   TEXT    NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
            trigger_on  TEXT    NOT NULL DEFAULT 'manual'
                        CHECK(trigger_on IN ('manual', 'auto', 'scheduled')),
            created_at  INTEGER NOT NULL DEFAULT (unixepoch()),
            PRIMARY KEY (tag_id, action_id)
        );

        CREATE TABLE IF NOT EXISTS events (
            id          TEXT    PRIMARY KEY,
            name        TEXT    NOT NULL,
            description TEXT,
            file_id     TEXT    REFERENCES files(id) ON DELETE SET NULL,
            tag_id      TEXT    REFERENCES tags(id)  ON DELETE SET NULL,
            occurred_at INTEGER NOT NULL DEFAULT (unixepoch())
        );

        CREATE INDEX IF NOT EXISTS idx_events_occurred_at ON events(occurred_at);
        CREATE INDEX IF NOT EXISTS idx_events_file_id     ON events(file_id);
        CREATE INDEX IF NOT EXISTS idx_events_tag_id      ON events(tag_id);
                                  """
        self.cursor.executescript(sql_script)
        self.connection.commit()
        self.logger.debug(f"{sql_script=}")
        self.logger.info("Database initialized")
