# Code by AkinoAlice@TyrantRey

import sqlite3
import threading
from pathlib import Path, PurePosixPath

import pytest

from tag_file_system.core.paths import is_anchored, posix_key
from tag_file_system.database.migrations import (
    SCHEMA_VERSION,
    MigrationError,
    SchemaTooNew,
    apply_migrations,
    relative_key,
    user_version,
)
from tag_file_system.database.sqlite import SQLiteBackend
from tests.helpers import fetch, metadata_of

EXPECTED_TABLES = {
    "files",
    "tags",
    "tagged_files",
    "events",
    "actions",
    "action_runs",
    "action_trace",
    "provenance",
    "problems",
}

# The schema as the pre-versioning code created it (absolute native paths).
LEGACY_V0 = """
CREATE TABLE files (
    id TEXT PRIMARY KEY, filename TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
    hash TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0, format TEXT, mime_type TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','deleted','archived')),
    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
    modified_at INTEGER NOT NULL DEFAULT (unixepoch()), deleted_at INTEGER
);
CREATE TABLE tags (
    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, category TEXT, description TEXT,
    created_at INTEGER NOT NULL DEFAULT (unixepoch()), updated_at INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE TABLE tagged_files (
    tag_id TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    assigned_at INTEGER NOT NULL DEFAULT (unixepoch()), PRIMARY KEY (tag_id, file_id)
);
CREATE TABLE events (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
    file_id TEXT REFERENCES files(id) ON DELETE SET NULL, tag_id TEXT REFERENCES tags(id) ON DELETE SET NULL,
    occurred_at INTEGER NOT NULL DEFAULT (unixepoch()), CHECK (file_id IS NOT NULL OR tag_id IS NOT NULL)
);
"""


def tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def make_legacy_db(path: Path, rows: list[tuple[str, str, str]]) -> None:
    """``rows``: (id, path, status)."""
    connection = sqlite3.connect(path)
    connection.executescript(LEGACY_V0)
    for file_id, file_path, status in rows:
        connection.execute(
            "INSERT INTO files (id, filename, path, hash, status) VALUES (?, ?, ?, 'h', ?)",
            (file_id, Path(file_path).name, file_path, status),
        )
    connection.execute("INSERT INTO tags (id, name) VALUES ('t1', 'keep')")
    for file_id, _, _ in rows:
        connection.execute(
            "INSERT INTO tagged_files (tag_id, file_id) VALUES ('t1', ?)", (file_id,)
        )
    connection.commit()
    connection.close()


# ------------------------------------------------------------------- fresh


def test_fresh_database_is_at_current_version(tmp_path: Path):
    backend = SQLiteBackend()
    backend.init_database(tmp_path / "fresh.db", root_dir=tmp_path)

    assert user_version(backend.connection) == SCHEMA_VERSION == 1
    assert tables(backend.connection) == EXPECTED_TABLES
    assert "mtime_ns" in columns(backend.connection, "files")
    assert backend.migration_report is not None
    assert backend.migration_report.applied
    assert backend.migration_report.from_version == 0
    backend.close()


def test_reopening_is_idempotent(tmp_path: Path):
    backend = SQLiteBackend()
    backend.init_database(tmp_path / "x.db", root_dir=tmp_path)
    backend.insert(filename="a.txt", file_path="a.txt", file_hash="h", file_size=1)
    backend.close()

    again = SQLiteBackend()
    again.init_database(tmp_path / "x.db", root_dir=tmp_path)

    assert again.migration_report is not None
    assert not again.migration_report.applied
    assert user_version(again.connection) == SCHEMA_VERSION
    assert fetch(again, "a.txt").path == PurePosixPath("a.txt")
    again.close()


def test_newer_database_is_refused(tmp_path: Path):
    db = tmp_path / "future.db"
    connection = sqlite3.connect(db)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    connection.close()

    with pytest.raises(SchemaTooNew):
        SQLiteBackend().init_database(db, root_dir=tmp_path)


