# Code by AkinoAlice@TyrantRey

"""The daemon's HTTP control channel and its client (DESIGN.md §8).

A small JSON-over-HTTP server bound to ``[daemon] bind:port``, every request
authenticated with ``Authorization: Bearer <.tfs/token>``. It is the seed of
the later API/MCP: ``/health``, ``/stop``, ``/reload``, ``/actions``,
``/files``. ``ControlClient`` is what the ``tfs`` CLI talks to.
"""

import ipaddress
import json
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from tag_file_system.core.interface.action import RunRecord
from tag_file_system.core.interface.file_metadata import TaggedFile
from tag_file_system.core.logger import logger
from tag_file_system.core.paths import has_parent_reference, is_anchored, posix_key

if TYPE_CHECKING:  # pragma: no cover
    from tag_file_system.services.daemon import Daemon


class ControlError(Exception):
    """The daemon answered with an error."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


class ControlUnavailable(ControlError):
    """No daemon answers on the configured address (or the root's config /
    token cannot be read, so none could be reached)."""

    def __init__(self, message: str) -> None:
        super().__init__(0, message)

    def __str__(self) -> str:
        return self.message


class BadRequest(ValueError):
    """A request parameter the server rejects (HTTP 400)."""


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def parse_flag(name: str, value: str | None) -> bool:
    if value is None:
        return False
    text = value.strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise BadRequest(f"{name} must be true or false, got {value!r}")


def validate_prefix(prefix: str | None) -> str | None:
    """A ``prefix`` must be a root-relative directory key."""
    if prefix is None:
        return None
    text = prefix.strip()
    if not text or text in (".", "./"):
        raise BadRequest("prefix must name a directory below the root")
    if is_anchored(text) or has_parent_reference(Path(text)):
        raise BadRequest(f"prefix must be a root-relative directory, got {prefix!r}")
    try:
        key = posix_key(text)
    except ValueError as e:
        raise BadRequest(f"prefix: {e}") from None
    if key == ".":
        raise BadRequest("prefix must name a directory below the root")
    return key


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def file_payload(
    file: TaggedFile, runs: list[RunRecord] | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "path": file.path.as_posix(),
        "file_id": file.file_id,
        "hash": file.file_hash,
        "status": file.status,
        "tags": [t.name for t in file.tags],
        "size": file.metadata.file_size if file.metadata else None,
        "mime_type": file.metadata.mime_type if file.metadata else None,
        "added": file.metadata.time_added.isoformat() if file.metadata else None,
    }
    if runs is not None:
        payload["runs"] = [_jsonable(r) for r in runs]
    return payload


# ------------------------------------------------------------------ server


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # SO_REUSEADDR lets a *second* process bind a port that is already
    # listening on Windows: two roots configured with the same port would
    # both "start", and only one of them could ever be reached. One daemon
    # per port, and a clashing start fails at bind.
    allow_reuse_address = False

    def __init__(
        self, bind: str, port: int, handler: type[BaseHTTPRequestHandler]
    ) -> None:
        if ipaddress.ip_address(bind).version == 6:
            self.address_family = socket.AF_INET6
        super().__init__((bind, port), handler)

    def server_bind(self) -> None:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:  # Windows: refuse to share the port at all
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
            except OSError:  # pragma: no cover - older stacks
                pass
        super().server_bind()


class ControlServer:
    def __init__(self, daemon: "Daemon", bind: str, port: int, token: str) -> None:
        self.daemon = daemon
        self.token = token
        self.logger = logger
        server_ref = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "tfs/1"

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                server_ref.logger.debug("control: " + format % args)

            def _authorized(self) -> bool:
                header = self.headers.get("Authorization", "")
                return header == f"Bearer {server_ref.token}"

            def _send(self, status: HTTPStatus, body: Any) -> None:
                data = json.dumps(_jsonable(body)).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def send_error(
                self, code: int, message: str | None = None, explain: str | None = None
            ) -> None:
                # http.server's own errors (414, 400, 501) as JSON, never HTML.
                try:
                    self._send(
                        HTTPStatus(code), {"error": message or HTTPStatus(code).phrase}
                    )
                except Exception:  # pragma: no cover - the socket is gone
                    pass

            def _route(self, method: str) -> None:
                if not self._authorized():
                    self._send(
                        HTTPStatus.UNAUTHORIZED, {"error": "missing or wrong token"}
                    )
                    return
                parsed = urllib.parse.urlsplit(self.path)
                query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                try:
                    status, body = server_ref.dispatch(method, parsed.path, query)
                except BadRequest as e:
                    status, body = HTTPStatus.BAD_REQUEST, {"error": str(e)}
                except Exception as e:  # never let a handler kill the server
                    server_ref.logger.exception("control request failed")
                    status, body = (
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"{type(e).__name__}: {e}"},
                    )
                self._send(status, body)

            def do_GET(self) -> None:  # noqa: N802
                self._route("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._route("POST")

            def do_PUT(self) -> None:  # noqa: N802
                self._route("PUT")

            def do_DELETE(self) -> None:  # noqa: N802
                self._route("DELETE")

            def do_PATCH(self) -> None:  # noqa: N802
                self._route("PATCH")

            def do_HEAD(self) -> None:  # noqa: N802
                self._route("HEAD")

            def do_OPTIONS(self) -> None:  # noqa: N802
                self._route("OPTIONS")

        self._server = _Server(bind, port, Handler)
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="tfs-control", daemon=True
        )
        self._thread.start()
        self.logger.info(
            f"Control channel on http://{self.address[0]}:{self.address[1]}"
        )

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(5)

    # ------------------------------------------------------------- routes

    def dispatch(
        self, method: str, path: str, query: dict[str, list[str]]
    ) -> tuple[HTTPStatus, Any]:
        routes: dict[tuple[str, str], Callable[[dict[str, list[str]]], Any]] = {
            ("GET", "/health"): self._health,
            ("POST", "/stop"): self._stop,
            ("POST", "/reload"): self._reload,
            ("GET", "/actions"): self._actions,
            ("GET", "/files"): self._files,
        }
        handler = routes.get((method, path))
        if handler is None:
            known = {p for _, p in routes}
            if path in known:
                return HTTPStatus.METHOD_NOT_ALLOWED, {
                    "error": f"{method} not allowed on {path}"
                }
            return HTTPStatus.NOT_FOUND, {"error": f"unknown endpoint {path}"}
        return HTTPStatus.OK, handler(query)

    def _health(self, query: dict[str, list[str]]) -> dict[str, Any]:
        return self.daemon.status()

    def _stop(self, query: dict[str, list[str]]) -> dict[str, Any]:
        self.daemon.request_stop()
        return {"stopping": True}

    def _reload(self, query: dict[str, list[str]]) -> dict[str, Any]:
        return self.daemon.reload()

    def _actions(self, query: dict[str, list[str]]) -> dict[str, Any]:
        return {
            "actions": self.daemon.describe_addons(),
            "problems": self.daemon.load_problems(),
        }

    def _files(self, query: dict[str, list[str]]) -> dict[str, Any]:
        first = {k: v[0] for k, v in query.items() if v}
        tags = query.get("tag") or []
        if any(not t.strip() for t in tags):
            raise BadRequest("tag cannot be blank")
        files = self.daemon.backend.query_files(
            tags=[t.strip() for t in tags] or None,
            filename=first.get("name") or None,
            file_format=first.get("format") or None,
            mime_type=first.get("mime") or None,
            include_deleted=parse_flag("deleted", first.get("deleted")),
            path_prefix=validate_prefix(first.get("prefix") or None),
        )
        with_runs = parse_flag("runs", first.get("runs"))
        payload = [
            file_payload(
                f, self.daemon.store.query_runs(file_path=f.path) if with_runs else None
            )
            for f in files
        ]
        return {"files": payload}


# ------------------------------------------------------------------ client


class ControlClient:
    def __init__(self, host: str, port: int, token: str, timeout: float = 5.0) -> None:
        connect_host = host
        if host == "0.0.0.0":
            connect_host = "127.0.0.1"
        elif host == "::":
            connect_host = "::1"
        if ":" in connect_host and not connect_host.startswith("["):
            connect_host = f"[{connect_host}]"
        self.base = f"http://{connect_host}:{port}"
        self.token = token
        self.timeout = timeout

    def _call(
        self, method: str, path: str, params: dict[str, Any] | None = None
    ) -> Any:
        url = self.base + path
        if params:
            pairs: list[tuple[str, str]] = []
            for key, value in params.items():
                if value is None or value is False:
                    continue
                if isinstance(value, (list, tuple)):
                    pairs.extend((key, str(v)) for v in value)
                else:
                    pairs.append((key, "1" if value is True else str(value)))
            if pairs:
                url += "?" + urllib.parse.urlencode(pairs)
        request = urllib.request.Request(
            url, method=method, headers={"Authorization": f"Bearer {self.token}"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                message = json.loads(e.read().decode("utf-8")).get("error", e.reason)
            except Exception:
                message = str(e.reason)
            raise ControlError(e.code, str(message)) from None
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            raise ControlUnavailable(f"no daemon answers at {self.base}: {e}") from None

    def health(self) -> dict[str, Any]:
        return self._call("GET", "/health")

    def stop(self) -> dict[str, Any]:
        return self._call("POST", "/stop")

    def reload(self) -> dict[str, Any]:
        return self._call("POST", "/reload")

    def actions(self) -> dict[str, Any]:
        """``{"actions": [...], "problems": [...]}``."""
        return self._call("GET", "/actions")

    def files(
        self,
        tags: list[str] | None = None,
        name: str | None = None,
        format: str | None = None,
        mime: str | None = None,
        deleted: bool = False,
        prefix: str | None = None,
        runs: bool = False,
    ) -> list[dict[str, Any]]:
        return self._call(
            "GET",
            "/files",
            {
                "tag": tags or None,
                "name": name,
                "format": format,
                "mime": mime,
                "deleted": deleted,
                "prefix": prefix,
                "runs": runs,
            },
        )["files"]
