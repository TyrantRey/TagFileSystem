# Code by AkinoAlice@TyrantRey

"""The standard-library updater (DESIGN/v0-2-0.md): the entry-point shim,
the registry, snapshots, the upgrade marker, git and tag parsing, and the
preflight against a fake checkout. The upgrade sequence itself is
``test_upgrade.py``."""

import ast
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tag_file_system import __main__ as entry
from tag_file_system import updater, version
from tag_file_system.config import Config, DaemonConfig
from tag_file_system.database.migrations import SCHEMA_VERSION
from tag_file_system.database.sqlite import SQLiteBackend
from tag_file_system.root import Lock, LockHeld, Root, Zone
from tests.fakerepo import FakeRepo, git, make_repo


@pytest.fixture
def root(tmp_path: Path) -> Root:
    root = Root.init(tmp_path / "vault")
    backend = SQLiteBackend()
    backend.init_database(root.db_path, root_dir=root.path)
    backend.close()
    return root


@pytest.fixture
def repo(tmp_path: Path) -> FakeRepo:
    return make_repo(tmp_path / "git")


def marker(pid: int, hostname: str | None = None, age: float = 0.0) -> str:
    return json.dumps(
        {
            "pid": pid,
            "hostname": hostname or socket.gethostname(),
            "created_at": time.time() - age,
            "upgrade": True,
        }
    )


# ------------------------------------------------------------ stdlib only


def test_updater_imports_only_the_standard_library():
    tree = ast.parse(Path(updater.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    assert imported
    assert imported <= sys.stdlib_module_names, imported - sys.stdlib_module_names


# ------------------------------------------------------------ entry point


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["--root", "x", "upgrade", "--to", "v1"], ("upgrade", 2)),
        (["-r", "x", "list"], ("list", 2)),
        (["--root=x", "upgrade"], ("upgrade", 1)),
        (["upgrade", "--root", "x"], ("upgrade", 0)),
        (["--version"], (None, -1)),
        ([], (None, -1)),
    ],
)
def test_entry_point_finds_the_subcommand(argv: list[str], expected: tuple):
    assert entry.subcommand(argv) == expected


def test_entry_point_routes_upgrade_to_the_updater(monkeypatch: pytest.MonkeyPatch):
    seen: dict = {}

    def fake_main(argv: list[str]) -> int:
        seen["argv"] = argv
        return 7

    monkeypatch.setattr(updater, "main", fake_main)
    with pytest.raises(SystemExit) as exited:
        entry.main(["--root", "x", "upgrade", "--dry-run"])
    assert exited.value.code == 7
    assert seen["argv"] == ["--root", "x", "--dry-run"]


def test_entry_point_routes_everything_else_to_typer(capsys: pytest.CaptureFixture):
    with pytest.raises(SystemExit) as exited:
        entry.main(["--version"])
    assert exited.value.code == 0
    assert version.describe() in capsys.readouterr().out