# ------------------------------------------------------------------ legacy


def test_legacy_database_is_migrated_in_place(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "legacy.db"
    inside = str(root / "sub" / "a--keep.txt")
    outside = str(tmp_path / "elsewhere" / "b.txt")
    make_legacy_db(
        db,
        [
            ("f1", inside, "active"),
            ("f2", outside, "active"),
            ("f3", "already/relative.txt", "active"),
            ("f4", str(root / "gone.txt"), "deleted"),
        ],
    )

    backend = SQLiteBackend()
    backend.init_database(db, root_dir=root)

    report = backend.migration_report
    assert report is not None
    assert (report.from_version, report.to_version) == (0, 1)
    assert report.relativized == 2  # f1 and f4
    assert report.outside_root == [outside]
    assert tables(backend.connection) == EXPECTED_TABLES

    moved = fetch(backend, root / "sub" / "a--keep.txt")
    assert moved.file_id == "f1"
    assert moved.path == PurePosixPath("sub/a--keep.txt")
    assert moved.original_path == root / "sub" / "a--keep.txt"
    assert [t.name for t in moved.tags] == ["keep"]  # links survived

    assert fetch(backend, "already/relative.txt").file_id == "f3"
    assert fetch(backend, "gone.txt", include_deleted=True).status == "deleted"

    row = backend.connection.execute(
        "SELECT path, status FROM files WHERE id = 'f2'"
    ).fetchone()
    assert (row["path"], row["status"]) == (outside, "deleted")
    problems = backend.connection.execute(
        "SELECT severity, kind, message FROM problems"
    ).fetchall()
    assert len(problems) == 1
    assert (problems[0]["severity"], problems[0]["kind"]) == (
        "warn",
        "migration.path_unresolved",
    )
    assert outside in problems[0]["message"]
    backend.close()


def test_legacy_database_with_absolute_paths_needs_a_root(tmp_path: Path):
    db = tmp_path / "legacy.db"
    absolute = str(tmp_path / "a.txt")
    make_legacy_db(db, [("f1", absolute, "active")])

    backend = SQLiteBackend()
    with pytest.raises(MigrationError):
        backend.init_database(db)  # no root_dir: the rows could never be keyed

    assert not backend.is_open  # a failed init leaves nothing open
    connection = sqlite3.connect(db)
    assert user_version(connection) == 0  # nothing half-applied
    connection.close()

    # relative rows are fine without a root
    other = tmp_path / "relative.db"
    make_legacy_db(other, [("f1", "a.txt", "active")])
    backend.init_database(other)
    assert fetch(backend, "a.txt").file_id == "f1"
    backend.close()


def test_legacy_keys_are_normalized_or_rejected(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "legacy.db"
    make_legacy_db(
        db,
        [
            ("f1", str(root), "active"),  # the root itself
            ("f2", str(root / "a" / ".." / "b.txt"), "active"),
            ("f3", "x/./y.txt", "active"),
            ("f4", "../escape.txt", "active"),
            ("f5", str(tmp_path / "gone.txt"), "deleted"),  # outside, already dead
        ],
    )

    backend = SQLiteBackend()
    backend.init_database(db, root_dir=root)

    report = backend.migration_report
    assert report is not None
    assert fetch(backend, "b.txt").file_id == "f2"
    assert fetch(backend, "x/y.txt").file_id == "f3"
    assert sorted(report.outside_root) == sorted([str(root), "../escape.txt"])
    problems = backend.connection.execute("SELECT message FROM problems").fetchall()
    assert len(problems) == 1
    assert "gone.txt" not in problems[0]["message"]  # already deleted: not noise
    backend.close()


def test_relative_root_dir_is_resolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "legacy.db"
    make_legacy_db(db, [("f1", str(root / "a.txt"), "active")])
    monkeypatch.chdir(tmp_path)

    connection = sqlite3.connect(db)
    report = apply_migrations(connection, Path("root"))

    assert report.relativized == 1 and report.outside_root == []
    connection.close()


def test_conflict_prefers_the_active_row(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "legacy.db"
    make_legacy_db(
        db, [("dead", "a.txt", "deleted"), ("live", str(root / "a.txt"), "active")]
    )

    backend = SQLiteBackend()
    backend.init_database(db, root_dir=root)

    assert fetch(backend, "a.txt").file_id == "live"
    parked = backend.connection.execute(
        "SELECT path, status FROM files WHERE id = 'dead'"
    ).fetchone()
    assert parked["path"].startswith(".tfs/duplicates/dead/")
    assert parked["status"] == "deleted"
    assert backend.migration_report is not None
    assert backend.migration_report.conflicts == []
    backend.close()


def test_apply_migrations_refuses_an_open_transaction(tmp_path: Path):
    connection = sqlite3.connect(tmp_path / "x.db")
    connection.execute("BEGIN")
    with pytest.raises(MigrationError):
        apply_migrations(connection, tmp_path)
    connection.close()


def test_duplicate_key_after_relativizing_is_reported(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "legacy.db"
    make_legacy_db(
        db, [("f1", "a.txt", "active"), ("f2", str(root / "a.txt"), "active")]
    )

    backend = SQLiteBackend()
    backend.init_database(db, root_dir=root)

    assert backend.migration_report is not None
    assert backend.migration_report.conflicts == [str(root / "a.txt")]
    assert fetch(backend, "a.txt").file_id == "f1"
    row = backend.connection.execute("SELECT status FROM files WHERE id = 'f2'").fetchone()
    assert row["status"] == "deleted"
    backend.close()


def test_failed_migration_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from tag_file_system.database import migrations

    def explode(connection, root_dir, report):
        connection.execute("CREATE TABLE half_done (x)")
        raise RuntimeError("boom")

    monkeypatch.setattr(migrations, "MIGRATIONS", [(1, explode)])
    db = tmp_path / "x.db"

    with pytest.raises(RuntimeError):
        SQLiteBackend().init_database(db, root_dir=tmp_path)

    connection = sqlite3.connect(db)
    assert user_version(connection) == 0
    assert "half_done" not in tables(connection)
    connection.close()


# -------------------------------------------------------------------- keys


def test_relative_key_helper(tmp_path: Path):
    assert relative_key("a/b.txt", None) == "a/b.txt"
    # posix_key splits on both separators by design, so one path yields one
    # key on every host: a database written on Windows reads back on Linux.
    assert relative_key("a\\b.txt", None) == "a/b.txt"
    assert relative_key("a/../b.txt", None) == "b.txt"
    assert relative_key("./a/./b.txt", None) == "a/b.txt"
    assert relative_key("../b.txt", None) is None
    assert relative_key(".", None) is None
    assert relative_key(str(tmp_path / "x" / "y.txt"), tmp_path) == "x/y.txt"
    assert relative_key(str(tmp_path / "x" / ".." / "y.txt"), tmp_path) == "y.txt"
    assert relative_key(str(tmp_path), tmp_path) is None
    assert relative_key(str(tmp_path.parent / "y.txt"), tmp_path) is None
    assert relative_key("/abs/y.txt", None) is None
    assert relative_key("C:\\abs\\y.txt", None) is None


def test_path_helpers():
    assert is_anchored("/x") and is_anchored("\\x") and is_anchored("C:\\x")
    assert is_anchored("c:/x") and is_anchored("\\\\server\\share")
    assert not is_anchored("a:b.txt")  # a legal POSIX filename, not a drive
    assert not is_anchored("C:") and not is_anchored("rel/x")
    assert posix_key("a/./b/../c.txt") == "a/c.txt"
    assert posix_key("a:b.txt") == "a:b.txt"  # same key on every host
    assert posix_key("dir\\a:b.txt") == "dir/a:b.txt"
    assert posix_key(PurePosixPath("a:b.txt")) == "a:b.txt"
    with pytest.raises(ValueError):
        posix_key("/abs")
    with pytest.raises(ValueError):
        posix_key("../up")


def test_parked_duplicates_are_reported(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    db = tmp_path / "legacy.db"
    make_legacy_db(
        db, [("dead", "a.txt", "deleted"), ("live", str(root / "a.txt"), "active")]
    )

    backend = SQLiteBackend()
    backend.init_database(db, root_dir=root)

    report = backend.migration_report
    assert report is not None
    assert report.parked == [".tfs/duplicates/dead/a.txt"]
    message = backend.connection.execute("SELECT message FROM problems").fetchone()[0]
    assert "parked at: .tfs/duplicates/dead/a.txt" in message
    backend.close()


def test_closed_backend_raises_a_clear_error(tmp_path: Path):
    backend = SQLiteBackend()
    with pytest.raises(RuntimeError):
        backend.query_files()
    backend.init_database(tmp_path / "x.db", root_dir=tmp_path)
    backend.close()
    backend.close()  # idempotent
    with pytest.raises(RuntimeError):
        backend.query_file("a.txt")
    assert not backend.is_open


def test_backend_keys_are_root_relative_posix(tmp_path: Path):
    backend = SQLiteBackend()
    backend.init_database(tmp_path / "x.db", root_dir=tmp_path)
    absolute = tmp_path / "2024--trip" / "img.jpg"

    backend.insert(filename="img.jpg", file_path=absolute, file_hash="h", file_size=1)

    by_relative = fetch(backend, PurePosixPath("2024--trip/img.jpg"))
    by_absolute = fetch(backend, absolute)
    assert by_relative.file_id == by_absolute.file_id
    assert by_relative.path == PurePosixPath("2024--trip/img.jpg")
    assert by_relative.original_path == absolute
    row = backend.connection.execute("SELECT path FROM files").fetchone()
    assert row["path"] == "2024--trip/img.jpg"
    backend.close()


def test_backend_rejects_paths_it_cannot_key(tmp_path: Path):
    backend = SQLiteBackend()
    backend.init_database(tmp_path / "x.db", root_dir=tmp_path)

    with pytest.raises(ValueError):
        backend.key(tmp_path.parent / "outside.txt")
    with pytest.raises(ValueError):
        backend.key("../escape.txt")
    with pytest.raises(ValueError):
        backend.key(".")
    backend.close()

    rootless = SQLiteBackend()
    rootless.init_database(tmp_path / "y.db")
    with pytest.raises(ValueError):
        rootless.key(tmp_path / "a.txt")
    assert rootless.key("a/b.txt") == "a/b.txt"
    rootless.close()


def test_mtime_is_stored_and_updated(backend: SQLiteBackend):
    backend.insert(
        filename="a.txt", file_path="a.txt", file_hash="h", file_size=1, mtime_ns=123
    )
    assert metadata_of(fetch(backend, "a.txt")).mtime_ns == 123

    backend.update(file_path="a.txt", file_hash="h2", file_size=2, mtime_ns=456)
    assert metadata_of(fetch(backend, "a.txt")).mtime_ns == 456

    backend.delete("a.txt")
    backend.insert(
        filename="a.txt", file_path="a.txt", file_hash="h3", file_size=3, mtime_ns=789
    )
    assert metadata_of(fetch(backend, "a.txt")).mtime_ns == 789


def test_connection_is_shared_safely_between_threads(backend: SQLiteBackend):
    errors: list[Exception] = []

    def worker(index: int) -> None:
        try:
            for i in range(20):
                name = f"t{index}-{i}.txt"
                backend.insert(filename=name, file_path=name, file_hash="h", file_size=1)
                backend.set_file_tags(name, [f"tag{index}"])
                backend.query_files(tags=[f"tag{index}"])
        except Exception as e:  # pragma: no cover - reported via assert
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(backend.query_files()) == 80
