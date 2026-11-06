"""
core/events.py

Append-only event log for workflow runs.

Each run owns a dense, gap-free sequence of events (``seq`` 1..N). The log is
the source of truth for "what happened" and lets a client reconnect with
``after_seq`` and receive exactly the missing events, with no duplicates.

Events are best-effort: a logging failure must never break a run, so ``emit``
swallows storage errors after warning.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

MAX_EVENTS_PER_RUN = max(100, int(os.getenv("ALFREDO_RUN_EVENTS_MAX", "5000") or 5000))
MAX_PAYLOAD_CHARS = 16000

# --- Event types -----------------------------------------------------------
RUN_STARTED = "run.started"
RUN_PAUSED = "run.paused"
RUN_FINISHED = "run.finished"
RUN_FAILED = "run.failed"
RUN_CANCELLED = "run.cancelled"
NODE_STARTED = "run.node.started"
NODE_COMPLETED = "run.node.completed"
NODE_FAILED = "run.node.failed"
INTERRUPT_OPENED = "run.interrupt.opened"
INTERRUPT_ANSWERED = "run.interrupt.answered"
AUTOMATION_FIRED = "automation.fired"
SYSTEM_MODEL_SWITCH = "system.model.switch"

TERMINAL_EVENTS = {RUN_FINISHED, RUN_FAILED, RUN_CANCELLED}


def _json_safe(payload: Dict[str, Any]) -> str:
    """Serialize a payload, keeping the row small and always valid JSON."""
    try:
        raw = json.dumps(payload or {}, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        raw = json.dumps({"_unserializable": str(payload)[:MAX_PAYLOAD_CHARS]})
    if len(raw) > MAX_PAYLOAD_CHARS:
        raw = json.dumps({"_truncated": True, "text": raw[:MAX_PAYLOAD_CHARS]})
    return raw


def emit(
    run_id: Optional[int],
    event_type: str,
    payload: Optional[Dict[str, Any]] = None,
    db: Any = None,
) -> Optional[int]:
    """Append one event and return its ``seq`` (``None`` when it was dropped)."""
    if run_id is None:
        return None
    owns_db = db is None
    if db is None:
        try:
            from core.db_manager import DBManager

            db = DBManager()
        except Exception as exc:  # noqa: BLE001 - never break the run for logging
            logger.warning("Event log unavailable: %s", exc)
            return None
    try:
        return db.append_run_event(
            int(run_id),
            str(event_type),
            _json_safe(payload or {}),
            keep=MAX_EVENTS_PER_RUN,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not emit %s for run %s: %s", event_type, run_id, exc)
        return None
    finally:
        if owns_db:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass


def _parse_row(row: Dict[str, Any]) -> Dict[str, Any]:
    raw = row.get("payload_json")
    payload: Dict[str, Any] = {}
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
        except (json.JSONDecodeError, TypeError):
            payload = {"_raw": raw[:MAX_PAYLOAD_CHARS]}
    return {
        "id": row.get("id"),
        "run_id": row.get("run_id"),
        "seq": int(row.get("seq") or 0),
        "type": row.get("type"),
        "payload": payload,
        "created_at": str(row.get("created_at") or ""),
    }


def tail(
    after_seq: int = 0,
    run_id: Optional[int] = None,
    limit: int = 200,
    db: Any = None,
) -> List[Dict[str, Any]]:
    """Return the events after ``after_seq`` (optionally scoped to one run)."""
    owns_db = db is None
    if db is None:
        from core.db_manager import DBManager

        db = DBManager()
    try:
        rows = db.read_run_events(after_seq=after_seq, run_id=run_id, limit=limit) or []
        return [_parse_row(row) for row in rows]
    finally:
        if owns_db:
            db.close()


def latest_seq(run_id: int, db: Any = None) -> int:
    owns_db = db is None
    if db is None:
        from core.db_manager import DBManager

        db = DBManager()
    try:
        return int(db.latest_run_event_seq(int(run_id)) or 0)
    finally:
        if owns_db:
            db.close()


def replay(events: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Fold an event stream into the current state of a run.

    This is what lets a client that reconnects render the run without asking the
    server for a separate snapshot.
    """
    status = "unknown"
    nodes: Dict[str, str] = {}
    interrupts: Dict[int, str] = {}
    last_seq = 0

    for event in events:
        seq = int(event.get("seq") or 0)
        last_seq = max(last_seq, seq)
        etype = str(event.get("type") or "")
        payload = event.get("payload") or {}

        if etype == RUN_STARTED:
            status = "running"
        elif etype == RUN_PAUSED:
            status = "paused"
        elif etype == RUN_FINISHED:
            status = "completed"
        elif etype == RUN_FAILED:
            status = "failed"
        elif etype == RUN_CANCELLED:
            status = "cancelled"
        elif etype == NODE_STARTED:
            node = str(payload.get("node_id") or "")
            if node:
                nodes[node] = "running"
        elif etype == NODE_COMPLETED:
            node = str(payload.get("node_id") or "")
            if node:
                nodes[node] = "completed"
        elif etype == NODE_FAILED:
            node = str(payload.get("node_id") or "")
            if node:
                nodes[node] = "failed"
        elif etype == INTERRUPT_OPENED:
            iid = payload.get("interrupt_id")
            if iid is not None:
                interrupts[int(iid)] = "pending"
        elif etype == INTERRUPT_ANSWERED:
            iid = payload.get("interrupt_id")
            if iid is not None:
                interrupts[int(iid)] = "answered"

    return {
        "status": status,
        "nodes": nodes,
        "interrupts": interrupts,
        "pending_interrupts": sorted(i for i, s in interrupts.items() if s == "pending"),
        "last_seq": last_seq,
    }


__all__ = [
    "AUTOMATION_FIRED",
    "INTERRUPT_ANSWERED",
    "INTERRUPT_OPENED",
    "MAX_EVENTS_PER_RUN",
    "NODE_COMPLETED",
    "NODE_FAILED",
    "NODE_STARTED",
    "RUN_CANCELLED",
    "RUN_FAILED",
    "RUN_FINISHED",
    "RUN_PAUSED",
    "RUN_STARTED",
    "TERMINAL_EVENTS",
    "emit",
    "latest_seq",
    "replay",
    "tail",
]
