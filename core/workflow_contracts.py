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

# Granular capability tiers (basic → expert)
MODEL_TIER_BASIC = "basic"
MODEL_TIER_STANDARD = "standard"
MODEL_TIER_ADVANCED = "advanced"
MODEL_TIER_EXPERT = "expert"
MODEL_TIERS = (MODEL_TIER_BASIC, MODEL_TIER_STANDARD,
               MODEL_TIER_ADVANCED, MODEL_TIER_EXPERT)

# Backward-compatible aliases for legacy code / configs
MODEL_TIER_SIMPLE = MODEL_TIER_BASIC      # "simple" → "basic"
MODEL_TIER_DEFAULT = MODEL_TIER_STANDARD   # "default" → "standard"

# Mapping that accepts both old and new tier names
_TIER_ALIAS_MAP = {
    "basic": MODEL_TIER_BASIC,
    "simple": MODEL_TIER_BASIC,
    "standard": MODEL_TIER_STANDARD,
    "default": MODEL_TIER_STANDARD,
    "advanced": MODEL_TIER_ADVANCED,
    "expert": MODEL_TIER_EXPERT,
}

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
        # Use alias map for backward compat (simple→basic, default→standard)
        resolved = _TIER_ALIAS_MAP.get(tier)
        if resolved:
            return resolved
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


def _extract_json_block(text: str) -> Optional[Dict[str, Any]]:
    """Extract first valid JSON dict from raw text or fenced ```json block."""
    if not text:
        return None
    stripped = text.strip()
    candidates = [stripped]
    fence_matches = re.findall(r"```(?:json)?\s*([\s\S]*?)```", stripped, flags=re.IGNORECASE)
    candidates.extend(m.strip() for m in fence_matches if m.strip())
    brace_start = stripped.find("{")
    brace_end = stripped.rfind("}")
    if brace_start != -1 and brace_end > brace_start:
        candidates.append(stripped[brace_start : brace_end + 1])

    for cand in candidates:
        try:
            parsed = json.loads(cand)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            continue
    return None


_BINARY_YES = {"yes", "y", "si", "sì", "true", "1", "ok", "approvato", "approved", "pass", "vero"}
_BINARY_NO = {"no", "n", "false", "0", "ko", "rifiutato", "rejected", "fail", "falso"}


def normalize_binary_decision(value: Any) -> Optional[str]:
    """Normalize English/Italian yes-no values to 'yes' or 'no' (or None)."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)) and value in (0, 1):
        return "yes" if int(value) == 1 else "no"
    if value is None:
        return None
    low = str(value).strip().lower()
    if low in _BINARY_YES:
        return "yes"
    if low in _BINARY_NO:
        return "no"
    return None


def build_structured_handoff_envelope(
    raw_output: str,
    *,
    task_id: Optional[int] = None,
    task_name: str = "",
    agent_role: str = "",
    node_id: str = "",
    handoff_contract: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Deterministically build a compact, schema-consistent handoff envelope from any upstream
    agent/node output so downstream models receive extracted summary, key facts, structured data,
    and binary decision without needing manual memory key lookups.
    """
    text = (raw_output or "").strip()
    structured_data: Dict[str, Any] = _extract_json_block(text) or {}

    decision: Optional[str] = None
    reason: str = ""
    if structured_data:
        for d_key in ("decision", "route", "approved", "pass", "go"):
            if d_key in structured_data:
                norm = normalize_binary_decision(structured_data[d_key])
                if norm:
                    decision = norm
                    break
        reason = str(
            structured_data.get("reason")
            or structured_data.get("rationale")
            or structured_data.get("explanation")
            or ""
        ).strip()

    # Extract key facts from JSON list or markdown bullets / numbered list
    key_facts: List[str] = []
    if isinstance(structured_data.get("key_facts"), list):
        key_facts = [str(item).strip() for item in structured_data["key_facts"] if str(item).strip()][:8]
    elif isinstance(structured_data.get("key_findings"), list):
        key_facts = [str(item).strip() for item in structured_data["key_findings"] if str(item).strip()][:8]

    if not key_facts and text:
        for line in text.splitlines():
            s = line.strip()
            m = re.match(r"^(?:[-*•]|\d+[.)])\s+(.+)$", s)
            if m:
                fact = re.sub(r"\*\*(.+?)\*\*", r"\1", m.group(1)).strip()
                if fact and fact not in key_facts:
                    key_facts.append(fact[:240])
                if len(key_facts) >= 8:
                    break

    # Extract concise summary
    summary = ""
    if structured_data:
        for s_key in ("summary", "conclusion", "executive_summary", "result", "answer"):
            val = structured_data.get(s_key)
            if isinstance(val, str) and val.strip():
                summary = val.strip()
                break

    if not summary and text:
        # Prefer explicit # Summary / # Conclusion sections if present
        sec_match = re.search(
            r"(?im)^#{1,3}\s*(?:conclusion|summary|riepilogo|sintesi|executive summary)[^\n]*\n+([\s\S]{15,700}?)(?=\n#{1,3}\s|\Z)",
            text,
        )
        if sec_match:
            summary = sec_match.group(1).strip()
        else:
            # Strip markdown headings / fences and pick opening informative paragraph
            cleaned_lines = [
                ln.strip()
                for ln in text.splitlines()
                if ln.strip() and not ln.strip().startswith(("```", "[KEYWORDS:", "# Topic:"))
            ]
            summary = " ".join(cleaned_lines)

    if len(summary) > 500:
        summary = summary[:497].rstrip() + "..."

    # Apply optional handoff contract metadata hints (e.g. expected schema / fields)
    if isinstance(handoff_contract, dict) and handoff_contract.get("required_fields"):
        for req_f in handoff_contract.get("required_fields") or []:
            if req_f not in structured_data and req_f in ("summary", "key_facts", "decision", "reason"):
                if req_f == "summary":
                    structured_data["summary"] = summary
                elif req_f == "key_facts":
                    structured_data["key_facts"] = key_facts
                elif req_f == "decision" and decision:
                    structured_data["decision"] = decision
                elif req_f == "reason" and reason:
                    structured_data["reason"] = reason

    return {
        "node_id": str(node_id or ""),
        "task_id": task_id,
        "task_name": str(task_name or ""),
        "agent_role": str(agent_role or ""),
        "summary": summary,
        "key_facts": key_facts,
        "structured_data": structured_data,
        "decision": decision,
        "reason": reason,
        "raw_output": text,
    }


