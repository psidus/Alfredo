# core/api_server.py
"""
FastAPI server for Alfredo Workflow API.
Exposes workflows as REST endpoints so external apps can trigger them
via the JavaScript SDK or direct HTTP calls.

Runs alongside the Telegram bot as a daemon thread.
"""

import os
import json
import logging
import secrets
import threading
from functools import partial
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Header, BackgroundTasks, Depends, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from core.db_manager import DBManager
from core.api_models import (
    TriggerRequest, TriggerResponse, JobStatusResponse,
    WorkflowInfo, RequiredInput, AppInfo, HealthResponse,
    ChatRequest, ChatResponse, DecideRequest, DecideResponse,
    StartRunRequest, StartRunResponse, AnswerInterruptRequest,
    WorkflowSuggestion, PushKeyResponse, PushSubscribeRequest,
    PushUnsubscribeRequest, ShareFileRequest, ShareFileResponse,
    UploadSessionRequest, UploadSession, UploadChunkStatus, CompleteUploadRequest,
    FileEntry, SystemStatus,
)

# Setup logging
logger = logging.getLogger(__name__)

# --- FastAPI App ---
app = FastAPI(
    title="Alfredo Workflow API",
    description="REST API for triggering Alfredo workflows from external applications.",
    version="1.0.0",
)

# CORS — permissive for local development, can be tightened in production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve the JavaScript SDK as static files
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SDK_DIR = os.path.join(ROOT_DIR, "sdk")
if os.path.isdir(SDK_DIR):
    app.mount("/sdk", StaticFiles(directory=SDK_DIR), name="sdk")
else:
    logger.warning(f"SDK directory not found at {SDK_DIR}. Static files will not be served.")

# Serve the built mobile PWA (mobile_web/dist) at /app when present.
# Build it with: cd mobile_web && npm install && npm run build
MOBILE_WEB_DIR = os.path.join(ROOT_DIR, "mobile_web", "dist")
if os.path.isdir(MOBILE_WEB_DIR):
    app.mount("/app", StaticFiles(directory=MOBILE_WEB_DIR, html=True), name="mobile")
else:
    logger.info(
        "Mobile PWA not built yet: /app is unavailable "
        "(cd mobile_web && npm install && npm run build)."
    )


# --- Authentication Dependency ---

async def verify_app(x_api_key: str = Header(..., description="API key of the registered app")) -> dict:
    """
    Validates the API key from the X-API-Key header.
    Returns the full app record if valid, raises 401 otherwise.
    """
    db = DBManager()
    try:
        app_record = db.get_app_by_api_key(x_api_key)
    finally:
        db.close()

    if not app_record:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if app_record.get("status") != "active":
        raise HTTPException(status_code=403, detail="App is disabled")

    return app_record


# --- Control API authentication (mobile app / remote dashboard) ---

CONTROL_TOKEN_ENV = "ALFREDO_CONTROL_TOKEN"


async def verify_control(x_control_token: str = Header(default="", alias="X-Control-Token")) -> None:
    """Validates ``X-Control-Token`` against ``ALFREDO_CONTROL_TOKEN``.

    If the variable is not configured the server stays open (single-machine
    setup) and a warning is logged at startup. Set it before exposing the
    server on a network the phone can reach.
    """
    expected = (os.getenv(CONTROL_TOKEN_ENV) or "").strip()
    if not expected:
        return
    if not secrets.compare_digest(x_control_token or "", expected):
        raise HTTPException(status_code=401, detail="Invalid control token")


_engine_lock = threading.Lock()
_engine_singleton = None


def _get_assistant_engine():
    """Lazily build one shared ``AssistantEngine`` (model discovery is costly)."""
    global _engine_singleton
    if _engine_singleton is None:
        with _engine_lock:
            if _engine_singleton is None:
                from core.assistant_engine import AssistantEngine
                from core.data_manager import DataManager

                DataManager.load_env()
                _engine_singleton = AssistantEngine()
    return _engine_singleton


def _card_to_dict(card) -> Dict[str, Any]:
    return {
        "id": card.id,
        "name": card.name,
        "description": card.description,
        "inputs": [{"key": i.key, "prompt": i.prompt} for i in card.inputs],
        "expected_exports": list(card.expected_exports or []),
    }


