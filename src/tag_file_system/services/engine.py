# Code by AkinoAlice@TyrantRey

import logging
from pathlib import Path

import watchfiles
from watchfiles import Change, watch

from tag_file_system.config import DatabaseSetting, FolderSetting
from tag_file_system.core.interface.database import (
    DatabaseEngineProtocol,
    DatabaseOperation,
)
from tag_file_system.core.logger import logger
from tag_file_system.core.router.database_event import DatabaseEventRouter
from tag_file_system.core.router.watch_event import WatchEventRouter

watchfiles.main.logger.setLevel(logging.WARNING)


class TagFileEngine:
    def __init__(
        self,
        watch_event_router: WatchEventRouter,
        database_event_router: DatabaseEventRouter,
        database_engine: DatabaseEngineProtocol,
        root_dir: Path | None = None,
        files_dir: Path | None = None,
        tags_dir: Path | None = None,
        database_path: Path | None = None,
    ):
        self.logger = logger

        self.folder_setting = FolderSetting()
        self.database_setting = DatabaseSetting()

        self.root_dir = (
            root_dir if root_dir is not None else self.folder_setting.root_dir
        )
        self.files_dir = (
            files_dir if files_dir is not None else self.folder_setting.files_dir
        )
        self.tags_dir = (
            tags_dir if tags_dir is not None else self.folder_setting.tags_dir
        )
        self.database_path = (
            database_path
            if database_path is not None
            else self.database_setting.db_file
        )

        self.logger.info(f"Root directory: {self.root_dir}")
        self.logger.info(f"Files directory: {self.files_dir}")
        self.logger.info(f"Tags directory: {self.tags_dir}")
        self.logger.info(f"Database path: {self.database_path}")

        self.watch_event_router = watch_event_router
        self.database_event_router = database_event_router

        self.database_engine = database_engine
        self.init_database(self.database_path)

        self.logger.info("Initialized TagFileEngine")

    def ensure_directories(self):
        for d in (self.files_dir, self.tags_dir):
            if not d.exists():
                self.logger.info(f"Creating directory: {d}")
                d.mkdir(parents=True, exist_ok=True)

    def init_database(self, database_path: Path):
        self.logger.info(f"Initializing database at {database_path}")

        self.database_engine.init_database(database_path)

    def start(self):
        self.logger.info("Starting TagFileEngine")
        self.ensure_directories()
        for changes in watch(self.files_dir.parent):
            consolidated = self._consolidate(changes)
            for raw_path, operation in consolidated.items():
                self.watch_event_router.dispatch(operation, Path(raw_path))

                match operation:
                    case Change.added:
                        self.logger.info(f"Insert into DB: {raw_path}")
                        self.database_event_router.dispatch(
                            DatabaseOperation.INSERT, Path(raw_path)
                        )
                    case Change.deleted:
                        self.logger.info(f"Remove from DB: {raw_path}")
                        self.database_event_router.dispatch(
                            DatabaseOperation.DELETE, Path(raw_path)
                        )
                    case Change.modified:
                        self.logger.info(f"Update DB: {raw_path}")
                        self.database_event_router.dispatch(
                            DatabaseOperation.UPDATE, Path(raw_path)
                        )

    def _consolidate(self, changes: set) -> dict[str, Change]:
        consolidated: dict[str, Change] = {}
        priority: dict[Change, int] = {
            Change.deleted: 0,
            Change.added: 1,
            Change.modified: 2,
        }
        for operation, raw_path in changes:
            if (
                raw_path not in consolidated
                or priority[operation] < priority[consolidated[raw_path]]
            ):
                consolidated[raw_path] = operation
        return consolidated
