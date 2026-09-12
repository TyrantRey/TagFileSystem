# Code by AkinoAlice@TyrantRey

"""The versioned API and the UI's files (DESIGN/v0-5-0.md §2–§3)."""

import http.client
import json
import socket
import sys
import textwrap
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tag_file_system.addons.loader import MODULE_PREFIX
from tag_file_system.config import Config, DaemonConfig
from tag_file_system.core.interface.action import RunStatus
from tag_file_system.root import FUNCTIONS_FILE, Root
from tag_file_system.services.control import ControlClient, ControlError
from tag_file_system.services.daemon import Daemon

ADDON = """
    from tag_file_system import action

    @action.added()
    def run(path, metadata, ctx, suffix: str = ".bak"):
        # out/a.bak, out/b.bak: copies without tags of their own
        ctx.copy(path, ctx.root / "out" / (path.name.split("--")[0] + suffix))
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
    root = Root.init(tmp_path / "vault")
    Config(daemon=DaemonConfig(port=free_port(), stop_timeout_seconds=0.5)).write(
        root.config_path
    )
    (root.script_dir / "copy.py").write_text(textwrap.dedent(ADDON), encoding="utf-8")
    folder = root.path / "copy"
    folder.mkdir()
    (folder / FUNCTIONS_FILE).write_text(
        "version: 1\nfunctions:\n  copy:\n    run: {}\n  nope:\n    run: {}\n",
        encoding="utf-8",
    )
    (folder / "a--photo.txt").write_text("a", encoding="utf-8")
    (folder / "b--photo--raw.txt").write_text("b", encoding="utf-8")
    return root


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    directory = tmp_path / "dist"
    (directory / "assets").mkdir(parents=True)
    (directory / "index.html").write_text("<!doctype html><title>tfs</title>")
    (directory / "assets" / "app-1a2b3c.js").write_text("console.log(1)")
    (directory / "assets" / "app-1a2b3c.css").write_text("body{}")
    (directory / "favicon.svg").write_text("<svg/>")
    return directory


@pytest.fixture
def daemon(root: Root, dist: Path):
    d = Daemon(root, control=True, poll_ms=50, ui_dir=dist)
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


def status_of(client: ControlClient, path: str, params: dict | None = None) -> int:
    try:
        client.get(path, params)
    except ControlError as e:
        return e.status
    return 200


def raw(client: ControlClient, path: str, method: str = "GET", token: bool = False):
    """A request as the browser sends it: no token unless asked, nothing
    normalized on the way out."""
    host, port = client.base.removeprefix("http://").rsplit(":", 1)
    connection = http.client.HTTPConnection(host, int(port), timeout=5)
    headers = {"Authorization": f"Bearer {client.token}"} if token else {}
    connection.request(method, path, headers=headers)
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response, body


# ------------------------------------------------------------------ status


def test_status_shape(client: ControlClient, daemon: Daemon):
    status = client.get("/api/v1/status")

    assert status["api"] == "v1" and status["status"] == "ok"
    assert status["functions"] == {"files": 1, "problems": 1}
    assert status["ui"] == {"built": True, "dist": str(daemon.ui_dir)}
    assert status["version"] == daemon.status()["version"]


# ------------------------------------------------------------------- files


def test_files_page_filters_and_400s(client: ControlClient):
    everything = client.get("/api/v1/files")
    assert everything["total"] == 4 and len(everything["items"]) == 4  # + out/*.bak
    assert everything["limit"] == 50 and everything["offset"] == 0
    assert all("runs" not in f for f in everything["items"])

    page = client.get("/api/v1/files", {"prefix": "copy"})
    assert page["total"] == 2
    assert [f["path"] for f in page["items"]] == [
        "copy/a--photo.txt",
        "copy/b--photo--raw.txt",
    ]
    assert page["items"][1]["tags"] == ["photo", "raw"]

    first = client.get("/api/v1/files", {"prefix": "copy", "limit": 1})
    assert [f["path"] for f in first["items"]] == ["copy/a--photo.txt"]
    second = client.get("/api/v1/files", {"prefix": "copy", "limit": 1, "offset": 1})
    assert [f["path"] for f in second["items"]] == ["copy/b--photo--raw.txt"]
    assert second["total"] == 2 and second["offset"] == 1
    newest = client.get("/api/v1/files", {"prefix": "copy", "newest": True, "limit": 1})
    assert [f["path"] for f in newest["items"]] == ["copy/b--photo--raw.txt"]
    assert client.get("/api/v1/files", {"tag": ["photo"]})["total"] == 2
    assert client.get("/api/v1/files", {"tag": ["raw"]})["total"] == 1
    assert client.get("/api/v1/files", {"tag": ["photo", "raw"]})["total"] == 1
    assert client.get("/api/v1/files", {"prefix": "out"})["total"] == 2
    assert client.get("/api/v1/files", {"prefix": "elsewhere"})["total"] == 0
    assert client.get("/api/v1/files", {"name": "B--"})["total"] == 1

    for params in (
        {"limit": "x"},
        {"limit": 0},
        {"limit": 501},
        {"offset": -1},
        {"prefix": ".."},
        {"tag": [""]},
        {"deleted": "maybe"},
    ):
        assert status_of(client, "/api/v1/files", params) == 400, params


def test_file_detail_history_explain_and_404(client: ControlClient):
    key = "copy/a--photo.txt"

    detail = client.get("/api/v1/file", {"path": key})
    assert detail["path"] == key and detail["format"] == ".txt"
    assert detail["tags"] == ["photo"] and isinstance(detail["mtime_ns"], int)

    history = client.get("/api/v1/file/history", {"path": key})
    assert history["file"]["path"] == key
    kinds = [entry["kind"] for entry in history["timeline"]]
    assert "run" in kinds and "event" in kinds
    assert history["runs"][0]["path"] == key
    assert history["runs"][0]["status"] == "ok"
    ats = [entry["at"] for entry in history["timeline"]]
    assert ats == sorted(ats)  # oldest first

    explained = client.get("/api/v1/file/explain", {"path": key})
    assert explained == client.explain(key)
    assert explained["source"] == "daemon"

    assert status_of(client, "/api/v1/file", {"path": "copy/missing.txt"}) == 404
    assert status_of(client, "/api/v1/file/history", {"path": "nope.txt"}) == 404
    for bad in ("", "../x", ".", f"copy/{FUNCTIONS_FILE}", "/abs/x"):
        assert status_of(client, "/api/v1/file", {"path": bad}) == 400, bad
        assert status_of(client, "/api/v1/file/explain", {"path": bad}) == 400, bad


def test_tags_with_counts(client: ControlClient):
    tags = client.get("/api/v1/tags")

    assert [(t["name"], t["files"]) for t in tags["items"]] == [
        ("photo", 2),
        ("raw", 1),
    ]
    assert tags["total"] == 2
    assert all(t["tag_id"] and t["added"] for t in tags["items"])


# -------------------------------------------------------------------- runs


def test_runs_and_run_detail(client: ControlClient, daemon: Daemon):
    runs = client.get("/api/v1/runs")

    assert runs["total"] == 2
    assert {r["path"] for r in runs["items"]} == {
        "copy/a--photo.txt",
        "copy/b--photo--raw.txt",
    }
    assert all(r["status"] == "ok" and r["handler"] == "run" for r in runs["items"])
    assert client.get("/api/v1/runs", {"status": ["ok"]})["total"] == 2
    assert client.get("/api/v1/runs", {"status": ["failed"]})["total"] == 0
    assert (
        client.get("/api/v1/runs", {"action": "copy", "handler": "run"})["total"] == 2
    )
    assert client.get("/api/v1/runs", {"path": "copy/a--photo.txt"})["total"] == 1
    assert client.get("/api/v1/runs", {"prefix": "copy", "limit": 1})["total"] == 2
    since = datetime.now(UTC).replace(year=2100).isoformat()
    assert client.get("/api/v1/runs", {"since": since})["total"] == 0
    assert status_of(client, "/api/v1/runs", {"status": ["bogus"]}) == 400
    assert status_of(client, "/api/v1/runs", {"since": "notadate"}) == 400

    run_id = runs["items"][0]["id"]
    detail = client.get("/api/v1/run", {"id": run_id})
    assert detail["run"]["id"] == run_id and detail["run"]["path"]
    assert isinstance(detail["trace"], list)
    assert [p["kind"] for p in detail["produced"]] == ["emitted"]
    assert detail["produced"][0]["path"].startswith("out/")
    assert [p["kind"] for p in detail["problems"]] == ["run.ok"]  # its own record
    assert detail["problems"][0]["path"] == detail["run"]["path"]
    assert status_of(client, "/api/v1/run", {"id": "nope"}) == 404
    assert status_of(client, "/api/v1/run") == 400


# ---------------------------------------------------------------- problems


def test_problems_feed(client: ControlClient, daemon: Daemon):
    feed = client.get("/api/v1/problems")

    kinds = [p["kind"] for p in feed["items"]]
    assert "functions.unbound" in kinds and "run.ok" in kinds
    ats = [p["occurred_at"] for p in feed["items"]]
    assert ats == sorted(ats, reverse=True)  # newest first
    assert feed["total"] == len(feed["items"])

    unbound = client.get("/api/v1/problems", {"kind": "functions.unbound"})
    assert unbound["total"] == 1 and "nope" in unbound["items"][0]["message"]
    assert client.get("/api/v1/problems", {"severity": "err"})["total"] == 1
    assert (
        client.get("/api/v1/problems", {"severity": "info"})["total"] == feed["total"]
    )
    assert status_of(client, "/api/v1/problems", {"severity": "bogus"}) == 400
    first = client.get("/api/v1/problems", {"limit": 1})
    second = client.get("/api/v1/problems", {"limit": 1, "offset": 1})
    assert first["items"][0]["id"] != second["items"][0]["id"]
    ok = client.get("/api/v1/problems", {"kind": "run.ok"})["items"][0]
    assert ok["path"] in ("copy/a--photo.txt", "copy/b--photo--raw.txt")
    assert client.get("/api/v1/problems", {"run": ok["run_id"]})["total"] == 1

    one = client.get("/api/v1/problem", {"id": ok["id"]})
    assert one["id"] == ok["id"] and one["path"] == ok["path"]
    assert status_of(client, "/api/v1/problem", {"id": "nope"}) == 404


# ----------------------------------------------------------- addons & co


def test_addons_functions_and_upgrades(client: ControlClient, daemon: Daemon):
    assert client.get("/api/v1/addons") == client.actions()

    functions = client.get("/api/v1/functions")
    (folder,) = functions["files"]
    assert folder["folder"] == "copy" and folder["file"] == f"copy/{FUNCTIONS_FILE}"
    assert folder["error"] is None and folder["invalid"] == []
    by_ref = {f"{e['script']}.{e['handler']}": e for e in folder["entries"]}
    assert by_ref["copy.run"]["valid"] is True
    assert (
        by_ref["copy.run"]["display"] == "copy.run()"
        and by_ref["copy.run"]["exclude"] == ""
    )
    assert by_ref["nope.run"]["valid"] is False
    assert by_ref["nope.run"]["problem"]["kind"] == "functions.unbound"
    assert [p["kind"] for p in functions["problems"]] == ["functions.unbound"]

    assert client.get("/api/v1/upgrades") == {"items": [], "total": 0}
    daemon.store.record_upgrade(
        from_tag="v0.4.0",
        from_hash="a" * 40,
        to_tag="v0.5.0",
        to_hash="b" * 40,
        schema_before=3,
        schema_after=3,
        tests_run=1,
        tests_passed=1,
        tests_skipped=0,
        snapshot_path=None,
        outcome="ok",
        started_at=datetime.now(UTC),
    )
    upgrades = client.get("/api/v1/upgrades", {"limit": 5})
    assert upgrades["total"] == 1 and upgrades["items"][0]["to_tag"] == "v0.5.0"
    assert status_of(client, "/api/v1/upgrades", {"limit": 0}) == 400


# ---------------------------------------------------------------- the ui


def test_api_needs_the_token_and_ui_does_not(client: ControlClient):
    response, body = raw(client, "/api/v1/status")
    assert response.status == 401 and b"token" in body

    for path in ("/ui", "/ui/", "/ui/index.html"):
        response, body = raw(client, path)
        assert response.status == 200, path
        assert response.getheader("Content-Type") == "text/html; charset=utf-8"
        assert response.getheader("Cache-Control") == "no-cache"
        assert response.getheader("X-Content-Type-Options") == "nosniff"
        assert body.startswith(b"<!doctype html")

    response, body = raw(client, "/ui/assets/app-1a2b3c.js")
    assert response.status == 200
    assert response.getheader("Content-Type") == "text/javascript; charset=utf-8"
    assert response.getheader("Cache-Control") == "public, max-age=31536000, immutable"
    response, _ = raw(client, "/ui/assets/app-1a2b3c.css")
    assert response.getheader("Content-Type") == "text/css; charset=utf-8"
    response, _ = raw(client, "/ui/favicon.svg")
    assert response.getheader("Content-Type") == "image/svg+xml"

    response, body = raw(client, "/ui/", method="HEAD")
    assert response.status == 200 and body == b""
    assert int(response.getheader("Content-Length") or 0) > 0

    for path in (
        "/ui/nope.js",
        "/ui/../pyproject.toml",
        "/ui/%2e%2e/pyproject.toml",
        "/ui/assets/../../pyproject.toml",
        "/ui/assets/",
        "/ui//index.html",
        "/ui/C:/Windows/win.ini",
    ):
        response, body = raw(client, path)
        assert response.status == 404, path
        assert json.loads(body) == {"error": "not found"}

    response, body = raw(client, "/ui/", method="POST")
    assert response.status == 405

    response, body = raw(client, "/api/v2/status", token=True)
    assert response.status == 404 and b"/api/v1" in body
    response, body = raw(client, "/api/v1/status", method="POST", token=True)
    assert response.status == 405


def test_ui_not_built(root: Root, tmp_path: Path):
    daemon = Daemon(root, control=True, poll_ms=50, ui_dir=tmp_path / "absent")
    daemon.startup()
    try:
        config = root.load_config()
        client = ControlClient(
            config.daemon.bind, config.daemon.port, root.read_token()
        )
        for path in ("/ui/", "/ui/index.html", "/ui/assets/x.js"):
            response, body = raw(client, path)
            assert response.status == 503, path
            payload = json.loads(body)
            assert "UI not built" in payload["error"]
            assert "npm ci && npm run build" in payload["error"]
            assert payload["dist"] == str(tmp_path / "absent")
        assert client.get("/api/v1/status")["ui"]["built"] is False
    finally:
        daemon.shutdown()


def test_legacy_endpoints_keep_their_shapes(client: ControlClient):
    files = client.files(prefix="copy", runs=True)
    assert [f["path"] for f in files] == ["copy/a--photo.txt", "copy/b--photo--raw.txt"]
    assert files[0]["runs"][0]["status"] == "ok"
    assert set(client.actions()) == {"version", "hash", "source", "actions", "problems"}
    assert client.health()["status"] == "ok"
    with pytest.raises(ControlError):
        client.get("/files", {"prefix": ".."})
    assert [r["status"] for r in files[0]["runs"]] == [RunStatus.OK.value]