def _compact_run(record: Dict[str, Any]) -> Dict[str, Any]:
    text = record.get("display_text") or ""
    return {
        "run_id": record.get("id"),
        "workflow_id": record.get("workflow_id"),
        "status": record.get("status"),
        "source": record.get("source"),
        "started_at": record.get("started_at"),
        "preview": text[:400],
    }


# --- Helper: extract required_inputs from workflow tasks ---

def _get_workflow_required_inputs(db: DBManager, workflow_record: dict) -> List[RequiredInput]:
    """
    Collects all required_inputs from the first task of a workflow.
    These are the inputs the SDK widget must map to DOM elements.
    """
    task_ids = workflow_record.get("task_ids", [])
    if not task_ids:
        return []

    all_inputs = []
    seen_keys = set()

    for task_id in task_ids:
        # Handle both plain int IDs and batch_loop dicts
        if isinstance(task_id, dict):
            continue
        task_record = db.read_task(int(task_id))
        if not task_record:
            continue
        req_inputs = task_record.get("required_inputs", [])
        for inp in req_inputs:
            key = inp.get("key", "") if isinstance(inp, dict) else str(inp)
            if key and key not in seen_keys:
                seen_keys.add(key)
                prompt = inp.get("prompt", "") if isinstance(inp, dict) else ""
                all_inputs.append(RequiredInput(key=key, prompt=prompt))

    return all_inputs


def _get_workflow_description(db: DBManager, workflow_record: dict) -> str:
    """Derives a workflow description from its first task's description."""
    task_ids = workflow_record.get("task_ids", [])
    if not task_ids:
        return ""
    first_tid = task_ids[0]
    if isinstance(first_tid, dict):
        return ""
    task_record = db.read_task(int(first_tid))
    if task_record:
        desc = task_record.get("description", "")
        # Truncate for SDK display
        return desc[:200] if len(desc) > 200 else desc
    return ""


# --- Background Workflow Execution ---

def _execute_workflow_background(run_id: int, workflow_id: int, inputs: dict):
    """
    Executes a workflow in a background thread.

    All status handling now lives in ``core.run_service`` so the API, the bot and
    the dashboard share one implementation.
    """
    from core.data_manager import DataManager
    from core.run_service import execute_run

    DataManager.load_env()
    logger.info(f"API: Starting background execution of Run ID {run_id} (Workflow {workflow_id})")
    outcome = execute_run(run_id)
    logger.info(f"API: Run ID {run_id} finished with status '{outcome.get('status')}'.")


# =============================================================================
# API ENDPOINTS
# =============================================================================

@app.get("/api/health", response_model=HealthResponse, tags=["System"])
async def health_check():
    """Health check endpoint. Used by the SDK to verify connectivity."""
    return HealthResponse()


@app.get("/api/system/status", response_model=SystemStatus, tags=["System"])
@app.get(
    "/api/v1/system/status",
    response_model=SystemStatus,
    tags=["System"],
    include_in_schema=False,
)
async def control_system_status(_: None = Depends(verify_control)):
    """GPU queue, VRAM, profiles and latency metrics (single-flight gate)."""
    from core.gpu_gate import system_status

    return await run_in_threadpool(system_status)


@app.get("/api/apps/{app_name}/workflows", response_model=List[WorkflowInfo], tags=["Workflows"])
async def list_app_workflows(app_name: str, app_record: dict = Depends(verify_app)):
    """
    Returns all workflows linked to the authenticated app.
    Called by the SDK widget on initialization to populate the workflow dropdown.
    Includes required_inputs for each workflow so the widget knows what to map.
    """
    if app_record["name"] != app_name:
        raise HTTPException(status_code=403, detail="API key does not match app name")

    db = DBManager()
    try:
        workflows = db.get_app_workflows(app_record["id"])
        result = []
        for wf in workflows:
            req_inputs = _get_workflow_required_inputs(db, wf)
            description = _get_workflow_description(db, wf)
            exports = wf.get("expected_exports") or []
            if isinstance(exports, str):
                try:
                    import json as _json
                    exports = _json.loads(exports)
                except Exception:
                    exports = [exports] if exports else []
            result.append(WorkflowInfo(
                id=wf["id"],
                name=wf["name"],
                description=description,
                required_inputs=req_inputs,
                has_output=True,
                expected_exports=list(exports) if isinstance(exports, list) else [],
            ))
        return result
    finally:
        db.close()


