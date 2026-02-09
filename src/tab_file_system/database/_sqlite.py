# Code by AkinoAlice@TyrantRey

import sqlite3
from pathlib import Path


class SQLiteBackend:
    def __init__(self, database_file: Path):
        self.database_file = database_file
        self.connection = sqlite3.connect(self.database_file)
        self.cursor = self.connection.cursor()
        self.connection.row_factory = sqlite3.Row

    def on_file_added(self, file_path: Path, file_hash: str, date) -> bool:
        return True
