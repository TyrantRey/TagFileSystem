# Code by AkinoAlice@TyrantRey

"""The daemon's JSON API, ``GET /api/v1/...`` (DESIGN/v0-5-0.md §2), and the
request helpers the control channel's older endpoints share with it.

Conventions: every endpoint is token-protected (the control channel checks
before dispatching), GET only, errors are ``{"error": ...}`` — ``BadRequest``
→ 400, ``NotFound`` → 404. Keys with slashes travel in ``?path=``, ids in
``?id=``. Lists are pages ``{"items", "total", "limit", "offset"}`` sliced in
SQL. Additive changes stay ``v1``; a rename or removal is ``/api/v2``.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePath, PurePosixPath
from typing import TYPE_CHECKING, Any, Callable, Protocol

from tag_file_system.core.interface.action import (
    FileHistory,
    ProblemRecord,
    RunRecord,
    RunStatus,
    Severity,
)
from tag_file_system.core.interface.file_metadata import TaggedFile
from tag_file_system.core.paths import has_parent_reference, is_anchored, posix_key
from tag_file_system.functions import functions_payload
from tag_file_system.root import FUNCTIONS_FILE, same_name
from tag_file_system.services.plan import PLAN_LIMIT, PLAN_MAX
from tag_file_system.services.tagging import TaggingParser
from tag_file_system.version import COMMIT, VERSION

if TYPE_CHECKING:  # pragma: no cover
    from tag_file_system.services.daemon import Daemon
    from tag_file_system.services.ui import UiFiles

API_VERSION = "v1"
API_PREFIX = f"/api/{API_VERSION}"
PAGE_DEFAULT = 50
PAGE_MAX = 500

Query = dict[str, list[str]]
Handler = Callable[[Query], Any]


class BadRequest(ValueError):
    """A request parameter the server rejects (HTTP 400)."""


class NotFound(LookupError):
    """The resource a request names does not exist (HTTP 404)."""


class Conflict(RuntimeError):
    """The request is well-formed but the state refuses it (HTTP 409): a run
    that is not in flight, a file that exists, a retry the runner declines."""


@dataclass(frozen=True)
class FileResponse:
    """A handler's answer that is a file's bytes, not JSON (DESIGN/v0-5-0.md
    §12.1): the control server streams it with these headers."""

    path: Path
    content_type: str
    filename: str
    size: int


class ByteReader(Protocol):
    """What an upload reads from: the request's ``rfile``, or a ``BytesIO``."""

    def read(self, size: int = ..., /) -> bytes: ...


# A handler that reads the request body itself (an upload): (query, body
# stream, Content-Length) -> payload.
RawHandler = Callable[[Query, ByteReader, int], Any]

_PARSER = TaggingParser()


# ---------------------------------------------------------------- parsing

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


def param(query: Query, name: str) -> str | None:
    """The first value of ``name``, or ``None`` when absent."""
    values = query.get(name)
    return values[0] if values else None


def parse_file_key(query: Query, name: str = "path") -> str:
    """``?path=`` as a root-relative POSIX key: a blank, anchored or escaping
    path, or the configuration file's own name, is a 400."""
    text = (param(query, name) or "").strip()
    if not text:
        raise BadRequest(f"{name} is required")
    if is_anchored(text) or has_parent_reference(text):
        raise BadRequest(f"{text!r} is not a root-relative path")
    try:
        key = posix_key(text)
    except ValueError as e:
        raise BadRequest(str(e)) from e
    if key == ".":
        raise BadRequest(f"{name} must name a file, not the root")
    if same_name(PurePosixPath(key).name, FUNCTIONS_FILE):
        raise BadRequest(f"{key} is a configuration file, not a data file")
    return key


def parse_data_key(query: Query, name: str = "path") -> str:
    """A root-relative key that may name a directory (``mkdir``, ``rm -r``,
    a copy's destination): ``parse_file_key`` without the "not the root"
    rule for directories — the root itself is still refused."""
    text = (param(query, name) or "").strip()
    if not text:
        raise BadRequest(f"{name} is required")
    if is_anchored(text) or has_parent_reference(text):
        raise BadRequest(f"{text!r} is not a root-relative path")
    try:
        key = posix_key(text)
    except ValueError as e:
        raise BadRequest(str(e)) from e
    if key == ".":
        raise BadRequest(f"{name} must name a path below the root")
    if same_name(PurePosixPath(key).name, FUNCTIONS_FILE):
        raise BadRequest(f"{key} is a configuration file, not a data file")
    return key