@app.post("/api/apps/{app_name}/trigger", response_model=TriggerResponse, tags=["Workflows"])
async def trigger_workflow(app_name: str, request: TriggerRequest, app_record: dict = Depends(verify_app)):
    """
    Triggers a workflow execution in the background.
    Returns a job_id immediately for status polling.
    
    The SDK calls this when the user presses the configured trigger button.
    Input data comes from the mapped DOM elements.
    """
    if app_record["name"] != app_name:
        raise HTTPException(status_code=403, detail="API key does not match app name")

    db = DBManager()
    try:
        # Verify workflow exists and belongs to this app
        workflow = db.read_workflow(request.workflow_id)
        if not workflow:
            raise HTTPException(status_code=404, detail=f"Workflow ID {request.workflow_id} not found")
        if workflow.get("app_id") != app_record["id"]:
            raise HTTPException(status_code=403, detail="Workflow does not belong to this app")

        # Merge inputs: request.inputs + request.context
        combined_inputs = {**request.inputs}
        if request.context:
            combined_inputs["_context"] = request.context

        # Create a run record with source="api"
        run_id = db.create_run(
            workflow_id=request.workflow_id,
            status="running",
            inputs=combined_inputs,
            source="api"
        )

        # Launch execution in background thread
        exec_thread = threading.Thread(
            target=_execute_workflow_background,
            args=(run_id, request.workflow_id, combined_inputs),
            daemon=True
        )
        exec_thread.start()

        logger.info(f"API: Triggered workflow {request.workflow_id} for app '{app_name}', run_id={run_id}")

        return TriggerResponse(
            job_id=run_id,
            status="running",
            message=f"Workflow '{workflow['name']}' execution started"
        )
    finally:
        db.close()


@app.get("/api/jobs/{job_id}", response_model=JobStatusResponse, tags=["Jobs"])
async def get_job_status(job_id: int):
    """
    Polls the status of a running workflow job.
    The SDK calls this every 2 seconds until status is 'completed' or 'failed'.
    """
    from core.workflow_contracts import parse_run_result_payload
    import json as _json

    db = DBManager()
    try:
        run = db.read_run(job_id)
        if not run:
            raise HTTPException(status_code=404, detail=f"Job ID {job_id} not found")

        status = run.get("status", "unknown")
        raw_result = run.get("result", "")

        # Build progress info
        progress = None
        if status == "running":
            current_idx = run.get("current_task_idx", 0)
            # Try to get total task count from workflow
            workflow = db.read_workflow(run.get("workflow_id"))
            if workflow:
                total = len(workflow.get("task_ids", []))
                progress = f"Step {current_idx}/{total}"

        display_text, payload = parse_run_result_payload(raw_result)
        # Also check task_outputs meta as fallback
        if not payload:
            try:
                outs = run.get("task_outputs")
                if isinstance(outs, str):
                    outs = _json.loads(outs)
                if isinstance(outs, dict) and outs.get("__workflow_meta__"):
                    payload = outs["__workflow_meta__"]
                    display_text = payload.get("final_result") or display_text
            except Exception:
                pass

        if status == "failed":
            return JobStatusResponse(
                job_id=job_id,
                status=status,
                result=None,
                progress=progress,
                error=str(raw_result) if raw_result else "Unknown error",
            )

        return JobStatusResponse(
            job_id=job_id,
            status=status,
            result=display_text if status == "completed" else None,
            procedure_summary=(payload or {}).get("procedure_summary") if status == "completed" else None,
            exports=(payload or {}).get("exports") if status == "completed" else None,
            handoffs=(payload or {}).get("handoffs") if status == "completed" else None,
            progress=progress,
            error=None,
        )
    finally:
        db.close()


@app.get("/api/apps/{app_name}/info", response_model=AppInfo, tags=["Apps"])
async def get_app_info(app_name: str, app_record: dict = Depends(verify_app)):
    """Returns public info about the authenticated app."""
    if app_record["name"] != app_name:
        raise HTTPException(status_code=403, detail="API key does not match app name")

    return AppInfo(
        name=app_record["name"],
        display_name=app_record.get("display_name", app_record["name"]),
        description=app_record.get("description", ""),
        status=app_record.get("status", "active")
    )


# =============================================================================
# CONTROL API — mobile app / remote dashboard
# =============================================================================

