# Code by AkinoAlice@TyrantRey

"""DESIGN/v0-2-0.md §10: on both platforms, fake a tag, run an upgrade, fail
the gate, and assert that code *and* database were restored.

The checkout is a real git repository with two release tags
(``tests/fakerepo.py``); ``uv``, ``pytest`` and ``tfs`` are
``tests/fake_tools.py``, whose fake daemon holds a real ``.tfs/lock`` and
answers ``/health`` and ``/stop`` for real. The orchestrator is the real
one: in-process for most cases, re-exec'd from a temporary copy on the
base interpreter (exactly as ``tfs upgrade`` does) for the success path.
"""

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tag_file_system import updater
from tag_file_system.database.action_store import ActionStore
from tag_file_system.database.migrations import SCHEMA_VERSION
from tag_file_system.database.sqlite import SQLiteBackend
from tag_file_system.root import Lock, Root
from tests.fakerepo import FakeRepo, make_repo

FAKE = str(Path(__file__).with_name("fake_tools.py"))


class Stage:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.repo: FakeRepo = make_repo(tmp_path / "git")
        self.root = Root.init(tmp_path / "vault")
        backend = SQLiteBackend()
        backend.init_database(self.root.db_path, root_dir=self.root.path)
        backend.close()
        self.log = tmp_path / "calls.jsonl"
        self.scenario_path = tmp_path / "scenario.json"
        self.scenario: dict = {
            "repo": str(self.repo.path),
            "log": str(self.log),
            "start": {"old": "ok", "new": "ok"},
        }
        monkeypatch.setenv("TFS_FAKE_SCENARIO", str(self.scenario_path))
        self.save()
        self.commands = {
            name: [sys.executable, FAKE, name] for name in ("uv", "tfs", "pytest")
        }

    def save(self) -> None:
        self.scenario_path.write_text(json.dumps(self.scenario), encoding="utf-8")

    def set(self, **changes) -> None:
        self.scenario.update(changes)
        self.save()

    def start_old_daemon(self) -> int:
        result = subprocess.run(
            [*self.commands["tfs"], "--root", str(self.root.path), "start", "-d"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        holder = Lock(self.root).holder()
        assert holder is not None and not holder.upgrade
        return holder.pid

    def plan(self, **kwargs) -> updater.Plan:
        return updater.preflight(
            self.root.path,
            repo=self.repo.path,
            commands=self.commands,
            python=sys.executable,
            health_timeout=5.0,
            **kwargs,
        )

    def calls(self, kind: str) -> list[dict]:
        if not self.log.exists():
            return []
        entries = [
            json.loads(line)
            for line in self.log.read_text(encoding="utf-8").splitlines()
        ]
        return [e for e in entries if e["kind"] == kind]

    def starts(self) -> list[tuple[str, str]]:
        return [(c["marker"], c["behaviour"]) for c in self.calls("start")]

    def syncs(self) -> int:
        return len([c for c in self.calls("uv") if c["args"][:1] == ["sync"]])

    def db_version(self) -> int:
        return updater.read_user_version(self.root.db_path)

    def upgrades(self) -> list:
        backend = SQLiteBackend()
        backend.init_database(self.root.db_path, root_dir=self.root.path)
        try:
            return ActionStore(backend).query_upgrades()
        finally:
            backend.close()

    def daemon_health(self) -> dict:
        holder = Lock(self.root).holder()
        assert holder is not None and holder.port, "no daemon holds the root"
        return updater.control_call(
            "127.0.0.1", holder.port, self.root.read_token(), "GET", "/health"
        )

    def cleanup(self) -> None:
        info = updater.read_lock(self.root.path)
        if info is None or info["upgrade"] or not updater.pid_alive(info["pid"]):
            return
        if info["port"]:
            try:
                updater.control_call(
                    "127.0.0.1", info["port"], "", "POST", "/stop", timeout=2.0
                )
            except updater.UpdateError:
                pass
        deadline = time.time() + 5
        while updater.pid_alive(info["pid"]) and time.time() < deadline:
            time.sleep(0.1)
        if updater.pid_alive(info["pid"]):
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(info["pid"]), "/F"], capture_output=True
                )
            else:
                import signal

                os.kill(info["pid"], getattr(signal, "SIGKILL", signal.SIGTERM))