def list_param(query: Query, name: str) -> list[str]:
    """``?add=a&add=b``, or a JSON list the body put in the query as text
    (``_read_body``): the names, in order, blanks dropped."""
    out: list[str] = []
    for value in query.get(name) or []:
        text = value.strip()
        if text.startswith("["):
            try:
                items = json.loads(text)
            except ValueError:
                raise BadRequest(f"{name} must be names or a JSON list") from None
            if not isinstance(items, list) or not all(
                isinstance(i, str) for i in items
            ):
                raise BadRequest(f"{name} must be a JSON list of strings")
            out.extend(i.strip() for i in items if i.strip())
        elif text:
            out.append(text)
    return out


def parse_id(query: Query) -> str:
    value = (param(query, "id") or "").strip()
    if not value:
        raise BadRequest("id is required")
    return value


def parse_int(name: str, value: str) -> int:
    try:
        return int(value.strip())
    except ValueError:
        raise BadRequest(f"{name} must be an integer, got {value!r}") from None


def parse_page(query: Query) -> tuple[int, int]:
    """``limit`` (default 50, at most 500) and ``offset`` (default 0)."""
    raw_limit = param(query, "limit")
    raw_offset = param(query, "offset")
    limit = PAGE_DEFAULT if raw_limit is None else parse_int("limit", raw_limit)
    offset = 0 if raw_offset is None else parse_int("offset", raw_offset)
    if limit < 1 or limit > PAGE_MAX:
        raise BadRequest(f"limit must be between 1 and {PAGE_MAX}, got {limit}")
    if offset < 0:
        raise BadRequest(f"offset must be 0 or more, got {offset}")
    return limit, offset


def parse_iso(name: str, value: str) -> datetime:
    """An ISO 8601 timestamp; a naive one is read as UTC."""
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        raise BadRequest(
            f"{name} must be an ISO 8601 timestamp, got {value!r}"
        ) from None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def parse_enum(name: str, value: str, enum: type) -> Any:
    try:
        return enum(value.strip().lower())
    except ValueError:
        accepted = ", ".join(member.value for member in enum)  # type: ignore[attr-defined]
        raise BadRequest(f"{name} must be one of {accepted}, got {value!r}") from None


def parse_file_filters(query: Query) -> dict[str, Any]:
    """The filters ``/files`` and ``/api/v1/files`` share, as ``query_files``
    keyword arguments."""
    tags = query.get("tag") or []
    if any(not t.strip() for t in tags):
        raise BadRequest("tag cannot be blank")
    return {
        "tags": [t.strip() for t in tags] or None,
        "filename": param(query, "name") or None,
        "file_format": param(query, "format") or None,
        "mime_type": param(query, "mime") or None,
        "include_deleted": parse_flag("deleted", param(query, "deleted")),
        "path_prefix": validate_prefix(param(query, "prefix") or None),
    }


# --------------------------------------------------------------- payloads


