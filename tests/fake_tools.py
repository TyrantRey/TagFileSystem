# Code by AkinoAlice@TyrantRey

"""Stand-ins for ``uv``, ``pytest`` and ``tfs`` driven by a JSON scenario
(``TFS_FAKE_SCENARIO``), so ``tests/test_upgrade.py`` can run the real
orchestrator against a real git checkout without syncing a real venv or
running a real suite.

    python fake_tools.py uv sync --locked ...     python fake_tools.py pytest -q
    python fake_tools.py tfs --root R start -d    python fake_tools.py tfs --root R record-upgrade FILE
    python fake_tools.py daemon R BEHAVIOUR       (what the fake `start -d` spawns)

The fake daemon writes a real ``.tfs/lock``, answers ``/health`` with the
checkout's current commit (or a wrong one) and exits on ``/stop``.
``record-upgrade`` is delegated to the real CLI. Scenario keys:

    repo, log (jsonl of every call), start: {marker: ok|die|wrong-hash|no-health},
    pytest_exit, pytest_summary, sync_fail_calls: [n, ...], in_flight: [...],
    die_user_version (what a dying "migration" leaves behind)
"""

import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def scenario() -> dict:
    return json.loads(Path(os.environ["TFS_FAKE_SCENARIO"]).read_text(encoding="utf-8"))


def log(kind: str, **data) -> None:
    with open(scenario()["log"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": kind, **data}) + "\n")


def calls(kind: str) -> list[dict]:
    try:
        lines = Path(scenario()["log"]).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [json.loads(line) for line in lines if json.loads(line)["kind"] == kind]


def repo() -> Path:
    return Path(scenario()["repo"])


def repo_marker() -> str:
    return (repo() / "marker.txt").read_text(encoding="utf-8").strip()


def repo_head() -> str:
    result = subprocess.run(
        ["git", "-C", str(repo()), "rev-parse", "HEAD"], capture_output=True, text=True
    )
    return result.stdout.strip()


def repo_version() -> str:
    data = tomllib.loads((repo() / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def read_lock(root: Path) -> dict | None:
    try:
        data = json.loads((root / ".tfs" / "lock").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def set_user_version(root: Path, value: int) -> None:
    connection = sqlite3.connect(root / ".tfs" / "db" / "system.db")
    try:
        connection.execute(f"PRAGMA user_version = {int(value)}")
        connection.commit()
    finally:
        connection.close()


# ------------------------------------------------------------------- tools


def uv(args: list[str]) -> int:
    log("uv", args=args)
    if args[:1] == ["--version"]:
        print("uv 0.0.0 (fake)")
        return 0
    if args[:1] == ["sync"]:
        nth = len([c for c in calls("uv") if c["args"][:1] == ["sync"]])
        if nth in scenario().get("sync_fail_calls", []):
            print("fake uv: sync failed on purpose", file=sys.stderr)
            return 1
        # Evidence of which code was synced last.
        (repo() / "synced-at.txt").write_text(repo_marker(), encoding="utf-8")
    return 0


def pytest_(args: list[str]) -> int:
    log("pytest", args=args)
    if args[:1] == ["--version"]:
        print("pytest 0.0.0 (fake)")
        return 0
    current = scenario()
    print(current.get("pytest_summary", "3 passed, 1 skipped in 0.01s"))
    return int(current.get("pytest_exit", 0))


def tfs(args: list[str]) -> int:
    at = args.index("--root")
    root, command, rest = Path(args[at + 1]), args[at + 2], args[at + 3 :]
    if command == "start":
        return start(root)
    if command == "record-upgrade":
        log("record", root=str(root))
        return subprocess.call(
            [
                sys.executable,
                "-P",
                "-m",
                "tag_file_system.cli",
                "--root",
                str(root),
                "record-upgrade",
                *rest,
            ]
        )
    print(f"fake tfs: unknown command {command}", file=sys.stderr)
    return 2


def start(root: Path) -> int:
    behaviour = scenario().get("start", {}).get(repo_marker(), "ok")
    log("start", root=str(root), marker=repo_marker(), behaviour=behaviour)
    if behaviour == "die":
        # A migration that went wrong before the daemon died.
        set_user_version(root, int(scenario().get("die_user_version", 99)))
        print(
            "error: the daemon exited with code 1:\nfake daemon: boom", file=sys.stderr
        )
        return 1
    if behaviour == "no-health":
        return 0
    out = root / ".tfs" / "fake-daemon.out"
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200
        ) | getattr(subprocess, "DETACHED_PROCESS", 0x8)
    else:
        kwargs["start_new_session"] = True
    before = read_lock(root)
    with out.open("ab") as handle:
        child = subprocess.Popen(
            [sys.executable, __file__, "daemon", str(root), behaviour],
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            **kwargs,
        )
    # Like the real `start -d`, judge by the lock the daemon writes, never by
    # child.pid: on Windows the venv's python.exe is a launcher whose child
    # is the interpreter that actually runs (and stamps its own pid).
    deadline = time.time() + 20
    while time.time() < deadline:
        lock = read_lock(root)
        if lock and lock.get("port") and lock != before:
            print(f"daemon started in the background (pid {lock['pid']}, watching)")
            return 0
        if child.poll() is not None:
            print("fake daemon died at once", file=sys.stderr)
            return 1
        time.sleep(0.05)
    print("fake daemon did not come up", file=sys.stderr)
    return 1


class Handler(BaseHTTPRequestHandler):
    behaviour = "ok"

    def log_message(self, *args) -> None:  # noqa: A002 - quiet
        pass

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            self._send(404, {"error": "unknown"})
            return
        self._send(
            200,
            {
                "status": "ok",
                "pid": os.getpid(),
                "started": True,
                "in_flight": scenario().get("in_flight", []),
                "version": repo_version(),
                "hash": "0" * 40 if self.behaviour == "wrong-hash" else repo_head(),
            },
        )

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/stop":
            self._send(404, {"error": "unknown"})
            return
        self._send(200, {"stopping": True})
        threading.Thread(target=self.server.shutdown, daemon=True).start()


def daemon(root: Path, behaviour: str) -> int:
    Handler.behaviour = behaviour
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    lock = root / ".tfs" / "lock"
    lock.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "hostname": socket.gethostname(),
                "created_at": time.time(),
                "port": port,
                "bind": "127.0.0.1",
            }
        ),
        encoding="utf-8",
    )
    # Never outlive the test session.
    watchdog = threading.Timer(180, lambda: os._exit(0))
    watchdog.daemon = True
    watchdog.start()
    log("daemon", root=str(root), pid=os.getpid(), port=port, marker=repo_marker())
    server.serve_forever()
    server.server_close()
    current = read_lock(root)
    if current and current.get("pid") == os.getpid():
        try:
            lock.unlink()
        except OSError:
            pass
    return 0


def main(argv: list[str]) -> int:
    tool, args = argv[0], argv[1:]
    if tool == "uv":
        return uv(args)
    if tool == "pytest":
        return pytest_(args)
    if tool == "tfs":
        return tfs(args)
    if tool == "daemon":
        return daemon(Path(args[0]), args[1] if len(args) > 1 else "ok")
    print(f"fake_tools: unknown tool {tool}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
