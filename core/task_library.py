"""
Task templates vs. workflow instances.

The ``tasks`` table is the **library of templates**. A workflow node can either:

* **link** a template (``task_id`` only) — legacy behaviour: editing the
  template changes every workflow that links it, which is convenient but
  silently mutates saved workflows;
* **own an instance** (``task_id`` + ``task_snapshot``) — a deep copy captured
  when the task is added to the workflow. Editing the template later does not
  touch the workflow; the UI shows a diff and offers an explicit *sync*.

Batch nodes keep one snapshot per inner task in ``task_snapshots`` keyed by the
task id as a string, so a batch can mix instances and plain links.

This module is pure Python (no CrewAI, no Streamlit) so the engine, the
dashboard and the API share the same rules and it can be unit-tested.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

# Fields copied from a task row into a node snapshot.
SNAPSHOT_FIELDS = (
    "name",
    "description",
    "expected_output",
    "agent_id",
    "model_id",
    "agent_specialization",
    "tools",
    "required_inputs",
    "vector_dbs",
    "human_validation",
    "max_input_context",
    "max_output_tokens",
    "output_pydantic",
    "tool_profile",
)

LIST_FIELDS = ("tools", "required_inputs", "vector_dbs")
INT_FIELDS = ("agent_id", "model_id", "max_input_context", "max_output_tokens")

SNAPSHOT_KEY = "task_snapshot"
BATCH_SNAPSHOTS_KEY = "task_snapshots"

_DEFAULTS: Dict[str, Any] = {
    "name": None,
    "description": "",
    "expected_output": "",
    "agent_id": None,
    "model_id": None,
    "agent_specialization": None,
    "tools": [],
    "required_inputs": [],
    "vector_dbs": [],
    "human_validation": 0,
    "max_input_context": 0,
    "max_output_tokens": 0,
    "output_pydantic": None,
    "tool_profile": "",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return [value]
        if isinstance(parsed, list):
            return parsed
        if parsed is None:
            return []
        return [parsed]
    if isinstance(value, (tuple, set)):
        return list(value)
    return [value]


def _coerce(field: str, value: Any) -> Any:
    if field in LIST_FIELDS:
        return _as_list(value)
    if field == "human_validation":
        try:
            return int(bool(int(value))) if not isinstance(value, bool) else int(value)
        except (TypeError, ValueError):
            return int(bool(value))
    if field in INT_FIELDS:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if value is None:
        return _DEFAULTS.get(field)
    return value


def _norm(value: Any) -> Any:
    """Normalize a value so snapshots and DB rows compare cleanly."""
    if isinstance(value, (list, dict)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    if isinstance(value, bool):
        return int(value)
    return value


# ---------------------------------------------------------------------------
# Snapshot creation / attachment
# ---------------------------------------------------------------------------


def snapshot_from_record(
    record: Optional[Dict[str, Any]],
    *,
    task_id: Optional[int] = None,
    captured_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a self-contained snapshot of a task template."""
    record = record or {}
    snapshot = {field: _coerce(field, record.get(field)) for field in SNAPSHOT_FIELDS}
    for field, default in _DEFAULTS.items():
        snapshot.setdefault(field, copy.deepcopy(default))
    snapshot["source_task_id"] = int(task_id) if task_id is not None else None
    snapshot["captured_at"] = captured_at or _now()
    snapshot["synced_at"] = snapshot["captured_at"]
    return snapshot


def is_batch_node(node: Any) -> bool:
    return isinstance(node, dict) and (
        node.get("type") == "batch_loop" or (node.get("task_ids") is not None and node.get("task_id") is None)
    )