@app.get("/api/workflows", tags=["Control"])
async def control_list_workflows(_: None = Depends(verify_control)):
    """Full catalogue of saved workflows, with their declared inputs."""
    from core.assistant_engine import build_workflow_catalog

    try:
        cards = await run_in_threadpool(build_workflow_catalog)
    except Exception as exc:  # noqa: BLE001 - boundary of the API
        logger.error("Control API: could not build the catalogue: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))
    return [_card_to_dict(c) for c in cards]


@app.post("/api/assistant/chat", response_model=ChatResponse, tags=["Control"])
async def control_assistant_chat(
    request: ChatRequest, _: None = Depends(verify_control)
):
    """Free chat turn; optionally suggests a saved workflow to run."""
    from core.assistant_engine import normalize_history, should_route

    message = (request.message or "").strip()
    if not message:
        raise HTTPException(status_code=422, detail="message is required")
    history = normalize_history(request.history)

    try:
        engine = await run_in_threadpool(_get_assistant_engine)
        reply = await run_in_threadpool(engine.chat, message, history)
    except Exception as exc:  # noqa: BLE001 - surface the real model error
        logger.error("Control API: chat failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail=f"Model error: {exc}")

    suggestion: Optional[WorkflowSuggestion] = None
    if request.auto_route:
        try:
            cards = await run_in_threadpool(engine.catalogue)
            if cards and should_route(message, cards):
                turn = await run_in_threadpool(engine.decide, message, history)
                if turn.action == "run_workflow" and turn.workflow_id:
                    card = next((c for c in cards if c.id == turn.workflow_id), None)
                    suggestion = WorkflowSuggestion(
                        workflow_id=int(turn.workflow_id),
                        name=card.name if card else "",
                        text=turn.text or "",
                        inputs=turn.inputs or {},
                        missing_inputs=turn.missing_inputs or [],
                    )
                    if not reply and turn.text:
                        reply = turn.text
        except Exception:  # noqa: BLE001 - routing is best effort, chat still answers
            logger.warning("Control API: routing failed (chat continues)", exc_info=True)

    return ChatResponse(reply=reply or "", workflow=suggestion)


@app.post("/api/assistant/decide", response_model=DecideResponse, tags=["Control"])
async def control_assistant_decide(
    request: DecideRequest, _: None = Depends(verify_control)
):
    """Ask the decision agent which saved workflow matches a request."""
    from core.assistant_engine import normalize_history

    message = (request.message or "").strip()
    if not message:
        raise HTTPException(status_code=422, detail="message is required")

    try:
        engine = await run_in_threadpool(_get_assistant_engine)
        turn = await run_in_threadpool(
            engine.decide, message, normalize_history(request.history)
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Control API: decide failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail=f"Model error: {exc}")

    return DecideResponse(
        text=turn.text or "",
        action=turn.action or "answer",
        workflow_id=int(turn.workflow_id) if turn.workflow_id else None,
        inputs=turn.inputs or {},
        missing_inputs=turn.missing_inputs or [],
    )


@app.post("/api/runs", response_model=StartRunResponse, tags=["Control"])
async def control_start_run(
    request: StartRunRequest, _: None = Depends(verify_control)
):
    """Start a workflow run in the background and return its id."""
    from core.run_service import RunError, start_run

    try:
        run_id = await run_in_threadpool(
            start_run,
            int(request.workflow_id),
            request.inputs or {},
            source=request.source or "mobile",
        )
    except RunError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.error("Control API: could not start run: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))

    return StartRunResponse(run_id=int(run_id), status="running")


@app.get("/api/runs", tags=["Control"])
async def control_list_runs(
    limit: int = Query(default=20, ge=1, le=100),
    _: None = Depends(verify_control),
):
    """Most recent runs (compact form, no payloads)."""
    from core.run_service import list_recent_runs

    runs = await run_in_threadpool(list_recent_runs, limit)
    return [_compact_run(r) for r in runs]


@app.get("/api/runs/{run_id}", tags=["Control"])
async def control_get_run(run_id: int, _: None = Depends(verify_control)):
    """Full state of one run, including the visible result and exports."""
    from core.run_service import get_run

    run = await run_in_threadpool(get_run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    payload = run.get("payload") or {}
    pending_id = None
    if str(run.get("status") or "").lower() == "waiting_human":
        from core.run_service import pending_interrupts

        pend = await run_in_threadpool(partial(pending_interrupts, run_id=run_id))
        pending_id = pend[0]["id"] if pend else None

    return {
        "run_id": run.get("id"),
        "workflow_id": run.get("workflow_id"),
        "status": run.get("status"),
        "source": run.get("source"),
        "started_at": run.get("started_at"),
        "inputs": run.get("inputs") or {},
        "result": run.get("display_text") or "",
        "exports": payload.get("exports") or [],
        "procedure_summary": payload.get("procedure_summary"),
        "handoffs": payload.get("handoffs") or [],
        "pending_interrupt_id": pending_id,
    }

@app.get("/api/runs/{run_id}/events", tags=["Control"])
async def control_run_events(
    run_id: int,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    _: None = Depends(verify_control),
):
    """Incremental event stream for one run.

    Poll with ``after_seq=latest_seq`` to receive exactly the new events, with no
    duplicates and no gaps (useful for reconnecting clients).
    """
    from core.events import latest_seq, tail
    from core.run_service import get_run

    run = await run_in_threadpool(get_run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    events = await run_in_threadpool(
        partial(tail, after_seq=after_seq, run_id=run_id, limit=limit)
    )
    latest = await run_in_threadpool(latest_seq, run_id)
    return {"run_id": int(run_id), "events": events, "latest_seq": int(latest)}


# ---------------------------------------------------------------------------
# Files and artifacts (read-only workspace access)
# ---------------------------------------------------------------------------


def _file_http_error(exc: Exception) -> HTTPException:
    from core.files import FileAccessError, FileNotFound
    from core.uploads import UploadError, UploadNotFound

    if isinstance(exc, UploadNotFound) or isinstance(exc, FileNotFound):
        return HTTPException(status_code=404, detail="File not found")
    if isinstance(exc, UploadError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, FileAccessError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="File access failed")


def _file_response(rel_path: str) -> FileResponse:
    """Resolve and stream one workspace file (shared by download endpoints)."""
    from core.files import describe, resolve

    resolved = resolve(rel_path)
    info = describe(rel_path)
    return FileResponse(
        path=str(resolved),
        media_type=info["mime"],
        filename=info["name"],
    )


@app.get("/api/runs/{run_id}/files", tags=["Control"])
async def control_run_files(run_id: int, _: None = Depends(verify_control)):
    """Files produced by a run, resolved inside the workspace jail."""
    from core.files import describe_run_exports
    from core.run_service import get_run

    run = await run_in_threadpool(get_run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    files = await run_in_threadpool(
        partial(describe_run_exports, run.get("payload") or {})
    )
    return {"run_id": int(run_id), "files": files}


@app.get("/api/files", tags=["Control"])
async def control_list_files(
    path: str = Query(default="", description="Workspace-relative folder"),
    limit: int = Query(default=500, ge=1, le=1000),
    _: None = Depends(verify_control),
):
    """List a workspace folder (paths are jailed inside ``workspace/``)."""
    from core.files import list_files

    try:
        return await run_in_threadpool(partial(list_files, path, limit=limit))
    except Exception as exc:  # noqa: BLE001 - mapped to a clean HTTP error
        raise _file_http_error(exc)


@app.get("/api/files/preview", tags=["Control"])
async def control_file_preview(
    path: str = Query(..., description="Workspace-relative file path"),
    _: None = Depends(verify_control),
):
    """Cheap preview (text, table, image thumbnail, PDF text)."""
    from core.files import preview

    try:
        return await run_in_threadpool(partial(preview, path))
    except Exception as exc:  # noqa: BLE001
        raise _file_http_error(exc)


@app.get("/api/files/download", tags=["Control"])
async def control_file_download(
    path: str = Query(..., description="Workspace-relative file path"),
    _: None = Depends(verify_control),
):
    """Stream a file from disk (never buffered fully in memory)."""
    try:
        return await run_in_threadpool(_file_response, path)
    except Exception as exc:  # noqa: BLE001
        raise _file_http_error(exc)


@app.post("/api/files/share", response_model=ShareFileResponse, tags=["Control"])
async def control_file_share(
    request: ShareFileRequest,
    http_request: Request,
    _: None = Depends(verify_control),
):
    """Create a short-lived, signed download link for one file."""
    from core.files import create_share_token

    try:
        token_info = await run_in_threadpool(
            partial(create_share_token, request.path, ttl_seconds=request.ttl_seconds)
        )
    except Exception as exc:  # noqa: BLE001
        raise _file_http_error(exc)

    base = (os.getenv("ALFREDO_PUBLIC_URL") or str(http_request.base_url)).rstrip("/")
    return {
        **token_info,
        "url": f"{base}/api/files/shared/{token_info['token']}",
    }


@app.get("/api/files/shared/{token}", tags=["Control"])
async def shared_file_download(token: str):
    """Download via a signed, expiring link (the token is the credential)."""
    from core.files import verify_share_token

    try:
        info = await run_in_threadpool(partial(verify_share_token, token))
    except Exception as exc:  # noqa: BLE001
        raise _file_http_error(exc)
    try:
        return await run_in_threadpool(_file_response, info["path"])
    except Exception as exc:  # noqa: BLE001
        raise _file_http_error(exc)


# -- chunked uploads ---------------------------------------------------------


@app.post("/api/files/upload", response_model=UploadSession, tags=["Control"])
async def control_upload_start(
    payload: UploadSessionRequest, _: None = Depends(verify_control)
):
    """Open a chunked upload session for a workspace-relative path."""
    from core.uploads import start_upload

    try:
        return await run_in_threadpool(
            partial(
                start_upload,
                payload.path,
                payload.size,
                chunk_size=payload.chunk_size,
                sha256=payload.sha256,
            )
        )
    except Exception as exc:  # noqa: BLE001
        raise _file_http_error(exc)


@app.get("/api/files/upload/{upload_id}", response_model=UploadSession, tags=["Control"])
async def control_upload_status(upload_id: str, _: None = Depends(verify_control)):
    """Progress of a session, so a client can resume after a reconnect."""
    from core.uploads import upload_status

    try:
        return await run_in_threadpool(partial(upload_status, upload_id))
    except Exception as exc:  # noqa: BLE001
        raise _file_http_error(exc)


@app.put(
    "/api/files/upload/{upload_id}/chunks/{index}",
    response_model=UploadChunkStatus,
    tags=["Control"],
)
async def control_upload_chunk(
    upload_id: str,
    index: int,
    request: Request,
    _: None = Depends(verify_control),
):
    """Store one raw chunk (``application/octet-stream``).

    Only a single chunk is buffered per request, never the whole file.
    """
    from core.uploads import write_chunk

    body = await request.body()
    chunk_hash = request.headers.get("x-chunk-sha256")
    try:
        return await run_in_threadpool(
            partial(write_chunk, upload_id, index, body, chunk_sha256=chunk_hash)
        )
    except Exception as exc:  # noqa: BLE001
        raise _file_http_error(exc)


@app.post(
    "/api/files/upload/{upload_id}/complete",
    response_model=FileEntry,
    tags=["Control"],
)
async def control_upload_complete(
    upload_id: str,
    payload: Optional[CompleteUploadRequest] = None,
    _: None = Depends(verify_control),
):
    """Verify checksum/size and atomically move the file into place."""
    from core.uploads import complete_upload

    checksum = payload.sha256 if payload else None
    try:
        return await run_in_threadpool(
            partial(complete_upload, upload_id, sha256=checksum)
        )
    except Exception as exc:  # noqa: BLE001
        raise _file_http_error(exc)


@app.delete("/api/files/upload/{upload_id}", tags=["Control"])
async def control_upload_abort(upload_id: str, _: None = Depends(verify_control)):
    """Drop a session and its temporary parts."""
    from core.uploads import abort_upload

    try:
        return await run_in_threadpool(partial(abort_upload, upload_id))
    except Exception as exc:  # noqa: BLE001
        raise _file_http_error(exc)


@app.post("/api/runs/{run_id}/cancel", tags=["Control"])
async def control_cancel_run(run_id: int, _: None = Depends(verify_control)):
    """Request cancellation of a running or paused run."""
    from core.run_service import cancel_run

    cancelled = await run_in_threadpool(cancel_run, run_id)
    if not cancelled:
        raise HTTPException(
            status_code=409, detail="Run not found or already finished"
        )
    return {"run_id": run_id, "status": "cancelled"}


@app.get("/api/interrupts", tags=["Control"])
async def control_list_interrupts(
    run_id: Optional[int] = Query(default=None, description="Filter by run"),
    _: None = Depends(verify_control),
):
    """Pending human approvals waiting for an answer."""
    from core.run_service import pending_interrupts

    pending = await run_in_threadpool(partial(pending_interrupts, run_id=run_id))
    return [
        {
            "id": intr.get("id"),
            "run_id": intr.get("run_id"),
            "node_id": intr.get("node_id"),
            "question": intr.get("question") or "",
            "options": intr.get("options") or [],
            "created_at": intr.get("created_at"),
        }
        for intr in pending
    ]


@app.post("/api/interrupts/{interrupt_id}/answer", tags=["Control"])
async def control_answer_interrupt(
    interrupt_id: int,
    request: AnswerInterruptRequest,
    _: None = Depends(verify_control),
):
    """Answer an approval; the run resumes immediately unless ``execute`` is false."""
    from core.run_service import resume_interrupt, start_execution

    answer = (request.answer or "").strip()
    if not answer:
        raise HTTPException(status_code=422, detail="answer is required")

    try:
        outcome = await run_in_threadpool(
            partial(resume_interrupt, interrupt_id, answer, execute=False)
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Control API: could not answer interrupt %s: %s", interrupt_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))

    if outcome.get("status") == "failed":
        raise HTTPException(status_code=500, detail=outcome.get("error") or "resume failed")
    if outcome.get("status") == "stale":
        raise HTTPException(
            status_code=409, detail="Interrupt not found or already answered"
        )

    if request.execute:
        await run_in_threadpool(
            partial(
                start_execution,
                int(outcome["run_id"]),
                chat_id=None,
            )
        )
        outcome["execution"] = "started"

    return outcome


# =============================================================================
# Web Push — approval notifications for the mobile PWA
# =============================================================================

@app.get("/api/push/key", response_model=PushKeyResponse, tags=["Control"])
async def control_push_key(_: None = Depends(verify_control)):
    """VAPID public key the browser needs to subscribe to push messages."""
    from core.push import is_available, public_key, subject

    try:
        key = await run_in_threadpool(public_key)
    except Exception as exc:  # noqa: BLE001
        logger.error("Control API: could not load VAPID keys: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))
    return PushKeyResponse(
        public_key=key,
        enabled=await run_in_threadpool(is_available),
        subject=subject(),
    )


@app.post("/api/push/subscribe", tags=["Control"])
async def control_push_subscribe(
    request: PushSubscribeRequest, _: None = Depends(verify_control)
):
    """Register (or refresh) a browser push subscription."""
    from core.push import save_subscription, subscription_count

    endpoint = (request.endpoint or "").strip()
    if not endpoint:
        raise HTTPException(status_code=422, detail="endpoint is required")
    try:
        subscription_id = await run_in_threadpool(
            partial(
                save_subscription,
                endpoint,
                request.keys.p256dh,
                request.keys.auth,
                user_agent=request.user_agent,
            )
        )
        total = await run_in_threadpool(subscription_count)
    except Exception as exc:  # noqa: BLE001
        logger.error("Control API: could not save push subscription: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))
    return {
        "status": "subscribed",
        "subscription_id": subscription_id,
        "subscriptions": total,
    }


@app.post("/api/push/unsubscribe", tags=["Control"])
async def control_push_unsubscribe(
    request: PushUnsubscribeRequest, _: None = Depends(verify_control)
):
    """Remove a push subscription (device unsubscribed or rotated keys)."""
    from core.push import remove_subscription, subscription_count

    removed = await run_in_threadpool(
        partial(remove_subscription, endpoint=(request.endpoint or "").strip())
    )
    return {
        "status": "unsubscribed" if removed else "not_found",
        "subscriptions": await run_in_threadpool(subscription_count),
    }


@app.post("/api/push/test", tags=["Control"])
async def control_push_test(_: None = Depends(verify_control)):
    """Send a test notification to every registered device."""
    from core.push import send_test_notification

    delivered = await run_in_threadpool(send_test_notification)
    if delivered <= 0:
        raise HTTPException(
            status_code=409,
            detail="No push subscription available (enable notifications in the app first)",
        )
    return {"status": "sent", "delivered": delivered}


# =============================================================================
# Server Launcher
# =============================================================================

def start_api_server(host: str = "0.0.0.0", port: int = 8000):
    """
    Starts the FastAPI server with Uvicorn.
    Called from bot.py as a daemon thread.
    """
    import uvicorn
    if not (os.getenv(CONTROL_TOKEN_ENV) or "").strip():
        logger.warning(
            "⚠️ %s is not set: the Control API (/api/assistant/*, /api/runs, "
            "/api/interrupts) accepts unauthenticated requests. Set it before "
            "exposing port %s outside this machine.",
            CONTROL_TOKEN_ENV,
            port,
        )
    logger.info(f"🌐 Starting Alfredo API server on {host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")