def test_upgrade_never_imports_pydantic_or_typer():
    """The process that waits for the orchestrator must hold no compiled
    dependency (``uv sync`` on Windows cannot replace a mapped .pyd)."""
    code = (
        "import sys, tag_file_system.__main__ as m\n"
        "try:\n"
        "    m.main(['upgrade', '--help'])\n"
        "except SystemExit:\n"
        "    pass\n"
        "print(sorted(n for n in ('pydantic', 'typer', 'pydantic_core', 'watchfiles') if n in sys.modules))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert "usage: tfs upgrade" in result.stdout
    assert result.stdout.strip().splitlines()[-1] == "[]"


# --------------------------------------------------------------- registry


def test_registry_path_honours_the_override_and_never_uses_dot_tfs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv(updater.REGISTRY_ENV, str(tmp_path / "r.json"))
    assert updater.registry_path() == tmp_path / "r.json"
    monkeypatch.delenv(updater.REGISTRY_ENV)
    default = updater.registry_path()
    assert default.name == "roots.json" and default.parent.name == "tfs"
    assert ".tfs" not in default.parts
    if os.name == "nt":
        monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
        assert updater.registry_path() == tmp_path / "appdata" / "tfs" / "roots.json"
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert updater.registry_path() == tmp_path / "xdg" / "tfs" / "roots.json"


def test_registry_remembers_roots_and_prunes_dead_ones(root: Root, tmp_path: Path):
    other = Root.init(tmp_path / "other")
    updater.register_root(root.path)
    updater.register_root(root.path)
    updater.register_root(other.path)
    assert updater.known_roots() == [root.path, other.path]

    shutil.rmtree(other.path)
    assert updater.known_roots() == [root.path]
    on_disk = json.loads(updater.registry_path().read_text(encoding="utf-8"))
    assert on_disk["roots"] == [str(root.path)]


def test_registry_survives_garbage_and_an_unwritable_location(
    root: Root, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    path = updater.registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert updater.known_roots() == []
    updater.register_root(root.path)
    assert updater.known_roots() == [root.path]

    (tmp_path / "in-the-way").write_text("x", encoding="utf-8")
    monkeypatch.setenv(
        updater.REGISTRY_ENV, str(tmp_path / "in-the-way" / "roots.json")
    )
    updater.register_root(root.path)  # best effort: no exception
    assert updater.known_roots() == []


# ---------------------------------------------------------------- backups


def test_snapshot_is_a_verified_copy_named_after_its_source(root: Root):
    when = datetime(2026, 9, 2, 4, 30, 0, tzinfo=UTC)
    path = updater.snapshot(root.path, "v0.1.1", now=when)
    assert path == root.tfs_dir / "backups" / "20260902T043000Z-v0.1.1.db"
    assert updater.read_user_version(path) == SCHEMA_VERSION
    assert root.zone(path) is Zone.TFS  # the watcher ignores it

    again = updater.snapshot(root.path, "v0.1.1", now=when)
    assert again.name == "20260902T043000Z-v0.1.1-2.db"
    weird = updater.snapshot(root.path, "master/ab c", now=when)
    assert weird.name == "20260902T043000Z-master_ab_c.db"


def test_snapshot_needs_a_database(root: Root):
    root.db_path.unlink()
    with pytest.raises(updater.UpdateError, match="does not exist"):
        updater.snapshot(root.path, "v0")


def test_list_and_prune_keep_the_newest(root: Root):
    for hour in (1, 2, 3, 4):
        updater.snapshot(
            root.path, f"v0.0.{hour}", now=datetime(2026, 1, 1, hour, tzinfo=UTC)
        )
    listed = updater.list_backups(root.path)
    assert [b.label for b in listed] == ["v0.0.4", "v0.0.3", "v0.0.2", "v0.0.1"]
    assert listed[0].created == datetime(2026, 1, 1, 4, tzinfo=UTC)
    assert listed[0].size > 0

    dry = updater.prune_backups(root.path, keep=2, dry_run=True)
    assert [b.label for b in dry] == ["v0.0.2", "v0.0.1"]
    assert len(updater.list_backups(root.path)) == 4
    removed = updater.prune_backups(root.path, keep=2)
    assert [b.label for b in removed] == ["v0.0.2", "v0.0.1"]
    assert [b.label for b in updater.list_backups(root.path)] == ["v0.0.4", "v0.0.3"]
    assert updater.prune_backups(root.path, keep=0)
    assert updater.list_backups(root.path) == []
    assert updater.list_backups(root.path / "nowhere") == []
    with pytest.raises(ValueError):
        updater.prune_backups(root.path, keep=-1)


def test_restore_puts_the_snapshot_back(root: Root):
    snapshot = updater.snapshot(root.path, "v0.1.1")
    connection = sqlite3.connect(root.db_path)
    connection.execute("PRAGMA user_version = 42")
    connection.commit()
    connection.close()
    assert updater.read_user_version(root.db_path) == 42
    stale_wal = root.db_path.with_name(root.db_path.name + "-wal")
    stale_wal.write_bytes(b"belongs to the discarded state")

    updater.restore_snapshot(root.path, snapshot)

    assert updater.read_user_version(root.db_path) == SCHEMA_VERSION
    assert not stale_wal.exists()
    assert snapshot.exists()  # the snapshot itself is kept


def test_restore_refuses_a_broken_snapshot(root: Root, tmp_path: Path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database at all")
    with pytest.raises(updater.UpdateError, match="not a usable database"):
        updater.restore_snapshot(root.path, bad)
    assert updater.read_user_version(root.db_path) == SCHEMA_VERSION


# ----------------------------------------------------------------- marker


def test_marker_is_a_live_lock_that_only_its_owner_removes(root: Root):
    updater.write_marker(root.path)
    holder = Lock(root).holder()
    assert holder is not None and holder.upgrade and holder.pid == os.getpid()
    assert updater.lock_state(root.path)[0] == "upgrade"

    assert updater.remove_marker(root.path) is True
    assert Lock(root).holder() is None
    assert updater.remove_marker(root.path) is False

    root.lock_path.write_text(marker(pid=4, hostname="elsewhere"), encoding="utf-8")
    assert updater.remove_marker(root.path) is False
    assert root.lock_path.exists()


def test_another_process_cannot_start_a_daemon_under_the_marker(root: Root):
    # The orchestrator is its own process; a `tfs start` racing it sees a
    # live lock it may not displace, --force included.
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        root.lock_path.write_text(marker(pid=sleeper.pid), encoding="utf-8")
        assert updater.lock_state(root.path)[0] == "upgrade"
        with pytest.raises(LockHeld, match="being upgraded"):
            Lock(root).acquire()
        with pytest.raises(LockHeld):
            Lock(root).acquire(force=True)
        assert updater.remove_marker(root.path) is False  # not ours
    finally:
        sleeper.kill()
        sleeper.wait()


def test_marker_replaces_the_daemon_lock_which_the_daemon_leaves_alone(root: Root):
    lock = Lock(root)  # this process plays the daemon
    lock.acquire(port=7411, bind="127.0.0.1")
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        root.lock_path.write_text(marker(pid=sleeper.pid), encoding="utf-8")
        lock.release()  # stopping: a daemon only removes the lock it wrote
        holder = Lock(root).holder()
        assert holder is not None and holder.upgrade and holder.pid == sleeper.pid
    finally:
        sleeper.kill()
        sleeper.wait()


def test_stale_marker_is_taken_over_with_a_warning(root: Root):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    root.lock_path.write_text(marker(pid=dead.pid), encoding="utf-8")
    assert Lock(root).holder() is None
    assert updater.lock_state(root.path)[0] == "stale"

    from tag_file_system.services.daemon import Daemon

    daemon = Daemon(root)
    try:
        daemon.startup()
        stale = [p for p in daemon.store.query_problems() if p.kind == "lock.stale"]
        assert stale and "upgrade marker" in stale[0].message
    finally:
        daemon.shutdown()


def test_inspect_root_reads_lock_config_and_schema(root: Root):
    Config(daemon=DaemonConfig(port=7999, stop_timeout_seconds=3)).write(
        root.config_path
    )
    state = updater.inspect_root(root.path)
    assert (state.running, state.port, state.stop_timeout) == (False, 7999, 3.0)
    assert (state.user_version, state.skipped, state.upgrading) == (
        SCHEMA_VERSION,
        None,
        None,
    )

    lock = Lock(root)
    lock.acquire(port=7412, bind="127.0.0.1")  # this process: a live daemon
    state = updater.inspect_root(root.path)
    assert state.running and state.pid == os.getpid() and state.port == 7412
    lock.release()

    root.lock_path.write_text(
        json.dumps({"pid": 1, "hostname": "nas", "created_at": time.time()})
    )
    state = updater.inspect_root(root.path)
    assert not state.running and state.skipped is not None and "nas" in state.skipped
    root.lock_path.write_text(
        json.dumps({"pid": 1, "hostname": "nas", "created_at": time.time() - 7 * 3600})
    )
    assert updater.inspect_root(root.path).skipped is None  # aged out

    root.lock_path.write_text(marker(pid=os.getpid()), encoding="utf-8")
    state = updater.inspect_root(root.path)
    assert state.upgrading == os.getpid() and state.skipped is not None
    root.lock_path.unlink()

    root.db_path.unlink()
    assert updater.inspect_root(root.path).user_version is None


def test_broken_config_falls_back_to_defaults(root: Root):
    root.config_path.write_text("[daemon]\nport = 'x'\n", encoding="utf-8")
    assert updater.daemon_config(root.path) == ("127.0.0.1", 7411, 30.0)
    root.config_path.write_text("not toml [", encoding="utf-8")
    assert updater.daemon_config(root.path) == ("127.0.0.1", 7411, 30.0)


# ---------------------------------------------------------------- parsing


def test_parse_helpers():
    assert updater.parse_version("v1.2.3") == (1, 2, 3)
    assert updater.parse_version("1.2.3") == (1, 2, 3)
    assert updater.parse_version("latest") is None
    assert updater.parse_version("v1.2") is None

    annotated, lightweight = updater.parse_remote_tags(
        "aaa\trefs/tags/v0.1.0\nbbb\trefs/tags/v0.1.0^{}\n"
        "ccc\trefs/tags/latest\nddd\trefs/heads/master\n"
    )
    assert annotated == {"v0.1.0": "bbb"} and lightweight == {"latest"}

    assert updater.schema_version_in("x = 1\nSCHEMA_VERSION = 2  # two\n") == 2
    assert updater.schema_version_in("SCHEMA_VERSION = compute()\n") is None
    assert updater.project_version_in('[project]\nversion = "0.2.0"\n') == "0.2.0"
    assert updater.project_version_in("nope [") is None

    summary = updater.parse_pytest_summary("....\n365 passed, 3 skipped in 60.12s\n")
    assert (summary.run, summary.passed, summary.skipped) == (368, 365, 3)
    summary = updater.parse_pytest_summary("1 failed, 2 passed, 1 error in 1s")
    assert (summary.run, summary.passed, summary.skipped) == (4, 2, 0)
    assert updater.parse_pytest_summary("no tests ran").run == 0

    assert updater.human_size(512) == "512 B"
    assert updater.human_size(2048) == "2.0 KB"
    when = datetime(2026, 9, 2, 4, 30, tzinfo=UTC)
    assert updater.snapshot_name("v0.1.1", when) == "20260902T043000Z-v0.1.1.db"


def test_head_state_prefers_a_release_tag(repo: FakeRepo):
    state = updater.head_state(repo.path)
    assert (state.tag, state.branch, state.dirty) == ("v0.9.0", None, False)
    assert state.ref == "refs/tags/v0.9.0"
    repo.checkout("master")  # `latest` and `docs-1` sit here too
    state = updater.head_state(repo.path)
    assert (state.tag, state.branch, state.ref) == ("v1.0.0", "master", "master")


# ------------------------------------------------------------------ check


def test_check_reports_the_checkout_and_origin(root: Root, repo: FakeRepo):
    report = updater.check(root.path, repo=repo.path)
    assert report.head.tag == "v0.9.0" and report.head.branch is None
    assert (report.current_version, report.current_schema) == ("0.9.0", 1)
    assert report.tags == ["v1.0.0", "v0.9.0"]  # `latest`, `docs-1`: not releases
    assert (report.target, report.to_hash) == ("v1.0.0", repo.tags["v1.0.0"])
    assert (report.to_version, report.to_schema) == ("1.0.0", 2)
    assert report.available is True
    assert [r.path for r in report.roots] == [str(root.path)]  # registered by the check

    as_dict = report.as_dict()
    assert as_dict["available"] and as_dict["latest"]["tag"] == "v1.0.0"
    assert as_dict["current"]["hash"] == repo.tags["v0.9.0"]
    assert as_dict["roots"][0]["user_version"] == SCHEMA_VERSION


def test_check_refuses_a_dirty_or_wandering_checkout(root: Root, repo: FakeRepo):
    (repo.path / "marker.txt").write_text("edited", encoding="utf-8")
    with pytest.raises(updater.UpdateError, match="uncommitted"):
        updater.check(root.path, repo=repo.path)
    git(repo.path, "checkout", "-q", "--", "marker.txt")

    git(repo.path, "checkout", "-q", "-b", "feature")
    with pytest.raises(updater.UpdateError, match="branch feature"):
        updater.check(root.path, repo=repo.path)

    git(repo.path, "checkout", "-q", "master")
    assert updater.check(root.path, repo=repo.path).head.branch == "master"

    git(repo.path, "commit", "-q", "--allow-empty", "-m", "wip")
    git(repo.path, "checkout", "-q", "--detach", "HEAD")
    with pytest.raises(updater.UpdateError, match="not at a release tag"):
        updater.check(root.path, repo=repo.path)


def test_check_needs_a_checkout_and_a_reachable_origin(
    root: Root, repo: FakeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    with pytest.raises(updater.UpdateError, match="no .git"):
        updater.check(root.path, repo=tmp_path / "nowhere")
    monkeypatch.setattr(updater, "repo_dir", lambda: None)
    with pytest.raises(updater.UpdateError, match="not a git checkout"):
        updater.check(root.path)
    git(repo.path, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    with pytest.raises(updater.UpdateError, match="origin is not reachable"):
        updater.check(root.path, repo=repo.path)


def test_check_to_a_named_tag(root: Root, repo: FakeRepo):
    assert updater.check(root.path, repo=repo.path, to="v0.9.0").available is False
    with pytest.raises(updater.UpdateError, match="lightweight"):
        updater.check(root.path, repo=repo.path, to="latest")
    with pytest.raises(updater.UpdateError, match="not a release tag"):
        updater.check(root.path, repo=repo.path, to="docs-1")
    with pytest.raises(updater.UpdateError, match="no tag"):
        updater.check(root.path, repo=repo.path, to="v9.9.9")


def test_check_is_not_available_when_master_is_ahead(root: Root, repo: FakeRepo):
    repo.checkout("master")
    git(repo.path, "commit", "-q", "--allow-empty", "-m", "wip")
    report = updater.check(root.path, repo=repo.path)
    assert report.target == "v1.0.0" and report.available is False


# -------------------------------------------------------------- preflight


def test_preflight_builds_the_plan(root: Root, repo: FakeRepo, tmp_path: Path):
    plan = updater.preflight(root.path, repo=repo.path, python=sys.executable)
    assert (plan.from_tag, plan.from_ref) == ("v0.9.0", "refs/tags/v0.9.0")
    assert plan.from_hash == repo.tags["v0.9.0"]
    assert (plan.to_tag, plan.to_hash) == ("v1.0.0", repo.tags["v1.0.0"])
    assert (plan.to_version, plan.to_schema) == ("1.0.0", 2)
    assert plan.snapshot_label == "v0.9.0"
    assert plan.schema_changes is False  # the root is at SCHEMA_VERSION == 2
    assert [r.path for r in plan.affected] == [str(root.path)] and plan.running == []
    assert plan.command("tfs")[0] == sys.executable and "-P" in plan.command("tfs")

    plan.dump(tmp_path / "plan.json")
    assert updater.Plan.load(tmp_path / "plan.json") == plan


def test_preflight_nothing_to_do_at_the_latest(root: Root, repo: FakeRepo):
    repo.checkout("v1.0.0")
    with pytest.raises(updater.NothingToDo, match="already at v1.0.0"):
        updater.preflight(root.path, repo=repo.path)
    repo.checkout("master")
    git(repo.path, "commit", "-q", "--allow-empty", "-m", "wip")
    with pytest.raises(updater.NothingToDo, match="not behind"):
        updater.preflight(root.path, repo=repo.path)


def test_preflight_refuses_a_schema_downgrade(root: Root, repo: FakeRepo):
    repo.checkout("v1.0.0")
    # The root's database is at SCHEMA_VERSION (2); v0.9.0 declares 1.
    with pytest.raises(updater.UpdateError, match="not downgrading"):
        updater.preflight(root.path, repo=repo.path, to="v0.9.0")


def test_preflight_refuses_while_another_upgrade_runs(root: Root, repo: FakeRepo):
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        root.lock_path.write_text(marker(pid=sleeper.pid), encoding="utf-8")
        with pytest.raises(updater.UpdateError, match="already in progress"):
            updater.preflight(root.path, repo=repo.path)
    finally:
        sleeper.kill()
        sleeper.wait()


def test_preflight_skips_a_foreign_root_and_needs_uv_and_pytest(
    root: Root, repo: FakeRepo, tmp_path: Path
):
    other = Root.init(tmp_path / "nas")
    updater.register_root(other.path)
    other.lock_path.write_text(
        json.dumps({"pid": 1, "hostname": "nas", "created_at": time.time()})
    )
    plan = updater.preflight(root.path, repo=repo.path)
    skipped = [r for r in plan.roots if r.skipped]
    assert [r.path for r in skipped] == [str(other.path)]
    assert skipped[0].skipped is not None and "nas" in skipped[0].skipped
    assert [r.path for r in plan.affected] == [str(root.path)]

    with pytest.raises(updater.UpdateError, match="not installed"):
        updater.preflight(
            root.path, repo=repo.path, commands={"uv": [str(tmp_path / "no-uv")]}
        )
    no_pytest = {"pytest": [sys.executable, "-c", "raise SystemExit(1)"]}
    with pytest.raises(updater.UpdateError, match="pytest"):
        updater.preflight(root.path, repo=repo.path, commands=no_pytest)
    plan = updater.preflight(
        root.path, repo=repo.path, commands=no_pytest, skip_tests=True
    )
    assert plan.skip_tests


# ------------------------------------------------------------------- main


def test_main_dry_run_prints_the_plan(
    root: Root,
    repo: FakeRepo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
):
    monkeypatch.setattr(updater, "repo_dir", lambda: repo.path)
    assert updater.main(["--root", str(root.path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "upgrade plan" in out and "v1.0.0 = 1.0.0" in out
    assert str(root.path) in out and "unchanged" in out
    assert repo.marker() == "old"

    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    assert updater.main(["--dry-run"]) == 1
    assert "not inside a TagFileSystem root" in capsys.readouterr().err

    repo.checkout("v1.0.0")
    assert updater.main(["--root", str(root.path), "--dry-run"]) == 0
    assert "already at v1.0.0" in capsys.readouterr().out


def test_main_needs_consent_for_a_schema_change(
    root: Root,
    repo: FakeRepo,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
):
    connection = sqlite3.connect(root.db_path)
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()
    monkeypatch.setattr(updater, "repo_dir", lambda: repo.path)

    assert updater.main(["--root", str(root.path)]) == 1  # stdin is not a tty
    captured = capsys.readouterr()
    assert "schema 1 -> 2" in captured.out and "--yes" in captured.err
    assert repo.marker() == "old"
    assert Lock(root).holder() is None
