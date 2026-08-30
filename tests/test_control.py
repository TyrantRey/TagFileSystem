# Code by AkinoAlice@TyrantRey

import json
import socket
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tag_file_system.addons.loader import MODULE_PREFIX
from tag_file_system.config import Config, DaemonConfig
from tag_file_system.root import Root
from tag_file_system.services.control import ControlClient, ControlError, ControlUnavailable
from tag_file_system.services.daemon import Daemon

ADDON = """
    from tag_file_system import action

    @action.added()
    def run(path, metadata, ctx, suffix: str = ".bak"):
        return path.name
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
    root = Root.init(tmp_path / "vault")
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


@pytest.fixture
def client(root: Root, daemon: Daemon) -> ControlClient:
    config = root.load_config()
    return ControlClient(config.daemon.bind, config.daemon.port, root.read_token())


def test_health_actions_and_files(root: Root, daemon: Daemon, client: ControlClient):
    health = client.health()
    assert health["status"] == "ok" and health["started"] is True
    assert health["addons"] == ["copy"] and health["in_flight"] == []
    assert Path(health["root"]) == root.path

    (action,) = client.actions()
    assert action["name"] == "copy" and action["hooks"] == ["added"]
    assert action["signature"]["added"]["properties"]["suffix"]["default"] == ".bak"

    files = client.files(tags=["photo"])
    assert [f["path"] for f in files] == ["@@copy/a--photo.txt"]
    assert files[0]["tags"] == ["photo"] and "runs" not in files[0]
    with_runs = client.files(prefix="@@copy", runs=True)
    assert with_runs[0]["runs"][0]["status"] == "ok"
    assert client.files(tags=["nope"]) == []
    assert client.files(name="A--", format=".txt")[0]["path"] == "@@copy/a--photo.txt"


def test_reload_and_stop(root: Root, daemon: Daemon, client: ControlClient):
    (root.script_dir / "extra.py").write_text(textwrap.dedent(ADDON), encoding="utf-8")
    Config(daemon=root.load_config().daemon, remotes={"x": "/x"}).write(root.config_path)

    result = client.reload()

    assert result == {"config": "reloaded", "addons": ["copy", "extra"]}
    assert daemon.config.remotes == {"x": "/x"}

    root.config_path.write_text("[daemon]\nport = 'oops'\n")
    assert client.reload()["config"] == "kept"
    assert any(p.kind == "config.invalid" for p in daemon.store.query_problems())

    assert client.stop() == {"stopping": True}
    deadline = time.time() + 10
    while daemon.backend.is_open and time.time() < deadline:
        time.sleep(0.05)
    assert not daemon.backend.is_open
    with pytest.raises(ControlUnavailable):
        client.health()


def test_token_and_routing(root: Root, daemon: Daemon, client: ControlClient):
    config = root.load_config()
    base = f"http://127.0.0.1:{config.daemon.port}"

    def call(path: str, method: str = "GET", token: str | None = None) -> tuple[int, dict]:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        request = urllib.request.Request(base + path, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    assert call("/health")[0] == 401
    assert call("/health", token="wrong")[0] == 401
    assert call("/health", token=root.read_token())[0] == 200
    assert call("/nope", token=root.read_token())[0] == 404
    assert call("/stop", "GET", token=root.read_token())[0] == 405
    assert call("/files?deleted=1&runs=1", token=root.read_token())[0] == 200
    assert daemon.backend.is_open  # GET /stop did nothing

    bad = ControlClient("127.0.0.1", config.daemon.port, "wrong")
    with pytest.raises(ControlError) as exc:
        bad.actions()
    assert exc.value.status == 401


def test_unavailable_when_no_daemon(root: Root):
    config = root.load_config()
    with pytest.raises(ControlUnavailable):
        ControlClient(config.daemon.bind, config.daemon.port, root.read_token(), timeout=1).health()
