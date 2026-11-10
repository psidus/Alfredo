# core/api_models.py
"""
Pydantic models for the Alfredo Workflow API.
Defines request/response schemas for external app integration.
"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any


class TriggerRequest(BaseModel):
    """Request body for triggering a workflow execution."""
    workflow_id: int = Field(..., description="ID of the workflow to execute")
    inputs: Dict[str, Any] = Field(default_factory=dict, description="Input data for the workflow (key-value pairs matching required_inputs)")
    context: Dict[str, Any] = Field(default_factory=dict, description="Optional additional context for the workflow")


class TriggerResponse(BaseModel):
    """Response after successfully triggering a workflow."""
    job_id: int = Field(..., description="Unique ID of the workflow run (for polling)")
    status: str = Field(default="running", description="Initial status of the job")
    message: str = Field(default="Workflow execution started", description="Human-readable confirmation")


class JobStatusResponse(BaseModel):
    """Response for polling a job's current status."""
    job_id: int
    status: str = Field(..., description="'running', 'completed', or 'failed'")
    result: Optional[str] = Field(None, description="Final textual output when status is 'completed'")
    procedure_summary: Optional[str] = Field(None, description="Structured summary of the workflow procedure")
    exports: Optional[List[str]] = Field(None, description="Paths of generated export files")
    handoffs: Optional[List[Dict[str, Any]]] = Field(None, description="Per-node handoff metadata (memory keys / files)")
    progress: Optional[str] = Field(None, description="Current task progress info")
    error: Optional[str] = Field(None, description="Error message if status is 'failed'")


class RequiredInput(BaseModel):
    """Schema for a single required input of a workflow."""
    key: str = Field(..., description="The input variable name")
    prompt: str = Field(default="", description="Human-readable description of what this input expects")


class WorkflowInfo(BaseModel):
    """Public info about a workflow, returned to the SDK."""
    id: int
    name: str
    description: str = Field(default="", description="Workflow description (derived from first task)")
    required_inputs: List[RequiredInput] = Field(default_factory=list, description="Inputs the widget must map to DOM elements")
    has_output: bool = Field(default=True, description="Whether the workflow produces visible output")
    expected_exports: List[str] = Field(default_factory=list, description="Configured export formats for this workflow")


class AppInfo(BaseModel):
    """Public info about a connected app."""
    name: str
    display_name: str
    description: str = ""
    status: str = "active"


class HealthResponse(BaseModel):
    """Health check response."""
    status: str = "ok"
    version: str = "1.1.0"
    api_active: bool = True


# =============================================================================
# Control API (mobile app / remote dashboard)
# =============================================================================

class ChatRequest(BaseModel):
    """Free-form chat turn from the mobile app."""
    message: str = Field(..., description="User message")
    history: List[Dict[str, Any]] = Field(
        default_factory=list, description="Previous turns: [{role, content}]"
    )
    auto_route: bool = Field(
        default=True, description="Also try to route the message to a saved workflow"
    )


class WorkflowSuggestion(BaseModel):
    """A workflow the decision agent matched to the user's message."""
    workflow_id: int
    name: str = ""
    text: str = ""
    inputs: Dict[str, Any] = Field(default_factory=dict)
    missing_inputs: List[str] = Field(default_factory=list)


class ChatResponse(BaseModel):
    reply: str = ""
    workflow: Optional[WorkflowSuggestion] = None


class DecideRequest(BaseModel):
    message: str
    history: List[Dict[str, Any]] = Field(default_factory=list)


class DecideResponse(BaseModel):
    text: str = ""
    action: str = Field(default="answer", description="'answer', 'run_workflow' or 'clarify'")
    workflow_id: Optional[int] = None
    inputs: Dict[str, Any] = Field(default_factory=dict)
    missing_inputs: List[str] = Field(default_factory=list)


class StartRunRequest(BaseModel):
    workflow_id: int
    inputs: Dict[str, Any] = Field(default_factory=dict)
    source: str = Field(default="mobile", description="Origin of the run")


class StartRunResponse(BaseModel):
    run_id: int
    status: str = "running"


class AnswerInterruptRequest(BaseModel):
    answer: str = Field(..., description="Option text, index, or free-form feedback")
    execute: bool = Field(
        default=True, description="Resume the run immediately after applying the answer"
    )


# =============================================================================
# Web Push (approval notifications for the mobile PWA)
# =============================================================================

class PushKeys(BaseModel):
    p256dh: str = Field(..., description="Browser public key (base64url)")
    auth: str = Field(..., description="Browser auth secret (base64url)")


class PushSubscribeRequest(BaseModel):
    endpoint: str = Field(..., description="Push service endpoint URL")
    keys: PushKeys
    user_agent: str = Field(default="", description="Optional device label")


class PushUnsubscribeRequest(BaseModel):
    endpoint: str = Field(..., description="Subscription endpoint to remove")