def format_upstream_handoff_block(
    parent_envelopes: List[Dict[str, Any]],
    wired_values: Optional[Dict[str, str]] = None,
    handoff_contract: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Format structured upstream envelopes + wired inputs_map ports into a concise, deterministic
    prompt block injected into downstream tasks so smaller models never lose handoff context.
    """
    blocks: List[str] = []
    for env in parent_envelopes or []:
        if not isinstance(env, dict):
            continue
        label_parts = []
        if env.get("task_name"):
            label_parts.append(str(env["task_name"]))
        elif env.get("node_id"):
            label_parts.append(f"Node {env['node_id']}")
        if env.get("agent_role"):
            label_parts.append(f"({env['agent_role']})")
        header = " ".join(label_parts) or "Upstream Step"

        section = [f"### Upstream Handoff: {header}"]
        if env.get("decision"):
            dec_line = f"- **Decision**: {str(env['decision']).upper()}"
            if env.get("reason"):
                dec_line += f" — {env['reason']}"
            section.append(dec_line)
        if env.get("summary"):
            section.append(f"- **Summary**: {env['summary']}")
        if env.get("key_facts"):
            facts_str = "\n".join(f"  - {f}" for f in env["key_facts"][:6])
            section.append(f"- **Key Facts**:\n{facts_str}")
        sdata = env.get("structured_data")
        if isinstance(sdata, dict) and sdata:
            compact = {
                k: v
                for k, v in sdata.items()
                if k not in ("raw_output", "summary", "key_facts", "key_findings")
            }
            if compact:
                compact_json = json.dumps(compact, ensure_ascii=False)
                if len(compact_json) > 600:
                    compact_json = compact_json[:597] + "..."
                section.append(f"- **Structured Data**: `{compact_json}`")
        blocks.append("\n".join(section))

    if wired_values:
        wired_lines = ["### Wired Port Inputs (`inputs_map`)"]
        for port, val in wired_values.items():
            val_s = str(val or "").strip()
            if len(val_s) > 600:
                val_s = val_s[:597] + "..."
            wired_lines.append(f"- **{port}**: {val_s}")
        blocks.append("\n".join(wired_lines))

    if isinstance(handoff_contract, dict) and handoff_contract.get("output_instruction"):
        blocks.append(f"### Required Output Format\n{handoff_contract['output_instruction']}")

    if not blocks:
        return ""
    return (
        "\n\n--- [STRUCTURED HANDOFF FROM UPSTREAM] ---\n"
        + "\n\n".join(blocks)
        + "\n--- [END STRUCTURED HANDOFF] ---\n"
    )


def enforce_node_handoff(
    memory_manager,
    *,
    run_id: int,
    task_id: int,
    task_out: str,
    agent_role: str,
    task_tools: Sequence[str],
    node_id: str = "",
    task_name: str = "",
    handoff_contract: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Hard handoff contract after a node completes:
    - memory key task_<id> must exist (auto-save structured envelope if missing)
    - if any WRITE_TOOL assigned → ensure at least one workspace file exists
    """
    memory_key = f"task_{task_id}"
    envelope = build_structured_handoff_envelope(
        task_out,
        task_id=task_id,
        task_name=task_name,
        agent_role=agent_role,
        node_id=node_id,
        handoff_contract=handoff_contract,
    )

    # Ensure ephemeral memory record (caller usually already auto-saved; this is a hard guarantee)
    try:
        existing = None
        if hasattr(memory_manager, "read_record"):
            existing = memory_manager.read_record(memory_key)
        elif hasattr(memory_manager, "get_record"):
            existing = memory_manager.get_record(memory_key)
    except Exception:
        existing = None
    if not existing and memory_manager is not None:
        try:
            structured_payload = dict(envelope.get("structured_data") or {})
            structured_payload.setdefault("summary", envelope.get("summary", ""))
            if envelope.get("key_facts"):
                structured_payload.setdefault("key_facts", envelope["key_facts"])
            if envelope.get("decision"):
                structured_payload.setdefault("decision", envelope["decision"])
                structured_payload.setdefault("reason", envelope.get("reason", ""))
            structured_payload.setdefault("raw_output", (task_out or "")[:2000])
            summary = envelope.get("summary") or (task_out or "")[:500]
            memory_manager.write_record(
                key=memory_key,
                content_summary=f"Output of task '{task_id}' by agent '{agent_role}': {summary}",
                structured_data=structured_payload,
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
        "envelope": envelope,
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


# Typed handoff ports (function-block schema):
# - Control deps: node.depends_on
# - Data wires: node.inputs_map[port] = {"from": node_id|"input", "key": field}
# - Out: ephemeral memory key task_<id> (+ optional output_pydantic fields)
# See core.workflow_graph for normalize / validate / desugar.


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
