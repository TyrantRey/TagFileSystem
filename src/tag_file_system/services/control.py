# Code by AkinoAlice@TyrantRey

"""The daemon's HTTP control channel and its client (DESIGN/v0-1-0.md §8,
DESIGN/v0-5-0.md §2–§3).

A small JSON-over-HTTP server bound to ``[daemon] bind:port``. Three kinds of
route: the CLI's own (``/health``, ``/stop``, ``/reload``, ``/actions``,
``/files``, ``/explain`` — shapes frozen), the versioned API (``/api/v1/...``,
``services/api.py``), and the built web UI's files (``/ui/...``,
``services/ui.py``). Everything but ``/ui/`` wants ``Authorization: Bearer
<.tfs/token>``; the UI's files hold no data and are served without it.
``ControlClient`` is what the ``tfs`` CLI talks to.
"""

import ipaddress
import json
import shutil
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from tag_file_system.core.logger import logger
from tag_file_system.services.api import (  # noqa: F401 - re-exported for the CLI
    API_PREFIX,
    Api,
    BadRequest,
    Conflict,
    FileResponse,
    NotFound,
    RawHandler,
    file_payload,
    jsonable,
    parse_file_filters,
    parse_file_key,
    parse_flag,
    validate_prefix,
)
from tag_file_system.services.ui import NOT_BUILT, UI_PREFIX, UiFiles
from tag_file_system.version import COMMIT, VERSION

if TYPE_CHECKING:  # pragma: no cover
    from tag_file_system.services.daemon import Daemon