@pytest.fixture
def stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    stage = Stage(tmp_path, monkeypatch)
    yield stage
    stage.cleanup()


def test_failed_start_restores_code_and_database(
    stage: Stage, capsys: pytest.CaptureFixture
):
    stage.set(start={"old": "ok", "new": "die"}, die_user_version=99)
    old_pid = stage.start_old_daemon()
    assert stage.db_version() == SCHEMA_VERSION
    plan = stage.plan()
    assert [r.pid for r in plan.running] == [old_pid]

    code = updater.run_plan(plan)

    out = capsys.readouterr().out
    assert code == 1, out
    assert stage.repo.marker() == "old"
    assert stage.repo.head() == stage.repo.tags["v0.9.0"]
    assert (stage.repo.path / "synced-at.txt").read_text(encoding="utf-8") == "old"
    assert stage.db_version() == SCHEMA_VERSION  # restored from the snapshot
    assert stage.upgrades() == []
    assert [b.label for b in updater.list_backups(stage.root.path)] == ["v0.9.0"]
    holder = Lock(stage.root).holder()
    assert holder is not None and not holder.upgrade and holder.pid != old_pid
    assert stage.daemon_health()["hash"] == stage.repo.tags["v0.9.0"]
    assert stage.starts() == [("old", "ok"), ("new", "die"), ("old", "ok")]
    assert stage.syncs() == 2
    assert "restored" in out and "reverted (code and every database)" in out


def test_wrong_hash_fails_and_reverts(stage: Stage, capsys: pytest.CaptureFixture):
    stage.set(start={"old": "ok", "new": "wrong-hash"})
    stage.start_old_daemon()

    code = updater.run_plan(stage.plan())

    out = capsys.readouterr().out
    assert code == 1 and "reports commit 0000000" in out, out
    assert stage.repo.marker() == "old"
    assert stage.daemon_health()["hash"] == stage.repo.tags["v0.9.0"]
    assert stage.starts() == [("old", "ok"), ("new", "wrong-hash"), ("old", "ok")]
    assert stage.upgrades() == []


def test_failed_tests_revert_the_code_only(stage: Stage, capsys: pytest.CaptureFixture):
    stage.set(pytest_exit=1, pytest_summary="1 failed, 2 passed in 0.1s")
    stage.start_old_daemon()

    code = updater.run_plan(stage.plan())

    out = capsys.readouterr().out
    assert code == 1 and "test suite failed" in out, out
    assert "restored" not in out and "reverted;" in out
    assert stage.repo.marker() == "old"
    assert stage.starts() == [("old", "ok"), ("old", "ok")]
    assert stage.syncs() == 2
    assert stage.db_version() == SCHEMA_VERSION
    assert len(updater.list_backups(stage.root.path)) == 1
    assert stage.daemon_health()["hash"] == stage.repo.tags["v0.9.0"]


def test_failed_sync_reverts_the_code(stage: Stage, capsys: pytest.CaptureFixture):
    stage.set(sync_fail_calls=[1])
    stage.start_old_daemon()

    code = updater.run_plan(stage.plan())

    out = capsys.readouterr().out
    assert code == 1 and "uv sync failed" in out, out
    assert stage.repo.marker() == "old"
    assert (stage.repo.path / "synced-at.txt").read_text(encoding="utf-8") == "old"
    assert [c for c in stage.calls("pytest") if "-q" in c["args"]] == []  # never ran
    assert stage.starts() == [("old", "ok"), ("old", "ok")]


