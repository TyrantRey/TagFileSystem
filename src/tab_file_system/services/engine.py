# Code by AkinoAlice@TyrantRey

import logging
from pathlib import Path

import watchfiles
from watchfiles import Change, watch

from tab_file_system.core.router.database_event import DatabaseEventRouter
from tab_file_system.core.router.watch_event import WatchEventRouter

watchfiles.main.logger.setLevel(logging.WARNING)


class TagFileEngine:
    def __init__(
        self,
        files_dir: Path,
        tags_dir: Path,
        watch_event_router: WatchEventRouter,
        database_event_router: DatabaseEventRouter,
    ):
        self.logger = logging.getLogger(__name__)
        self.files_dir = files_dir
        self.tags_dir = tags_dir
        self.watch_event_router = watch_event_router
        self.database_event_router = database_event_router
        self.logger.info("Initialized TagFileEngine")

    def ensure_directories(self):
        for d in (self.files_dir, self.tags_dir):
            if not d.exists():
                self.logger.info(f"Creating directory: {d}")
                d.mkdir(parents=True, exist_ok=True)

    def start(self):
        self.logger.info("Starting TagFileEngine")
        self.ensure_directories()
        for changes in watch(self.files_dir.parent):
            consolidated = self._consolidate(changes)
            for raw_path, operation in consolidated.items():
                self.watch_event_router.dispatch(operation, Path(raw_path))

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
