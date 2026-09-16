# Code by AkinoAlice@TyrantRey

"""The operator's commands (DESIGN/v0-5-0.md §11.1): ``status``, ``doctor``,
``plan``, ``pause``/``resume``, ``retry``, ``cancel``, ``rerun``,
``reload --yes``, and the file commands — through the daemon and offline."""

import json
import socket
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tag_file_system.addons.loader import MODULE_PREFIX
from tag_file_system.cli import app
from tag_file_system.config import Config, DaemonConfig
from tag_file_system.core.interface.action import RunStatus
from tag_file_system.root import FUNCTIONS_FILE, Root
from tag_file_system.services.daemon import Daemon

runner = CliRunner()

ADDON = """
    from tag_file_system import action

    @action.added()
    def run(path, metadata, ctx, suffix: str = ".bak"):
        if path.name.startswith("bad"):
            raise ValueError("no")
        return path.name

    @action.removed(on_move=True)
    def gone(path, metadata, ctx, suffix: str = ".bak"):
        return "gone"
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def clean_modules():
    yield
    for name in [m for m in sys.modules if m.startswith(MODULE_PREFIX)]:
        del sys.modules[name]


def tfs(*args: str):
    return runner.invoke(app, [str(a) for a in args])


def wait_for(condition, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


@pytest.fixture
def root(tmp_path: Path) -> Root:
    result = tfs("init", str(tmp_path / "vault"))
    assert result.exit_code == 0, result.output
    root = Root(tmp_path / "vault")
    Config(
        daemon=DaemonConfig(
            max_concurrent_runs=0, port=free_port(), stop_timeout_seconds=0.5
        )
    ).write(root.config_path)
    (root.script_dir / "copy.py").write_text(textwrap.dedent(ADDON), encoding="utf-8")
    (root.path / "copy").mkdir()
    (root.path / "copy" / "a--photo.txt").write_text("a")
    (root.path / "copy" / FUNCTIONS_FILE).write_text(
        "version: 1\nfunctions:\n  copy:\n    run: {}\n    gone: {}\n", encoding="utf-8"
    )
    return root


@pytest.fixture
def daemon(root: Root, tmp_path: Path):
    d = Daemon(root, control=True, poll_ms=50, ui_dir=tmp_path / "no-dist")
    d.startup()
    thread = threading.Thread(target=d.run_forever, daemon=True)
    thread.start()
    yield d
    d.request_stop()
    thread.join(10)


# ------------------------------------------------------------------ status


def test_status_through_the_daemon_and_offline(root: Root, daemon: Daemon):
    result = tfs("status", "--root", str(root.path))
    assert result.exit_code == 0, result.output
    assert f"daemon pid {daemon.status()['pid']}" in result.output
    assert "running" in result.output and "queue     0 waiting" in result.output
    assert "files     1 indexed" in result.output
    assert "confirm_above 500" in result.output

    as_json = tfs("status", "--root", str(root.path), "--json")
    payload = json.loads(as_json.output)
    assert payload["paused"] is False and payload["files"] == 1

    tfs("pause", "--root", str(root.path))
    assert "paused" in tfs("status", "--root", str(root.path)).output
    tfs("resume", "--root", str(root.path))


def test_status_offline_reports_the_lock_and_the_counts(root: Root):
    result = tfs("status", "--root", str(root.path))
    assert result.exit_code == 0, result.output
    assert "no daemon running" in result.output
    assert "lock      none" in result.output
    assert "files     0 indexed" in result.output and "schema    3" in result.output
    payload = json.loads(tfs("status", "--root", str(root.path), "--json").output)
    assert payload["source"] == "disk" and payload["database"]["schema"] == 3


# ------------------------------------------------------------------ doctor


def test_doctor_through_the_daemon(root: Root, daemon: Daemon):
    result = tfs("doctor", "--root", str(root.path))
    assert result.exit_code == 0, result.output
    lines = {
        line.split()[1]: line.split()[0] for line in result.output.splitlines()[:-1]
    }
    assert lines["layout"] == "ok" and lines["daemon"] == "ok"
    assert lines["database"] == "ok" and lines["lock"] == "ok"
    assert lines["scripts"] == "ok" and lines["functions"] == "ok"
    assert lines["ui"] == "warn"  # no dist in the fixture
    assert "warning(s)" in result.output.splitlines()[-1]


def test_doctor_offline_flags_what_is_wrong(root: Root):
    (root.script_dir / "Bad.py").write_text("x = 1\n")
    (root.path / FUNCTIONS_FILE).write_text(
        "version: 1\nfunctions:\n  nope:\n    run: {}\n", encoding="utf-8"
    )
    result = tfs("doctor", "--root", str(root.path))
    assert result.exit_code == 0, result.output
    assert "warn  daemon     not running" in result.output
    assert "warn  scripts" in result.output and "addon.filename" in result.output
    assert "warn  functions" in result.output and "functions.unbound" in result.output
    assert "ok    database   schema 3" in result.output

    root.config_path.write_text("[daemon]\nport = 'x'\n", encoding="utf-8")
    broken = tfs("doctor", "--root", str(root.path), "--json")
    assert broken.exit_code == 1
    payload = json.loads(broken.output)
    assert payload["ok"] is False
    assert {c["check"]: c["status"] for c in payload["checks"]}["config"] == "fail"


# -------------------------------------------------------------------- plan


def test_plan_through_the_daemon(root: Root, daemon: Daemon):
    (root.path / "copy" / FUNCTIONS_FILE).write_text(
        "version: 1\nfunctions:\n  copy:\n    run: {suffix: .new}\n", encoding="utf-8"
    )
    result = tfs("plan", "--root", str(root.path))
    assert result.exit_code == 0, result.output
    assert "against what the daemon loaded" in result.output
    assert "copy/.tfsfunctions.yaml" in result.output and "changed" in result.output
    assert '+copy.run(suffix=".new")' in result.output
    assert "would run 1 handler(s) on 1 file(s); 0 already ran" in result.output
    assert "arguments changed from copy.run()" in result.output
    assert "copy.gone() leaves: entry removed" in result.output

    one = tfs("plan", "copy/a--photo.txt", "--root", str(root.path), "--json")
    payload = json.loads(one.output)
    assert payload["scope"] == {"path": "copy/a--photo.txt", "prefix": None}
    under = json.loads(tfs("plan", "copy", "--root", str(root.path), "--json").output)
    assert under["scope"] == {"path": None, "prefix": "copy"}
    assert tfs("plan", "../x", "--root", str(root.path)).exit_code == 2
    assert tfs("plan", FUNCTIONS_FILE, "--root", str(root.path)).exit_code == 2


def test_plan_offline_says_what_the_next_start_runs(root: Root):
    result = tfs("plan", "--root", str(root.path))
    assert result.exit_code == 0, result.output
    assert "no daemon running" in result.output
    assert "would run 0 handler(s) on 0 file(s)" in result.output  # nothing indexed yet
    payload = json.loads(tfs("plan", "--root", str(root.path), "--json").output)
    assert payload["source"] == "disk" and payload["compared"] is False


# ---------------------------------------------------------------- reload


def test_reload_refuses_above_the_threshold(root: Root, daemon: Daemon):
    config = root.load_config()
    config.daemon.confirm_above = 1
    config.write(root.config_path)
    (root.path / "copy" / "b--photo.txt").write_text("b")
    (root.path / "copy" / "c--photo.txt").write_text("c")
    assert wait_for(lambda: daemon.backend.count_files() == 3)
    (root.path / "copy" / FUNCTIONS_FILE).write_text(
        "version: 1\nfunctions:\n  copy:\n    run: {suffix: .new}\n", encoding="utf-8"
    )

    refused = tfs("reload", "--root", str(root.path))
    assert refused.exit_code == 1
    assert (
        "reload refused: it would start 3 run(s), more than confirm_above = 1"
        in refused.output
    )
    assert daemon.functions.entries("copy")[0].args == {}

    accepted = tfs("reload", "--root", str(root.path), "--yes")
    assert accepted.exit_code == 0, accepted.output
    assert "functions: 2 file(s)" in accepted.output  # the root's skeleton and copy/
    assert daemon.functions.entries("copy")[0].args == {"suffix": ".new"}


# ------------------------------------------------------------ work control


def test_pause_resume_retry_cancel_and_rerun(root: Root, daemon: Daemon):
    assert tfs("pause", "--root", str(root.path)).output.startswith("paused: 0 item(s)")
    (root.path / "copy" / "bad--photo.txt").write_text("bad")
    assert wait_for(
        lambda: daemon.backend.query_file("copy/bad--photo.txt") is not None
    )
    assert daemon.store.count_runs(file_path="copy/bad--photo.txt") == 0  # queued
    resumed = tfs("resume", "--root", str(root.path))
    assert resumed.output.startswith("resumed: ") and "item(s) queued" in resumed.output
    assert wait_for(lambda: daemon.store.count_runs(status=RunStatus.FAILED) == 1)
    failed = daemon.store.query_runs(status=RunStatus.FAILED)[0]

    retried = tfs("retry", failed.id, "--root", str(root.path))
    assert retried.exit_code == 0, retried.output
    assert f"queued a retry of {failed.id}" in retried.output
    assert wait_for(lambda: daemon.store.count_runs(status=RunStatus.FAILED) == 2)
    assert tfs("retry", "nope", "--root", str(root.path)).exit_code == 1
    ok = daemon.store.query_runs(status=RunStatus.OK)[0]
    not_failed = tfs("retry", ok.id, "--root", str(root.path))
    assert (
        not_failed.exit_code == 1
        and "only a failed or interrupted run" in not_failed.output
    )

    cancelled = tfs("cancel", ok.id, "--root", str(root.path))
    assert cancelled.exit_code == 1 and "not in flight" in cancelled.output
    assert tfs("cancel", "nope", "--root", str(root.path)).exit_code == 1

    dry = tfs("rerun", "--handler", "copy.run", "--dry-run", "--root", str(root.path))
    assert dry.exit_code == 0, dry.output
    assert "would rerun 2 run(s) of copy.run on 2 file(s)" in dry.output
    only_failed = tfs(
        "rerun", "--handler", "copy.run", "--failed", "--root", str(root.path)
    )
    assert (
        "queued 1 run(s) of copy.run on 1 file(s); skipped: 1 not failed"
        in only_failed.output
    )
    assert wait_for(lambda: daemon.store.count_runs(status=RunStatus.FAILED) == 3)
    assert (
        tfs("rerun", "--handler", "nope.run", "--root", str(root.path)).exit_code == 1
    )


def test_work_control_needs_a_daemon(root: Root):
    for args in (
        ("pause",),
        ("resume",),
        ("retry", "x"),
        ("cancel", "x"),
        ("rerun", "--handler", "copy.run"),
    ):
        result = tfs(*args, "--root", str(root.path))
        assert result.exit_code == 1 and "is the daemon running" in result.output, args


# ------------------------------------------------------------ file commands


def test_file_commands_through_the_daemon(root: Root, daemon: Daemon):
    created = tfs(
        "touch", "copy/new--photo.txt", "--content", "hi", "--root", str(root.path)
    )
    assert created.exit_code == 0, created.output
    assert "created copy/new--photo.txt    tags: photo" in created.output
    assert "  runs copy.run()" in created.output
    assert (root.path / "copy" / "new--photo.txt").read_text() == "hi"
    assert daemon.backend.query_file("copy/new--photo.txt") is not None
    touched = tfs("touch", "copy/new--photo.txt", "--root", str(root.path))
    assert touched.output.startswith("touched copy/new--photo.txt")

    made = tfs("mkdir", "2024--trip", "--root", str(root.path))
    assert made.output == "created 2024--trip    tags: trip\n"

    copied = tfs("cp", "copy/new--photo.txt", "2024--trip/", "--root", str(root.path))
    assert copied.exit_code == 0, copied.output
    assert (
        "copied copy/new--photo.txt -> 2024--trip/new--photo.txt    tags: photo, trip"
        in copied.output
    )
    assert (
        tfs(
            "cp", "copy/new--photo.txt", "2024--trip/", "--root", str(root.path)
        ).exit_code
        == 1
    )

    moved = tfs(
        "mv", "copy/new--photo.txt", "2024--trip/moved.txt", "--root", str(root.path)
    )
    assert moved.exit_code == 0, moved.output
    assert (
        "moved copy/new--photo.txt -> 2024--trip/moved.txt    tags: trip"
        in moved.output
    )
    assert daemon.backend.query_file("copy/new--photo.txt") is None
    assert daemon.backend.query_file("2024--trip/moved.txt") is not None
    gone = [r for r in daemon.store.query_runs(handler="gone")]
    assert (
        gone and gone[0].status is RunStatus.OK
    )  # removed(on_move) for the entry it left

    removed = tfs("rm", "2024--trip/moved.txt", "--root", str(root.path))
    assert removed.output == "removed 2024--trip/moved.txt\n"
    assert tfs("rm", "2024--trip", "--root", str(root.path)).exit_code == 1  # needs -r
    tree = tfs("rm", "-r", "2024--trip", "--root", str(root.path))
    assert tree.exit_code == 0 and "removed 2024--trip/new--photo.txt" in tree.output
    assert not (root.path / "2024--trip").exists()

    for bad in (".tfs/x", "script/x.py", FUNCTIONS_FILE, "../x", "."):
        assert tfs("touch", bad, "--root", str(root.path)).exit_code == 2, bad
    assert tfs("touch", "copy", "--root", str(root.path)).exit_code == 2  # a directory


def test_file_commands_offline_act_on_disk(root: Root):
    created = tfs("touch", "copy/x--photo.txt", "--root", str(root.path))
    assert created.exit_code == 0 and "created copy/x--photo.txt" in created.output
    assert "no daemon running" in created.output
    assert (root.path / "copy" / "x--photo.txt").exists()
    assert tfs("mkdir", "2024--trip", "--root", str(root.path)).output.startswith(
        "created 2024--trip    tags: trip"
    )
    assert (
        tfs(
            "cp", "copy/x--photo.txt", "2024--trip/", "--root", str(root.path)
        ).exit_code
        == 0
    )
    assert (
        tfs(
            "mv", "copy/x--photo.txt", "2024--trip/y.txt", "--root", str(root.path)
        ).exit_code
        == 0
    )
    assert not (root.path / "copy" / "x--photo.txt").exists()
    assert (root.path / "2024--trip" / "y.txt").exists()
    assert tfs("rm", "2024--trip/y.txt", "--root", str(root.path)).exit_code == 0
    assert tfs("rm", "2024--trip", "--root", str(root.path)).exit_code == 2
    assert tfs("rm", "-r", "2024--trip", "--root", str(root.path)).exit_code == 0
    assert not (root.path / "2024--trip").exists()
    assert tfs("mv", "copy/nope.txt", "copy/", "--root", str(root.path)).exit_code == 1


# ------------------------------------------------------------------- tags


def test_tag_and_untag_through_the_daemon(root: Root, daemon: Daemon):
    result = tfs("tag", "copy/a--photo.txt", "hot", "Raw", "--root", str(root.path))
    assert result.exit_code == 0, result.output
    assert "copy/a--photo.txt    tags: hot, photo, raw" in result.output
    assert "added: hot, raw" in result.output

    def tags_now() -> list[str]:
        row = daemon.backend.query_file("copy/a--photo.txt")
        return sorted(t.name for t in row.tags) if row is not None else []

    assert wait_for(lambda: tags_now() == ["hot", "photo", "raw"])
    result = tfs("untag", "copy/a--photo.txt", "hot", "photo", "--root", str(root.path))
    assert result.exit_code == 0, result.output
    assert "removed: hot" in result.output
    assert "kept: photo (spelled by the name" in result.output
    assert tfs("tag", "copy/nope.txt", "x", "--root", str(root.path)).exit_code == 1
    assert (
        tfs("tag", "copy/a--photo.txt", "a:b", "--root", str(root.path)).exit_code == 1
    )
    assert tfs("tag", ".tfs/x", "x", "--root", str(root.path)).exit_code == 2


def test_tag_needs_a_daemon(root: Root):
    result = tfs("tag", "copy/a--photo.txt", "x", "--root", str(root.path))
    assert result.exit_code == 1 and "is the daemon running" in result.output