class PushKeyResponse(BaseModel):
    public_key: str = Field(..., description="VAPID public key (base64url)")
    enabled: bool = Field(
        default=False, description="False when the server cannot send pushes yet"
    )
    subject: str = Field(default="", description="VAPID subject claim")


# =============================================================================
# Run event log
# =============================================================================

class RunEvent(BaseModel):
    id: Optional[int] = None
    run_id: int
    seq: int = Field(..., description="Dense per-run sequence number")
    type: str = Field(..., description="e.g. 'run.started', 'run.node.completed'")
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""


class RunEventPage(BaseModel):
    run_id: int
    events: List[RunEvent] = Field(default_factory=list)
    latest_seq: int = Field(default=0, description="Ask again with after_seq=latest_seq")


# =============================================================================
# Files and artifacts (workspace, read-only)
# =============================================================================

class FileEntry(BaseModel):
    name: str
    path: str = ""
    is_dir: bool = False
    size: Optional[int] = None
    modified: str = ""
    kind: str = "binary"
    mime: str = ""
    accessible: bool = True
    exists: bool = True


class FileListing(BaseModel):
    path: str = ""
    entries: List[FileEntry] = Field(default_factory=list)
    truncated: bool = False


class FilePreview(BaseModel):
    name: str
    path: str = ""
    size: Optional[int] = None
    modified: str = ""
    kind: str = "binary"
    mime: str = ""
    text: Optional[str] = None
    rows: Optional[List[List[str]]] = None
    sheets: Optional[List[str]] = None
    data_uri: Optional[str] = None
    pages: Optional[int] = None
    truncated: bool = False
    message: Optional[str] = None
    error: Optional[str] = None


class ShareFileRequest(BaseModel):
    path: str = Field(..., description="Workspace-relative file path")
    ttl_seconds: int = Field(default=3600, ge=60, le=604800, description="Link lifetime")


class ShareFileResponse(BaseModel):
    token: str
    url: str
    path: str
    expires_at: str
    ttl_seconds: int


# =============================================================================
# Chunked uploads (workspace, write)
# =============================================================================

class UploadSessionRequest(BaseModel):
    path: str = Field(..., min_length=1, description="Workspace-relative destination path")
    size: int = Field(..., gt=0, description="Total file size in bytes")
    chunk_size: int = Field(default=1048576, ge=65536, le=8388608, description="Bytes per chunk")
    sha256: Optional[str] = Field(
        default=None,
        pattern=r"^[0-9a-fA-F]{64}$",
        description="Optional checksum of the whole file",
    )


class UploadSession(BaseModel):
    upload_id: str
    path: str
    size: int
    chunk_size: int
    chunks: int
    received: List[int] = Field(default_factory=list)
    complete: bool = False
    expires_at: str


class UploadChunkStatus(BaseModel):
    upload_id: str
    index: int
    received: List[int] = Field(default_factory=list)
    chunks: int
    complete: bool = False


class CompleteUploadRequest(BaseModel):
    sha256: Optional[str] = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")


# =============================================================================
# System / GPU status
# =============================================================================

class GpuInfo(BaseModel):
    available: bool = False
    index: Optional[int] = None
    name: str = ""
    total_mb: Optional[int] = None
    used_mb: Optional[int] = None
    free_mb: Optional[int] = None
    reserve_mb: int = 0
    error: Optional[str] = None


class GpuProfileStatus(BaseModel):
    name: str
    model: str
    context_tokens: int = 0
    estimated_vram_mb: int = 0
    description: str = ""
    fits: Optional[bool] = None
    headroom_mb: Optional[int] = None
    avg_call_s: Optional[float] = None


class GpuQueueCurrent(BaseModel):
    profile: str = ""
    model: str = ""
    run_id: Optional[int] = None
    started_at: Optional[str] = None
    elapsed_s: float = 0.0


class GpuQueueWaiting(BaseModel):
    position: int
    profile: str
    model: str = ""
    run_id: Optional[int] = None
    waiting_s: float = 0.0
    eta_s: Optional[float] = None


class GpuQueueState(BaseModel):
    current: Optional[GpuQueueCurrent] = None
    waiting: List[GpuQueueWaiting] = Field(default_factory=list)
    length: int = 0


class GpuFirstToken(BaseModel):
    count: int = 0
    avg_s: Optional[float] = None
    p95_s: Optional[float] = None
    last_s: Optional[float] = None


class GpuMetrics(BaseModel):
    served: int = 0
    rejected: int = 0
    timeouts: int = 0
    evictions: int = 0
    avg_wait_s: Optional[float] = None
    first_token: Dict[str, GpuFirstToken] = Field(default_factory=dict)


class SystemStatus(BaseModel):
    gpu: GpuInfo = Field(default_factory=GpuInfo)
    profiles: Dict[str, GpuProfileStatus] = Field(default_factory=dict)
    queue: GpuQueueState = Field(default_factory=GpuQueueState)
    metrics: GpuMetrics = Field(default_factory=GpuMetrics)
    loaded_model: Optional[str] = None
    preload_error: Optional[str] = None
