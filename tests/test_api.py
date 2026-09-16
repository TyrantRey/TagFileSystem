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
    Config(
        daemon=DaemonConfig(
            max_concurrent_runs=0, port=free_port(), stop_timeout_seconds=0.5
        )
    ).write(root.config_path)
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


def status_of(
    client: ControlClient, path: str, params: dict | None = None, method: str = "GET"
) -> int:
    try:
        if method == "POST":
            client.post(path, params)
        else:
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


def test_default_ui_dir_honours_the_environment(
    root: Root, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An install that is not a checkout (the Docker image, DESIGN/v0-5-0.md
    §10.2) names the built UI through TFS_UI_DIR; a blank value is no value."""
    from tag_file_system.services.ui import UI_DIR_ENV, default_ui_dir
    from tag_file_system.version import REPO

    assert default_ui_dir({UI_DIR_ENV: str(tmp_path / "ui")}) == tmp_path / "ui"
    assert REPO is not None and default_ui_dir({}) == REPO / "Frontend" / "dist"
    assert default_ui_dir({UI_DIR_ENV: "  "}) == default_ui_dir({})

    monkeypatch.setenv(UI_DIR_ENV, str(tmp_path / "elsewhere"))
    daemon = Daemon(root, control=False, poll_ms=50)  # no ui_dir: the environment
    try:
        assert daemon.ui_dir == tmp_path / "elsewhere"
    finally:
        daemon.shutdown()


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


# ------------------------------------------------ operations (DESIGN §11.7)


def test_operation_endpoints_answer_400_404_409(client: ControlClient, daemon: Daemon):
    """The POST routes of DESIGN/v0-5-0.md §11.7 through HTTP: the JSON body,
    the status codes, and that the UI's GET-only client never needs them."""
    assert (
        status_of(
            client, "/api/v1/plan", {"path": "copy/a--photo.txt", "prefix": "copy"}
        )
        == 400
    )
    assert status_of(client, "/api/v1/plan", {"limit": 0}) == 400
    plan = client.get("/api/v1/plan", {"prefix": "copy"})
    assert plan["source"] == "daemon" and plan["summary"]["files"] == 2

    queue = client.get("/api/v1/queue")
    assert queue["paused"] is False and queue["in_flight"] == []
    assert client.post("/api/v1/pause")["paused"] is True
    assert client.get("/api/v1/status")["paused"] is True
    assert client.post("/api/v1/resume")["paused"] is False

    checks = {c["check"]: c["status"] for c in client.get("/api/v1/doctor")["checks"]}
    assert checks["database"] == "ok" and checks["queue"] == "ok"

    ok = client.get("/api/v1/runs")["items"][0]
    with pytest.raises(ControlError) as refused:
        client.post("/api/v1/run/retry", {"id": ok["id"]})
    assert refused.value.status == 409 and "only a failed" in refused.value.message
    assert status_of(client, "/api/v1/run/retry", {"id": "nope"}, method="POST") == 404
    assert status_of(client, "/api/v1/run/retry", method="POST") == 400
    assert (
        status_of(client, "/api/v1/run/cancel", {"id": ok["id"]}, method="POST") == 409
    )
    assert status_of(client, "/api/v1/rerun", method="POST") == 400
    dry = client.post("/api/v1/rerun", {"handler": "copy.run", "dry": True})
    assert dry["candidates"] == 2 and dry["queued"] == 0
    assert (
        status_of(client, "/api/v1/rerun", {"handler": "nope.run"}, method="POST")
        == 404
    )

    made = client.post(
        "/api/v1/files/touch", {"path": "copy/c--photo.txt"}, body={"content": "c"}
    )
    assert made["created"] is True and made["file"]["tags"] == ["photo"]
    assert made["applies"] == ["copy.run()"]
    assert (daemon.root.path / "copy" / "c--photo.txt").read_text() == "c"
    assert (
        status_of(client, "/api/v1/files/touch", {"path": ".tfs/x"}, method="POST")
        == 400
    )
    assert (
        status_of(
            client, "/api/v1/files/touch", {"path": FUNCTIONS_FILE}, method="POST"
        )
        == 400
    )
    assert (
        status_of(
            client,
            "/api/v1/files/copy",
            {"src": "copy/nope", "dst": "out"},
            method="POST",
        )
        == 404
    )
    assert (
        status_of(
            client,
            "/api/v1/files/copy",
            {"src": "copy/c--photo.txt", "dst": "copy/a--photo.txt"},
            method="POST",
        )
        == 409
    )
    moved = client.post(
        "/api/v1/files/move", {"src": "copy/c--photo.txt", "dst": "out/"}
    )
    assert moved["path"] == "out/c--photo.txt" and moved["from"] == "copy/c--photo.txt"
    assert client.post("/api/v1/files/mkdir", {"path": "2024--trip"}) == {
        "path": "2024--trip",
        "tags": ["trip"],
    }
    assert client.post("/api/v1/files/remove", {"path": "out/c--photo.txt"}) == {
        "removed": ["out/c--photo.txt"]
    }
    assert (
        status_of(client, "/api/v1/files/remove", {"path": "out"}, method="POST") == 400
    )
    # a body that is not a JSON object is a 400; the GET routes ignore none
    connection = http.client.HTTPConnection(
        "127.0.0.1", daemon.config.daemon.port, timeout=5
    )
    try:
        connection.request(
            "POST",
            "/api/v1/files/touch?path=copy/d.txt",
            body=b"[1, 2]",
            headers={
                "Authorization": f"Bearer {daemon.root.read_token()}",
                "Content-Type": "application/json",
                "Content-Length": "6",
            },
        )
        response = connection.getresponse()
        assert response.status == 400
        assert b"JSON object" in response.read()
    finally:
        connection.close()
    assert status_of(client, "/api/v1/pause") == 405  # GET on a POST route
    assert client.post("/reload", {"yes": True})["config"] == "reloaded"


# ---------------------------------------- the browser's writes (DESIGN §12)


def test_upload_download_and_tags(client: ControlClient, daemon: Daemon):
    made = client.upload(
        "/api/v1/files/upload", {"path": "copy/c--photo.txt"}, b"hello upload"
    )
    assert made["created"] is True and made["size"] == 12
    assert made["file"]["tags"] == ["photo"] and made["applies"] == ["copy.run()"]
    assert (daemon.root.path / "copy" / "c--photo.txt").read_bytes() == b"hello upload"
    assert not list((daemon.root.tfs_dir / "uploads").iterdir())  # nothing staged
    run = daemon.store.query_runs(file_path="copy/c--photo.txt")[0]
    assert run.source.value == "api" and run.status.value == "ok"

    with pytest.raises(ControlError) as refused:
        client.upload("/api/v1/files/upload", {"path": "copy/c--photo.txt"}, b"again")
    assert refused.value.status == 409
    replaced = client.upload(
        "/api/v1/files/upload", {"path": "copy/c--photo.txt", "overwrite": True}, b"v2"
    )
    assert replaced["created"] is False and replaced["size"] == 2
    row = daemon.backend.query_file("copy/c--photo.txt")
    assert row is not None and row.metadata is not None and row.metadata.file_size == 2
    for params in ({"path": "copy"}, {"path": ".tfs/x"}, {"path": FUNCTIONS_FILE}):
        with pytest.raises(ControlError) as bad:
            client.upload("/api/v1/files/upload", params, b"x")
        assert bad.value.status == 400, params

    body, headers = client.download(
        "/api/v1/file/content", {"path": "copy/c--photo.txt"}
    )
    assert body == b"v2"
    assert headers["Content-Type"] == "text/plain"
    assert headers["Content-Length"] == "2"
    disposition = headers["Content-Disposition"]
    assert 'filename="c--photo.txt"' in disposition
    assert "filename*=UTF-8" in disposition and disposition.endswith("c--photo.txt")
    assert status_of(client, "/api/v1/file/content", {"path": "copy/nope.txt"}) == 404
    assert status_of(client, "/api/v1/file/content", {"path": ".."}) == 400

    detail = client.get("/api/v1/file", {"path": "copy/c--photo.txt"})
    assert detail["name_tags"] == ["photo"]
    tagged = client.post(
        "/api/v1/file/tags",
        {"path": "copy/c--photo.txt"},
        body={"add": ["Hot", "raw"]},
    )
    assert tagged["added"] == ["hot", "raw"]
    assert tagged["file"]["tags"] == ["hot", "photo", "raw"]
    assert client.get("/api/v1/files", {"tag": ["hot"]})["total"] == 1
    untagged = client.post(
        "/api/v1/file/tags", {"path": "copy/c--photo.txt", "remove": ["hot", "photo"]}
    )
    assert untagged["removed"] == ["hot"] and untagged["kept"] == ["photo"]
    assert untagged["file"]["tags"] == ["photo", "raw"]
    for params in (
        {"path": "copy/c--photo.txt"},  # nothing to do
        {"path": "copy/c--photo.txt", "add": ["a/b"]},  # illegal
    ):
        assert status_of(client, "/api/v1/file/tags", params, method="POST") == 400
    assert (
        status_of(
            client,
            "/api/v1/file/tags",
            {"path": "copy/nope.txt", "add": ["x"]},
            method="POST",
        )
        == 404
    )
    # a raw route with a bad length is refused (http.client adds a
    # Content-Length of its own, so the header is given by hand)
    connection = http.client.HTTPConnection(
        "127.0.0.1", daemon.config.daemon.port, timeout=5
    )
    try:
        connection.request(
            "POST",
            "/api/v1/files/upload?path=copy/never.txt",
            headers={
                "Authorization": f"Bearer {daemon.root.read_token()}",
                "Content-Length": "abc",
            },
        )
        response = connection.getresponse()
        assert response.status == 400 and b"Content-Length" in response.read()
    finally:
        connection.close()
    assert not (daemon.root.path / "copy" / "never.txt").exists()
    assert status_of(client, "/api/v1/files/upload") == 405