def snapshot_for(node: Any, task_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Return the instance snapshot of a node (or of one batch inner task)."""
    if not isinstance(node, dict):
        return None
    if task_id is not None and is_batch_node(node):
        snapshot = (node.get(BATCH_SNAPSHOTS_KEY) or {}).get(str(task_id))
        return snapshot if isinstance(snapshot, dict) and snapshot else None
    snapshot = node.get(SNAPSHOT_KEY)
    if isinstance(snapshot, dict) and snapshot:
        return snapshot
    return None


def is_instance(node: Any, task_id: Optional[int] = None) -> bool:
    """True when the node owns a snapshot instead of linking the template."""
    return snapshot_for(node, task_id=task_id) is not None


def attach_snapshot(
    node: Dict[str, Any],
    record: Optional[Dict[str, Any]],
    *,
    task_id: Optional[int] = None,
    captured_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a copy of ``node`` carrying a snapshot of ``record``."""
    out = copy.deepcopy(node or {})
    tid = task_id if task_id is not None else out.get("task_id")
    snapshot = snapshot_from_record(record, task_id=tid, captured_at=captured_at)

    if is_batch_node(out):
        if tid is None:
            out[SNAPSHOT_KEY] = snapshot
        else:
            snapshots = dict(out.get(BATCH_SNAPSHOTS_KEY) or {})
            snapshots[str(int(tid))] = snapshot
            out[BATCH_SNAPSHOTS_KEY] = snapshots
    else:
        out[SNAPSHOT_KEY] = snapshot
    return out


def detach_snapshot(node: Dict[str, Any], *, task_id: Optional[int] = None) -> Dict[str, Any]:
    """Return a copy of ``node`` that links the template again."""
    out = copy.deepcopy(node or {})
    if task_id is not None and is_batch_node(out):
        snapshots = dict(out.get(BATCH_SNAPSHOTS_KEY) or {})
        snapshots.pop(str(int(task_id)), None)
        if snapshots:
            out[BATCH_SNAPSHOTS_KEY] = snapshots
        else:
            out.pop(BATCH_SNAPSHOTS_KEY, None)
    else:
        out.pop(SNAPSHOT_KEY, None)
    return out


# ---------------------------------------------------------------------------
# Runtime resolution
# ---------------------------------------------------------------------------


def resolve_task_record(
    task_id: Optional[int],
    node: Any,
    db: Any,
) -> Dict[str, Any]:
    """Task record to execute: the node snapshot, falling back to the library.

    A snapshot fully replaces the template fields, so a workflow keeps working
    (and keeps its behaviour) even if the template was edited or deleted.
    """
    if task_id is None:
        return {}

    snapshot = snapshot_for(node, task_id=task_id)
    try:
        base = db.read_task(int(task_id)) if db is not None else None
    except Exception:  # noqa: BLE001 - never fail a run because of a read
        base = None

    if not snapshot:
        return base or {}

    merged: Dict[str, Any] = dict(base or {})
    merged.setdefault("id", int(task_id))
    for field in SNAPSHOT_FIELDS:
        merged[field] = _coerce(field, snapshot.get(field))
    return merged


# ---------------------------------------------------------------------------
# Diff / sync
# ---------------------------------------------------------------------------


def snapshot_diff(
    node: Dict[str, Any],
    record: Optional[Dict[str, Any]],
    *,
    task_id: Optional[int] = None,
) -> Dict[str, Dict[str, Any]]:
    """Fields where the node instance differs from the current template."""
    snapshot = snapshot_for(node, task_id=task_id)
    if not snapshot:
        return {}
    record = record or {}
    diff: Dict[str, Dict[str, Any]] = {}
    for field in SNAPSHOT_FIELDS:
        instance_value = _coerce(field, snapshot.get(field))
        template_value = _coerce(field, record.get(field))
        if _norm(instance_value) != _norm(template_value):
            diff[field] = {"instance": instance_value, "template": template_value}
    return diff


def sync_from_template(
    node: Dict[str, Any],
    record: Optional[Dict[str, Any]],
    *,
    task_id: Optional[int] = None,
    fields: Optional[Iterable[str]] = None,
    captured_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Update the instance snapshot with the template values.

    ``fields`` restricts the sync (e.g. only ``description``); by default every
    field is refreshed. Unknown fields are ignored on purpose.
    """
    out = copy.deepcopy(node or {})
    tid = task_id if task_id is not None else out.get("task_id")
    if is_batch_node(out):
        snapshots = dict(out.get(BATCH_SNAPSHOTS_KEY) or {})
        current = dict(snapshots.get(str(int(tid))) or {}) if tid is not None else {}
    else:
        current = dict(out.get(SNAPSHOT_KEY) or {})
    if not current:
        return out

    record = record or {}
    wanted = [f for f in (fields or SNAPSHOT_FIELDS) if f in SNAPSHOT_FIELDS]
    for field in wanted:
        current[field] = _coerce(field, record.get(field))
    current["synced_at"] = captured_at or _now()
    current.setdefault("source_task_id", int(tid) if tid is not None else None)

    if is_batch_node(out):
        if tid is not None:
            snapshots[str(int(tid))] = current
            out[BATCH_SNAPSHOTS_KEY] = snapshots
    else:
        out[SNAPSHOT_KEY] = current
    return out


# ---------------------------------------------------------------------------
# Library usage report (which workflows link a template)
# ---------------------------------------------------------------------------


def _as_steps(steps: Any) -> List[Any]:
    """Accept a list of graph steps or the raw ``task_ids_json`` string."""
    if steps is None:
        return []
    if isinstance(steps, str):
        try:
            parsed = json.loads(steps)
        except (json.JSONDecodeError, TypeError):
            return []
        return list(parsed) if isinstance(parsed, list) else []
    return list(steps)


def _graph_task_ids(steps: Any) -> List[int]:
    out: List[int] = []
    seen = set()
    for step in _as_steps(steps):
        ids: List[Any] = []
        if isinstance(step, int):
            ids = [step]
        elif isinstance(step, dict):
            if step.get("task_ids") is not None and step.get("task_id") is None:
                ids = list(step.get("task_ids") or [])
            elif step.get("task_id") is not None:
                ids = [step.get("task_id")]
        for tid in ids:
            try:
                value = int(tid)
            except (TypeError, ValueError):
                continue
            if value not in seen:
                seen.add(value)
                out.append(value)
    return out


def shared_task_usage(db: Any) -> Dict[int, List[int]]:
    """Map ``task_id -> [workflow ids]`` for every task used by a workflow."""
    usage: Dict[int, List[int]] = {}
    for workflow in db.read_all_workflows() or []:
        wf_id = workflow.get("id")
        steps = workflow.get("task_ids")
        if steps is None:
            steps = workflow.get("task_ids_json")
        for task_id in _graph_task_ids(steps):
            usage.setdefault(task_id, [])
            if wf_id is not None and wf_id not in usage[task_id]:
                usage[task_id].append(wf_id)
    return usage


def isolate_workflow_nodes(
    steps: Any,
    db: Any,
    *,
    only_shared: bool = True,
    usage: Optional[Dict[int, List[int]]] = None,
) -> List[Dict[str, Any]]:
    """Return graph steps where linked tasks become instances (snapshots).

    Used to migrate existing workflows: ``only_shared=True`` isolates just the
    tasks used by more than one workflow, which is where the mutation risk is.
    """
    usage = usage if usage is not None else shared_task_usage(db)
    out: List[Dict[str, Any]] = []
    for step in _as_steps(steps):
        if isinstance(step, int):
            node = {"type": "task", "task_id": step}
        elif isinstance(step, dict):
            node = copy.deepcopy(step)
        else:
            out.append(step)
            continue

        ids = _graph_task_ids([node])
        for task_id in ids:
            if only_shared and len(usage.get(task_id) or []) < 2:
                continue
            if is_instance(node, task_id=task_id):
                continue
            try:
                record = db.read_task(int(task_id))
            except Exception:  # noqa: BLE001
                record = None
            if not record:
                continue
            node = attach_snapshot(node, record, task_id=task_id)
        out.append(node)
    return out


__all__ = [
    "BATCH_SNAPSHOTS_KEY",
    "SNAPSHOT_FIELDS",
    "SNAPSHOT_KEY",
    "attach_snapshot",
    "detach_snapshot",
    "is_batch_node",
    "is_instance",
    "isolate_workflow_nodes",
    "resolve_task_record",
    "shared_task_usage",
    "snapshot_diff",
    "snapshot_for",
    "snapshot_from_record",
    "sync_from_template",
]
