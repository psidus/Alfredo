"""Typed views over the Alfredo Control API payloads.

The server returns plain JSON; these dataclasses give the mobile app:

* discoverable field names (no stringly-typed dict lookups),
* tolerant parsing (unknown/missing fields never crash the client),
* small helpers for UI state (``needs_approval``, ``is_finished``, ...).

Every model keeps the original payload in ``raw`` so the app can render
server-side fields that are not modelled yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional


def _as_dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _opt_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


@dataclass
class WorkflowInput:
    """One declared input of a saved workflow."""

    key: str
    prompt: str = ""

    @classmethod
    def from_dict(cls, data: Any) -> "WorkflowInput":
        if isinstance(data, Mapping):
            return cls(key=_str(data.get("key")), prompt=_str(data.get("prompt")))
        return cls(key=_str(data))

    def to_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "prompt": self.prompt}


@dataclass
class WorkflowCard:
    """A workflow as shown in the mobile catalogue."""

    id: int
    name: str = ""
    description: str = ""
    inputs: List[WorkflowInput] = field(default_factory=list)
    expected_exports: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WorkflowCard":
        raw = _as_dict(data)
        return cls(
            id=int(raw.get("id") or 0),
            name=_str(raw.get("name")),
            description=_str(raw.get("description")),
            inputs=[WorkflowInput.from_dict(i) for i in _as_list(raw.get("inputs"))],
            expected_exports=[_str(e) for e in _as_list(raw.get("expected_exports")) if _str(e)],
            raw=raw,
        )

    @property
    def input_keys(self) -> List[str]:
        return [i.key for i in self.inputs]

    def missing_inputs(self, provided: Iterable[str] | Mapping[str, Any]) -> List[str]:
        """Keys still needed before the run can start (order preserved)."""
        given = set(provided or ())
        return [k for k in self.input_keys if k not in given]


@dataclass
class WorkflowSuggestion:
    """A workflow the decision agent matched to a chat message."""

    workflow_id: int
    name: str = ""
    text: str = ""
    inputs: Dict[str, Any] = field(default_factory=dict)
    missing_inputs: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Any) -> Optional["WorkflowSuggestion"]:
        if not isinstance(data, Mapping):
            return None
        raw = _as_dict(data)
        wid = _opt_int(raw.get("workflow_id"))
        if wid is None:
            return None
        return cls(
            workflow_id=wid,
            name=_str(raw.get("name")),
            text=_str(raw.get("text")),
            inputs=_as_dict(raw.get("inputs")),
            missing_inputs=[_str(m) for m in _as_list(raw.get("missing_inputs"))],
            raw=raw,
        )

    @property
    def ready_to_run(self) -> bool:
        return not self.missing_inputs


@dataclass
class ChatReply:
    """Result of ``POST /api/assistant/chat``."""

    reply: str = ""
    workflow: Optional[WorkflowSuggestion] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Any) -> "ChatReply":
        raw = _as_dict(data)
        return cls(
            reply=_str(raw.get("reply")),
            workflow=WorkflowSuggestion.from_dict(raw.get("workflow")),
            raw=raw,
        )


@dataclass
class Decision:
    """Result of ``POST /api/assistant/decide``."""

    text: str = ""
    action: str = "answer"
    workflow_id: Optional[int] = None
    inputs: Dict[str, Any] = field(default_factory=dict)
    missing_inputs: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Any) -> "Decision":
        raw = _as_dict(data)
        return cls(
            text=_str(raw.get("text")),
            action=_str(raw.get("action"), "answer") or "answer",
            workflow_id=_opt_int(raw.get("workflow_id")),
            inputs=_as_dict(raw.get("inputs")),
            missing_inputs=[_str(m) for m in _as_list(raw.get("missing_inputs"))],
            raw=raw,
        )

    @property
    def wants_run(self) -> bool:
        return self.action == "run_workflow" and self.workflow_id is not None


@dataclass
class RunStart:
    """Result of ``POST /api/runs``."""

    run_id: int
    status: str = "running"
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Any) -> "RunStart":
        raw = _as_dict(data)
        run_id = _opt_int(raw.get("run_id"))
        if run_id is None:
            raise ValueError("run response without run_id")
        return cls(run_id=run_id, status=_str(raw.get("status"), "running"), raw=raw)


TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "canceled"})


@dataclass
class RunSummary:
    """Compact run row from ``GET /api/runs``."""

    run_id: int
    workflow_id: Optional[int] = None
    status: str = ""
    source: str = ""
    started_at: str = ""
    preview: str = ""
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Any) -> "RunSummary":
        raw = _as_dict(data)
        return cls(
            run_id=int(raw.get("run_id") or 0),
            workflow_id=_opt_int(raw.get("workflow_id")),
            status=_str(raw.get("status")),
            source=_str(raw.get("source")),
            started_at=_str(raw.get("started_at")),
            preview=_str(raw.get("preview")),
            raw=raw,
        )

    @property
    def is_finished(self) -> bool:
        return self.status.lower() in TERMINAL_STATUSES


@dataclass
class RunDetail:
    """Full run state from ``GET /api/runs/{id}``."""

    run_id: int
    workflow_id: Optional[int] = None
    status: str = ""
    source: str = ""
    started_at: str = ""
    inputs: Dict[str, Any] = field(default_factory=dict)
    result: str = ""
    exports: List[str] = field(default_factory=list)
    procedure_summary: Optional[str] = None
    handoffs: List[Dict[str, Any]] = field(default_factory=list)
    pending_interrupt_id: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Any) -> "RunDetail":
        raw = _as_dict(data)
        return cls(
            run_id=int(raw.get("run_id") or 0),
            workflow_id=_opt_int(raw.get("workflow_id")),
            status=_str(raw.get("status")),
            source=_str(raw.get("source")),
            started_at=_str(raw.get("started_at")),
            inputs=_as_dict(raw.get("inputs")),
            result=_str(raw.get("result")),
            exports=[_str(e) for e in _as_list(raw.get("exports")) if _str(e)],
            procedure_summary=raw.get("procedure_summary"),
            handoffs=[_as_dict(h) for h in _as_list(raw.get("handoffs"))],
            pending_interrupt_id=_opt_int(raw.get("pending_interrupt_id")),
            raw=raw,
        )

    @property
    def is_finished(self) -> bool:
        return self.status.lower() in TERMINAL_STATUSES

    @property
    def needs_approval(self) -> bool:
        return self.pending_interrupt_id is not None or self.status.lower() == "waiting_human"

    @property
    def is_failed(self) -> bool:
        return self.status.lower() == "failed"


@dataclass
class Interrupt:
    """Pending human approval from ``GET /api/interrupts``."""

    id: int
    run_id: int
    node_id: str = ""
    question: str = ""
    options: List[str] = field(default_factory=list)
    created_at: str = ""
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Any) -> "Interrupt":
        raw = _as_dict(data)
        return cls(
            id=int(raw.get("id") or 0),
            run_id=int(raw.get("run_id") or 0),
            node_id=_str(raw.get("node_id")),
            question=_str(raw.get("question")),
            options=[_str(o) for o in _as_list(raw.get("options")) if _str(o)],
            created_at=_str(raw.get("created_at")),
            raw=raw,
        )

    @property
    def is_yes_no(self) -> bool:
        lowered = {o.strip().lower() for o in self.options}
        return bool(lowered) and lowered <= {"yes", "no", "sì", "si"}


@dataclass
class InterruptOutcome:
    """Result of ``POST /api/interrupts/{id}/answer``."""

    status: str = ""
    run_id: Optional[int] = None
    execution: str = ""
    error: str = ""
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Any) -> "InterruptOutcome":
        raw = _as_dict(data)
        return cls(
            status=_str(raw.get("status")),
            run_id=_opt_int(raw.get("run_id")),
            execution=_str(raw.get("execution")),
            error=_str(raw.get("error")),
            raw=raw,
        )

    @property
    def resumed(self) -> bool:
        return self.status in ("resumed", "completed", "paused") or self.execution == "started"


@dataclass
class RunEvent:
    """One entry of the append-only run log (``GET /api/runs/{id}/events``)."""

    run_id: int
    seq: int
    type: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RunEvent":
        raw = _as_dict(data)
        return cls(
            run_id=int(raw.get("run_id") or 0),
            seq=int(raw.get("seq") or 0),
            type=_str(raw.get("type")),
            payload=_as_dict(raw.get("payload")),
            created_at=_str(raw.get("created_at")),
            raw=raw,
        )


@dataclass
class RunEventPage:
    """A page of run events plus the cursor to ask for the next ones."""

    run_id: int
    events: List[RunEvent] = field(default_factory=list)
    latest_seq: int = 0
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Any) -> "RunEventPage":
        raw = _as_dict(data)
        events = [RunEvent.from_dict(e) for e in _as_list(raw.get("events"))]
        latest = _opt_int(raw.get("latest_seq"))
        if latest is None:
            latest = max((e.seq for e in events), default=0)
        return cls(
            run_id=int(raw.get("run_id") or 0),
            events=events,
            latest_seq=latest,
            raw=raw,
        )


@dataclass
class RunFile:
    """One workspace file, either listed or produced by a run."""

    name: str
    path: str = ""
    size: Optional[int] = None
    kind: str = "binary"
    mime: str = ""
    modified: str = ""
    is_dir: bool = False
    accessible: bool = True
    exists: bool = True
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Any) -> "RunFile":
        raw = _as_dict(data)
        return cls(
            name=_str(raw.get("name")),
            path=_str(raw.get("path")),
            size=_opt_int(raw.get("size")),
            kind=_str(raw.get("kind"), "binary") or "binary",
            mime=_str(raw.get("mime")),
            modified=_str(raw.get("modified")),
            is_dir=bool(raw.get("is_dir")),
            accessible=bool(raw.get("accessible", True)),
            exists=bool(raw.get("exists", True)),
            raw=raw,
        )

    @property
    def downloadable(self) -> bool:
        return self.accessible and self.exists and bool(self.path) and not self.is_dir


@dataclass
class FilePreview:
    """Server-side preview of a workspace file."""

    name: str = ""
    path: str = ""
    kind: str = "binary"
    size: Optional[int] = None
    text: Optional[str] = None
    rows: List[List[str]] = field(default_factory=list)
    sheets: List[str] = field(default_factory=list)
    data_uri: Optional[str] = None
    pages: Optional[int] = None
    truncated: bool = False
    message: Optional[str] = None
    error: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Any) -> "FilePreview":
        raw = _as_dict(data)
        rows = [[_str(cell) for cell in row] for row in _as_list(raw.get("rows"))]
        return cls(
            name=_str(raw.get("name")),
            path=_str(raw.get("path")),
            kind=_str(raw.get("kind"), "binary") or "binary",
            size=_opt_int(raw.get("size")),
            text=raw.get("text") if isinstance(raw.get("text"), str) else None,
            rows=rows,
            sheets=[_str(s) for s in _as_list(raw.get("sheets"))],
            data_uri=raw.get("data_uri") if isinstance(raw.get("data_uri"), str) else None,
            pages=_opt_int(raw.get("pages")),
            truncated=bool(raw.get("truncated")),
            message=raw.get("message") if isinstance(raw.get("message"), str) else None,
            error=raw.get("error") if isinstance(raw.get("error"), str) else None,
            raw=raw,
        )


@dataclass
class ShareLink:
    """A signed, expiring download URL for one file."""

    token: str = ""
    url: str = ""
    path: str = ""
    expires_at: str = ""
    ttl_seconds: int = 0
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Any) -> "ShareLink":
        raw = _as_dict(data)
        return cls(
            token=_str(raw.get("token")),
            url=_str(raw.get("url")),
            path=_str(raw.get("path")),
            expires_at=_str(raw.get("expires_at")),
            ttl_seconds=int(raw.get("ttl_seconds") or 0),
            raw=raw,
        )


@dataclass
class UploadSession:
    """A resumable chunked upload (``POST /api/files/upload``)."""

    upload_id: str = ""
    path: str = ""
    size: int = 0
    chunk_size: int = 0
    chunks: int = 0
    received: List[int] = field(default_factory=list)
    expires_at: str = ""
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Any) -> "UploadSession":
        raw = _as_dict(data)
        return cls(
            upload_id=_str(raw.get("upload_id")),
            path=_str(raw.get("path")),
            size=int(raw.get("size") or 0),
            chunk_size=int(raw.get("chunk_size") or 0),
            chunks=int(raw.get("chunks") or 0),
            received=[int(i) for i in _as_list(raw.get("received"))],
            expires_at=_str(raw.get("expires_at")),
            raw=raw,
        )

    @property
    def missing(self) -> List[int]:
        """Chunk indexes still to send (useful to resume after a reconnect)."""
        received = set(self.received)
        return [index for index in range(self.chunks) if index not in received]

    @property
    def is_complete(self) -> bool:
        return self.chunks > 0 and not self.missing


@dataclass
class SystemStatus:
    """GPU gate snapshot (``GET /api/system/status``)."""

    gpu: Dict[str, Any] = field(default_factory=dict)
    profiles: Dict[str, Any] = field(default_factory=dict)
    queue: Dict[str, Any] = field(default_factory=dict)
    metrics: Dict[str, Any] = field(default_factory=dict)
    loaded_model: str = ""
    preload_error: str = ""
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Any) -> "SystemStatus":
        raw = _as_dict(data)
        return cls(
            gpu=_as_dict(raw.get("gpu")),
            profiles=_as_dict(raw.get("profiles")),
            queue=_as_dict(raw.get("queue")),
            metrics=_as_dict(raw.get("metrics")),
            loaded_model=_str(raw.get("loaded_model")),
            preload_error=_str(raw.get("preload_error")),
            raw=raw,
        )

    @property
    def waiting(self) -> List[Dict[str, Any]]:
        return [_as_dict(item) for item in _as_list(self.queue.get("waiting"))]

    @property
    def waiting_count(self) -> int:
        return len(self.waiting)

    @property
    def current_run_id(self) -> Optional[int]:
        return _opt_int(_as_dict(self.queue.get("current")).get("run_id"))

    @property
    def gpu_free_mb(self) -> Optional[int]:
        return _opt_int(self.gpu.get("free_mb"))

    @property
    def gpu_available(self) -> bool:
        return bool(self.gpu.get("available"))


