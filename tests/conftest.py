# Code by AkinoAlice@TyrantRey

from collections.abc import Iterator
from pathlib import Path

import pytest

from tag_file_system.database.sqlite import SQLiteBackend


@pytest.fixture
def backend(tmp_path: Path) -> Iterator[SQLiteBackend]:
    engine = SQLiteBackend()
    engine.init_database(tmp_path / "test.db", root_dir=tmp_path)
    yield engine
    engine.close()
