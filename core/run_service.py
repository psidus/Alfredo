"""
Single entry point for workflow runs.

Before this module, run creation/execution/status handling was duplicated in
``bot.py``, ``core/api_server.py`` and the dashboard. Everything that starts or
resumes a run should go through here, so the Telegram bot, the Streamlit UI and
the (future) mobile API behave the same way.

The CrewAI engine is imported lazily: importing this module stays cheap and does
not require provider keys.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {"completed", "failed", "stopped", "cancelled"}
PAUSED_STATUSES = {"paused", "waiting_human"}


class RunError(RuntimeError):
    """Raised when a run cannot be created or executed."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _json_dict(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


def parse_run_record(run: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Normalize a raw DB run row into the shape the UI/API expect."""
    if not run:
        return {}
    from core.workflow_contracts import parse_run_result_payload

    display, payload = parse_run_result_payload(run.get("result"))
    task_outputs = _json_dict(run.get("task_outputs"))
    if not payload and isinstance(task_outputs.get("__workflow_meta__"), dict):
        payload = task_outputs["__workflow_meta__"]
        display = payload.get("final_result") or display

    return {
        **run,
        "inputs": _json_dict(run.get("inputs")),
        "task_outputs": task_outputs,
        "display_text": display,
        "payload": payload,
    }


# ---------------------------------------------------------------------------
# Create / execute
# ---------------------------------------------------------------------------


def create_run(
    workflow_id: int,
    inputs: Optional[Dict[str, Any]] = None,
    source: str = "dashboard",
    db: Any = None,
) -> int:
    """Create a ``workflow_runs`` row and return its id."""
    owns_db = db is None
    if db is None:
        from core.db_manager import DBManager

        db = DBManager()
    try:
        workflow = db.read_workflow(int(workflow_id))
        if not workflow:
            raise RunError(f"Workflow {workflow_id} not found.")
        run_id = int(
            db.create_run(
                workflow_id=int(workflow_id),
                status="running",
                inputs=inputs or {},
                source=source,
            )
        )
    finally:
        if owns_db:
            db.close()

    from core import events

    events.emit(
        run_id,
        events.RUN_STARTED,
        {
            "workflow_id": int(workflow_id),
            "source": source,
            "input_keys": sorted(str(k) for k in (inputs or {})),
        },
    )
    return run_id


def _unpack_execution_result(result: Any) -> tuple[str, Optional[str]]:
    if isinstance(result, tuple) and len(result) == 2:
        return str(result[0] or ""), result[1]
    return str(result or ""), None


def execute_run(
    run_id: int,
    *,
    status_callback: Optional[Callable[..., Any]] = None,
    accumulated_context: Optional[str] = None,
    chat_id: Optional[str] = None,
    on_flight_change: Optional[Callable[..., Any]] = None,
    raise_on_error: bool = False,
) -> Dict[str, Any]:
    """Execute (or resume) a run synchronously and keep the DB status consistent.

    Returns a dict with ``run_id``, ``status`` and, when available, ``result``
    and ``payload``. By default exceptions are recorded on the run and returned
    instead of raised, which is what background callers want.
    """
    from core.crew_builder import RateLimitError, execute_run_with_resume
    from core.db_manager import DBManager
    from core import events, run_context
    from core.workflow_contracts import (
        parse_run_result_payload,
        serialize_run_result_payload,
    )

    def _emit_status(status: str, **extra: Any) -> None:
        event_type = {
            "paused": events.RUN_PAUSED,
            "waiting_human": events.RUN_PAUSED,
            "completed": events.RUN_FINISHED,
            "failed": events.RUN_FAILED,
            "cancelled": events.RUN_CANCELLED,
            "stopped": events.RUN_CANCELLED,
        }.get(str(status or "").lower())
        if event_type:
            events.emit(run_id, event_type, {"status": status, **extra})

    db = DBManager()
    try:
        run = db.read_run(run_id)
        if not run:
            raise RunError(f"Run {run_id} not found.")

        # Model switches on the GPU are recorded on this run's event log.
        try:
            from core.gpu_gate import set_event_sink

            set_event_sink(events.emit)
        except Exception:  # noqa: BLE001 - telemetry is optional
            logger.debug("GPU event sink not installed", exc_info=True)

        try:
            with run_context.run_context(run_id):
                result_tuple = execute_run_with_resume(
                    run_id,
                    status_callback=status_callback,
                    accumulated_context=accumulated_context,
                    chat_id=chat_id,
                    on_flight_change=on_flight_change,
                )
        except RateLimitError as exc:
            message = f"Rate limit / model overloaded: {exc}"
            db.update_run(run_id, status="paused", result=message)
            logger.warning("Run %s paused: %s", run_id, exc)
            _emit_status("paused", reason="rate_limit", error=str(exc))
            return {"run_id": run_id, "status": "paused", "error": message}
        except Exception as exc:  # noqa: BLE001 - boundary of the engine
            message = f"Execution error: {exc}"
            db.update_run(run_id, status="failed", result=message)
            logger.error("Run %s failed: %s", run_id, exc, exc_info=True)
            _emit_status("failed", error=str(exc))
            if raise_on_error:
                raise RunError(message) from exc
            return {"run_id": run_id, "status": "failed", "error": message}

        final_result, global_context = _unpack_execution_result(result_tuple)

        # The engine sets "paused" itself on HITL/rate limits. Never overwrite it.
        current = db.read_run(run_id) or {}
        status = str(current.get("status") or "").lower()
        if status in PAUSED_STATUSES or status in TERMINAL_STATUSES:
            _emit_status(status)
            return {
                "run_id": run_id,
                "status": status or "unknown",
                "result": final_result,
                "global_context": global_context,
            }

        stored = current.get("result") or ""
        _, payload = parse_run_result_payload(stored)
        if payload:
            db.update_run(run_id, status="completed", result=serialize_run_result_payload(payload))
        else:
            db.update_run(run_id, status="completed", result=str(final_result))

        logger.info("Run %s completed.", run_id)
        _emit_status("completed", result_chars=len(str(final_result or "")))
        return {
            "run_id": run_id,
            "status": "completed",
            "result": final_result,
            "payload": payload,
            "global_context": global_context,
        }
    finally:
        db.close()


def start_run(
    workflow_id: int,
    inputs: Optional[Dict[str, Any]] = None,
    *,
    source: str = "dashboard",
    accumulated_context: Optional[str] = None,
    chat_id: Optional[str] = None,
) -> int:
    """Create a run and execute it in a daemon thread. Returns the run id."""
    run_id = create_run(workflow_id, inputs=inputs, source=source)

    def _worker() -> None:
        try:
            execute_run(
                run_id,
                accumulated_context=accumulated_context,
                chat_id=chat_id,
            )
        except Exception:  # noqa: BLE001 - never kill a background thread silently
            logger.exception("Background run %s crashed.", run_id)

    threading.Thread(
        target=_worker,
        name=f"alfredo-run-{run_id}",
        daemon=True,
    ).start()
    return run_id


# ---------------------------------------------------------------------------
# Read / control
# ---------------------------------------------------------------------------


def get_run(run_id: int, db: Any = None) -> Dict[str, Any]:
    owns_db = db is None
    if db is None:
        from core.db_manager import DBManager

        db = DBManager()
    try:
        return parse_run_record(db.read_run(int(run_id)))
    finally:
        if owns_db:
            db.close()


def list_recent_runs(limit: int = 10, db: Any = None) -> List[Dict[str, Any]]:
    owns_db = db is None
    if db is None:
        from core.db_manager import DBManager

        db = DBManager()
    try:
        return [parse_run_record(r) for r in (db.read_all_runs(limit=limit) or [])]
    finally:
        if owns_db:
            db.close()


def start_resume_interrupt(
    interrupt_id: int,
    answer: str,
    *,
    on_flight_change: Optional[Callable[..., Any]] = None,
) -> None:
    """Apply a human answer and resume the run in a daemon thread (non-blocking)."""
    iid = int(interrupt_id)

    def _worker() -> None:
        try:
            resume_interrupt(iid, answer, on_flight_change=on_flight_change)
        except Exception:  # noqa: BLE001 - never kill a background thread silently
            logger.exception("Background resume of interrupt %s crashed.", iid)

    threading.Thread(
        target=_worker,
        name=f"alfredo-resume-{iid}",
        daemon=True,
    ).start()


def start_execution(run_id: int, *, chat_id: Optional[str] = None) -> None:
    """Execute an already-created run in a daemon thread.

    Used by the API after an interrupt answer has been applied, so the HTTP call
    returns immediately while the graph keeps running.
    """
    rid = int(run_id)

    def _worker() -> None:
        try:
            execute_run(rid, chat_id=chat_id)
        except Exception:  # noqa: BLE001 - never kill a background thread silently
            logger.exception("Background execution of run %s crashed.", rid)

    threading.Thread(
        target=_worker,
        name=f"alfredo-exec-{rid}",
        daemon=True,
    ).start()


def cancel_run(run_id: int, db: Any = None) -> bool:
    """Request cancellation.

    The graph scheduler reads the run status between nodes and stops when it
    sees ``cancelled``/``stopped``. This works across processes (unlike the old
    in-memory ``ABORT_FLAGS``).
    """
    owns_db = db is None
    if db is None:
        from core.db_manager import DBManager

        db = DBManager()
    try:
        run = db.read_run(int(run_id))
        if not run:
            return False
        if str(run.get("status") or "").lower() in TERMINAL_STATUSES:
            return False
        try:
            from core.interrupts import cancel_for_run

            cancel_for_run(int(run_id), db=db)
        except Exception:
            logger.warning("Could not cancel interrupts for run %s", run_id, exc_info=True)
        db.update_run(int(run_id), status="cancelled", result=run.get("result") or "")
        from core import events

        events.emit(int(run_id), events.RUN_CANCELLED, {"reason": "user_request"})
        return True
    finally:
        if owns_db:
            db.close()


# ---------------------------------------------------------------------------
# Human approvals (persistent interrupts)
# ---------------------------------------------------------------------------


def pending_interrupts(
    run_id: Optional[int] = None,
    chat_id: Optional[str] = None,
    db: Any = None,
) -> List[Dict[str, Any]]:
    from core.interrupts import pending_interrupts as _pending

    return _pending(run_id=run_id, chat_id=chat_id, db=db)


def resume_interrupt(
    interrupt_id: int,
    answer: str,
    *,
    execute: bool = True,
    on_flight_change: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Apply a human answer to a paused run and resume it.

    Returns ``{"status": "stale"|"resumed"|"completed"|"failed"|"paused", ...}``.
    The approval row is kept with ``status='answered'`` for auditability.
    """
    from core.db_manager import DBManager
    from core.interrupts import is_approval_answer

    db = DBManager()
    try:
        intr = db.get_run_interrupt(int(interrupt_id))
        if not intr or str(intr.get("status")).lower() != "pending":
            return {"status": "stale", "interrupt_id": int(interrupt_id)}

        run_id = int(intr["run_id"])
        run = db.read_run(run_id)
        if not run:
            return {"status": "failed", "error": f"Run {run_id} not found."}

        payload = intr.get("payload") or {}
        task_id = intr.get("task_id")
        node_id = str(intr.get("node_id") or "")
        task_outputs = _json_dict(run.get("task_outputs"))

        original = str(payload.get("task_out") or "")
        edited = original
        answer_text = (answer or "").strip()

        try:
            if answer_text and "Git Rollback" in answer_text and payload.get("pre_step_sha"):
                from core.git_workspace import get_git_workspace

                rollback = get_git_workspace().rollback_to_commit(
                    payload["pre_step_sha"],
                    reason=f"HITL rollback su {node_id or task_id}",
                )
                edited = original + f"\n\n[⏪ Git Rollback Eseguito: {rollback.get('message', '')}]"
            elif answer_text and not is_approval_answer(answer_text):
                from core.master_ai import MasterAI

                edited = MasterAI().process_validation_feedback(original, answer_text)
        except Exception as exc:  # noqa: BLE001 - keep the interrupt pending
            logger.error("Could not apply feedback for interrupt %s: %s", interrupt_id, exc, exc_info=True)
            return {"status": "failed", "interrupt_id": int(interrupt_id), "error": str(exc)}

        if task_id is not None:
            task_outputs[str(task_id)] = edited
        if node_id:
            task_outputs[f"__hitl_ok__{node_id}"] = True
            task_outputs.pop(f"__interrupt__{node_id}", None)

        db.update_run(run_id, status="running", result=edited, task_outputs=task_outputs)
        db.answer_run_interrupt(int(interrupt_id), answer_text or "approved")
        from core import events

        events.emit(
            run_id,
            events.INTERRUPT_ANSWERED,
            {
                "interrupt_id": int(interrupt_id),
                "node_id": node_id,
                "answer": (answer_text or "approved")[:400],
                "edited": edited != original,
            },
            db=db,
        )
        logger.info("Interrupt %s answered; resuming run %s.", interrupt_id, run_id)

        if not execute:
            return {"status": "resumed", "run_id": run_id, "interrupt_id": int(interrupt_id)}

        outcome = execute_run(
            run_id,
            chat_id=str(intr.get("chat_id")) if intr.get("chat_id") else None,
            on_flight_change=on_flight_change,
        )
        outcome["interrupt_id"] = int(interrupt_id)
        return outcome
    finally:
        db.close()


def is_active(run: Dict[str, Any]) -> bool:
    status = str((run or {}).get("status") or "").lower()
    return status not in TERMINAL_STATUSES and status not in PAUSED_STATUSES


__all__ = [
    "RunError",
    "PAUSED_STATUSES",
    "TERMINAL_STATUSES",
    "cancel_run",
    "create_run",
    "execute_run",
    "get_run",
    "is_active",
    "list_recent_runs",
    "parse_run_record",
    "pending_interrupts",
    "resume_interrupt",
    "start_execution",
    "start_resume_interrupt",
    "start_run",
]
