# Code by AkinoAlice@TyrantRey

"""The CLI side of DESIGN/v0-2-0.md: ``--version``, the version in ``list``,
refusals while an upgrade holds the root, ``backup``, ``update``, the hidden
``record-upgrade`` and the registry."""

import json
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tag_file_system import updater, version
from tag_file_system.cli import app
from tag_file_system.config import Config, DaemonConfig
from tag_file_system.database.action_store import ActionStore
from tag_file_system.database.migrations import SCHEMA_VERSION
from tag_file_system.database.sqlite import SQLiteBackend
from tag_file_system.root import Root
from tests.fakerepo import make_repo

runner = CliRunner()


def tfs(*args: object, input: str | None = None):
    return runner.invoke(app, [str(a) for a in args], input=input)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def root(tmp_path: Path) -> Root:
    result = tfs("init", str(tmp_path / "vault"))
    assert result.exit_code == 0, result.output
    root = Root(tmp_path / "vault")
    Config(daemon=DaemonConfig(port=free_port())).write(root.config_path)
    return root


def test_version_flag():
    result = tfs("--version")
    assert result.exit_code == 0
    assert result.output.strip() == version.describe()


def test_list_reports_the_version(root: Root):
    result = tfs("list", "--root", str(root.path))
    assert result.exit_code == 0, result.output
    assert f"tfs {version.describe()}" in result.output
    payload = json.loads(tfs("list", "--root", str(root.path), "--json").output)
    assert payload["version"] == version.VERSION and payload["hash"] == version.COMMIT


def test_root_scoped_commands_register_the_root(root: Root, tmp_path: Path):
    assert updater.known_roots() == [root.path]  # `init` registered it
    other = Root.init(tmp_path / "other")
    assert tfs("query", "--root", str(other.path)).exit_code == 0
    assert updater.known_roots() == [root.path, other.path]


def test_commands_refuse_while_an_upgrade_holds_the_root(root: Root):
    updater.write_marker(root.path)
    try:
        for args in (
            ("list",),
            ("query", "-t", "x"),
            ("reload",),
            ("start",),
            ("start", "-d"),
            ("stop",),
            ("backup", "prune", "--yes"),
        ):
            result = tfs(*args, "--root", str(root.path))
            assert result.exit_code == 1, (args, result.output)
            assert "upgrade is in progress" in result.output, args
            assert "Traceback" not in result.output
        assert tfs("backup", "list", "--root", str(root.path)).exit_code == 0
    finally:
        updater.remove_marker(root.path)
    assert tfs("query", "--root", str(root.path)).exit_code == 0


def test_backup_list_and_prune(root: Root):
    for hour in (1, 2, 3, 4):
        updater.snapshot(
            root.path, f"v0.0.{hour}", now=datetime(2026, 1, 1, hour, tzinfo=UTC)
        )
    listed = tfs("backup", "list", "--root", str(root.path))
    assert listed.exit_code == 0, listed.output
    assert "4 snapshot(s)" in listed.output and "from v0.0.4" in listed.output
    as_json = json.loads(
        tfs("backup", "list", "--root", str(root.path), "--json").output
    )
    assert [b["tag"] for b in as_json] == ["v0.0.4", "v0.0.3", "v0.0.2", "v0.0.1"]

    dry = tfs("backup", "prune", "--root", str(root.path), "--keep", "2", "--dry-run")
    assert dry.exit_code == 0 and "would delete 2" in dry.output
    assert len(updater.list_backups(root.path)) == 4
    declined = tfs(
        "backup", "prune", "--root", str(root.path), "--keep", "2", input="n\n"
    )
    assert declined.exit_code == 0 and "kept" in declined.output
    assert len(updater.list_backups(root.path)) == 4
    pruned = tfs("backup", "prune", "--root", str(root.path), "--keep", "2", "--yes")
    assert pruned.exit_code == 0 and "deleted 2" in pruned.output
    assert [b.label for b in updater.list_backups(root.path)] == ["v0.0.4", "v0.0.3"]
    again = tfs("backup", "prune", "--root", str(root.path), "--keep", "2", "--yes")
    assert "nothing to prune" in again.output
    # the global --root reaches the sub-command too
    assert "2 snapshot(s)" in tfs("--root", str(root.path), "backup", "list").output


def test_record_upgrade_writes_the_row(root: Root, tmp_path: Path):
    payload = tmp_path / "upgrade.json"
    payload.write_text(
        json.dumps(
            {
                "from_tag": "v0.1.1",
                "from_hash": "a" * 40,
                "to_tag": "v0.2.0",
                "to_hash": "b" * 40,
                "schema_before": 1,
                "tests_run": 4,
                "tests_passed": 3,
                "tests_skipped": 1,
                "snapshot_path": "x.db",
                "started_at": 1700000000,
            }
        ),
        encoding="utf-8",
    )
    result = tfs("record-upgrade", str(payload), "--root", str(root.path))
    assert result.exit_code == 0, result.output
    assert "v0.1.1 -> v0.2.0" in result.output

    backend = SQLiteBackend()
    backend.init_database(root.db_path, root_dir=root.path)
    try:
        (row,) = ActionStore(backend).query_upgrades()
    finally:
        backend.close()
    assert (row.from_tag, row.to_tag, row.schema_before) == ("v0.1.1", "v0.2.0", 1)
    assert row.schema_after == SCHEMA_VERSION
    assert (row.tests_run, row.tests_passed, row.tests_skipped) == (4, 3, 1)
    assert row.snapshot_path == "x.db" and row.outcome == "ok"
    assert row.started_at == datetime.fromtimestamp(1700000000, UTC)

    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    assert tfs("record-upgrade", str(bad), "--root", str(root.path)).exit_code == 1
    assert "record-upgrade" not in tfs("--help").output  # hidden


def test_update_reports_against_the_checkout(
    root: Root, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = make_repo(tmp_path / "git")
    monkeypatch.setattr(updater, "repo_dir", lambda: repo.path)

    result = tfs("update", "--root", str(root.path))
    assert result.exit_code == 0, result.output
    assert "current   0.9.0" in result.output and "v1.0.0 = 1.0.0" in result.output
    assert "available" in result.output and "tfs upgrade" in result.output
    assert "schema    1 -> 2" in result.output

    as_json = json.loads(tfs("update", "--root", str(root.path), "--json").output)
    assert as_json["available"] is True and as_json["latest"]["tag"] == "v1.0.0"
    assert as_json["current"]["tag"] == "v0.9.0"

    (repo.path / "marker.txt").write_text("dirty", encoding="utf-8")
    failed = tfs("update", "--root", str(root.path))
    assert failed.exit_code == 1 and "uncommitted" in failed.output


def test_upgrade_command_delegates_to_the_updater(
    root: Root, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = make_repo(tmp_path / "git")
    monkeypatch.setattr(updater, "repo_dir", lambda: repo.path)
    result = tfs("upgrade", "--root", str(root.path), "--dry-run")
    assert result.exit_code == 0, result.output
    assert "upgrade plan" in result.output and "v1.0.0" in result.output
    assert repo.marker() == "old"
