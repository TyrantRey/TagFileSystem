# Code by AkinoAlice@TyrantRey

"""Records of the action layer (DESIGN/v0-1-0.md §6–§7): actions, runs, traces,
provenance edges and problems. Pure data; the ``ActionStore`` persists them.
"""

import base64
import dataclasses
import json
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum, StrEnum
from pathlib import PurePath, PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Hook(StrEnum):
    """Which add-on entry point a run went through."""

    ADDED = "added"
    MODIFIED = "modified"
    REMOVED = "removed"
    TAGGED = "tagged"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"
    INTERRUPTED = "interrupted"

    @property
    def is_final(self) -> bool:
        return self not in (RunStatus.QUEUED, RunStatus.RUNNING)


class RunSource(StrEnum):
    WATCH = "watch"
    RECONCILE = "reconcile"
    RETRY = "retry"
    CHAIN = "chain"


class Severity(StrEnum):
    """P0..P3. Handlers subscribe to a level *and above* (DESIGN/v0-1-0.md §6.4)."""

    CRIT = "crit"
    ERR = "err"
    WARN = "warn"
    INFO = "info"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]

    def covers(self, other: "Severity") -> bool:
        """Whether a handler at this level receives ``other``."""
        return other.rank <= self.rank


_SEVERITY_RANK = {
    Severity.CRIT: 0,
    Severity.ERR: 1,
    Severity.WARN: 2,
    Severity.INFO: 3,
}


class TraceKind(StrEnum):
    LOG = "log"
    FS_COPY = "fs.copy"
    FS_MOVE = "fs.move"
    FS_WRITE = "fs.write"
    FS_DELETE = "fs.delete"
    EMIT = "emit"
    RECORD = "record"
    OBSERVED = "observed"


class ProvenanceKind(StrEnum):
    EMITTED = "emitted"  # written through ctx: authoritative
    OBSERVED = "observed"  # seen by the watcher while the run was in flight


def _encode(value: Any) -> Any:
    """Turn the values add-ons commonly return into portable JSON.

    Deterministic (sets are sorted) and host-independent (paths are POSIX);
    anything else is a ``TypeError`` rather than a ``repr`` smuggled into a
    JSON string.
    """
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, PurePath):
        return value.as_posix()
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=lambda item: canonical_json(item))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"__bytes__": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, (tuple, list)):
        return list(value)
    raise TypeError(f"{type(value).__name__} is not JSON-serializable")


def canonical_json(value: Any) -> str:
    """Stable JSON for keys and comparisons: sorted keys, no whitespace,
    strict (``nan``/``inf`` rejected), see ``_encode`` for extra types."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_encode,
    )


class ActionRecord(BaseModel):
    """One loaded add-on version: ``(name, script_hash)`` is unique."""

    id: str
    name: str
    script_path: PurePosixPath
    script_hash: str
    signature: dict[str, Any]  # JSON Schema of the handler's args
    hooks: list[Hook]
    loaded_at: datetime


class RunKey(BaseModel):
    """Identity of a run (DESIGN/v0-1-0.md §6.1): a run starts iff no row of any
    status has this key. Equality and hashing go through the canonical
    ``args_json``, so two keys are equal exactly when the store would match
    them."""

    model_config = ConfigDict(frozen=True)

    file_hash: str
    action_name: str
    hook: Hook
    args: dict[str, Any] = Field(default_factory=dict)

    @field_validator("args")
    @classmethod
    def validate_args(cls, v: dict[str, Any]) -> dict[str, Any]:
        # Fail at construction, not inside __eq__/__hash__: NaN/inf and
        # unsupported values could never form a key.
        canonical_json(v)
        return v

    @property
    def args_json(self) -> str:
        return canonical_json(self.args)

    def _identity(self) -> tuple[str, str, str, str]:
        return (self.file_hash, self.action_name, self.hook.value, self.args_json)

    def __hash__(self) -> int:
        return hash(self._identity())

    def __eq__(self, other: object) -> bool:
        return isinstance(other, RunKey) and other._identity() == self._identity()


class RunRecord(BaseModel):
    id: str
    action_id: str
    action_name: str
    hook: Hook
    file_id: str | None
    file_hash: str
    slug: str
    args: dict[str, Any]
    result: Any = None
    status: RunStatus
    error: str | None = None
    source: RunSource
    parent_run_id: str | None = None
    retry_of: str | None = None
    started_at: datetime
    finished_at: datetime | None = None

    @property
    def key(self) -> RunKey:
        return RunKey(
            file_hash=self.file_hash,
            action_name=self.action_name,
            hook=self.hook,
            args=self.args,
        )


class TraceEntry(BaseModel):
    run_id: str
    seq: int
    ts: datetime
    kind: str  # a TraceKind value, or an add-on defined kind
    payload: Any


class ProvenanceRecord(BaseModel):
    file_id: str
    run_id: str
    kind: ProvenanceKind
    ambiguous: bool = False
    created_at: datetime


class ProblemRecord(BaseModel):
    id: str
    severity: Severity
    kind: str
    message: str
    action_name: str | None = None
    file_id: str | None = None
    run_id: str | None = None
    occurred_at: datetime
    delivered_at: datetime | None = None


class EventRecord(BaseModel):
    """A row of the legacy ``events`` audit table."""

    id: str
    name: str
    description: str | None = None
    file_id: str | None = None
    tag_id: str | None = None
    tag_name: str | None = None
    occurred_at: datetime


class TimelineEntry(BaseModel):
    """One thing that happened to a file, for the merged timeline."""

    at: datetime
    kind: str  # event | run | provenance | problem
    record: EventRecord | RunRecord | ProvenanceRecord | ProblemRecord


class FileHistory(BaseModel):
    """Everything that happened to one file, oldest first (DESIGN/v0-1-0.md §7,
    "what happened to file X")."""

    file_id: str
    events: list[EventRecord]
    runs: list[RunRecord]
    provenance: list[ProvenanceRecord]
    problems: list[ProblemRecord]

    @property
    def timeline(self) -> list[TimelineEntry]:
        """All four lists merged, oldest first."""
        entries = [
            *(
                TimelineEntry(at=e.occurred_at, kind="event", record=e)
                for e in self.events
            ),
            *(TimelineEntry(at=r.started_at, kind="run", record=r) for r in self.runs),
            *(
                TimelineEntry(at=p.created_at, kind="provenance", record=p)
                for p in self.provenance
            ),
            *(
                TimelineEntry(at=p.occurred_at, kind="problem", record=p)
                for p in self.problems
            ),
        ]
        return sorted(entries, key=lambda entry: entry.at)