def jsonable(value: Any) -> Any:
    """Plain JSON from records, models, paths and timestamps."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, PurePath):
        return value.as_posix()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
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
        payload["runs"] = [jsonable(r) for r in runs]
    return payload


def file_detail(file: TaggedFile) -> dict[str, Any]:
    return {
        **file_payload(file),
        "format": file.metadata.file_format if file.metadata else None,
        "mtime_ns": file.metadata.mtime_ns if file.metadata else None,
        # The tags the path itself spells: a client cannot remove those
        # (DESIGN/v0-5-0.md §12.2).
        "name_tags": _PARSER.parse_path(file.path).tag_names,
    }


def run_payload(run: RunRecord, path: str | None) -> dict[str, Any]:
    return {**run.model_dump(mode="json"), "path": path}


def problem_payload(problem: ProblemRecord, path: str | None) -> dict[str, Any]:
    return {**problem.model_dump(mode="json"), "path": path}


def page(items: list[Any], total: int, limit: int, offset: int) -> dict[str, Any]:
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def timeline_payload(history: FileHistory) -> list[dict[str, Any]]:
    return [
        {
            "at": entry.at.isoformat(),
            "kind": entry.kind,
            "record": jsonable(entry.record),
        }
        for entry in history.timeline
    ]


# ------------------------------------------------------------------- api


class Api:
    """The ``/api/v1`` handlers, each ``(query) -> payload``."""

    def __init__(self, daemon: "Daemon", ui: "UiFiles") -> None:
        self.daemon = daemon
        self.ui = ui

    def routes(self) -> dict[tuple[str, str], Handler]:
        return {
            ("GET", f"{API_PREFIX}/status"): self.status,
            ("GET", f"{API_PREFIX}/files"): self.files,
            ("GET", f"{API_PREFIX}/file"): self.file,
            ("GET", f"{API_PREFIX}/file/history"): self.file_history,
            ("GET", f"{API_PREFIX}/file/explain"): self.file_explain,
            ("GET", f"{API_PREFIX}/tags"): self.tags,
            ("GET", f"{API_PREFIX}/runs"): self.runs,
            ("GET", f"{API_PREFIX}/run"): self.run,
            ("GET", f"{API_PREFIX}/problems"): self.problems,
            ("GET", f"{API_PREFIX}/problem"): self.problem,
            ("GET", f"{API_PREFIX}/addons"): self.addons,
            ("GET", f"{API_PREFIX}/functions"): self.functions,
            ("GET", f"{API_PREFIX}/upgrades"): self.upgrades,
            # DESIGN/v0-5-0.md §11.7: the operator's controls and the file
            # commands. POST where state changes; the UI never calls them.
            ("GET", f"{API_PREFIX}/plan"): self.plan,
            ("GET", f"{API_PREFIX}/queue"): self.queue,
            ("GET", f"{API_PREFIX}/doctor"): self.doctor,
            ("POST", f"{API_PREFIX}/pause"): self.pause,
            ("POST", f"{API_PREFIX}/resume"): self.resume,
            ("POST", f"{API_PREFIX}/run/retry"): self.run_retry,
            ("POST", f"{API_PREFIX}/run/cancel"): self.run_cancel,
            ("POST", f"{API_PREFIX}/rerun"): self.rerun,
            ("POST", f"{API_PREFIX}/files/touch"): self.files_touch,
            ("POST", f"{API_PREFIX}/files/copy"): self.files_copy,
            ("POST", f"{API_PREFIX}/files/move"): self.files_move,
            ("POST", f"{API_PREFIX}/files/remove"): self.files_remove,
            ("POST", f"{API_PREFIX}/files/mkdir"): self.files_mkdir,
            # DESIGN/v0-5-0.md §12: the browser's writes.
            ("GET", f"{API_PREFIX}/file/content"): self.file_content,
            ("POST", f"{API_PREFIX}/file/tags"): self.file_tags,
        }

    def raw_routes(self) -> dict[tuple[str, str], RawHandler]:
        """Routes whose body is not JSON: the upload's is the file."""
        return {("POST", f"{API_PREFIX}/files/upload"): self.files_upload}

    # -- helpers

    def _paths(self, records: list[Any]) -> dict[str, str]:
        return self.daemon.backend.query_paths(
            [r.file_id for r in records if getattr(r, "file_id", None)]
        )

    # -- endpoints

    def status(self, query: Query) -> dict[str, Any]:
        functions = self.daemon.functions
        return {
            **self.daemon.status(),
            "api": API_VERSION,
            "functions": {
                "files": len(functions.files),
                "problems": len(functions.problems),
            },
            "ui": self.ui.describe(),
            **self.daemon.status_detail(),
        }

    # -- operations (DESIGN/v0-5-0.md §11)

    def _scope(self, query: Query) -> tuple[str | None, str | None]:
        """``path`` (one file) or ``prefix`` (a directory), never both."""
        key = parse_file_key(query) if param(query, "path") else None
        prefix = validate_prefix(param(query, "prefix") or None)
        if key is not None and prefix is not None:
            raise BadRequest("give path or prefix, not both")
        return key, prefix

    def plan(self, query: Query) -> dict[str, Any]:
        key, prefix = self._scope(query)
        raw_limit = param(query, "limit")
        limit = PLAN_LIMIT if raw_limit is None else parse_int("limit", raw_limit)
        if limit < 1 or limit > PLAN_MAX:
            raise BadRequest(f"limit must be between 1 and {PLAN_MAX}, got {limit}")
        return self.daemon.plan(key=key, prefix=prefix, limit=limit)

    def queue(self, query: Query) -> dict[str, Any]:
        raw_limit = param(query, "limit")
        limit = 20 if raw_limit is None else parse_int("limit", raw_limit)
        if limit < 0 or limit > PAGE_MAX:
            raise BadRequest(f"limit must be between 0 and {PAGE_MAX}, got {limit}")
        return self.daemon.queue_view(limit)

    def doctor(self, query: Query) -> dict[str, Any]:
        return self.daemon.doctor()

    def pause(self, query: Query) -> dict[str, Any]:
        return self.daemon.pause()

    def resume(self, query: Query) -> dict[str, Any]:
        return self.daemon.resume()

    def run_retry(self, query: Query) -> dict[str, Any]:
        return self.daemon.retry_run(parse_id(query))

    def run_cancel(self, query: Query) -> dict[str, Any]:
        return self.daemon.cancel_run(parse_id(query))

    def rerun(self, query: Query) -> dict[str, Any]:
        handler = (param(query, "handler") or "").strip()
        if not handler:
            raise BadRequest("handler is required (script.handler)")
        key, prefix = self._scope(query)
        return self.daemon.rerun(
            handler,
            key=key,
            prefix=prefix,
            failed=parse_flag("failed", param(query, "failed")),
            stale=parse_flag("stale", param(query, "stale")),
            dry=parse_flag("dry", param(query, "dry")),
            yes=parse_flag("yes", param(query, "yes")),
        )

    def files_touch(self, query: Query) -> dict[str, Any]:
        return self.daemon.touch(parse_file_key(query), param(query, "content"))

    def files_copy(self, query: Query) -> dict[str, Any]:
        return self.daemon.copy(
            parse_file_key(query, "src"), parse_data_key(query, "dst")
        )

    def files_move(self, query: Query) -> dict[str, Any]:
        return self.daemon.move(
            parse_file_key(query, "src"), parse_data_key(query, "dst")
        )

    def files_remove(self, query: Query) -> dict[str, Any]:
        return self.daemon.remove(
            parse_data_key(query, "path"),
            recursive=parse_flag("recursive", param(query, "recursive")),
        )

    def files_mkdir(self, query: Query) -> dict[str, Any]:
        return self.daemon.mkdir(parse_data_key(query, "path"))

    # -- the browser's writes (DESIGN/v0-5-0.md §12)

    def files_upload(
        self, query: Query, body: ByteReader, length: int
    ) -> dict[str, Any]:
        return self.daemon.upload(
            parse_file_key(query),
            body,
            length,
            overwrite=parse_flag("overwrite", param(query, "overwrite")),
        )

    def file_content(self, query: Query) -> FileResponse:
        return self.daemon.content(parse_file_key(query))

    def file_tags(self, query: Query) -> dict[str, Any]:
        return self.daemon.retag(
            parse_file_key(query),
            list_param(query, "add"),
            list_param(query, "remove"),
        )

    def files(self, query: Query) -> dict[str, Any]:
        filters = parse_file_filters(query)
        newest = parse_flag("newest", param(query, "newest"))
        limit, offset = parse_page(query)
        backend = self.daemon.backend
        rows = backend.query_files(
            **filters, newest_first=newest, limit=limit, offset=offset
        )
        total = backend.count_files(**filters)
        return page([file_payload(f) for f in rows], total, limit, offset)

    def file(self, query: Query) -> dict[str, Any]:
        key = parse_file_key(query)
        deleted = parse_flag("deleted", param(query, "deleted"))
        row = self.daemon.backend.query_file(key, include_deleted=deleted)
        if row is None:
            raise NotFound(f"no such file: {key}")
        return file_detail(row)

    def file_history(self, query: Query) -> dict[str, Any]:
        key = parse_file_key(query)
        history = self.daemon.store.file_history(key)
        row = self.daemon.backend.query_file(key, include_deleted=True)
        if history is None or row is None:
            raise NotFound(f"no such file: {key}")
        return {
            "file": file_payload(row),
            "timeline": timeline_payload(history),
            "events": jsonable(history.events),
            "runs": [run_payload(r, key) for r in history.runs],
            "provenance": jsonable(history.provenance),
            "problems": [problem_payload(p, key) for p in history.problems],
        }

    def file_explain(self, query: Query) -> dict[str, Any]:
        return self.daemon.explain(parse_file_key(query))

    def tags(self, query: Query) -> dict[str, Any]:
        deleted = parse_flag("deleted", param(query, "deleted"))
        items = [
            {
                "name": tag.name,
                "tag_id": tag.tag_id,
                "added": tag.time_added.isoformat(),
                "files": count,
            }
            for tag, count in self.daemon.backend.tag_counts(include_deleted=deleted)
        ]
        return {"items": items, "total": len(items)}

    def _run_filters(self, query: Query) -> dict[str, Any]:
        statuses = [parse_enum("status", s, RunStatus) for s in query.get("status", [])]
        since = param(query, "since")
        return {
            "file_path": parse_file_key(query) if param(query, "path") else None,
            "file_hash": param(query, "hash") or None,
            "action_name": param(query, "action") or None,
            "handler": param(query, "handler") or None,
            "status": statuses or None,
            "since": parse_iso("since", since) if since else None,
            "path_prefix": validate_prefix(param(query, "prefix") or None),
        }

    def runs(self, query: Query) -> dict[str, Any]:
        filters = self._run_filters(query)
        limit, offset = parse_page(query)
        store = self.daemon.store
        rows = store.query_runs(**filters, limit=limit, offset=offset)
        paths = self._paths(rows)
        items = [run_payload(r, paths.get(r.file_id or "")) for r in rows]
        return page(items, store.count_runs(**filters), limit, offset)

    def run(self, query: Query) -> dict[str, Any]:
        run_id = parse_id(query)
        store = self.daemon.store
        run = store.get_run(run_id)
        if run is None:
            raise NotFound(f"no such run: {run_id}")
        edges = store.query_provenance(run_id=run_id, include_deleted=True)
        problems = store.query_problems(run_id=run_id)
        paths = self._paths([run, *edges, *problems])
        return {
            "run": run_payload(run, paths.get(run.file_id or "")),
            "trace": jsonable(store.query_trace(run_id)),
            "produced": [
                {
                    "path": paths.get(edge.file_id),
                    "file_id": edge.file_id,
                    "kind": edge.kind.value,
                    "ambiguous": edge.ambiguous,
                    "created_at": edge.created_at.isoformat(),
                }
                for edge in edges
            ],
            "problems": [
                problem_payload(p, paths.get(p.file_id or "")) for p in problems
            ],
        }

    def _problem_filters(self, query: Query) -> dict[str, Any]:
        severity = param(query, "severity")
        since = param(query, "since")
        return {
            "at_least": parse_enum("severity", severity, Severity)
            if severity
            else None,
            "since": parse_iso("since", since) if since else None,
            "undelivered_only": parse_flag("undelivered", param(query, "undelivered")),
            "kind": param(query, "kind") or None,
            "action_name": param(query, "action") or None,
            "file_path": parse_file_key(query) if param(query, "path") else None,
            "run_id": param(query, "run") or None,
        }

    def problems(self, query: Query) -> dict[str, Any]:
        filters = self._problem_filters(query)
        limit, offset = parse_page(query)
        store = self.daemon.store
        rows = store.query_problems(
            **filters, limit=limit, offset=offset, newest_first=True
        )
        paths = self._paths(rows)
        items = [problem_payload(p, paths.get(p.file_id or "")) for p in rows]
        return page(items, store.count_problems(**filters), limit, offset)

    def problem(self, query: Query) -> dict[str, Any]:
        problem_id = parse_id(query)
        problem = self.daemon.store.get_problem(problem_id)
        if problem is None:
            raise NotFound(f"no such problem: {problem_id}")
        return problem_payload(
            problem, self._paths([problem]).get(problem.file_id or "")
        )

    def addons(self, query: Query) -> dict[str, Any]:
        return {"version": VERSION, "hash": COMMIT, **self.daemon.actions_view()}

    def functions(self, query: Query) -> dict[str, Any]:
        return functions_payload(self.daemon.functions)

    def upgrades(self, query: Query) -> dict[str, Any]:
        raw_limit = param(query, "limit")
        limit = PAGE_DEFAULT if raw_limit is None else parse_int("limit", raw_limit)
        if limit < 1 or limit > PAGE_MAX:
            raise BadRequest(f"limit must be between 1 and {PAGE_MAX}, got {limit}")
        store = self.daemon.store
        items = jsonable(store.query_upgrades(limit))
        return {"items": items, "total": len(store.query_upgrades())}
