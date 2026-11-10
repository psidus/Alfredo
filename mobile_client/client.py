"""HTTP client for the Alfredo Control API (mobile / remote clients).

Standard library only. The client is transport-injectable so it can be tested
without a live server and reused by scripts, bots, or a thin mobile gateway.

Environment fallbacks (handy on a phone-friendly dev setup):

* ``ALFREDO_CONTROL_URL``   -> default ``http://127.0.0.1:8000``
* ``ALFREDO_CONTROL_TOKEN`` -> sent as ``X-Control-Token``
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, IO, List, Mapping, Optional, Sequence, Tuple, Union

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
)

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT = 30.0
DEFAULT_UPLOAD_CHUNK = 1024 * 1024
CONTROL_TOKEN_ENV = "ALFREDO_CONTROL_TOKEN"
CONTROL_URL_ENV = "ALFREDO_CONTROL_URL"

UploadSource = Union[bytes, bytearray, str, os.PathLike, IO[bytes]]
ProgressCallback = Callable[[int, int], None]

# transport(method, url, headers, body, timeout) -> (status_code, body_bytes)
Transport = Callable[[str, str, Dict[str, str], Optional[bytes], float], "tuple[int, bytes]"]


class ControlError(RuntimeError):
    """Base error for the Control API."""

    def __init__(self, message: str, *, status: Optional[int] = None, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.detail = detail


class ControlConnectionError(ControlError):
    """The server is unreachable / timed out."""


class ControlAuthError(ControlError):
    """401: wrong or missing ``X-Control-Token``."""


class ControlNotFoundError(ControlError):
    """404: unknown workflow / run / interrupt."""


class ControlConflictError(ControlError):
    """409: the run already finished or the approval was already answered."""


class ControlValidationError(ControlError):
    """422: the request body is invalid."""


class ControlServerError(ControlError):
    """5xx: the server failed (model errors, DB issues, ...)."""


def urllib_transport(
    method: str,
    url: str,
    headers: Dict[str, str],
    body: Optional[bytes],
    timeout: float,
) -> "tuple[int, bytes]":
    """Default transport built on ``urllib`` (no third-party dependency)."""
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(getattr(response, "status", 200)), response.read()
    except urllib.error.HTTPError as exc:  # non-2xx still carries a JSON body
        return int(exc.code), exc.read() or b""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ControlConnectionError(f"Cannot reach Alfredo at {url}: {exc}") from exc


def _parse_json(body: bytes) -> Any:
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"detail": body.decode("utf-8", "replace")[:500]}


def _detail_of(payload: Any) -> str:
    if isinstance(payload, Mapping):
        detail = payload.get("detail")
        if isinstance(detail, str):
            return detail
        if detail is not None:
            return json.dumps(detail, ensure_ascii=False)
    if isinstance(payload, str):
        return payload
    return ""


def _raise_for_status(status: int, payload: Any, *, path: str) -> None:
    if 200 <= status < 300:
        return
    detail = _detail_of(payload)
    message = f"{path} -> HTTP {status}" + (f": {detail}" if detail else "")
    if status == 401:
        raise ControlAuthError(message, status=status, detail=payload)
    if status == 404:
        raise ControlNotFoundError(message, status=status, detail=payload)
    if status == 409:
        raise ControlConflictError(message, status=status, detail=payload)
    if status == 422:
        raise ControlValidationError(message, status=status, detail=payload)
    if status >= 500:
        raise ControlServerError(message, status=status, detail=payload)
    raise ControlError(message, status=status, detail=payload)


def _open_source(source: UploadSource) -> "tuple[IO[bytes], int, bool]":
    """Return ``(handle, size, owns_handle)`` for the supported upload sources."""
    if isinstance(source, (bytes, bytearray)):
        payload = bytes(source)
        return io.BytesIO(payload), len(payload), True
    if isinstance(source, (str, os.PathLike)):
        handle = open(source, "rb")
        try:
            size = int(os.fstat(handle.fileno()).st_size)
        except OSError:
            handle.close()
            raise
        return handle, size, True
    if hasattr(source, "read"):
        handle = source  # type: ignore[assignment]
        try:
            handle.seek(0, os.SEEK_END)
            size = int(handle.tell())
            handle.seek(0)
        except (AttributeError, OSError, ValueError) as exc:
            raise ControlError("Upload source must be seekable") from exc
        return handle, size, False
    raise ControlError("Unsupported upload source")


class ControlClient:
    """Thin, typed wrapper over the Alfredo Control API."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        transport: Optional[Transport] = None,
        env: Optional[Mapping[str, str]] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        environment = os.environ if env is None else env
        self.base_url = (
            base_url or environment.get(CONTROL_URL_ENV) or DEFAULT_BASE_URL
        ).rstrip("/")
        self.token = token if token is not None else environment.get(CONTROL_TOKEN_ENV, "")
        self.timeout = float(timeout)
        self._transport: Transport = transport or urllib_transport
        self._sleep = sleep
        self._clock = clock

    # -- low level ---------------------------------------------------------

    def _url(self, path: str, query: Optional[Mapping[str, Any]] = None) -> str:
        url = f"{self.base_url}{path}"
        if query:
            clean = {k: v for k, v in query.items() if v is not None}
            if clean:
                url = f"{url}?{urllib.parse.urlencode(clean)}"
        return url

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Mapping[str, Any]] = None,
        query: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        headers = {"Accept": "application/json"}
        body: Optional[bytes] = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["X-Control-Token"] = self.token

        url = self._url(path, query)
        status, raw = self._transport(method.upper(), url, headers, body, self.timeout)
        data = _parse_json(raw)
        _raise_for_status(status, data, path=path)
        return data

    def _raw_request(
        self,
        method: str,
        path: str,
        *,
        query: Optional[Mapping[str, Any]] = None,
    ) -> bytes:
        """Same auth/error handling as :meth:`request`, but returns raw bytes."""
        headers = {"Accept": "*/*"}
        if self.token:
            headers["X-Control-Token"] = self.token
        url = self._url(path, query)
        status, raw = self._transport(method.upper(), url, headers, None, self.timeout)
        if not (200 <= status < 300):
            _raise_for_status(status, _parse_json(raw), path=path)
        return raw

    # -- catalogue ---------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        """``GET /api/health`` (no control token required)."""
        return self.request("GET", "/api/health") or {}

    def system_status(self) -> SystemStatus:
        """GPU gate state: VRAM, profiles, queue and first-token metrics."""
        return SystemStatus.from_dict(self.request("GET", "/api/system/status"))

    def workflows(self) -> List[WorkflowCard]:
        """Saved workflows, with their declared inputs."""
        payload = self.request("GET", "/api/workflows") or []
        return [WorkflowCard.from_dict(row) for row in payload]

    # -- assistant ---------------------------------------------------------

    def chat(
        self,
        message: str,
        history: Optional[Sequence[Mapping[str, Any]]] = None,
        *,
        auto_route: bool = True,
    ) -> ChatReply:
        """Free-form turn; the reply may suggest a saved workflow to run."""
        return ChatReply.from_dict(
            self.request(
                "POST",
                "/api/assistant/chat",
                payload={
                    "message": message,
                    "history": list(history or []),
                    "auto_route": bool(auto_route),
                },
            )
        )

    def decide(
        self, message: str, history: Optional[Sequence[Mapping[str, Any]]] = None
    ) -> Decision:
        """Ask the decision agent which saved workflow matches the request."""
        return Decision.from_dict(
            self.request(
                "POST",
                "/api/assistant/decide",
                payload={"message": message, "history": list(history or [])},
            )
        )

    # -- runs --------------------------------------------------------------

    def start_run(
        self,
        workflow_id: int,
        inputs: Optional[Mapping[str, Any]] = None,
        *,
        source: str = "mobile",
    ) -> RunStart:
        """Start a workflow in the background and return its run id."""
        return RunStart.from_dict(
            self.request(
                "POST",
                "/api/runs",
                payload={
                    "workflow_id": int(workflow_id),
                    "inputs": dict(inputs or {}),
                    "source": source,
                },
            )
        )

    def runs(self, limit: int = 20) -> List[RunSummary]:
        """Most recent runs, newest first (compact rows)."""
        payload = self.request("GET", "/api/runs", query={"limit": int(limit)}) or []
        return [RunSummary.from_dict(row) for row in payload]

    def run(self, run_id: int) -> RunDetail:
        """Full state of one run, including the visible result."""
        return RunDetail.from_dict(self.request("GET", f"/api/runs/{int(run_id)}"))

    def cancel_run(self, run_id: int) -> Dict[str, Any]:
        """Cancel a running or paused run."""
        return self.request("POST", f"/api/runs/{int(run_id)}/cancel") or {}

    def events(self, run_id: int, *, after_seq: int = 0, limit: int = 200) -> RunEventPage:
        """Incremental event log for a run.

        Call again with ``after_seq=page.latest_seq`` to get only the new events.
        """
        return RunEventPage.from_dict(
            self.request(
                "GET",
                f"/api/runs/{int(run_id)}/events",
                query={"after_seq": int(after_seq), "limit": int(limit)},
            )
        )

    # -- files -------------------------------------------------------------

    def run_files(self, run_id: int) -> List[RunFile]:
        """Files produced by a run, ready for preview/download/share."""
        payload = self.request("GET", f"/api/runs/{int(run_id)}/files") or {}
        return [RunFile.from_dict(row) for row in (payload.get("files") or [])]

    def files(self, path: str = "", *, limit: int = 500) -> List[RunFile]:
        """List a workspace folder."""
        payload = (
            self.request("GET", "/api/files", query={"path": path, "limit": int(limit)})
            or {}
        )
        return [RunFile.from_dict(row) for row in (payload.get("entries") or [])]

    def file_preview(self, path: str) -> FilePreview:
        """Text / table / thumbnail preview, generated server-side."""
        return FilePreview.from_dict(
            self.request("GET", "/api/files/preview", query={"path": path}) or {}
        )

    def download_file(self, path: str) -> bytes:
        """Raw file bytes (use ``share_file`` for links that need no token)."""
        return self._raw_request("GET", "/api/files/download", query={"path": path})

    def share_file(self, path: str, *, ttl_seconds: int = 3600) -> ShareLink:
        """Create a signed link with a TTL (works without the control token)."""
        return ShareLink.from_dict(
            self.request(
                "POST",
                "/api/files/share",
                payload={"path": path, "ttl_seconds": int(ttl_seconds)},
            )
        )

    # -- uploads -----------------------------------------------------------

    def start_upload(
        self,
        path: str,
        size: int,
        *,
        chunk_size: int = DEFAULT_UPLOAD_CHUNK,
        sha256: Optional[str] = None,
    ) -> UploadSession:
        """Open a chunked upload session for a workspace-relative path."""
        return UploadSession.from_dict(
            self.request(
                "POST",
                "/api/files/upload",
                payload={
                    "path": path,
                    "size": int(size),
                    "chunk_size": int(chunk_size),
                    "sha256": sha256,
                },
            )
        )

    def upload_status(self, upload_id: str) -> UploadSession:
        """How many chunks the server already has (for resume)."""
        return UploadSession.from_dict(
            self.request("GET", f"/api/files/upload/{urllib.parse.quote(upload_id, safe='')}")
        )

    def upload_chunk(
        self,
        upload_id: str,
        index: int,
        data: bytes,
        *,
        sha256: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send one raw chunk; re-sending the same index overwrites it."""
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/octet-stream",
        }
        if self.token:
            headers["X-Control-Token"] = self.token
        if sha256:
            headers["X-Chunk-Sha256"] = sha256
        path = (
            f"/api/files/upload/{urllib.parse.quote(upload_id, safe='')}"
            f"/chunks/{int(index)}"
        )
        url = self._url(path)
        status, raw = self._transport("PUT", url, headers, bytes(data), self.timeout)
        payload = _parse_json(raw)
        _raise_for_status(status, payload, path=path)
        return payload if isinstance(payload, Mapping) else {}

    def complete_upload(self, upload_id: str, *, sha256: Optional[str] = None) -> RunFile:
        """Verify size/checksum and move the file into the workspace."""
        payload = self.request(
            "POST",
            f"/api/files/upload/{urllib.parse.quote(upload_id, safe='')}/complete",
            payload={"sha256": sha256} if sha256 else {},
        )
        return RunFile.from_dict(payload)

    def abort_upload(self, upload_id: str) -> Dict[str, Any]:
        """Drop a session and its temporary parts."""
        return (
            self.request(
                "DELETE",
                f"/api/files/upload/{urllib.parse.quote(upload_id, safe='')}",
            )
            or {}
        )

    def upload_file(
        self,
        path: str,
        source: UploadSource,
        *,
        chunk_size: int = DEFAULT_UPLOAD_CHUNK,
        progress: Optional[ProgressCallback] = None,
        session: Optional[UploadSession] = None,
    ) -> RunFile:
        """Upload a file in chunks, hashing client-side and never buffering it.

        ``source`` may be ``bytes``, a filesystem path or a binary file object.
        Pass ``session`` to resume an interrupted upload: received chunks are
        skipped and only the missing ones are sent.
        """
        handle, total, owns = _open_source(source)
        created = False
        try:
            if total <= 0:
                raise ControlError("Cannot upload an empty file")
            if session is None:
                session = self.start_upload(path, total, chunk_size=chunk_size)
                created = True
            step = int(session.chunk_size or chunk_size)
            if step <= 0:
                raise ControlError("Upload session without a valid chunk_size")
            pending = set(session.missing)
            digest = hashlib.sha256()
            sent = 0
            while sent < total:
                block = handle.read(min(step, total - sent))
                if not block:
                    raise ControlError(
                        f"Source ended after {sent} of {total} bytes"
                    )
                digest.update(block)
                index = sent // step
                if index in pending:
                    self.upload_chunk(session.upload_id, index, block)
                    pending.discard(index)
                sent += len(block)
                if progress:
                    progress(sent, total)
            if pending:
                raise ControlError(f"Missing chunks: {sorted(pending)}")
            return self.complete_upload(session.upload_id, sha256=digest.hexdigest())
        except Exception:
            if created and session is not None:
                try:
                    self.abort_upload(session.upload_id)
                except ControlError:
                    pass  # the original error is more useful
            raise
        finally:
            if owns:
                handle.close()

    # -- approvals ---------------------------------------------------------

    def interrupts(self, run_id: Optional[int] = None) -> List[Interrupt]:
        """Pending human approvals, optionally filtered by run."""
        payload = (
            self.request("GET", "/api/interrupts", query={"run_id": run_id}) or []
        )
        return [Interrupt.from_dict(row) for row in payload]

    def answer_interrupt(
        self, interrupt_id: int, answer: str, *, execute: bool = True
    ) -> InterruptOutcome:
        """Answer an approval; the run resumes immediately unless ``execute`` is false."""
        return InterruptOutcome.from_dict(
            self.request(
                "POST",
                f"/api/interrupts/{int(interrupt_id)}/answer",
                payload={"answer": answer, "execute": bool(execute)},
            )
        )

    # -- high level helpers ------------------------------------------------

    def wait_for_run(
        self,
        run_id: int,
        *,
        timeout: float = 900.0,
        interval: float = 2.0,
        max_interval: float = 10.0,
        stop_on_interrupt: bool = True,
        on_update: Optional[Callable[[RunDetail], None]] = None,
    ) -> RunDetail:
        """Poll a run until it finishes (or needs a human, by default).

        Back off from ``interval`` up to ``max_interval`` so a phone screen does
        not hammer the server. Raises :class:`ControlError` on timeout.
        """
        deadline = self._clock() + float(timeout)
        delay = max(0.5, float(interval))
        while True:
            detail = self.run(run_id)
            if on_update:
                on_update(detail)
            if detail.is_finished:
                return detail
            if stop_on_interrupt and detail.needs_approval:
                return detail
            if self._clock() >= deadline:
                raise ControlError(
                    f"Run {run_id} still '{detail.status}' after {timeout:.0f}s",
                    status=None,
                    detail=detail.raw,
                )
            self._sleep(delay)
            delay = min(float(max_interval), delay * 1.5)

    def start_and_wait(
        self,
        workflow_id: int,
        inputs: Optional[Mapping[str, Any]] = None,
        *,
        source: str = "mobile",
        timeout: float = 900.0,
        interval: float = 2.0,
        stop_on_interrupt: bool = True,
        on_update: Optional[Callable[[RunDetail], None]] = None,
    ) -> RunDetail:
        """Convenience: start a run and poll it to completion."""
        started = self.start_run(workflow_id, inputs, source=source)
        return self.wait_for_run(
            started.run_id,
            timeout=timeout,
            interval=interval,
            stop_on_interrupt=stop_on_interrupt,
            on_update=on_update,
        )
