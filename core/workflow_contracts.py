"""
Shared workflow contracts for Alfredo.

Defines:
- Per-node handoff: ephemeral memory + optional write-tool file
- Global workflow result: procedure summary + expected exports
- Tool profiles: intermediate (no final writes) vs final_writer

Orchestration model:
- Master AI is the sole orchestrator (plan / decompose / refine / export).
- Every DAG agent/task is a worker by default — no per-node supervisor role.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

# Tools that persist durable artifacts (must stay sandboxed / ACL-gated)
WRITE_TOOLS: Set[str] = {
    "write_file",
    "write_python_file",
    "create_word_document",
    "edit_word_document",
    "create_excel_document",
    "merge_and_save_data",
}

# Always allowed even for intermediate agents (handoff channel)
MEMORY_TOOLS: Set[str] = {
    "read_atomic_memory",
    "write_atomic_memory",
}

TOOL_PROFILE_INTERMEDIATE = "intermediate"
TOOL_PROFILE_FINAL_WRITER = "final_writer"
TOOL_PROFILES = (TOOL_PROFILE_INTERMEDIATE, TOOL_PROFILE_FINAL_WRITER)

MODEL_TIER_DEFAULT = "default"
MODEL_TIER_SIMPLE = "simple"
MODEL_TIERS = (MODEL_TIER_DEFAULT, MODEL_TIER_SIMPLE)

WORKER_HANDOFF_DIRECTIVE = """