BODY_MAX = 16 * 1024 * 1024  # a `tfs touch --content` is small; a file is not


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
    def __init__(
        self,
        daemon: "Daemon",
        bind: str,
        port: int,
        token: str,
        ui_dir: Path | None = None,
    ) -> None:
        self.daemon = daemon
        self.token = token
        self.logger = logger
        self.ui = UiFiles(ui_dir)
        self.api = Api(daemon, self.ui)
        # Built once: the CLI's routes, then the versioned API's.
        self._routes: dict[tuple[str, str], Callable[[dict[str, list[str]]], Any]] = {
            ("GET", "/health"): self._health,
            ("POST", "/stop"): self._stop,
            ("POST", "/reload"): self._reload,
            ("GET", "/actions"): self.api.addons,
            ("GET", "/files"): self._files,
            ("GET", "/explain"): self._explain,
            **self.api.routes(),
        }
        self._raw_routes: dict[tuple[str, str], RawHandler] = self.api.raw_routes()
        server_ref = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "tfs/1"

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                server_ref.logger.debug("control: " + format % args)

            def _authorized(self) -> bool:
                header = self.headers.get("Authorization", "")
                return header == f"Bearer {server_ref.token}"

            def _send(self, status: HTTPStatus, body: Any) -> None:
                data = json.dumps(jsonable(body)).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _serve_ui(self, method: str, path: str) -> None:
                """A file of the built UI: no token, no data, no listing."""
                if method not in ("GET", "HEAD"):
                    self._send(
                        HTTPStatus.METHOD_NOT_ALLOWED,
                        {"error": f"{method} not allowed on {path}"},
                    )
                    return
                ui = server_ref.ui
                if not ui.built:
                    self._send(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": NOT_BUILT, "dist": ui.describe()["dist"]},
                    )
                    return
                asset = ui.resolve(path)
                if asset is None:
                    self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                data = asset.path.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", asset.content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", asset.cache_control)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                if method == "GET":
                    self.wfile.write(data)

            def _read_body(self, query: dict[str, list[str]]) -> None:
                """A JSON object body (``touch``'s content, DESIGN/v0-5-0.md
                §11.7) joins the query: strings as they are, other values as
                JSON text. The query wins on a clash."""
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    return
                if length > BODY_MAX:
                    raise BadRequest(f"body larger than {BODY_MAX} bytes")
                raw = self.rfile.read(length)
                try:
                    data = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    raise BadRequest("body must be a JSON object") from None
                if not isinstance(data, dict):
                    raise BadRequest("body must be a JSON object")
                for key, value in data.items():
                    if key in query:
                        continue
                    query[str(key)] = [
                        value if isinstance(value, str) else json.dumps(value)
                    ]

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
                parsed = urllib.parse.urlsplit(self.path)
                path = parsed.path
                if path == UI_PREFIX or path.startswith(UI_PREFIX + "/"):
                    # The UI's files hold no data: served before the token
                    # check, and only ever a file under dist (DESIGN/v0-5-0.md §3.2).
                    self._serve_ui(method, path)
                    return
                if not self._authorized():
                    self._send(
                        HTTPStatus.UNAUTHORIZED, {"error": "missing or wrong token"}
                    )
                    return
                query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                try:
                    raw = server_ref.raw_handler(method, path)
                    if raw is not None:
                        # The body is the payload (an upload): the handler
                        # streams it; nothing is read into memory here.
                        length_text = self.headers.get("Content-Length")
                        if length_text is None:
                            raise BadRequest("Content-Length is required")
                        try:
                            length = int(length_text)
                        except ValueError:
                            raise BadRequest(
                                "Content-Length must be an integer"
                            ) from None
                        status, body = HTTPStatus.OK, raw(query, self.rfile, length)
                    else:
                        self._read_body(query)
                        status, body = server_ref.dispatch(method, path, query)
                except BadRequest as e:
                    status, body = HTTPStatus.BAD_REQUEST, {"error": str(e)}
                except NotFound as e:
                    status, body = HTTPStatus.NOT_FOUND, {"error": str(e)}
                except Conflict as e:
                    status, body = HTTPStatus.CONFLICT, {"error": str(e)}
                except Exception as e:  # never let a handler kill the server
                    server_ref.logger.exception("control request failed")
                    status, body = (
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"{type(e).__name__}: {e}"},
                    )
                if isinstance(body, FileResponse):
                    self._send_file(body, method)
                else:
                    self._send(status, body)

            def _send_file(self, file: FileResponse, method: str) -> None:
                """A download (DESIGN/v0-5-0.md §12.2): streamed, never read
                whole; the name in both the plain and the RFC 5987 form."""
                ascii_name = (
                    file.filename.encode("ascii", "replace")
                    .decode("ascii")
                    .replace('"', "'")
                )
                quoted = urllib.parse.quote(file.filename, safe="")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", file.content_type)
                self.send_header("Content-Length", str(file.size))
                self.send_header(
                    "Content-Disposition",
                    f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}",
                )
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if method == "HEAD":
                    return
                with file.path.open("rb") as handle:
                    shutil.copyfileobj(handle, self.wfile, 1 << 20)

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

    def raw_handler(self, method: str, path: str) -> RawHandler | None:
        """The handler that reads the body itself, if this is such a route."""
        return self._raw_routes.get((method, path))

    def dispatch(
        self, method: str, path: str, query: dict[str, list[str]]
    ) -> tuple[HTTPStatus, Any]:
        handler = self._routes.get((method, path))
        if handler is None:
            known = {p for _, p in self._routes} | {p for _, p in self._raw_routes}
            if path in known:
                return HTTPStatus.METHOD_NOT_ALLOWED, {
                    "error": f"{method} not allowed on {path}"
                }
            if path.startswith("/api/") and not path.startswith(API_PREFIX + "/"):
                return HTTPStatus.NOT_FOUND, {
                    "error": f"unknown API version; this daemon serves {API_PREFIX}"
                }
            return HTTPStatus.NOT_FOUND, {"error": f"unknown endpoint {path}"}
        return HTTPStatus.OK, handler(query)

    def _health(self, query: dict[str, list[str]]) -> dict[str, Any]:
        return self.daemon.status()

    def _stop(self, query: dict[str, list[str]]) -> dict[str, Any]:
        self.daemon.request_stop()
        return {"stopping": True}

    def _reload(self, query: dict[str, list[str]]) -> dict[str, Any]:
        """``?yes=1`` consents to a reload above ``confirm_above``
        (DESIGN/v0-5-0.md §11.3)."""
        return self.daemon.reload(
            yes=parse_flag("yes", (query.get("yes") or [None])[0])
        )

    def _explain(self, query: dict[str, list[str]]) -> dict[str, Any]:
        """``tfs explain``: ``?path=<root-relative file>``."""
        return self.daemon.explain(parse_file_key(query))

    def _files(self, query: dict[str, list[str]]) -> dict[str, Any]:
        """``tfs query``: the CLI's list, unpaged, ``runs=1`` for each file's
        history — its shape is frozen (DESIGN/v0-5-0.md §2.2)."""
        files = self.daemon.backend.query_files(**parse_file_filters(query))
        with_runs = parse_flag("runs", (query.get("runs") or [None])[0])
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
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        timeout: float | None = None,
        raw: bytes | None = None,
        binary: bool = False,
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
        headers = {"Authorization": f"Bearer {self.token}"}
        data = None
        if raw is not None:
            data = raw
            headers["Content-Type"] = "application/octet-stream"
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout if timeout is None else timeout
            ) as response:
                if binary:
                    return response.read(), dict(response.headers.items())
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

    def reload(self, yes: bool = False) -> dict[str, Any]:
        # A reload reconciles the whole root before it answers; with no
        # workers that includes the runs it starts. Wait for it.
        return self._call("POST", "/reload", {"yes": yes}, timeout=600.0)

    def actions(self) -> dict[str, Any]:
        """``{"actions": [...], "problems": [...]}``."""
        return self._call("GET", "/actions")

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Any ``GET`` — the versioned API (``/api/v1/...``) included."""
        return self._call("GET", path, params, timeout=timeout)

    def post(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Any ``POST`` of the API (DESIGN/v0-5-0.md §11.7)."""
        return self._call("POST", path, params, body, timeout=timeout)

    def upload(
        self,
        path: str,
        params: dict[str, Any] | None,
        data: bytes,
        timeout: float | None = None,
    ) -> Any:
        """``POST`` with the bytes as the body (DESIGN/v0-5-0.md §12.1)."""
        return self._call("POST", path, params, raw=data, timeout=timeout)

    def download(
        self, path: str, params: dict[str, Any] | None = None
    ) -> tuple[bytes, dict[str, str]]:
        """A ``GET`` answered as bytes: ``(body, headers)``."""
        return self._call("GET", path, params, binary=True)

    def explain(self, path: str) -> dict[str, Any]:
        """What applies to one file and why (DESIGN/v0-4-0.md §9)."""
        return self._call("GET", "/explain", {"path": path})

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
