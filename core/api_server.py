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
import threading
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Header, BackgroundTasks, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse

from core.db_manager import DBManager
from core.api_models import (
    TriggerRequest, TriggerResponse, JobStatusResponse,
    WorkflowInfo, RequiredInput, AppInfo, HealthResponse
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
SDK_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "sdk")
if os.path.isdir(SDK_DIR):
    app.mount("/sdk", StaticFiles(directory=SDK_DIR), name="sdk")


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
    Same logic as bot.py but without Telegram-specific callbacks.
    """
    db = DBManager()
    try:
        from core.crew_builder import execute_run_with_resume, RateLimitError
        from core.data_manager import DataManager
        DataManager.load_env()

        logger.info(f"API: Starting background execution of Run ID {run_id} (Workflow {workflow_id})")

        result_tuple = execute_run_with_resume(run_id, status_callback=None, accumulated_context=None, chat_id=None)

        # Unpack result
        if isinstance(result_tuple, tuple) and len(result_tuple) == 2:
            final_result, global_context = result_tuple
        else:
            final_result = str(result_tuple)

        db.update_run(run_id, status="completed", result=final_result)
        logger.info(f"API: Run ID {run_id} completed successfully.")

    except RateLimitError as rle:
        error_msg = f"Rate limit / model overloaded: {str(rle)}"
        db.update_run(run_id, status="failed", result=error_msg)
        logger.warning(f"API: Run ID {run_id} failed with rate limit: {rle}")

    except Exception as e:
        error_msg = f"Execution error: {str(e)}"
        db.update_run(run_id, status="failed", result=error_msg)
        logger.error(f"API: Run ID {run_id} failed: {e}", exc_info=True)

    finally:
        db.close()


# =============================================================================
# API ENDPOINTS
# =============================================================================

@app.get("/api/health", response_model=HealthResponse, tags=["System"])
async def health_check():
    """Health check endpoint. Used by the SDK to verify connectivity."""
    return HealthResponse()


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
            result.append(WorkflowInfo(
                id=wf["id"],
                name=wf["name"],
                description=description,
                required_inputs=req_inputs,
                has_output=True  # Default: all workflows produce output
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
    db = DBManager()
    try:
        run = db.read_run(job_id)
        if not run:
            raise HTTPException(status_code=404, detail=f"Job ID {job_id} not found")

        status = run.get("status", "unknown")
        result = run.get("result", "")

        # Build progress info
        progress = None
        if status == "running":
            current_idx = run.get("current_task_idx", 0)
            # Try to get total task count from workflow
            workflow = db.read_workflow(run.get("workflow_id"))
            if workflow:
                total = len(workflow.get("task_ids", []))
                progress = f"Step {current_idx}/{total}"

        return JobStatusResponse(
            job_id=job_id,
            status=status,
            result=result if status == "completed" else None,
            progress=progress,
            error=result if status == "failed" else None
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
# Server Launcher
# =============================================================================

def start_api_server(host: str = "0.0.0.0", port: int = 8000):
    """
    Starts the FastAPI server with Uvicorn.
    Called from bot.py as a daemon thread.
    """
    import uvicorn
    logger.info(f"🌐 Starting Alfredo API server on {host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")