--- WORKER HANDOFF CONTRACT ---
You are a worker agent in this workflow. Master AI is the sole orchestrator.
When you finish:
1. Your output is auto-saved to ephemeral memory under key task_<id> for the next worker.
2. If you have a write tool assigned, you MUST also write the corresponding file in the workspace and mention the relative path in your output.
"""


def resolve_model_tier(step_def: Any) -> str:
    if isinstance(step_def, dict):
        tier = (step_def.get("model_tier") or MODEL_TIER_DEFAULT).strip().lower()
        if tier in MODEL_TIERS:
            return tier
    return MODEL_TIER_DEFAULT


def resolve_tool_profile(
    task_record: Optional[Dict[str, Any]] = None,
    step_def: Any = None,
) -> str:
    """
    Precedence: node override → task.tool_profile → inferred from write tools → intermediate.
    Nodes with write tools default to final_writer; others to intermediate.
    """
    if isinstance(step_def, dict):
        override = (step_def.get("tool_profile") or "").strip().lower()
        if override in TOOL_PROFILES:
            return override

    if task_record:
        profile = (task_record.get("tool_profile") or "").strip().lower()
        if profile in TOOL_PROFILES:
            return profile
        tools = task_record.get("tools") or []
        if any(t in WRITE_TOOLS for t in tools):
            return TOOL_PROFILE_FINAL_WRITER

    return TOOL_PROFILE_INTERMEDIATE


def filter_tool_names_by_profile(tool_names: Sequence[str], profile: str) -> List[str]:
    """Strip durable write tools from intermediate agents; keep memory tools."""
    names = list(tool_names or [])
    if profile != TOOL_PROFILE_INTERMEDIATE:
        return names
    filtered = [t for t in names if t not in WRITE_TOOLS]
    if len(filtered) != len(names):
        stripped = sorted(set(names) - set(filtered))
        logger.info(f"Tool profile '{profile}': stripped write tools {stripped}")
    return filtered


def resolve_pydantic_kwargs(output_pydantic: Optional[str]) -> Dict[str, Any]:
    """Build CrewAI Task kwargs for output_pydantic (single or multi-schema)."""
    if not output_pydantic:
        return {}
    from core.schema_loader import get_schema_class
    from pydantic import create_model

    schemas = [s.strip() for s in str(output_pydantic).split(",") if s.strip()]
    if not schemas:
        return {}
    if len(schemas) == 1:
        cls = get_schema_class(schemas[0])
        return {"output_pydantic": cls} if cls else {}

    fields = {}
    for s in schemas:
        cls = get_schema_class(s)
        if cls:
            fields[s.lower()] = (cls, ...)
    if not fields:
        return {}
    DynamicModel = create_model("DynamicOutputSchema", **fields)
    return {"output_pydantic": DynamicModel}


_FILE_PATH_PATTERNS = [
    re.compile(r"(?:written to|saved to|created|file(?:path)?)\s*[:\-]?\s*['\"]?([^\s'\"]+\.\w+)", re.I),
    re.compile(r"(?:workspace[/\\])([^\s'\"]+\.\w+)", re.I),
    re.compile(r"['\"]([A-Za-z0-9_\-./\\]+\.(?:py|md|txt|json|docx|xlsx|csv|html))['\"]"),
]


def extract_file_paths_from_text(text: str) -> List[str]:
    if not text:
        return []
    found: List[str] = []
    for pat in _FILE_PATH_PATTERNS:
        for m in pat.finditer(text):
            path = m.group(1).strip().lstrip("./")
            if path and path not in found:
                found.append(path)
    return found


def workspace_path_exists(rel_or_abs: str) -> bool:
    from tools.local_tools import WORKSPACE_DIR

    candidate = rel_or_abs
    if not os.path.isabs(candidate):
        candidate = os.path.join(WORKSPACE_DIR, candidate)
    return os.path.isfile(os.path.abspath(candidate))


def ensure_fallback_write_file(
    run_id: int,
    task_id: int,
    content: str,
    preferred_ext: str = "txt",
) -> str:
    """Write agent output to workspace when a write tool was assigned but no file was produced."""
    from tools.local_tools import WORKSPACE_DIR

    rel = os.path.join("runs", str(run_id), f"task_{task_id}.{preferred_ext}")
    full = os.path.join(WORKSPACE_DIR, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content or "")
    return rel.replace("\\", "/")


def enforce_node_handoff(
    memory_manager,
    *,
    run_id: int,
    task_id: int,
    task_out: str,
    agent_role: str,
    task_tools: Sequence[str],
    node_id: str = "",
) -> Dict[str, Any]:
    """
    Hard handoff contract after a node completes:
    - memory key task_<id> must exist (auto-save if missing)
    - if any WRITE_TOOL assigned → ensure at least one workspace file exists
    """
    memory_key = f"task_{task_id}"

    # Ensure ephemeral memory record (caller usually already auto-saved; this is a hard guarantee)
    try:
        existing = None
        if hasattr(memory_manager, "read_record"):
            existing = memory_manager.read_record(memory_key)
        elif hasattr(memory_manager, "get_record"):
            existing = memory_manager.get_record(memory_key)
    except Exception:
        existing = None
    if not existing:
        try:
            try:
                structured = json.loads(task_out) if task_out and task_out.strip().startswith(("{", "[")) else {"raw_output": task_out}
            except Exception:
                structured = {"raw_output": task_out}
            summary = (task_out or "")[:500]
            memory_manager.write_record(
                key=memory_key,
                content_summary=f"Output of task '{task_id}' by agent '{agent_role}': {summary}",
                structured_data=structured if isinstance(structured, dict) else {"raw_output": task_out},
                agent_role=agent_role,
            )
        except Exception as e:
            logger.warning(f"Handoff auto-save failed for {memory_key}: {e}")

    assigned_writes = sorted(set(task_tools or []) & WRITE_TOOLS)
    file_paths = extract_file_paths_from_text(task_out or "")
    verified = [p for p in file_paths if workspace_path_exists(p)]

    if assigned_writes and not verified:
        # Prefer .py if write_python_file was assigned
        ext = "py" if "write_python_file" in assigned_writes else "txt"
        if "create_excel_document" in assigned_writes or "merge_and_save_data" in assigned_writes:
            ext = "txt"  # content dump; real xlsx may already exist elsewhere
        fallback = ensure_fallback_write_file(run_id, task_id, task_out, preferred_ext=ext)
        verified = [fallback]
        logger.info(
            f"Handoff: write tool(s) {assigned_writes} assigned but no file found; "
            f"wrote fallback '{fallback}'"
        )

    return {
        "node_id": node_id,
        "task_id": task_id,
        "memory_key": memory_key,
        "file_paths": verified,
        "write_tools": assigned_writes,
        "agent_role": agent_role,
    }


def build_procedure_summary(
    dag_nodes: Dict[str, Dict[str, Any]],
    node_outputs: Dict[str, str],
    handoffs: List[Dict[str, Any]],
    completed_order: Optional[List[str]] = None,
) -> str:
    """Human-readable procedure summary of the workflow run."""
    handoff_by_task = {h["task_id"]: h for h in handoffs if "task_id" in h}
    order = completed_order or list(node_outputs.keys())
    lines = ["# Workflow Procedure Summary", "", "_Orchestrator: Master AI — all steps below are workers._", ""]
    step_n = 0
    for n_id in order:
        data = dag_nodes.get(n_id) or {}
        step_n += 1
        out = node_outputs.get(n_id, "") or ""
        snippet = out.strip().replace("\n", " ")
        if len(snippet) > 220:
            snippet = snippet[:217] + "..."

        if data.get("is_batch"):
            lines.append(f"{step_n}. **[{n_id}]** batch_loop (worker)")
        else:
            tid = data.get("task_id")
            lines.append(f"{step_n}. **[{n_id}]** task `{tid}` (worker)")
            h = handoff_by_task.get(tid)
            if h:
                files = ", ".join(h.get("file_paths") or []) or "—"
                lines.append(f"   - memory: `{h.get('memory_key')}` | files: {files}")
        if snippet:
            lines.append(f"   - outcome: {snippet}")
        lines.append("")
    return "\n".join(lines).strip()


def assemble_workflow_result(
    *,
    final_result: str,
    procedure_summary: str,
    exports: Optional[List[str]] = None,
    handoffs: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    return {
        "final_result": final_result or "",
        "procedure_summary": procedure_summary or "",
        "exports": list(exports or []),
        "handoffs": list(handoffs or []),
    }


def serialize_run_result_payload(payload: Dict[str, Any]) -> str:
    """Store structured result as JSON string in workflow_runs.result when useful."""
    return json.dumps(payload, ensure_ascii=False, indent=2)


def parse_run_result_payload(result: Any) -> Tuple[str, Optional[Dict[str, Any]]]:
    """
    Returns (display_text, structured_payload_or_None).
    Backward compatible with plain-string results.
    """
    if result is None:
        return "", None
    if isinstance(result, dict):
        text = result.get("final_result") or result.get("result") or ""
        return str(text), result
    text = str(result)
    try:
        data = json.loads(text)
        if isinstance(data, dict) and ("final_result" in data or "procedure_summary" in data):
            return str(data.get("final_result") or ""), data
    except (json.JSONDecodeError, TypeError):
        pass
    return text, None
