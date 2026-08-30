# Code by AkinoAlice@TyrantRey

import json
import socket
import subprocess
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
from tag_file_system.root import Lock, Root
from tag_file_system.services.daemon import Daemon

runner = CliRunner()

ADDON = """
    from tag_file_system import action

    @action.added()
    def run(path, metadata, ctx, suffix: str = ".bak", width: int = 1):
        return path.name

    @action.err()
    def notify(problem, ctx):
        pass
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


@pytest.fixture
def root(tmp_path: Path) -> Root:
    result = runner.invoke(app, ["init", str(tmp_path / "vault")])
    assert result.exit_code == 0, result.output
    root = Root(tmp_path / "vault")
    Config(daemon=DaemonConfig(port=free_port(), stop_timeout_seconds=0.5)).write(root.config_path)
    (root.script_dir / "copy.py").write_text(textwrap.dedent(ADDON), encoding="utf-8")
    (root.path / "@@copy" / "a--photo.txt").parent.mkdir()
    (root.path / "@@copy" / "a--photo.txt").write_text("a")
    return root


@pytest.fixture
def daemon(root: Root):
    d = Daemon(root, control=True, poll_ms=50)
    d.startup()
    thread = threading.Thread(target=d.run_forever, daemon=True)
    thread.start()
    yield d
    d.request_stop()
    thread.join(10)


def tfs(*args: str, cwd: Path | None = None):
    return runner.invoke(app, [str(a) for a in args])


# ------------------------------------------------------------------- init


def test_init_creates_a_root_and_refuses_twice(tmp_path: Path):
    result = tfs("init", str(tmp_path / "v"))
    assert result.exit_code == 0
    assert "Initialized TagFileSystem root" in result.output
    assert (tmp_path / "v" / ".tfs" / "db" / "system.db").exists()

    again = tfs("init", str(tmp_path / "v" / "inner"))
    assert again.exit_code == 1
    assert "already a TagFileSystem root" in again.output


def test_commands_need_a_root(tmp_path: Path):
    result = tfs("list", "--root", str(tmp_path))
    assert result.exit_code == 1
    assert "tfs init" in result.output


# ------------------------------------------------------------------- list


def test_list_without_a_daemon_loads_scripts_directly(root: Root):
    (root.script_dir / "Bad.py").write_text("x = 1\n")

    result = tfs("list", "--root", str(root.path))

    assert result.exit_code == 0, result.output
    assert "no daemon running" in result.output
    assert "copy" in result.output and "added" in result.output and "on err" in result.output
    assert "suffix: string = '.bak'" in result.output and "width: integer = 1" in result.output
    assert "[warn] addon.filename" in result.output

    as_json = json.loads(tfs("list", "--root", str(root.path), "--json").output)
    assert as_json["actions"][0]["name"] == "copy"
    assert as_json["problems"][0][1] == "addon.filename"


def test_list_and_query_through_the_daemon(root: Root, daemon: Daemon):
    listed = tfs("list", "--root", str(root.path))
    assert listed.exit_code == 0, listed.output
    assert "add-ons from daemon" in listed.output and "copy" in listed.output

    found = tfs("query", "--root", str(root.path), "-t", "photo")
    assert found.exit_code == 0, found.output
    assert "@@copy/a--photo.txt" in found.output and "tags: photo" in found.output

    nothing = tfs("query", "--root", str(root.path), "-t", "nope")
    assert "(no files)" in nothing.output

    with_runs = json.loads(tfs("query", "--root", str(root.path), "--under", "@@copy", "--runs", "--json").output)
    assert with_runs[0]["runs"][0]["action_name"] == "copy"

    reloaded = tfs("reload", "--root", str(root.path))
    assert reloaded.exit_code == 0 and "config reloaded" in reloaded.output


def test_query_falls_back_to_the_database(root: Root):
    d = Daemon(root)
    d.startup()
    d.shutdown()

    result = tfs("query", "--root", str(root.path), "-t", "photo", "--runs")

    assert result.exit_code == 0, result.output
    assert "@@copy/a--photo.txt" in result.output
    assert "copy added ok" in result.output


def test_reload_and_stop_need_a_daemon(root: Root):
    reload = tfs("reload", "--root", str(root.path))
    assert reload.exit_code == 1 and "is the daemon running" in reload.output

    stop = tfs("stop", "--root", str(root.path))
    assert stop.exit_code == 1 and "no daemon is running" in stop.output


def test_stop_through_the_daemon(root: Root, daemon: Daemon):
    result = tfs("stop", "--root", str(root.path), "--timeout", "10")

    assert result.exit_code == 0, result.output
    assert "stopped" in result.output
    assert not root.lock_path.exists()


def test_start_refuses_a_live_lock(root: Root):
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        lock = Lock(root)
        lock.acquire()
        info = json.loads(root.lock_path.read_text())
        info["pid"] = sleeper.pid
        root.lock_path.write_text(json.dumps(info))

        result = tfs("start", "--root", str(root.path))

        assert result.exit_code == 1
        assert "locked by pid" in result.output and "--force" in result.output
    finally:
        sleeper.kill()
        sleeper.wait()


def test_start_foreground_runs_until_stopped(root: Root):
    config = root.load_config()
    from tag_file_system.services.control import ControlClient

    client = ControlClient(config.daemon.bind, config.daemon.port, root.read_token(), timeout=2)
    box: dict = {}

    def run() -> None:
        box["result"] = tfs("start", "--root", str(root.path))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if client.health()["started"]:
                break
        except Exception:
            time.sleep(0.1)
    else:
        pytest.fail("daemon did not come up")

    client.stop()
    thread.join(15)

    assert not thread.is_alive()
    assert box["result"].exit_code == 0, box["result"].output
    assert "watching" in box["result"].output and "stopped" in box["result"].output
    assert not root.lock_path.exists()
