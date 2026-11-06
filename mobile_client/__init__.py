"""Mobile / remote client for the Alfredo Control API.

This package is deliberately dependency-free (standard library only): it can be
vendored into a Python mobile backend, a companion CLI, or used as executable
documentation for a native app (Flutter / React Native / Swift).

Typical use::

    from mobile_client import ControlClient

    client = ControlClient("http://192.168.1.20:8000", token="...")
    reply = client.chat("Ciao, cosa sai fare?")
    if reply.workflow:
        run = client.start_run(reply.workflow.workflow_id, reply.workflow.inputs)
        final = client.wait_for_run(run.run_id)

All endpoints require the ``X-Control-Token`` header, whose expected value is
the server-side ``ALFREDO_CONTROL_TOKEN``.
"""

from mobile_client.client import (
    ControlAuthError,
    ControlClient,
    ControlConflictError,
    ControlConnectionError,
    ControlError,
    ControlNotFoundError,
    ControlServerError,
    ControlValidationError,
)
from mobile_client.models import (
    ChatReply,
    Decision,
    FilePreview,
    Interrupt,
    InterruptOutcome,
    RunDetail,
    RunEvent,
    RunEventPage,
    RunFile,
    RunStart,
    RunSummary,
    ShareLink,
    SystemStatus,
    UploadSession,
    WorkflowCard,
    WorkflowInput,
    WorkflowSuggestion,
)

__all__ = [
    "ChatReply",
    "ControlAuthError",
    "ControlClient",
    "ControlConflictError",
    "ControlConnectionError",
    "ControlError",
    "ControlNotFoundError",
    "ControlServerError",
    "ControlValidationError",
    "Decision",
    "FilePreview",
    "Interrupt",
    "InterruptOutcome",
    "RunDetail",
    "RunEvent",
    "RunEventPage",
    "RunFile",
    "RunStart",
    "RunSummary",
    "ShareLink",
    "SystemStatus",
    "UploadSession",
    "WorkflowCard",
    "WorkflowInput",
    "WorkflowSuggestion",
]
