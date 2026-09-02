# Code by AkinoAlice@TyrantRey

from collections.abc import Iterator
from pathlib import Path

import pytest

from tag_file_system.database.sqlite import SQLiteBackend
from tag_file_system.updater import REGISTRY_ENV


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every root-scoped command registers its root (DESIGN/v0-2-0.md §5):
    keep the suite out of the user's real registry. Subprocesses started by
    a test inherit the variable."""
    registry = tmp_path / "registry" / "roots.json"
    monkeypatch.setenv(REGISTRY_ENV, str(registry))
    return registry


@pytest.fixture
def backend(tmp_path: Path) -> Iterator[SQLiteBackend]:
    engine = SQLiteBackend()
    engine.init_database(tmp_path / "test.db", root_dir=tmp_path)
    yield engine
    engine.close()
