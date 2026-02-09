# Code by AkinoAlice@TyrantRey

import logging
from pathlib import Path

import watchfiles
from watchfiles import watch

watchfiles.main.logger.setLevel(logging.WARNING)


class WatchFileEngine:
    def __init__(self, files_dir: Path, tags_dir: Path, database_file: Path):
        self.logger = logging.getLogger(name=__name__)

        self.files_dir = files_dir
        self.tags_dir = tags_dir
        self.database_file = database_file

    def create_watchfolder(self):
        """Create watch folder for files and tags."""

        if not self.files_dir.exists():
            self.logger.info(f"Creating files directory: {self.files_dir}")
            self.files_dir.mkdir(parents=True, exist_ok=True)
        else:
            self.logger.info(f"Skipping create {self.files_dir=}")

        if not self.tags_dir.exists():
            self.logger.info(f"Creating tags directory: {self.tags_dir}")
            self.tags_dir.mkdir(parents=True, exist_ok=True)
        else:
            self.logger.info(f"Skipping create {self.tags_dir=}")

        # Setting up watch folder for database file
        for changes in watch(self.files_dir.parent):
            self.logger.debug(f"Detected changes: {changes}")
            match changes:
                case watchfiles.Change.added:
                    self.logger.info(f"File added: {changes}")
                case watchfiles.Change.modified:
                    self.logger.info(f"File modified: {changes}")
                case watchfiles.Change.deleted:
                    self.logger.info(f"File deleted: {changes}")