def test_a_failed_revert_stops_and_prints_the_manual_steps(
    stage: Stage, capsys: pytest.CaptureFixture
):
    stage.set(start={"old": "ok", "new": "die"}, sync_fail_calls=[2])
    stage.start_old_daemon()

    code = updater.run_plan(stage.plan())

    out = capsys.readouterr().out
    assert code == 2, out
    assert "could not be reverted automatically" in out
    assert "git checkout refs/tags/v0.9.0" in out and "uv sync --locked" in out
    snapshot = updater.list_backups(stage.root.path)[0].path
    assert str(snapshot) in out and str(stage.root.db_path) in out
    assert stage.db_version() == SCHEMA_VERSION  # the database was restored first
    assert stage.repo.marker() == "old"  # the checkout succeeded, the sync did not
    assert Lock(stage.root).holder() is None  # marker gone, nothing restarted
    assert stage.starts() == [("old", "ok"), ("new", "die")]


def test_successful_upgrade_records_restarts_and_prunes(stage: Stage):
    for hour in (1, 2, 3):
        updater.snapshot(
            stage.root.path, f"v0.0.{hour}", now=datetime(2020, 1, 1, hour, tzinfo=UTC)
        )
    old_pid = stage.start_old_daemon()
    plan = stage.plan()

    code = updater.hand_off(plan)  # the real re-exec, on the base interpreter

    assert code == 0
    assert stage.repo.marker() == "new"
    assert stage.repo.head() == stage.repo.tags["v1.0.0"]
    assert (stage.repo.path / "synced-at.txt").read_text(encoding="utf-8") == "new"
    holder = Lock(stage.root).holder()
    assert holder is not None and not holder.upgrade and holder.pid != old_pid
    health = stage.daemon_health()
    assert health["hash"] == stage.repo.tags["v1.0.0"] and health["version"] == "1.0.0"
    assert stage.starts() == [("old", "ok"), ("new", "ok")]
    assert stage.syncs() == 1 and len(stage.calls("pytest")) == 2  # --version, -q

    (record,) = stage.upgrades()
    assert (record.from_tag, record.from_hash) == ("v0.9.0", stage.repo.tags["v0.9.0"])
    assert (record.to_tag, record.to_hash) == ("v1.0.0", stage.repo.tags["v1.0.0"])
    assert (record.schema_before, record.schema_after) == (
        SCHEMA_VERSION,
        SCHEMA_VERSION,
    )
    assert (record.tests_run, record.tests_passed, record.tests_skipped) == (4, 3, 1)
    assert record.snapshot_path is not None and Path(record.snapshot_path).exists()
    assert record.outcome == "ok"
    assert record.started_at.timestamp() == pytest.approx(plan.started_at, abs=1)

    labels = [b.label for b in updater.list_backups(stage.root.path)]
    assert labels == ["v0.9.0", "v0.0.3", "v0.0.2"]  # retention keeps the newest 3
    assert updater.known_roots() == [stage.root.path]


def test_a_root_without_a_daemon_is_snapshotted_and_recorded(
    stage: Stage, capsys: pytest.CaptureFixture
):
    stage.set(skip=True)
    plan = stage.plan(skip_tests=True)
    assert plan.running == [] and plan.skip_tests

    code = updater.run_plan(plan)

    assert code == 0, capsys.readouterr().out
    assert stage.repo.marker() == "new"
    assert stage.starts() == [] and stage.calls("pytest") == []
    (record,) = stage.upgrades()
    assert record.tests_run is None and record.tests_passed is None
    assert Lock(stage.root).holder() is None  # the marker is gone
    assert len(updater.list_backups(stage.root.path)) == 1


def test_in_flight_runs_block_the_preflight(stage: Stage):
    stage.set(in_flight=["run-1"])
    stage.start_old_daemon()
    with pytest.raises(updater.UpdateError, match="runs are in flight"):
        stage.plan(wait=0)
    assert stage.repo.marker() == "old"
    assert stage.starts() == [("old", "ok")]


def test_nothing_to_do_at_the_latest_tag(stage: Stage):
    stage.repo.checkout("v1.0.0")
    with pytest.raises(updater.NothingToDo):
        stage.plan()
