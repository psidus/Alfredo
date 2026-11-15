"""
AI Team & Workflow Architect for Alfredo.

Enables rapid, guided multi-agent creation and workflow orchestration:
- Ready-to-use agent role archetypes (`AGENT_ARCHETYPES`)
- Binary Yes/No decision gate templates (`DECISION_GATE_TEMPLATES`)
- Orchestrator-driven blueprint generation (`generate_team_and_workflow_blueprint`)
  with automatic reuse matching of existing DB agents, optimal model tier selection,
  structured handoff contracts (`inputs_map`), and binary Yes/No decision branches.
- One-click DB + Canvas materialization (`materialize_blueprint_to_db`).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
from typing import Any, Callable, Dict, List, Optional

from core.node_executor import is_jev_decision_model
from core.task_library import attach_snapshot
from core.workflow_canvas import hydrate_graph_for_canvas
from core.workflow_contracts import WRITE_TOOLS, TOOL_PROFILE_FINAL_WRITER, TOOL_PROFILE_INTERMEDIATE
from core.workflow_graph import (
    LOOP_MODES,
    LOOP_MODE_FOR_EACH,
    LOOP_MODE_WHILE,
    LOOP_SCOPES,
    LOOP_SCOPE_LEVELS,
    LOOP_SCOPE_NODES,
    LOOP_SCOPE_WORKFLOW,
    NODE_TYPE_BATCH,
    NODE_TYPE_EXPORT,
    NODE_TYPE_HITL,
    NODE_TYPE_INPUT,
    NODE_TYPE_LLM,
    NODE_TYPE_LOOP,
    NODE_TYPE_ROUTER,
    NODE_TYPE_TASK,
    NODE_TYPE_TOOL,
    TYPED_NODE_TYPES,
    new_node_id,
    normalize_graph,
    validate_graph,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Commercial-grade Agent Archetypes (1-Click Templates)
# ---------------------------------------------------------------------------

AGENT_ARCHETYPES: Dict[str, Dict[str, Any]] = {
    "researcher": {
        "label": "🔍 Researcher & Sources (Web / Literature)",
        "name": "Source Researcher",
        "role": "Senior Information Researcher",
        "goal": "Gather verified data, primary sources, and objective evidence for the assigned goal.",
        "backstory": (
            "Investigative analyst skilled in retrieving verifiable facts from web, documentary, "
            "and scientific sources. Never fabricates missing data: always separates verified facts "
            "from hypotheses and cites precise references."
        ),
        "tools": ["search_web"],
        "recommended_tier": "standard",
    },
    "data_analyst": {
        "label": "📊 Data & Metrics Analyst",
        "name": "Data Analyst",
        "role": "Senior Data & Quantitative Analyst",
        "goal": "Transform raw data into structured metrics, quantitative comparisons, and actionable insights.",
        "backstory": (
            "Specialist in quantitative analysis, table normalization, and anomaly or trend detection. "
            "Produces structured outputs that downstream workflow steps can consume cleanly."
        ),
        "tools": [],
        "recommended_tier": "standard",
    },
    "strategist": {
        "label": "🧭 Strategist & Planner",
        "name": "Operations Strategist",
        "role": "Lead Strategy & Operations Architect",
        "goal": "Define concrete action plans, priorities, risk assessments, and decision recommendations.",
        "backstory": (
            "Results-driven strategy consultant. Synthesizes complex analyses into clear operational "
            "options, highlighting costs, benefits, risks, and Go/No-Go criteria."
        ),
        "tools": [],
        "recommended_tier": "advanced",
    },
    "tech_architect": {
        "label": "⚙️ Tech Architect / Developer",
        "name": "Tech Architect",
        "role": "Principal Software & Systems Architect",
        "goal": "Design reliable technical solutions, implementation specs, and clean maintainable code.",
        "backstory": (
            "Senior software engineer focused on reliability, explicit I/O contracts, security, "
            "and maintainability. Always verifies constraints and requirements before producing artifacts."
        ),
        "tools": ["read_file", "write_file"],
        "recommended_tier": "advanced",
    },
    "writer_editor": {
        "label": "✍️ Copywriter & Executive Editor",
        "name": "Executive Editor",
        "role": "Executive Technical Writer & Editor",
        "goal": "Draft a clear, cohesive final document ready for delivery or export.",
        "backstory": (
            "Expert editor skilled at turning technical notes and structured data into polished "
            "executive deliverables strictly grounded in upstream evidence without hallucinations."
        ),
        "tools": ["write_file"],
        "recommended_tier": "standard",
    },
    "qa_critic": {
        "label": "🛡️ QA & Fact-Checker Reviewer",
        "name": "Quality Reviewer",
        "role": "Quality Assurance & Fact-Checking Reviewer",
        "goal": "Verify completeness, factual consistency, and constraint compliance before release.",
        "backstory": (
            "Rigorous auditor tasked with catching gaps, contradictions, or unsupported claims "
            "against upstream workflow evidence."
        ),
        "tools": [],
        "recommended_tier": "basic",
    },
}


# ---------------------------------------------------------------------------
# Assisted Task Templates & Variable Detection (Task Builder + Workflow Cards)
# ---------------------------------------------------------------------------

RESERVED_TEMPLATE_VARS = {
    "previous_result",
    "context",
    "user_input",
    "input",
    "upstream",
    "source_summary",
    "step1_summary",
    "step1_facts",
    "step2_summary",
    "deep_summary",
    "gate_reason",
    "request",
}


def extract_template_variables(
    text: str,
    existing_inputs: Optional[List[Dict[str, Any]]] = None,
    *,
    excluded_vars: Optional[set[str]] = None,
) -> List[Dict[str, str]]:
    """Extract `{variable_name}` placeholders from text into Required Inputs rows."""
    existing_map: Dict[str, str] = {}
    for item in (existing_inputs or []):
        if isinstance(item, dict) and item.get("key"):
            k = str(item["key"]).strip()
            p = str(item.get("prompt") or item.get("question") or "").strip()
            if k:
                existing_map[k] = p

    blocked = {v.lower() for v in RESERVED_TEMPLATE_VARS}
    if excluded_vars:
        blocked |= {str(v).strip().lower() for v in excluded_vars if str(v).strip()}

    seen: set[str] = set()
    extracted: List[Dict[str, str]] = []
    for match in re.finditer(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", text or ""):
        var_name = match.group(1).strip()
        if not var_name or var_name.lower() in blocked or var_name in seen:
            continue
        seen.add(var_name)
        prompt_txt = existing_map.get(var_name) or (
            f"What value should be used for '{var_name.replace('_', ' ')}'?"
        )
        extracted.append({"key": var_name, "prompt": prompt_txt})
    return extracted


TASK_ARCHETYPES: Dict[str, Dict[str, Any]] = {
    "web_research": {
        "label": "🔍 Deep Web & Source Research",
        "name": "market_and_source_research",
        "description": (
            "Conduct deep research on {topic} within {target_sector}. "
            "Collect verifiable facts, quantitative benchmarks, primary sources, and URLs. "
            "Additional runtime request context: {user_input}"
        ),
        "expected_output": (
            "Structured research brief with verified key facts, bulleted insights, "
            "cited primary sources, and explicit notation of any unresolved data gaps."
        ),
        "recommended_role_key": "researcher",
        "specialization": "source verification and primary evidence extraction",
        "tools": ["search_web"],
        "tool_profile": "intermediate",
        "human_validation": False,
        "required_inputs": [
            {"key": "topic", "prompt": "What topic or product should be researched?"},
            {"key": "target_sector", "prompt": "Which industry or market sector should we focus on?"},
        ],
    },
    "data_extraction": {
        "label": "📊 Quantitative Data & KPI Analysis",
        "name": "quantitative_kpi_analysis",
        "description": (
            "Analyze quantitative metrics and trends for {dataset_focus} using upstream data ({previous_result}). "
            "Normalize comparisons, compute ratios or deltas, and highlight anomalies."
        ),
        "expected_output": (
            "Markdown summary table of normalized KPIs, key quantitative findings, and risk anomalies."
        ),
        "recommended_role_key": "data_analyst",
        "specialization": "quantitative metrics and anomaly detection",
        "tools": ["calculator", "read_file"],
        "tool_profile": "intermediate",
        "human_validation": False,
        "required_inputs": [
            {"key": "dataset_focus", "prompt": "Which metrics or dataset should be analyzed?"},
        ],
    },
    "code_generation": {
        "label": "⚙️ Code & Technical Implementation",
        "name": "technical_implementation",
        "description": (
            "Design and implement the technical module described in {component_spec}. "
            "Follow clean architecture principles, incorporate upstream context ({previous_result}), "
            "and write production-ready Python code."
        ),
        "expected_output": (
            "Clean, modular implementation with usage notes, edge-case handling, and saved workspace artifacts."
        ),
        "recommended_role_key": "tech_architect",
        "specialization": "clean architecture and automated testing",
        "tools": ["read_file", "write_python_file"],
        "tool_profile": "final_writer",
        "human_validation": False,
        "required_inputs": [
            {"key": "component_spec", "prompt": "What module or technical specification should be implemented?"},
        ],
    },
    "document_drafting": {
        "label": "✍️ Executive Report & Document Writer",
        "name": "executive_report_draft",
        "description": (
            "Synthesize upstream findings ({previous_result}) into a cohesive deliverable tailored for {audience_and_tone}. "
            "Stay strictly faithful to verified upstream evidence without fabricating numbers."
        ),
        "expected_output": (
            "Polished executive report with clear sections, actionable takeaways, and saved output file."
        ),
        "recommended_role_key": "writer_editor",
        "specialization": "executive communication and structured reporting",
        "tools": ["write_file"],
        "tool_profile": "final_writer",
        "human_validation": False,
        "required_inputs": [
            {"key": "audience_and_tone", "prompt": "Who is the target audience and what tone should be used?"},
        ],
    },
    "qa_audit": {
        "label": "🛡️ Quality, Compliance & Fact-Check Audit",
        "name": "quality_and_compliance_audit",
        "description": (
            "Audit the upstream output ({previous_result}) against {compliance_criteria}. "
            "Identify unsupported claims, missing requirements, security issues, and concrete fixes."
        ),
        "expected_output": (
            "Structured audit checklist noting PASS/FAIL per criterion, severity ratings, and corrective actions."
        ),
        "recommended_role_key": "qa_critic",
        "specialization": "fact-checking and risk verification",
        "tools": [],
        "tool_profile": "intermediate",
        "human_validation": False,
        "required_inputs": [
            {"key": "compliance_criteria", "prompt": "Which quality or compliance criteria must be verified?"},
        ],
    },
    "action_strategy": {
        "label": "🧭 Strategic Plan & Go/No-Go Options",
        "name": "strategic_action_plan",
        "description": (
            "Evaluate strategic options for {business_goal} using upstream context ({previous_result}). "
            "Compare trade-offs, costs, risks, and define measurable Go/No-Go milestones."
        ),
        "expected_output": (
            "Prioritized action roadmap with pros/cons, resource estimates, and recommended Go/No-Go decision criteria."
        ),
        "recommended_role_key": "strategist",
        "specialization": "operational planning and risk trade-offs",
        "tools": [],
        "tool_profile": "intermediate",
        "human_validation": False,
        "required_inputs": [
            {"key": "business_goal", "prompt": "What is the primary business goal and timeframe?"},
        ],
    },
}


JEV_DECISION_MODELS: Dict[str, Dict[str, Any]] = {
    "tev1": {
        "label": "Ollama tev1 (~4B · Local System One)",
        "provider": "ollama",
        "default_primitive": "noul",
    },
    "tev1:0.8b": {
        "label": "Ollama tev1:0.8b (Ultra-fast · System One)",
        "provider": "ollama",
        "default_primitive": "noul",
    },
    "jev": {
        "label": "Local Jev (Ollama /v1/systemone)",
        "provider": "ollama",
        "default_primitive": "noul",
    },
    "nimble": {
        "label": "Bespoke Nimble (Ollama System One)",
        "provider": "ollama",
        "default_primitive": "noul",
    },
    "typesafe/jev-1.14": {
        "label": "TypeSafe Jev 1.14 (OpenRouter Decisions API)",
        "provider": "openrouter",
        "default_primitive": "noul",
    },
}


DECISION_GATE_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "quality_check": {
        "label": "✅ Quality & Completeness Gate (Jev Noul Yes/No)",
        "name": "Quality Gate (Yes/No)",
        "question": (
            "Is the previous step output complete, aligned with the objective, and free of critical gaps? "
            "Yes (noul >= threshold) if ready to proceed, No if additions or fixes are required."
        ),
        "default_route": "no",
        "decision_engine": "jev",
        "decision_primitive": "noul",
        "threshold": 0.5,
    },
    "data_sufficiency": {
        "label": "📦 Data Sufficiency Check (Jev Noul Yes/No)",
        "name": "Data Sufficiency Gate (Yes/No)",
        "question": (
            "Does the collected data contain enough verified information to produce the final deliverable? "
            "Yes if sufficient, No if deeper research or additional enrichment is needed."
        ),
        "default_route": "no",
        "decision_engine": "jev",
        "decision_primitive": "noul",
        "threshold": 0.5,
    },
    "feasibility_go_nogo": {
        "label": "🚦 Go / No-Go Evaluation (Jev Noul Yes/No)",
        "name": "Go/No-Go Decision (Yes/No)",
        "question": (
            "Based on the analyzed constraints, risks, and costs, is the proposal approved (GO = Yes) "
            "or rejected / needing revision (NO-GO = No)?"
        ),
        "default_route": "no",
        "decision_engine": "jev",
        "decision_primitive": "noul",
        "threshold": 0.5,
    },
    "compliance_gate": {
        "label": "🔒 Compliance / Safety Check (Jev Noul Yes/No)",
        "name": "Compliance Gate (Yes/No)",
        "question": (
            "Does the output satisfy all security, policy, and accuracy requirements without blocking risks? "
            "Yes if compliant, No otherwise."
        ),
        "default_route": "no",
        "decision_engine": "jev",
        "decision_primitive": "noul",
        "threshold": 0.65,
    },
}


# ---------------------------------------------------------------------------
# Tool Catalog & Active Orchestrator Model Introspection
# ---------------------------------------------------------------------------

_TOOL_ALIASES: Dict[str, str] = {
    "web_search": "search_web",
    "python_repl": "python_repl_tool",
    "shell": "execute_shell_command",
    "docling": "parse_complex_document",
}
_INTERNAL_SENTINEL_TOOLS = {"read_atomic_memory", "write_atomic_memory"}


def _normalize_tool_list(raw_tools: Any, valid_tool_keys: Optional[set[str]] = None) -> List[str]:
    out: List[str] = []
    for t in (raw_tools or []):
        name = str(t or "").strip()
        if not name:
            continue
        name = _TOOL_ALIASES.get(name, name)
        if name in _INTERNAL_SENTINEL_TOOLS:
            continue
        if valid_tool_keys and name not in valid_tool_keys:
            continue
        if name not in out:
            out.append(name)
    return out


def get_available_tools_catalog(db: Any = None) -> Dict[str, str]:
    """Return {tool_id: description} combining tools_map.yaml and registered ALLOWED_TOOLS (incl. custom tools)."""
    catalog: Dict[str, str] = {}
    try:
        from core.data_manager import DataManager

        cfg_path = os.path.join(os.getcwd(), "config", "tools_map.yaml")
        if os.path.isfile(cfg_path):
            tools_cfg = DataManager.load_yaml(cfg_path) or {}
            for tid, meta in (tools_cfg.get("tools_registry") or {}).items():
                key = str(tid or "").strip()
                if not key or key in _INTERNAL_SENTINEL_TOOLS:
                    continue
                desc = ""
                if isinstance(meta, dict):
                    desc = str(meta.get("description") or meta.get("display_name") or "").strip()
                catalog[key] = desc or key
    except Exception as exc:
        logger.debug(f"Could not read config/tools_map.yaml for tool catalog: {exc}")

    try:
        from core.crew_builder import ALLOWED_TOOLS

        for tid, tool_obj in (ALLOWED_TOOLS or {}).items():
            key = str(tid or "").strip()
            if not key or key in _INTERNAL_SENTINEL_TOOLS or key in catalog:
                continue
            desc = ""
            if tool_obj is not None:
                desc = str(getattr(tool_obj, "description", "") or getattr(tool_obj, "__doc__", "") or "").strip()
            catalog[key] = (desc[:160] + "...") if len(desc) > 160 else (desc or key)
    except Exception as exc:
        logger.debug(f"Could not inspect core.crew_builder.ALLOWED_TOOLS: {exc}")

    return catalog


def resolve_orchestrator_model_info(
    db: Any = None,
    orchestrator_model_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Resolve the active MasterAI model (id, provider/model string, display label, is_local)."""
    try:
        from core.master_ai import MasterAI

        master = MasterAI(model_id=orchestrator_model_id, db_manager=db)
        m_str = master._get_model_string()
        is_loc = bool(getattr(master, "is_local", False))
        return {
            "model_id": getattr(master, "model_id", orchestrator_model_id),
            "model_string": m_str,
            "label": f"{m_str} (local)" if is_loc else m_str,
            "is_local": is_loc,
        }
    except Exception:
        pass

    models: List[Dict[str, Any]] = []
    if db is not None and hasattr(db, "read_all_models"):
        try:
            models = db.read_all_models() or []
        except Exception:
            models = []

    def _to_info(m: Dict[str, Any]) -> Dict[str, Any]:
        prov = str(m.get("provider") or "gemini").strip().lower()
        prov = {"google": "gemini", "mistralai": "mistral", "bionic": "lmstudio", "lm_studio": "lmstudio"}.get(prov, prov)
        name = str(m.get("model_name") or "").strip()
        if prov == "gemini" and name.startswith("models/"):
            name = name.replace("models/", "", 1)
        is_loc = bool(m.get("is_local")) or prov in ("ollama", "lmstudio", "bionic", "vllm", "llama.cpp")
        m_str = f"{prov}/{name}" if name else prov
        return {
            "model_id": int(m["id"]) if m.get("id") is not None else None,
            "model_string": m_str,
            "label": f"{m_str} (local)" if is_loc else m_str,
            "is_local": is_loc,
        }

    if orchestrator_model_id is not None and db is not None and hasattr(db, "read_model"):
        try:
            rec = db.read_model(int(orchestrator_model_id))
            if rec:
                return _to_info(rec)
        except Exception:
            pass

    gen = [m for m in models if not _is_decision_only_model(m)]
    if gen:
        locals_gen = [
            m for m in gen
            if bool(m.get("is_local")) or str(m.get("provider") or "").lower() in ("ollama", "lmstudio", "lm_studio", "bionic", "vllm", "llama.cpp")
        ]
        chosen = locals_gen[0] if locals_gen else gen[0]
        return _to_info(chosen)

    return {
        "model_id": None,
        "model_string": "gemini/gemini-2.5-flash-lite",
        "label": "gemini/gemini-2.5-flash-lite (default)",
        "is_local": False,
    }


# ---------------------------------------------------------------------------
# Model Selection & Existing Agent Matching
# ---------------------------------------------------------------------------


def _is_decision_only_model(model: Dict[str, Any], registry: Any = None) -> bool:
    name = str(model.get("model_name") or "").strip()
    if is_jev_decision_model(name):
        return True
    tier_str = str(model.get("capability_tier") or "").strip().lower()
    if tier_str in ("decision", "jev", "system_one", "systemone"):
        return True
    if registry is not None and hasattr(registry, "_catalog"):
        entry = registry._catalog.get(name.lower()) or registry._catalog.get(name.split(":")[0].lower())
        if isinstance(entry, dict) and entry.get("decision_only"):
            return True
    return False


def _safe_model_tier_rank(model: Dict[str, Any], registry: Any = None) -> int:
    tier_str = str(model.get("capability_tier") or "").strip().lower()
    rank_map = {
        "decision": 1,
        "jev": 1,
        "system_one": 1,
        "basic": 1,
        "simple": 1,
        "standard": 2,
        "default": 2,
        "advanced": 3,
        "expert": 4,
    }
    if tier_str in rank_map:
        return rank_map[tier_str]
    if registry is not None:
        try:
            return int(registry.get_tier(str(model.get("model_name") or "")))
        except Exception:
            pass
    name = str(model.get("model_name") or "").lower()
    if is_jev_decision_model(name):
        return 1
    if any(k in name for k in ("lite", "mini", "nano", "1b", "3b", "4b", "7b")):
        return 1
    if any(k in name for k in ("pro", "opus", "gpt-4o", "70b", "72b")):
        return 4
    if any(k in name for k in ("flash", "14b", "32b", "sonnet")):
        return 3
    return 2


def recommend_models_for_tiers(db: Any, prefer_local: bool = True) -> Dict[str, Any]:
    """
    Pick optimal existing model IDs from DB for:
    - 'decision': dedicated Jev/System One model if present, else fastest/lightweight model
    - 'decision_model_name': model name for the chosen Jev/System One decision model
    - 'basic': lightweight generative worker model (excluding decision-only Jev models)
    - 'standard': general-purpose generative worker model
    - 'advanced': strongest generative reasoning / synthesis model
    """
    models: List[Dict[str, Any]] = []
    if db is not None and hasattr(db, "read_all_models"):
        try:
            models = db.read_all_models() or []
        except Exception:
            models = []

    if not models:
        return {
            "decision": None,
            "decision_model_name": "tev1" if prefer_local else "typesafe/jev-1.14",
            "basic": None,
            "standard": None,
            "advanced": None,
        }

    registry = None
    try:
        from core.model_capability import ModelCapabilityRegistry
        registry = ModelCapabilityRegistry()
    except Exception:
        registry = None

    scored = []
    for m in models:
        rank = _safe_model_tier_rank(m, registry)
        is_local = bool(m.get("is_local")) or str(m.get("provider") or "").lower() in (
            "ollama", "lmstudio", "lm_studio", "bionic", "vllm", "llama.cpp"
        )
        local_bonus = 1 if (prefer_local and is_local) else 0
        supports_tools = 1 if bool(m.get("supports_tools", 1)) else 0
        is_jev = _is_decision_only_model(m, registry)
        scored.append((m, rank, local_bonus, supports_tools, is_jev))

    jev_models = [item for item in scored if item[4]]
    gen_models = [item for item in scored if not item[4]] or scored

    # Decision model: prioritize Jev/System One models first, then smallest lightweight rank
    if jev_models:
        dec_sorted = sorted(jev_models, key=lambda x: (-x[2], x[1], int(x[0].get("id") or 0)))
        dec_record = dec_sorted[0][0]
        decision_id = int(dec_record["id"])
        decision_model_name = str(dec_record.get("model_name") or "tev1")
    else:
        dec_sorted = sorted(gen_models, key=lambda x: (x[1], -x[2], int(x[0].get("id") or 0)))
        dec_record = dec_sorted[0][0]
        decision_id = int(dec_record["id"])
        decision_model_name = "tev1" if prefer_local else "typesafe/jev-1.14"

    # Generative worker models (excluding Jev/System One models whenever others exist)
    basic_sorted = sorted(gen_models, key=lambda x: (x[1], -x[2], int(x[0].get("id") or 0)))
    basic_id = int(basic_sorted[0][0]["id"])

    std_sorted = sorted(
        gen_models,
        key=lambda x: (abs(x[1] - 2), -x[3], -x[2], int(x[0].get("id") or 0)),
    )
    std_id = int(std_sorted[0][0]["id"])

    adv_sorted = sorted(
        gen_models,
        key=lambda x: (-x[1], -x[3], -x[2], int(x[0].get("id") or 0)),
    )
    adv_id = int(adv_sorted[0][0]["id"])

    return {
        "decision": decision_id,
        "decision_model_name": decision_model_name,
        "basic": basic_id,
        "standard": std_id,
        "advanced": adv_id,
    }


def _tokenize_role(text: str) -> set[str]:
    words = re.findall(r"[a-zA-ZÀ-ÿ0-9]{3,}", (text or "").lower())
    stop = {"the", "and", "for", "with", "per", "con", "dei", "delle", "degli", "nel", "senior", "lead"}
    return {w for w in words if w not in stop}


def match_existing_agent(
    proposed_agent: Dict[str, Any],
    existing_agents: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Find the closest matching existing DB agent by name/role similarity."""
    if not existing_agents:
        return None
    target_name = str(proposed_agent.get("name") or "").strip().lower()
    target_role = str(proposed_agent.get("role") or "").strip().lower()
    target_tokens = _tokenize_role(
        f"{proposed_agent.get('name', '')} {proposed_agent.get('role', '')}"
    )
    best_agent = None
    best_score = 0.0

    for ag in existing_agents:
        ex_name = str(ag.get("name") or "").strip().lower()
        ex_role = str(ag.get("role") or "").strip().lower()
        if target_name and ex_name == target_name:
            return ag
        if target_role and ex_role == target_role:
            return ag
        ex_tokens = _tokenize_role(f"{ag.get('name', '')} {ag.get('role', '')} {ag.get('backstory', '')[:120]}")
        if not target_tokens or not ex_tokens:
            continue
        overlap = len(target_tokens & ex_tokens)
        union = len(target_tokens | ex_tokens)
        score = overlap / max(1, union)
        if overlap >= 1 and score > best_score:
            best_score = score
            best_agent = ag

    return best_agent if best_score >= 0.25 else None


# ---------------------------------------------------------------------------
# Blueprint Prompt & Heuristic Fallback
# ---------------------------------------------------------------------------

TEAM_ARCHITECT_PROMPT = """You are Alfredo's Lead Orchestrator & Workflow Architect.
Given the user's objective, design a complete, cohesive multi-agent team and executable DAG workflow using ALL of Alfredo's graph constructs when appropriate.

EXISTING AGENTS IN DATABASE (reuse compatible ones via "reuse_agent_id" when appropriate, or set null to create new):
{existing_agents_json}

AVAILABLE LLM MODELS IN DATABASE:
{models_json}

RECOMMENDED MODEL IDS BY TIER:
{recommended_models_json}

AVAILABLE TOOLS CATALOG (use ONLY canonical tool IDs from keys below):
{available_tools_json}

AVAILABLE VECTOR DATABASES (optional integer IDs for RAG / vector_search):
{vector_dbs_json}

OPTIONS:
- Include binary Yes/No decision gates (`decision` nodes): {include_decision_gates}
- Expected exports: {expected_exports_json}

SUPPORTED GRAPH BLOCK TYPES (use the best fit per step):
1. `task`: CrewAI agent step (`agent_key`, `execution_level`, `depends_on`, `when_route`, `description`, `expected_output`, `tools` from AVAILABLE TOOLS, `agent_specialization`, `vector_dbs`, `human_validation`, `required_inputs`, `inputs_map`, `handoff_contract`).
2. `decision` / `router`: Binary Yes/No Jev/System One decision gate (`decision_mode: true`, `decision_engine`: `"jev"`|`"auto"`|`"llm"`, `decision_primitive`: `"noul"`|`"choice"`|`"score"`, `threshold`: 0.05..0.95, `jev_model`: `"tev1"` or `"typesafe/jev-1.14"`, `routes`: `["yes", "no"]`, `default_route`: `"no"`, `question`, `model_id`: {decision_model_id}), OR multi-route `router` (`routes`, `router_mode`: `"auto"`|`"rules"`|`"llm"`, `rules`, `default_route`).
3. `loop` (Structured Loop Frame): iterative refinement or per-item loop (`loop_mode`: `"while"`|`"for_each"`, `scope`: `"nodes"`|`"levels"`|`"workflow"`, `body_node_ids`: `["step_id"]`, `body_levels`: `[]`, `max_iterations`: 3..10, `source_variable`: `"{{previous_result}}"`, `exit_on_hitl`: true|false, `exit_condition`: `{{"output_match": {{"source_node": "<body_node_id>", "pattern": "APPROVED|COMPLETE"}}}}`).
   - CRITICAL LOOP RULES: `body_node_ids` may ONLY contain `task`, `batch_loop`, or `hitl` node IDs (NEVER `decision`/`router`/`tool`/`llm`/`export`/`loop`). Downstream nodes after a loop MUST depend on the `loop` node ID itself, NEVER directly on an inner `body_node_ids` node.
4. `batch_loop`: chunked execution (`body_node_ids` or `task_ids`, `batch_size`, `source_variable`).
5. `tool`: deterministic single tool execution (`tool_name`, `tool_args`, `execution_level`, `depends_on`).
6. `llm`: lightweight direct LLM block (`prompt`, `system_prompt`, `output_format`: `"text"`|`"json"`, `temperature`, `execution_level`, `depends_on`).
7. `hitl`: explicit Human-in-the-Loop checkpoint (`message`, `gate_mode`: `"after_parents"`, `execution_level`, `depends_on`; place after `task` nodes, not directly after `decision`/`tool`/`llm`).
8. `input`: declared run-input keys (`keys`: `["topic"]`).

REQUIREMENTS:
1. Return ONLY valid JSON with this exact schema:
{{
  "workflow_name": "Short descriptive title",
  "description": "One-sentence summary of what this workflow accomplishes",
  "expected_exports": [],
  "agents": [
    {{
      "agent_key": "researcher",
      "name": "Short agent name",
      "role": "Specific expert role",
      "goal": "Clear measurable goal",
      "backstory": "Concise persona & constraints",
      "tools": ["search_web"],
      "recommended_tier": "basic|standard|advanced",
      "model_id": 1,
      "reuse_agent_id": null
    }}
  ],
  "nodes": [
    {{
      "id": "step_1",
      "type": "task",
      "name": "Step title",
      "agent_key": "researcher",
      "agent_specialization": "optional domain focus",
      "execution_level": 1,
      "depends_on": [],
      "description": "Exact instructions. Can reference {{user_input}} or upstream ports.",
      "expected_output": "Exact structured deliverable expected from this agent.",
      "tools": ["search_web"],
      "vector_dbs": [],
      "human_validation": false,
      "required_inputs": [],
      "inputs_map": {{}},
      "handoff_contract": {{
        "required_fields": ["summary", "key_facts"],
        "output_instruction": "Conclude with a concise bullet summary of verified key facts."
      }}
    }},
    {{
      "id": "gate_1",
      "type": "decision",
      "name": "Quality Gate (Yes/No)",
      "execution_level": 2,
      "depends_on": ["step_1"],
      "question": "Clear binary YES/NO evaluation question on step_1 output",
      "routes": ["yes", "no"],
      "default_route": "no",
      "decision_engine": "jev",
      "decision_primitive": "noul",
      "threshold": 0.5,
      "jev_model": "tev1",
      "model_tier": "basic",
      "model_id": {decision_model_id}
    }},
    {{
      "id": "step_yes",
      "type": "task",
      "name": "Final Deliverable (Yes Branch)",
      "agent_key": "writer_editor",
      "execution_level": 3,
      "depends_on": ["gate_1", "step_1"],
      "when_route": {{"from": "gate_1", "equals": "yes"}},
      "description": "Use {{source_summary}} to generate the final deliverable.",
      "expected_output": "Complete final document/result.",
      "inputs_map": {{"source_summary": {{"from": "step_1", "key": "summary"}}}},
      "tools": [],
      "required_inputs": []
    }}
  ]
}}
2. Design principles:
- Parallelism via `execution_level`: give independent tasks the same `execution_level` so they execute in parallel; increment `execution_level` for sequential phases.
- Keep teams lean (2-4 agents unless the objective clearly needs more).
- If `include_decision_gates` is true, include at least 1 binary `decision` node (`routes: ["yes", "no"]`, Jev/System One) and branch downstream tasks cleanly using `when_route: {{"from": "<decision_id>", "equals": "yes"|"no"}}`.
- Use `loop` (`while` or `for_each`) when the objective asks for iterative refinement, multi-pass improvement, or processing batches/lists until criteria pass.
- Wire `inputs_map` deterministically between tasks using `{{"from": "<parent_id>", "key": "summary"}}` or `"key_facts"` so downstream models automatically receive structured upstream context.
- Every node id referenced in `depends_on`, `when_route.from`, `body_node_ids`, or `inputs_map.*.from` must exist in `nodes` (or be `"input"`).
"""


def _detect_exports_from_objective(objective_lower: str) -> List[str]:
    detected: List[str] = []
    checks = (
        (r"\b(excel|xlsx|spreadsheet)\b", "excel"),
        (r"\b(word|docx)\b", "word"),
        (r"\b(python)\b|\.py\b", "python"),
        (r"\b(markdown)\b|\.md\b", "markdown"),
        (r"\b(json)\b", "json"),
    )
    for pat, fmt in checks:
        if re.search(pat, objective_lower) and fmt not in detected:
            detected.append(fmt)
    return detected


def _build_heuristic_blueprint(
    objective: str,
    *,
    existing_agents: List[Dict[str, Any]],
    model_tiers: Dict[str, Optional[int]],
    include_decision_gates: bool = True,
    reuse_existing_agents: bool = True,
    expected_exports: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Deterministic blueprint builder used when offline or as LLM fallback."""
    clean_obj = (objective or "Operational workflow execution").strip()
    low = clean_obj.lower()
    exports = list(expected_exports or []) or _detect_exports_from_objective(low)

    # Detect needed archetypes from objective keywords (supports English and Italian inputs)
    need_research = any(
        k in low
        for k in (
            "search", "research", "analy", "competitor", "market", "source", "data", "paper", "web",
            "cerca", "ricerc", "analisi", "mercato", "fonti", "dati",
        )
    )
    need_tech = any(
        k in low
        for k in (
            "code", "script", "python", "api", "app", "architect", "bug", "test", "sql", "software",
            "codice", "architett",
        )
    )
    need_data = any(
        k in low
        for k in (
            "metric", "excel", "csv", "calc", "number", "financ", "budget", "kpi",
            "metriche", "calcol", "numeri", "finanzi",
        )
    )
    need_loop = any(
        k in low
        for k in (
            "loop", "iterat", "ripeti", "finché", "finche", "until", "while",
            "for each", "for_each", "ciclo", "refin", "raffin",
        )
    )

    lead_arch_key = "researcher" if need_research or not need_tech else "tech_architect"
    if need_data and not need_research and not need_tech:
        lead_arch_key = "data_analyst"

    second_arch_key = "data_analyst" if need_research and need_data else "strategist"
    if need_tech:
        second_arch_key = "tech_architect" if lead_arch_key != "tech_architect" else "qa_critic"

    selected_keys = [lead_arch_key]
    if second_arch_key not in selected_keys:
        selected_keys.append(second_arch_key)
    if "writer_editor" not in selected_keys:
        selected_keys.append("writer_editor")

    agents: List[Dict[str, Any]] = []
    for k in selected_keys:
        arch = copy.deepcopy(AGENT_ARCHETYPES[k])
        tier = arch.get("recommended_tier", "standard")
        mid = model_tiers.get(tier) or model_tiers.get("standard")
        agent_entry = {
            "agent_key": k,
            "name": arch["name"],
            "role": arch["role"],
            "goal": arch["goal"],
            "backstory": arch["backstory"],
            "tools": _normalize_tool_list(arch.get("tools") or []),
            "recommended_tier": tier,
            "model_id": mid,
            "reuse_agent_id": None,
        }
        if reuse_existing_agents:
            matched = match_existing_agent(agent_entry, existing_agents)
            if matched:
                agent_entry["reuse_agent_id"] = int(matched["id"])
                agent_entry["model_id"] = matched.get("model_id") or mid
        agents.append(agent_entry)

    first_agent_key = selected_keys[0]
    analyst_agent_key = selected_keys[1] if len(selected_keys) > 1 else first_agent_key
    writer_agent_key = "writer_editor"

    nodes: List[Dict[str, Any]] = [
        {
            "id": "step_1",
            "type": "task",
            "name": "Data Collection & Initial Analysis",
            "agent_key": first_agent_key,
            "execution_level": 1,
            "depends_on": [],
            "description": (
                f"Analyze the following objective thoroughly and gather all essential facts:\n"
                f"{clean_obj}\n\n"
                "Additional user context: {user_input}"
            ),
            "expected_output": (
                "Structured summary with verified facts, numbered key points (Key Facts), "
                "and explicit flagging of any missing information."
            ),
            "tools": list(agents[0].get("tools") or []),
            "required_inputs": [],
            "inputs_map": {"request": {"from": "input", "key": "user_input"}},
            "handoff_contract": {
                "required_fields": ["summary", "key_facts"],
                "output_instruction": "Include a concise summary section and a bullet list of key facts.",
            },
        }
    ]

    anchor_id = "step_1"
    anchor_level = 1

    if need_loop:
        is_for_each = any(k in low for k in ("for each", "for_each", "batch"))
        nodes.append(
            {
                "id": "step_iterate",
                "type": "task",
                "name": "Iterative Analysis & Refinement",
                "agent_key": analyst_agent_key,
                "execution_level": 2,
                "depends_on": ["step_1"],
                "description": (
                    "Iteratively review and refine the initial analysis ({step1_summary}) against "
                    f"all quality criteria for:\n{clean_obj}\n\n"
                    "Conclude with APPROVED when all checks pass."
                ),
                "expected_output": "Refined structured analysis ending with APPROVED or COMPLETE once criteria pass.",
                "tools": [],
                "required_inputs": [],
                "inputs_map": {"step1_summary": {"from": "step_1", "key": "summary"}},
                "handoff_contract": {
                    "required_fields": ["summary", "key_facts"],
                },
            }
        )
        nodes.append(
            {
                "id": "loop_refine",
                "type": "loop",
                "loop_mode": "for_each" if is_for_each else "while",
                "scope": "nodes",
                "body_node_ids": ["step_iterate"],
                "body_levels": [],
                "source_variable": "{previous_result}",
                "max_iterations": 3,
                "exit_on_hitl": True,
                "exit_condition": {
                    "output_match": {
                        "source_node": "step_iterate",
                        "pattern": "APPROVED|COMPLETE",
                    }
                },
                "depends_on": ["step_1"],
                "execution_level": 2,
                "inputs_map": {},
            }
        )
        anchor_id = "loop_refine"
        anchor_level = 2

    if include_decision_gates:
        gate_tpl = DECISION_GATE_TEMPLATES["data_sufficiency"]
        gate_lvl = anchor_level + 1
        nodes.append(
            {
                "id": "gate_1",
                "type": "decision",
                "name": gate_tpl["name"],
                "execution_level": gate_lvl,
                "depends_on": [anchor_id],
                "question": (
                    f"For the objective '{clean_obj[:140]}', are the data and analysis produced by {anchor_id} "
                    "complete and sufficient to draft the final deliverable directly ('yes'), "
                    "or do they require deeper investigation and enrichment ('no')?"
                ),
                "routes": ["yes", "no"],
                "default_route": "no",
                "decision_mode": True,
                "decision_engine": "jev",
                "decision_primitive": "noul",
                "threshold": float(gate_tpl.get("threshold", 0.5)),
                "jev_model": str(model_tiers.get("decision_model_name") or "tev1"),
                "model_tier": "basic",
                "model_id": model_tiers.get("decision"),
            }
        )
        nodes.append(
            {
                "id": "step_deepen",
                "type": "task",
                "name": "Deep-Dive & Enrichment (NO Branch)",
                "agent_key": analyst_agent_key,
                "execution_level": gate_lvl + 1,
                "depends_on": ["gate_1", anchor_id],
                "when_route": {"from": "gate_1", "equals": "no"},
                "description": (
                    "The decision gate identified gaps in the initial analysis ({gate_reason}).\n"
                    "Starting from the preliminary findings ({step1_summary}), fill critical gaps and "
                    "structure a complete assessment for the objective:\n"
                    f"{clean_obj}"
                ),
                "expected_output": "Complete supplementary analysis with metrics, risks, and recommendations.",
                "tools": [],
                "required_inputs": [],
                "inputs_map": {
                    "step1_summary": {"from": "step_1", "key": "summary"},
                    "gate_reason": {"from": "gate_1", "key": "reason"},
                },
                "handoff_contract": {
                    "required_fields": ["summary", "key_facts"],
                },
            }
        )
        nodes.append(
            {
                "id": "step_final_yes",
                "type": "task",
                "name": "Final Report & Deliverable (YES Branch)",
                "agent_key": writer_agent_key,
                "execution_level": gate_lvl + 1,
                "depends_on": ["gate_1", anchor_id],
                "when_route": {"from": "gate_1", "equals": "yes"},
                "description": (
                    "The quality gate approved the collected findings ({step1_facts}).\n"
                    f"Draft the complete, ready-to-use final deliverable for:\n{clean_obj}"
                ),
                "expected_output": "Verified, polished final deliverable ready for immediate use.",
                "tools": ["write_file"] if exports else [],
                "required_inputs": [],
                "inputs_map": {
                    "step1_summary": {"from": "step_1", "key": "summary"},
                    "step1_facts": {"from": "step_1", "key": "key_facts"},
                },
                "handoff_contract": {
                    "required_fields": ["summary", "key_facts"],
                },
            }
        )
        nodes.append(
            {
                "id": "step_final_no",
                "type": "task",
                "name": "Final Consolidation after Enrichment (NO Branch)",
                "agent_key": writer_agent_key,
                "execution_level": gate_lvl + 2,
                "depends_on": ["step_deepen"],
                "when_route": {"from": "gate_1", "equals": "no"},
                "description": (
                    "Consolidate the supplementary analysis ({deep_summary}) into the final deliverable for:\n"
                    f"{clean_obj}"
                ),
                "expected_output": "Consolidated final document incorporating all supplementary findings.",
                "tools": ["write_file"] if exports else [],
                "required_inputs": [],
                "inputs_map": {
                    "deep_summary": {"from": "step_deepen", "key": "summary"},
                },
                "handoff_contract": {
                    "required_fields": ["summary", "key_facts"],
                },
            }
        )
    elif need_loop:
        nodes.append(
            {
                "id": "step_final_after_loop",
                "type": "task",
                "name": "Executive Synthesis & Final Deliverable",
                "agent_key": writer_agent_key,
                "execution_level": anchor_level + 1,
                "depends_on": [anchor_id],
                "description": (
                    "Draft the final deliverable combining the refined analysis ({step1_facts}) "
                    f"to satisfy:\n{clean_obj}"
                ),
                "expected_output": "Structured final output ready for delivery.",
                "tools": ["write_file"] if exports else [],
                "required_inputs": [],
                "inputs_map": {
                    "step1_summary": {"from": "step_1", "key": "summary"},
                    "step1_facts": {"from": "step_1", "key": "key_facts"},
                },
            }
        )
    else:
        nodes.append(
            {
                "id": "step_2",
                "type": "task",
                "name": "Analysis & Operational Strategy",
                "agent_key": analyst_agent_key,
                "execution_level": 2,
                "depends_on": ["step_1"],
                "description": (
                    "Process the gathered data ({step1_summary}) into a technical/strategic assessment "
                    f"and action plan for:\n{clean_obj}"
                ),
                "expected_output": "Structured evaluation with pros/cons, risks, and operational next steps.",
                "tools": [],
                "required_inputs": [],
                "inputs_map": {
                    "step1_summary": {"from": "step_1", "key": "summary"},
                    "step1_facts": {"from": "step_1", "key": "key_facts"},
                },
            }
        )
        nodes.append(
            {
                "id": "step_3",
                "type": "task",
                "name": "Executive Synthesis & Final Deliverable",
                "agent_key": writer_agent_key,
                "execution_level": 3,
                "depends_on": ["step_2", "step_1"],
                "description": (
                    "Draft the final deliverable combining analysis ({step2_summary}) and initial evidence "
                    f"({{step1_facts}}) to satisfy:\n{clean_obj}"
                ),
                "expected_output": "Structured final output ready for delivery.",
                "tools": ["write_file"] if exports else [],
                "required_inputs": [],
                "inputs_map": {
                    "step2_summary": {"from": "step_2", "key": "summary"},
                    "step1_facts": {"from": "step_1", "key": "key_facts"},
                },
            }
        )

    title_words = [w for w in re.findall(r"[A-Za-zÀ-ÿ0-9]+", clean_obj)][:6]
    wf_name = "Workflow: " + (" ".join(title_words) if title_words else "Guided Team")

    return {
        "workflow_name": wf_name,
        "description": clean_obj[:240],
        "expected_exports": exports,
        "agents": agents,
        "nodes": nodes,
        "source": "heuristic",
        "orchestrator_model": "heuristic",
    }


def _normalize_blueprint(
    raw_bp: Dict[str, Any],
    *,
    objective: str,
    existing_agents: List[Dict[str, Any]],
    model_tiers: Dict[str, Optional[int]],
    reuse_existing_agents: bool = True,
    expected_exports: Optional[List[str]] = None,
    valid_tool_keys: Optional[set[str]] = None,
    valid_vdb_ids: Optional[set[int]] = None,
) -> Dict[str, Any]:
    bp = copy.deepcopy(raw_bp or {})
    bp["workflow_name"] = str(bp.get("workflow_name") or "AI Team Workflow").strip()
    bp["description"] = str(bp.get("description") or objective or "").strip()
    bp["expected_exports"] = list(bp.get("expected_exports") or expected_exports or [])

    if valid_tool_keys is None:
        catalog = get_available_tools_catalog()
        valid_tool_keys = set(catalog.keys()) if catalog else None

    clean_agents: List[Dict[str, Any]] = []
    seen_keys: set[str] = set()
    for idx, ag in enumerate(bp.get("agents") or []):
        if not isinstance(ag, dict):
            continue
        akey = str(ag.get("agent_key") or ag.get("key") or f"agent_{idx + 1}").strip()
        if akey in seen_keys:
            continue
        seen_keys.add(akey)
        tier = str(ag.get("recommended_tier") or "standard").strip().lower()
        if tier not in ("basic", "standard", "advanced"):
            tier = "standard"
        default_mid = model_tiers.get(tier) or model_tiers.get("standard")
        mid = ag.get("model_id")
        if mid in (None, ""):
            mid = default_mid
        entry = {
            "agent_key": akey,
            "name": str(ag.get("name") or f"Agent {idx + 1}").strip(),
            "role": str(ag.get("role") or "Workflow Specialist").strip(),
            "goal": str(ag.get("goal") or "").strip(),
            "backstory": str(ag.get("backstory") or "").strip(),
            "tools": _normalize_tool_list(ag.get("tools") or [], valid_tool_keys),
            "recommended_tier": tier,
            "model_id": int(mid) if mid not in (None, "") else None,
            "reuse_agent_id": (
                int(ag["reuse_agent_id"]) if ag.get("reuse_agent_id") not in (None, "") else None
            ),
        }
        if reuse_existing_agents and entry["reuse_agent_id"] is None:
            matched = match_existing_agent(entry, existing_agents)
            if matched:
                entry["reuse_agent_id"] = int(matched["id"])
        clean_agents.append(entry)

    if not clean_agents:
        arch = AGENT_ARCHETYPES["strategist"]
        clean_agents.append(
            {
                "agent_key": "lead_agent",
                "name": arch["name"],
                "role": arch["role"],
                "goal": arch["goal"],
                "backstory": arch["backstory"],
                "tools": [],
                "recommended_tier": "standard",
                "model_id": model_tiers.get("standard"),
                "reuse_agent_id": None,
            }
        )

    valid_agent_keys = {a["agent_key"] for a in clean_agents}
    default_agent_key = clean_agents[0]["agent_key"]

    allowed_node_types = {
        "task",
        "decision",
        "router",
        "loop",
        "batch_loop",
        "tool",
        "llm",
        "hitl",
        "input",
        "export",
    }

    clean_nodes: List[Dict[str, Any]] = []
    seen_nids: set[str] = set()
    for idx, node in enumerate(bp.get("nodes") or []):
        if not isinstance(node, dict):
            continue
        raw_ntype = str(node.get("type") or "task").strip().lower()
        if raw_ntype == "agent":
            raw_ntype = "task"
        elif raw_ntype == "batch":
            raw_ntype = "batch_loop"
        if raw_ntype not in allowed_node_types:
            raw_ntype = "task"

        nid = str(node.get("id") or f"node_{idx + 1}").strip()
        if nid in seen_nids:
            nid = f"{nid}_{idx + 1}"
        seen_nids.add(nid)

        try:
            lvl = max(1, int(node.get("execution_level") or 1))
        except (TypeError, ValueError):
            lvl = 1

        deps = [str(d) for d in (node.get("depends_on") or []) if str(d).strip()]
        is_binary_dec = (
            raw_ntype == "decision"
            or bool(node.get("decision_mode"))
            or (
                raw_ntype == "router"
                and (
                    not node.get("routes")
                    or {str(r).strip().lower() for r in (node.get("routes") or [])} <= {"yes", "no"}
                )
            )
        )
        canonical_type = "decision" if is_binary_dec else raw_ntype

        item: Dict[str, Any] = {
            "id": nid,
            "type": canonical_type,
            "name": str(node.get("name") or f"Step {idx + 1}").strip(),
            "execution_level": lvl,
            "depends_on": deps,
            "inputs_map": dict(node.get("inputs_map") or {}) if isinstance(node.get("inputs_map"), dict) else {},
        }
        if isinstance(node.get("when_route"), dict) and node["when_route"].get("from"):
            wr_from = str(node["when_route"]["from"]).strip()
            wr_eq = node["when_route"].get("equals")
            wr_in = node["when_route"].get("in")
            wr_obj: Dict[str, Any] = {"from": wr_from}
            if wr_eq is not None:
                wr_obj["equals"] = str(wr_eq).strip().lower()
            elif isinstance(wr_in, list) and wr_in:
                wr_obj["in"] = [str(x).strip().lower() for x in wr_in if str(x).strip()]
            else:
                wr_obj["equals"] = "yes"
            item["when_route"] = wr_obj
            if wr_from not in item["depends_on"]:
                item["depends_on"].append(wr_from)

        if canonical_type == "decision":
            q = str(node.get("question") or node.get("prompt") or "Are the criteria to proceed satisfied?").strip()
            item["question"] = q
            item["routes"] = ["yes", "no"]
            item["default_route"] = str(node.get("default_route") or "no").strip().lower()
            if item["default_route"] not in ("yes", "no"):
                item["default_route"] = "no"
            item["decision_mode"] = True
            engine = str(node.get("decision_engine") or "jev").strip().lower()
            item["decision_engine"] = engine if engine in ("auto", "jev", "llm") else "jev"
            prim = str(node.get("decision_primitive") or "noul").strip().lower()
            item["decision_primitive"] = prim if prim in ("noul", "choice", "score") else "noul"
            try:
                thr = float(0.5 if node.get("threshold") in (None, "") else node.get("threshold"))
            except (TypeError, ValueError):
                thr = 0.5
            item["threshold"] = max(0.0, min(1.0, thr))
            item["jev_model"] = str(
                node.get("jev_model") or model_tiers.get("decision_model_name") or "tev1"
            ).strip()
            item["model_tier"] = "basic"
            dmid = node.get("model_id")
            item["model_id"] = int(dmid) if dmid not in (None, "") else model_tiers.get("decision")

        elif canonical_type == "router":
            routes = list(dict.fromkeys(str(r).strip() for r in (node.get("routes") or []) if str(r).strip()))
            if len(routes) < 2:
                routes = ["yes", "no"]
            item["routes"] = routes
            def_r = str(node.get("default_route") or routes[0]).strip()
            item["default_route"] = def_r if def_r in routes else routes[0]
            rmode = str(node.get("router_mode") or "auto").strip().lower()
            item["router_mode"] = rmode if rmode in ("auto", "rules", "llm", "jev") else "auto"
            clean_rules = []
            for r in (node.get("rules") or []):
                if isinstance(r, dict) and str(r.get("route") or "") in routes:
                    if any(k in r for k in ("pattern", "equals", "contains", "exists")):
                        clean_rules.append(dict(r))
            item["rules"] = clean_rules
            item["prompt"] = str(node.get("prompt") or node.get("question") or "").strip()
            item["model_tier"] = str(node.get("model_tier") or "basic")
            if node.get("model_id") not in (None, ""):
                item["model_id"] = int(node["model_id"])

        elif canonical_type == "loop":
            lmode = str(node.get("loop_mode") or LOOP_MODE_WHILE).strip().lower()
            item["loop_mode"] = lmode if lmode in (LOOP_MODE_WHILE, LOOP_MODE_FOR_EACH) else LOOP_MODE_WHILE
            lscope = str(node.get("scope") or LOOP_SCOPE_NODES).strip().lower()
            item["scope"] = lscope if lscope in LOOP_SCOPES else LOOP_SCOPE_NODES
            item["body_node_ids"] = [str(b).strip() for b in (node.get("body_node_ids") or []) if str(b).strip()]
            body_lvls: List[int] = []
            for lv in (node.get("body_levels") or []):
                try:
                    body_lvls.append(max(1, int(lv)))
                except (TypeError, ValueError):
                    pass
            item["body_levels"] = sorted(set(body_lvls))
            try:
                item["max_iterations"] = max(1, int(node.get("max_iterations") or 3))
            except (TypeError, ValueError):
                item["max_iterations"] = 3
            try:
                item["batch_size"] = max(1, int(node.get("batch_size") or 5))
            except (TypeError, ValueError):
                item["batch_size"] = 5
            item["source_variable"] = str(node.get("source_variable") or "{previous_result}").strip()
            item["exit_on_hitl"] = bool(node.get("exit_on_hitl", True))
            ec = node.get("exit_condition") if isinstance(node.get("exit_condition"), dict) else {}
            om = ec.get("output_match") if isinstance(ec.get("output_match"), dict) else {}
            item["exit_condition"] = {
                "output_match": {
                    "source_node": str(om.get("source_node") or node.get("exit_condition_node") or "").strip(),
                    "pattern": str(om.get("pattern") or node.get("exit_condition_pattern") or "").strip(),
                }
            }

        elif canonical_type == "batch_loop":
            item["body_node_ids"] = [str(b).strip() for b in (node.get("body_node_ids") or []) if str(b).strip()]
            raw_tids = []
            for t in (node.get("task_ids") or []):
                if isinstance(t, int) or str(t).isdigit():
                    raw_tids.append(int(t))
                elif str(t).strip() and str(t).strip() not in item["body_node_ids"]:
                    item["body_node_ids"].append(str(t).strip())
            item["task_ids"] = raw_tids
            try:
                item["batch_size"] = max(1, int(node.get("batch_size") or 5))
            except (TypeError, ValueError):
                item["batch_size"] = 5
            item["source_variable"] = str(node.get("source_variable") or "{previous_result}").strip()

        elif canonical_type == "tool":
            tname = _TOOL_ALIASES.get(str(node.get("tool_name") or "").strip(), str(node.get("tool_name") or "").strip())
            if not tname or (valid_tool_keys and tname not in valid_tool_keys):
                seen_nids.discard(nid)
                continue
            item["tool_name"] = tname
            item["tool_args"] = dict(node.get("tool_args") or {}) if isinstance(node.get("tool_args"), dict) else {}
            item["append_previous"] = bool(node.get("append_previous", False))

        elif canonical_type == "llm":
            prompt_txt = str(node.get("prompt") or objective or "{previous_result}").strip()
            item["prompt"] = prompt_txt or "{previous_result}"
            item["system_prompt"] = str(node.get("system_prompt") or "").strip()
            fmt = str(node.get("output_format") or "text").strip().lower()
            item["output_format"] = fmt if fmt in ("text", "json") else "text"
            try:
                temp = float(0.2 if node.get("temperature") is None else node.get("temperature"))
            except (TypeError, ValueError):
                temp = 0.2
            item["temperature"] = max(0.0, min(1.5, temp))
            item["append_previous"] = bool(node.get("append_previous", True))
            item["model_tier"] = str(node.get("model_tier") or "default")
            if node.get("model_id") not in (None, ""):
                item["model_id"] = int(node["model_id"])

        elif canonical_type == "hitl":
            item["message"] = str(node.get("message") or "Please review and approve before continuing.").strip()
            item["gate_mode"] = str(node.get("gate_mode") or "after_parents").strip()

        elif canonical_type == "input":
            keys = [str(k).strip() for k in (node.get("keys") or []) if str(k).strip()]
            item["keys"] = keys or ["user_input"]

        elif canonical_type == "export":
            item["exports"] = [str(e).strip() for e in (node.get("exports") or []) if str(e).strip()]
            item["export_instructions"] = str(node.get("export_instructions") or "").strip()

        else:  # task
            akey = str(node.get("agent_key") or "").strip()
            item["agent_key"] = akey if akey in valid_agent_keys else default_agent_key
            item["description"] = str(node.get("description") or objective or "").strip()
            item["expected_output"] = str(node.get("expected_output") or "Structured and verified result.").strip()
            item["tools"] = _normalize_tool_list(node.get("tools") or [], valid_tool_keys)
            item["agent_specialization"] = str(node.get("agent_specialization") or "").strip()
            raw_vdbs: List[int] = []
            for v in (node.get("vector_dbs") or []):
                try:
                    vid = int(v)
                except (TypeError, ValueError):
                    continue
                if valid_vdb_ids is None or vid in valid_vdb_ids:
                    if vid not in raw_vdbs:
                        raw_vdbs.append(vid)
            item["vector_dbs"] = raw_vdbs
            item["required_inputs"] = list(node.get("required_inputs") or [])
            item["human_validation"] = bool(node.get("human_validation", False))
            if node.get("model_id") not in (None, ""):
                try:
                    item["model_id"] = int(node["model_id"])
                except (TypeError, ValueError):
                    pass
            if isinstance(node.get("handoff_contract"), dict):
                item["handoff_contract"] = dict(node["handoff_contract"])

        clean_nodes.append(item)

    # Prune dangling dependency & port references
    node_ids_set = {n["id"] for n in clean_nodes}
    by_id: Dict[str, Dict[str, Any]] = {n["id"]: n for n in clean_nodes}
    for n in clean_nodes:
        n["depends_on"] = list(dict.fromkeys(d for d in n["depends_on"] if d in node_ids_set and d != n["id"]))
        if isinstance(n.get("inputs_map"), dict):
            n["inputs_map"] = {
                p: b
                for p, b in n["inputs_map"].items()
                if isinstance(b, dict) and b.get("from") in (node_ids_set | {"input"})
            }

    # Validate & enforce structured loop constraints
    loop_allowed_member_types = {"task", "batch_loop", "hitl"}
    claimed_by_loop: Dict[str, str] = {}
    kept_nodes: List[Dict[str, Any]] = []
    for n in clean_nodes:
        if n["type"] != "loop":
            kept_nodes.append(n)
            continue
        lid = n["id"]
        scope = n.get("scope") or LOOP_SCOPE_NODES
        if scope == LOOP_SCOPE_NODES:
            body_ids = list(dict.fromkeys(
                bid for bid in (n.get("body_node_ids") or [])
                if bid in by_id
                and bid != lid
                and by_id[bid]["type"] in loop_allowed_member_types
                and bid not in claimed_by_loop
            ))
            n["body_node_ids"] = body_ids
            if not body_ids:
                continue
        elif scope == LOOP_SCOPE_LEVELS:
            lvls = set(n.get("body_levels") or [])
            body_ids = [
                m["id"] for m in clean_nodes
                if m["id"] != lid
                and m["execution_level"] in lvls
                and m["type"] in loop_allowed_member_types
                and m["id"] not in claimed_by_loop
            ]
            if not body_ids:
                continue
        else:  # LOOP_SCOPE_WORKFLOW
            body_ids = [
                m["id"] for m in clean_nodes
                if m["id"] != lid
                and m["type"] in loop_allowed_member_types
                and m["id"] not in claimed_by_loop
            ]

        body_set = set(body_ids)
        for bid in body_ids:
            claimed_by_loop[bid] = lid
        if body_ids:
            n["execution_level"] = min(by_id[bid]["execution_level"] for bid in body_ids)
        n["depends_on"] = [d for d in n["depends_on"] if d not in body_set]
        ec = n.get("exit_condition") or {}
        om = ec.get("output_match") if isinstance(ec, dict) else {}
        pat = str((om or {}).get("pattern") or "").strip()
        src = str((om or {}).get("source_node") or "").strip()
        if pat and (not src or src not in body_set):
            src = body_ids[-1] if body_ids else ""
        if pat and not src:
            pat = ""
        n["exit_condition"] = {"output_match": {"source_node": src, "pattern": pat}}
        kept_nodes.append(n)

    clean_nodes = kept_nodes
    node_ids_set = {n["id"] for n in clean_nodes}
    by_id = {n["id"]: n for n in clean_nodes}

    # Rewrite any outer node dependency pointing into a loop body to point to the loop node itself
    for n in clean_nodes:
        if n["id"] in claimed_by_loop or n["type"] == "loop":
            n["depends_on"] = [d for d in n["depends_on"] if d in node_ids_set and d != n["id"]]
            continue
        rewritten_deps: List[str] = []
        for d in n.get("depends_on") or []:
            mapped = claimed_by_loop.get(d, d)
            if mapped != n["id"] and mapped in node_ids_set and mapped not in rewritten_deps:
                rewritten_deps.append(mapped)
        n["depends_on"] = rewritten_deps

    # Ensure HITL nodes do not directly depend on typed blocks (decision/router/tool/llm)
    typed_blk_types = {"decision", "router", "tool", "llm"}
    for n in clean_nodes:
        if n["type"] != "hitl":
            continue
        fixed_deps: List[str] = []
        for d in n.get("depends_on") or []:
            parent = by_id.get(d)
            if parent and parent["type"] in typed_blk_types:
                for gd in parent.get("depends_on") or []:
                    if gd in by_id and by_id[gd]["type"] not in typed_blk_types and gd != n["id"] and gd not in fixed_deps:
                        fixed_deps.append(gd)
            elif d in by_id and d != n["id"] and d not in fixed_deps:
                fixed_deps.append(d)
        n["depends_on"] = fixed_deps

    # Final pass: validate when_route references
    for n in clean_nodes:
        wr = n.get("when_route")
        if not isinstance(wr, dict):
            continue
        src = str(wr.get("from") or "")
        src_node = by_id.get(src)
        if not src_node or src_node["type"] not in ("decision", "router") or src not in n["depends_on"]:
            n.pop("when_route", None)
            continue
        valid_routes = set(src_node.get("routes") or ["yes", "no"])
        if "equals" in wr and str(wr["equals"]) not in valid_routes:
            n.pop("when_route", None)

    bp["agents"] = clean_agents
    bp["nodes"] = clean_nodes
    bp.setdefault("source", "llm")
    return bp


def generate_team_and_workflow_blueprint(
    objective: str,
    db: Any,
    *,
    include_decision_gates: bool = True,
    prefer_local_models: bool = True,
    reuse_existing_agents: bool = True,
    expected_exports: Optional[List[str]] = None,
    orchestrator_model_id: Optional[int] = None,
    llm_callable: Optional[Callable[..., str]] = None,
) -> Dict[str, Any]:
    """
    Use MasterAI (or supplied `llm_callable`) + structured JSON repair to design a team
    and branched DAG workflow blueprint for `objective`. Falls back cleanly to a
    domain-aware heuristic blueprint when offline or unconfigured.
    """
    existing_agents: List[Dict[str, Any]] = []
    models: List[Dict[str, Any]] = []
    vector_dbs: List[Dict[str, Any]] = []
    if db is not None:
        try:
            existing_agents = db.read_all_agents() or []
        except Exception:
            existing_agents = []
        try:
            models = db.read_all_models() or []
        except Exception:
            models = []
        try:
            if hasattr(db, "read_all_vector_dbs"):
                vector_dbs = db.read_all_vector_dbs() or []
        except Exception:
            vector_dbs = []

    model_tiers = recommend_models_for_tiers(db, prefer_local=prefer_local_models)
    tools_catalog = get_available_tools_catalog(db)
    valid_tool_keys = set(tools_catalog.keys()) if tools_catalog else None

    compact_agents = [
        {
            "id": a.get("id"),
            "name": a.get("name"),
            "role": a.get("role"),
            "tools": _normalize_tool_list(a.get("tools") or [], valid_tool_keys),
            "model_id": a.get("model_id"),
        }
        for a in existing_agents[:25]
    ]
    compact_models = [
        {
            "id": m.get("id"),
            "provider": m.get("provider"),
            "model_name": m.get("model_name"),
            "capability_tier": m.get("capability_tier", "standard"),
            "is_local": bool(m.get("is_local")),
        }
        for m in models[:25]
    ]
    compact_vdbs = [
        {"id": int(v["id"]), "name": v.get("name")}
        for v in (vector_dbs or [])[:20]
        if v.get("id") is not None
    ]
    valid_vdb_ids = {int(v["id"]) for v in compact_vdbs} if compact_vdbs else None

    sys_prompt = TEAM_ARCHITECT_PROMPT.format(
        existing_agents_json=json.dumps(compact_agents, ensure_ascii=False),
        models_json=json.dumps(compact_models, ensure_ascii=False),
        recommended_models_json=json.dumps(model_tiers, ensure_ascii=False),
        available_tools_json=json.dumps(tools_catalog, ensure_ascii=False),
        vector_dbs_json=json.dumps(compact_vdbs, ensure_ascii=False),
        include_decision_gates=json.dumps(bool(include_decision_gates)),
        expected_exports_json=json.dumps(list(expected_exports or []), ensure_ascii=False),
        decision_model_id=json.dumps(model_tiers.get("decision")),
    )
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": f"USER OBJECTIVE:\n{(objective or '').strip()}"},
    ]

    def _sanitize(raw: str) -> str:
        s = (raw or "").strip()
        if s.startswith("```"):
            s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
            s = re.sub(r"\s*```$", "", s)
        start, end = s.find("{"), s.rfind("}")
        if 0 <= start < end:
            return s[start : end + 1]
        return s

    raw_bp: Optional[Dict[str, Any]] = None
    orchestrator_model_label = "heuristic"
    try:
        if llm_callable is not None:
            from core.structured_llm import validate_or_repair_json

            first = llm_callable(model="custom", messages=messages, temperature=0.2, force_json_mode=True)
            raw_bp = validate_or_repair_json(first, _sanitize, llm_callable, "custom", messages, max_repairs=1)
            orchestrator_model_label = "custom"
        elif models:
            from core.master_ai import MasterAI
            from core.structured_llm import validate_or_repair_json

            master = MasterAI(model_id=orchestrator_model_id, db_manager=db)
            m_str = master._get_model_string()
            first = master._call_llm_with_retry(model=m_str, messages=messages, temperature=0.2, force_json_mode=True)
            raw_bp = validate_or_repair_json(first, _sanitize, master._call_llm_with_retry, m_str, messages, max_repairs=1)
            orchestrator_model_label = m_str
    except Exception as exc:
        logger.info(f"TeamArchitect LLM generation fell back to heuristic blueprint: {exc}")
        raw_bp = None

    if not raw_bp or not isinstance(raw_bp.get("nodes"), list) or not raw_bp["nodes"]:
        raw_bp = _build_heuristic_blueprint(
            objective,
            existing_agents=existing_agents,
            model_tiers=model_tiers,
            include_decision_gates=include_decision_gates,
            reuse_existing_agents=reuse_existing_agents,
            expected_exports=expected_exports,
        )
        orchestrator_model_label = "heuristic"

    normalized_bp = _normalize_blueprint(
        raw_bp,
        objective=objective,
        existing_agents=existing_agents,
        model_tiers=model_tiers,
        reuse_existing_agents=reuse_existing_agents,
        expected_exports=expected_exports,
        valid_tool_keys=valid_tool_keys,
        valid_vdb_ids=valid_vdb_ids,
    )
    normalized_bp["orchestrator_model"] = orchestrator_model_label
    return normalized_bp


_TASK_ASSIST_PROMPT = """You are Alfredo's Lead Task Architect.
Design a complete single task specification from the user's natural-language request.

AVAILABLE AGENTS IN DATABASE:
{agents_json}

AVAILABLE NON-DECISION LLM MODELS IN DATABASE:
{models_json}

AVAILABLE TOOL IDS:
{tools_json}

REQUIREMENTS:
1. Return ONLY valid JSON with this exact schema:
{{
  "name": "short_snake_case_label",
  "description": "Clear actionable instructions. Use {{variable_name}} for runtime inputs and {{previous_result}} if building on upstream steps.",
  "expected_output": "Exact structured deliverable expected from this task.",
  "agent_id": 1,
  "model_id": null,
  "agent_specialization": "optional concise focus area or empty string",
  "tools": ["search_web"],
  "tool_profile": "intermediate|final_writer",
  "human_validation": false,
  "required_inputs": [
    {{"key": "variable_name", "prompt": "Question to ask the user before execution?"}}
  ]
}}
2. Only pick `agent_id` from AVAILABLE AGENTS (or null if empty), `model_id` from AVAILABLE NON-DECISION MODELS (or null to use agent default), and `tools` strictly from AVAILABLE TOOL IDS.
3. Every custom `{{placeholder}}` used in `description` or `expected_output` (excluding reserved names like `previous_result`, `context`, `user_input`) must appear in `required_inputs`.
"""


def _select_task_archetype_key(goal: str) -> str:
    low = (goal or "").lower()
    if any(k in low for k in ("code", "python", "script", "api", "bug", "app", "sql", "codice", "svilup")):
        return "code_generation"
    if any(k in low for k in ("data", "excel", "csv", "metric", "kpi", "calc", "financ", "dati", "metriche", "calcol")):
        return "data_extraction"
    if any(k in low for k in ("audit", "verify", "check", "review", "compliance", "qa", "verific", "controll", "conform")):
        return "qa_audit"
    if any(k in low for k in ("write", "report", "word", "doc", "summary", "scriv", "documento", "redig")):
        return "document_drafting"
    if any(k in low for k in ("plan", "strategy", "roadmap", "go/no-go", "risk", "piano", "strateg", "rischi")):
        return "action_strategy"
    return "web_research"


def _match_best_agent_id(
    agents: List[Dict[str, Any]],
    *,
    role_key: str,
    preferred_agent_id: Optional[int] = None,
) -> Optional[int]:
    valid_ids = {int(a["id"]) for a in agents if a.get("id") is not None}
    if role_key in AGENT_ARCHETYPES and agents:
        matched = match_existing_agent(AGENT_ARCHETYPES[role_key], agents)
        if matched and matched.get("id") is not None:
            return int(matched["id"])
    if preferred_agent_id is not None and int(preferred_agent_id) in valid_ids:
        return int(preferred_agent_id)
    if agents and agents[0].get("id") is not None:
        return int(agents[0]["id"])
    return None


def generate_assisted_task_spec(
    goal: str,
    db: Any,
    *,
    available_tools: Optional[List[str]] = None,
    preferred_agent_id: Optional[int] = None,
    orchestrator_model_id: Optional[int] = None,
    llm_callable: Optional[Callable[..., str]] = None,
) -> Dict[str, Any]:
    """
    Design a ready-to-save Task specification (name, description, expected_output, agent_id,
    model_id, tools, tool_profile, specialization, HITL, and required_inputs) from `goal`.
    """
    clean_goal = (goal or "").strip()
    agents: List[Dict[str, Any]] = []
    models: List[Dict[str, Any]] = []
    if db is not None:
        try:
            agents = db.read_all_agents() or []
        except Exception:
            agents = []
        try:
            models = db.read_all_models() or []
        except Exception:
            models = []

    gen_models = [m for m in models if not _is_decision_only_model(m)]
    resolved_tools = (
        list(available_tools)
        if available_tools is not None
        else list(get_available_tools_catalog(db).keys())
    )
    valid_tool_set = set(resolved_tools) if resolved_tools else None

    compact_agents = [
        {"id": a.get("id"), "name": a.get("name"), "role": a.get("role"), "tools": a.get("tools") or []}
        for a in agents[:25]
    ]
    compact_models = [
        {"id": m.get("id"), "provider": m.get("provider"), "model_name": m.get("model_name"), "capability_tier": m.get("capability_tier", "standard")}
        for m in gen_models[:25]
    ]
    sys_prompt = _TASK_ASSIST_PROMPT.format(
        agents_json=json.dumps(compact_agents, ensure_ascii=False),
        models_json=json.dumps(compact_models, ensure_ascii=False),
        tools_json=json.dumps(resolved_tools, ensure_ascii=False),
    )
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": f"TASK REQUEST:\n{clean_goal or 'Create a structured workflow task'}"},
    ]

    def _sanitize(raw: str) -> str:
        s = (raw or "").strip()
        if s.startswith("```"):
            s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
            s = re.sub(r"\s*```$", "", s)
        start, end = s.find("{"), s.rfind("}")
        if 0 <= start < end:
            return s[start : end + 1]
        return s

    raw_spec: Optional[Dict[str, Any]] = None
    source = "heuristic"
    try:
        if llm_callable is not None:
            from core.structured_llm import validate_or_repair_json

            first = llm_callable(model="custom", messages=messages, temperature=0.2, force_json_mode=True)
            raw_spec = validate_or_repair_json(first, _sanitize, llm_callable, "custom", messages, max_repairs=1)
        elif gen_models:
            from core.master_ai import MasterAI
            from core.structured_llm import validate_or_repair_json

            master = MasterAI(model_id=orchestrator_model_id, db_manager=db)
            m_str = master._get_model_string()
            first = master._call_llm_with_retry(model=m_str, messages=messages, temperature=0.2, force_json_mode=True)
            raw_spec = validate_or_repair_json(first, _sanitize, master._call_llm_with_retry, m_str, messages, max_repairs=1)
    except Exception as exc:
        logger.info(f"Task Co-Pilot LLM generation fell back to heuristic archetype: {exc}")
        raw_spec = None

    arch_key = _select_task_archetype_key(clean_goal)
    tpl = copy.deepcopy(TASK_ARCHETYPES[arch_key])

    if isinstance(raw_spec, dict) and str(raw_spec.get("description") or "").strip():
        source = "llm"
    else:
        slug_words = [w.lower() for w in re.findall(r"[A-Za-z0-9]+", clean_goal)][:4]
        gen_name = "_".join(slug_words) if slug_words else tpl["name"]
        heuristic_tools = list(tpl.get("tools") or [])
        low = clean_goal.lower()
        if any(k in low for k in ("excel", "xlsx", "spreadsheet", "foglio")):
            heuristic_tools.append("create_excel_document")
        if any(k in low for k in ("word", "docx")):
            heuristic_tools.append("create_word_document")
        if any(k in low for k in ("python", ".py")):
            heuristic_tools.append("write_python_file")
        if any(k in low for k in ("email", "mail")):
            heuristic_tools.append("manage_email")
        if any(k in low for k in ("web", "search", "online", "cerca")):
            heuristic_tools.append("search_web")
        if any(k in low for k in ("save", "file", "export", "salva")) and not any(t in WRITE_TOOLS for t in heuristic_tools):
            heuristic_tools.append("write_file")
        # Deduplicate preserving order
        heuristic_tools = list(dict.fromkeys(heuristic_tools))

        desc_body = tpl["description"]
        if clean_goal:
            desc_body = f"Objective: {clean_goal}\n\n{desc_body}"

        raw_spec = {
            "name": gen_name,
            "description": desc_body,
            "expected_output": tpl["expected_output"],
            "agent_id": _match_best_agent_id(
                agents,
                role_key=tpl.get("recommended_role_key", "researcher"),
                preferred_agent_id=preferred_agent_id,
            ),
            "model_id": None,
            "agent_specialization": tpl.get("specialization") or "",
            "tools": heuristic_tools,
            "tool_profile": tpl.get("tool_profile") or "",
            "human_validation": bool(tpl.get("human_validation", False)),
            "required_inputs": list(tpl.get("required_inputs") or []),
        }

    # Normalize validated fields
    valid_agent_ids = {int(a["id"]) for a in agents if a.get("id") is not None}
    valid_model_ids = {int(m["id"]) for m in gen_models if m.get("id") is not None}

    aid = raw_spec.get("agent_id")
    try:
        aid = int(aid) if aid not in (None, "") else None
    except (TypeError, ValueError):
        aid = None
    if aid not in valid_agent_ids:
        aid = _match_best_agent_id(
            agents,
            role_key=tpl.get("recommended_role_key", "researcher"),
            preferred_agent_id=preferred_agent_id,
        )

    mid = raw_spec.get("model_id")
    try:
        mid = int(mid) if mid not in (None, "") else None
    except (TypeError, ValueError):
        mid = None
    if mid not in valid_model_ids:
        mid = None

    tools = _normalize_tool_list(raw_spec.get("tools") or [], valid_tool_set)

    profile = str(raw_spec.get("tool_profile") or "").strip().lower()
    if any(t in WRITE_TOOLS for t in tools):
        profile = TOOL_PROFILE_FINAL_WRITER
    elif profile not in ("", TOOL_PROFILE_INTERMEDIATE, TOOL_PROFILE_FINAL_WRITER):
        profile = TOOL_PROFILE_INTERMEDIATE

    desc = str(raw_spec.get("description") or clean_goal or tpl["description"]).strip()
    exp_out = str(raw_spec.get("expected_output") or tpl["expected_output"]).strip()

    # Merge explicit required_inputs and auto-detected {variables} from description/expected_output
    explicit_inputs: List[Dict[str, str]] = []
    seen_vars: set[str] = set()
    for item in (raw_spec.get("required_inputs") or []):
        if isinstance(item, dict) and item.get("key"):
            k = str(item["key"]).strip()
            if not k or k.lower() in RESERVED_TEMPLATE_VARS or k in seen_vars:
                continue
            p = str(item.get("prompt") or item.get("question") or f"Enter value for '{k}':").strip()
            seen_vars.add(k)
            explicit_inputs.append({"key": k, "prompt": p, "question": p})
        elif isinstance(item, str) and item.strip():
            k = item.strip()
            if k.lower() in RESERVED_TEMPLATE_VARS or k in seen_vars:
                continue
            p = f"Enter value for '{k}':"
            seen_vars.add(k)
            explicit_inputs.append({"key": k, "prompt": p, "question": p})

    detected = extract_template_variables(f"{desc}\n{exp_out}", existing_inputs=explicit_inputs)
    for det in detected:
        if det["key"] not in seen_vars:
            seen_vars.add(det["key"])
            explicit_inputs.append({"key": det["key"], "prompt": det["prompt"], "question": det["prompt"]})

    return {
        "name": str(raw_spec.get("name") or tpl["name"]).strip(),
        "description": desc,
        "expected_output": exp_out,
        "agent_id": aid,
        "model_id": mid,
        "agent_specialization": str(raw_spec.get("agent_specialization") or "").strip(),
        "tools": tools,
        "tool_profile": profile,
        "human_validation": bool(raw_spec.get("human_validation", False)),
        "required_inputs": explicit_inputs,
        "source": source,
    }


def build_task_spec_from_archetype(
    archetype_key: str,
    db: Any,
    *,
    available_tools: Optional[List[str]] = None,
    preferred_agent_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Build a ready-to-save task spec deterministically from `TASK_ARCHETYPES[archetype_key]`."""
    tpl = copy.deepcopy(TASK_ARCHETYPES.get(archetype_key) or next(iter(TASK_ARCHETYPES.values())))
    agents: List[Dict[str, Any]] = []
    if db is not None and hasattr(db, "read_all_agents"):
        try:
            agents = db.read_all_agents() or []
        except Exception:
            agents = []

    valid_tool_set = set(available_tools) if available_tools else None
    tools = [
        t for t in (tpl.get("tools") or [])
        if valid_tool_set is None or t in valid_tool_set
    ]
    req_inputs = [
        {"key": r["key"], "prompt": r["prompt"], "question": r["prompt"]}
        for r in (tpl.get("required_inputs") or [])
        if isinstance(r, dict) and r.get("key")
    ]
    profile = tpl.get("tool_profile") or ""
    if any(t in WRITE_TOOLS for t in tools):
        profile = TOOL_PROFILE_FINAL_WRITER

    return {
        "name": tpl["name"],
        "description": tpl["description"],
        "expected_output": tpl["expected_output"],
        "agent_id": _match_best_agent_id(
            agents,
            role_key=tpl.get("recommended_role_key", "researcher"),
            preferred_agent_id=preferred_agent_id,
        ),
        "model_id": None,
        "agent_specialization": tpl.get("specialization") or "",
        "tools": tools,
        "tool_profile": profile,
        "human_validation": bool(tpl.get("human_validation", False)),
        "required_inputs": req_inputs,
        "source": "archetype",
    }


# ---------------------------------------------------------------------------
# Materialization to DB + Hydrated Canvas Graph
# ---------------------------------------------------------------------------


def _unique_agent_name(base_name: str, existing_names: set[str]) -> str:
    candidate = (base_name or "Agent").strip()
    if candidate not in existing_names:
        existing_names.add(candidate)
        return candidate
    idx = 2
    while f"{candidate} #{idx}" in existing_names:
        idx += 1
    unique = f"{candidate} #{idx}"
    existing_names.add(unique)
    return unique


def materialize_blueprint_to_db(
    blueprint: Dict[str, Any],
    db: Any,
    *,
    save_workflow: bool = False,
) -> Dict[str, Any]:
    """
    Persist new agents & tasks defined in `blueprint` into `db`, assemble a normalized,
    canvas-hydrated graph (with task snapshots and binary decision routers), validate it,
    and optionally save the workflow record.
    """
    existing_agents = {int(a["id"]): a for a in (db.read_all_agents() or []) if a.get("id") is not None}
    existing_names = {str(a.get("name") or "") for a in existing_agents.values()}

    agent_key_to_id: Dict[str, int] = {}
    agent_key_to_model: Dict[str, Optional[int]] = {}
    created_agent_ids: List[int] = []
    reused_agent_ids: List[int] = []

    for ag in blueprint.get("agents") or []:
        akey = str(ag.get("agent_key") or "").strip()
        if not akey:
            continue
        reuse_id = ag.get("reuse_agent_id")
        if reuse_id is not None and int(reuse_id) in existing_agents:
            aid = int(reuse_id)
            agent_key_to_id[akey] = aid
            agent_key_to_model[akey] = existing_agents[aid].get("model_id") or ag.get("model_id")
            reused_agent_ids.append(aid)
            continue

        name = _unique_agent_name(str(ag.get("name") or akey), existing_names)
        role = str(ag.get("role") or "Specialist").strip()
        goal = str(ag.get("goal") or "").strip()
        backstory = str(ag.get("backstory") or "").strip()
        if goal and backstory:
            if "Goal: " in backstory and "\n\nBackstory: " in backstory:
                full_backstory = backstory
            else:
                full_backstory = f"Goal: {goal}\n\nBackstory: {backstory}".strip()
        elif goal:
            full_backstory = f"Goal: {goal}\n\nBackstory: {role}".strip()
        else:
            full_backstory = backstory or role
        mid = int(ag["model_id"]) if ag.get("model_id") not in (None, "") else None
        tools = [str(t) for t in (ag.get("tools") or []) if str(t).strip()]
        aid = int(db.create_agent(name=name, role=role, backstory=full_backstory, model_id=mid, tools=tools))
        agent_key_to_id[akey] = aid
        agent_key_to_model[akey] = mid
        created_agent_ids.append(aid)

    bp_nodes = list(blueprint.get("nodes") or [])
    loop_body_node_ids: set[str] = set()
    for bn in bp_nodes:
        if str(bn.get("type") or "").strip().lower() == "loop":
            for bid in (bn.get("body_node_ids") or []):
                if str(bid).strip():
                    loop_body_node_ids.add(str(bid).strip())

    task_node_ids = {
        str(bn.get("id") or "").strip()
        for bn in bp_nodes
        if str(bn.get("type") or "task").strip().lower() == "task" and str(bn.get("id") or "").strip()
    }
    batch_consumed_map: Dict[str, str] = {}
    for bn in bp_nodes:
        if str(bn.get("type") or "").strip().lower() == "batch_loop":
            bn_id = str(bn.get("id") or "").strip()
            for bid in (bn.get("body_node_ids") or []):
                clean_bid = str(bid).strip()
                if clean_bid in task_node_ids and clean_bid not in loop_body_node_ids and bn_id:
                    batch_consumed_map[clean_bid] = bn_id

    created_task_ids: List[int] = []
    task_map: Dict[int, Dict[str, Any]] = {}
    node_id_to_task_id: Dict[str, int] = {}
    raw_graph: List[Dict[str, Any]] = []
    any_hitl = False

    # Pass 1: persist all DB task records (needed before loop / batch_loop materialization)
    for node in bp_nodes:
        ntype = str(node.get("type") or "task").strip().lower()
        if ntype != "task":
            continue
        nid = str(node.get("id") or new_node_id("step"))
        akey = str(node.get("agent_key") or "")
        aid = agent_key_to_id.get(akey)
        if aid is None and agent_key_to_id:
            aid = next(iter(agent_key_to_id.values()))
        t_tools = _normalize_tool_list(node.get("tools") or [])
        t_profile = (
            TOOL_PROFILE_FINAL_WRITER
            if any(t in WRITE_TOOLS for t in t_tools)
            else TOOL_PROFILE_INTERMEDIATE
        )
        t_model_id = node.get("model_id") if node.get("model_id") not in (None, "") else agent_key_to_model.get(akey)
        is_hitl = bool(node.get("human_validation", False))
        any_hitl = any_hitl or is_hitl

        req_inputs = node.get("required_inputs") or []
        norm_req_inputs = []
        seen_req_keys: set[str] = set()
        for r in req_inputs:
            if isinstance(r, dict) and r.get("key"):
                k = str(r["key"]).strip()
                p = str(r.get("prompt") or r.get("question") or f"Enter {k}:").strip()
                if k and k not in seen_req_keys:
                    seen_req_keys.add(k)
                    norm_req_inputs.append({"key": k, "prompt": p, "question": p})
            elif isinstance(r, str) and r.strip():
                k = r.strip()
                if k not in seen_req_keys:
                    seen_req_keys.add(k)
                    p = f"Enter {k}:"
                    norm_req_inputs.append({"key": k, "prompt": p, "question": p})

        # Auto-extract unbound {template_vars} from node description/expected_output
        wired_keys = set((node.get("inputs_map") or {}).keys())
        for det in extract_template_variables(
            f"{node.get('description') or ''}\n{node.get('expected_output') or ''}",
            existing_inputs=norm_req_inputs,
            excluded_vars=wired_keys,
        ):
            if det["key"] not in seen_req_keys:
                seen_req_keys.add(det["key"])
                norm_req_inputs.append({"key": det["key"], "prompt": det["prompt"], "question": det["prompt"]})

        t_spec = str(node.get("agent_specialization") or "").strip() or None
        t_vdbs = [int(v) for v in (node.get("vector_dbs") or []) if str(v).isdigit()]
        t_name = str(node.get("name") or nid).strip()
        t_desc = str(node.get("description") or "").strip()
        t_exp = str(node.get("expected_output") or "Complete and structured result.").strip()

        tid = int(
            db.create_task(
                description=t_desc,
                expected_output=t_exp,
                agent_id=aid,
                tools=t_tools,
                required_inputs=norm_req_inputs,
                vector_dbs=t_vdbs,
                agent_specialization=t_spec,
                name=t_name,
                model_id=int(t_model_id) if t_model_id not in (None, "") else None,
                human_validation=is_hitl,
                tool_profile=t_profile,
            )
        )
        created_task_ids.append(tid)
        node_id_to_task_id[nid] = tid
        t_record = db.read_task(tid) or {
            "id": tid,
            "name": t_name,
            "description": t_desc,
            "expected_output": t_exp,
            "agent_id": aid,
            "tools": t_tools,
            "required_inputs": norm_req_inputs,
            "vector_dbs": t_vdbs,
            "agent_specialization": t_spec,
            "human_validation": int(is_hitl),
            "tool_profile": t_profile,
        }
        task_map[tid] = t_record

    def _remap_deps(nid: str, raw_deps: List[Any]) -> List[str]:
        out: List[str] = []
        for raw_d in raw_deps or []:
            d = str(raw_d).strip()
            if not d:
                continue
            d = batch_consumed_map.get(d, d)
            if d != nid and d not in out:
                out.append(d)
        return out

    def _remap_inputs_map(raw_imap: Any) -> Dict[str, Any]:
        if not isinstance(raw_imap, dict):
            return {}
        out: Dict[str, Any] = {}
        for port, binding in raw_imap.items():
            if not isinstance(binding, dict):
                continue
            b = dict(binding)
            if b.get("from"):
                src = str(b["from"]).strip()
                b["from"] = batch_consumed_map.get(src, src)
            out[port] = b
        return out

    def _remap_when_route(raw_wr: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(raw_wr, dict) or not raw_wr.get("from"):
            return None
        wr = dict(raw_wr)
        wr_from = str(wr["from"]).strip()
        wr["from"] = batch_consumed_map.get(wr_from, wr_from)
        return wr

    # Pass 2: materialize all canvas/graph nodes (task, decision/router, loop, batch_loop, typed blocks)
    for node in bp_nodes:
        nid = str(node.get("id") or new_node_id("step"))
        ntype = str(node.get("type") or "task").strip().lower()
        lvl = max(1, int(node.get("execution_level") or 1))
        deps = _remap_deps(nid, node.get("depends_on") or [])
        imap = _remap_inputs_map(node.get("inputs_map") or {})
        when_route = _remap_when_route(node.get("when_route"))

        if ntype in ("decision", "router"):
            is_decision = (ntype == "decision") or bool(node.get("decision_mode"))
            routes = list(dict.fromkeys(str(r).strip() for r in (node.get("routes") or []) if str(r).strip()))
            if is_decision or len(routes) < 2:
                routes = ["yes", "no"]
                is_decision = True
            q = str(node.get("question") or node.get("prompt") or "Are the criteria satisfied?").strip()
            try:
                thr = float(0.5 if node.get("threshold") in (None, "") else node.get("threshold"))
            except (TypeError, ValueError):
                thr = 0.5
            def_r = str(node.get("default_route") or ("no" if is_decision else routes[0])).strip()
            if def_r not in routes:
                def_r = "no" if is_decision else routes[0]
            d_node: Dict[str, Any] = {
                "id": nid,
                "type": NODE_TYPE_ROUTER,
                "name": str(node.get("name") or ("Decision (Yes/No)" if is_decision else "Router")).strip(),
                "decision_mode": is_decision,
                "routes": routes,
                "default_route": def_r,
                "router_mode": str(node.get("router_mode") or "auto").strip().lower(),
                "question": q if is_decision else str(node.get("question") or "").strip(),
                "prompt": q if is_decision else str(node.get("prompt") or "").strip(),
                "model_tier": str(node.get("model_tier") or "basic"),
                "execution_level": lvl,
                "depends_on": deps,
                "inputs_map": imap,
            }
            if is_decision:
                d_node["decision_engine"] = str(node.get("decision_engine") or "jev").strip().lower()
                d_node["decision_primitive"] = str(node.get("decision_primitive") or "noul").strip().lower()
                d_node["threshold"] = max(0.0, min(1.0, thr))
            else:
                if isinstance(node.get("rules"), list) and node["rules"]:
                    d_node["rules"] = [dict(r) for r in node["rules"] if isinstance(r, dict)]
            if node.get("jev_model") not in (None, ""):
                d_node["jev_model"] = str(node["jev_model"]).strip()
            if node.get("model_id") not in (None, ""):
                d_node["model_id"] = int(node["model_id"])
            if when_route:
                d_node["when_route"] = when_route
            raw_graph.append(d_node)
            continue

        if ntype == "loop":
            lscope = str(node.get("scope") or LOOP_SCOPE_NODES).strip().lower()
            if lscope not in LOOP_SCOPES:
                lscope = LOOP_SCOPE_NODES
            lmode = str(node.get("loop_mode") or LOOP_MODE_WHILE).strip().lower()
            if lmode not in LOOP_MODES:
                lmode = LOOP_MODE_WHILE
            body_ids = [
                str(b).strip()
                for b in (node.get("body_node_ids") or [])
                if str(b).strip() and str(b).strip() not in batch_consumed_map
            ]
            body_lvls = [int(lv) for lv in (node.get("body_levels") or []) if str(lv).isdigit()]
            if lscope == LOOP_SCOPE_NODES and not body_ids:
                continue
            l_node: Dict[str, Any] = {
                "id": nid,
                "type": NODE_TYPE_LOOP,
                "name": str(node.get("name") or "Structured Loop").strip(),
                "loop_mode": lmode,
                "scope": lscope,
                "body_node_ids": body_ids,
                "body_levels": body_lvls,
                "batch_size": max(1, int(node.get("batch_size") or 5)),
                "source_variable": str(node.get("source_variable") or "{previous_result}").strip(),
                "max_iterations": max(1, int(node.get("max_iterations") or 3)),
                "exit_on_hitl": bool(node.get("exit_on_hitl", True)),
                "exit_condition": copy.deepcopy(node.get("exit_condition") or {"output_match": {"source_node": "", "pattern": ""}}),
                "execution_level": lvl,
                "depends_on": deps,
                "inputs_map": imap,
            }
            if when_route:
                l_node["when_route"] = when_route
            raw_graph.append(l_node)
            continue

        if ntype == "batch_loop":
            batch_tids: List[int] = []
            for raw_t in (node.get("task_ids") or []):
                if str(raw_t).isdigit() and int(raw_t) not in batch_tids:
                    batch_tids.append(int(raw_t))
            for bid in (node.get("body_node_ids") or []):
                mapped_tid = node_id_to_task_id.get(str(bid).strip())
                if mapped_tid is not None and mapped_tid not in batch_tids:
                    batch_tids.append(mapped_tid)
            if not batch_tids:
                continue
            b_node: Dict[str, Any] = {
                "id": nid,
                "type": NODE_TYPE_BATCH,
                "name": str(node.get("name") or "Batch Loop").strip(),
                "task_ids": batch_tids,
                "batch_size": max(1, int(node.get("batch_size") or 5)),
                "source_variable": str(node.get("source_variable") or "{previous_result}").strip(),
                "execution_level": lvl,
                "depends_on": deps,
                "inputs_map": imap,
            }
            if when_route:
                b_node["when_route"] = when_route
            raw_graph.append(b_node)
            continue

        if ntype == "tool":
            tname = str(node.get("tool_name") or "").strip()
            if not tname:
                continue
            tool_node: Dict[str, Any] = {
                "id": nid,
                "type": NODE_TYPE_TOOL,
                "name": str(node.get("name") or tname).strip(),
                "tool_name": tname,
                "tool_args": dict(node.get("tool_args") or {}),
                "append_previous": bool(node.get("append_previous", False)),
                "execution_level": lvl,
                "depends_on": deps,
                "inputs_map": imap,
            }
            if when_route:
                tool_node["when_route"] = when_route
            raw_graph.append(tool_node)
            continue

        if ntype == "llm":
            prompt_txt = str(node.get("prompt") or "").strip()
            if not prompt_txt:
                continue
            llm_node: Dict[str, Any] = {
                "id": nid,
                "type": NODE_TYPE_LLM,
                "name": str(node.get("name") or "Direct LLM").strip(),
                "prompt": prompt_txt,
                "system_prompt": str(node.get("system_prompt") or "").strip(),
                "output_format": str(node.get("output_format") or "text").strip().lower(),
                "temperature": float(0.2 if node.get("temperature") is None else node.get("temperature")),
                "append_previous": bool(node.get("append_previous", True)),
                "model_tier": str(node.get("model_tier") or "default"),
                "execution_level": lvl,
                "depends_on": deps,
                "inputs_map": imap,
            }
            if node.get("model_id") not in (None, ""):
                llm_node["model_id"] = int(node["model_id"])
            if when_route:
                llm_node["when_route"] = when_route
            raw_graph.append(llm_node)
            continue

        if ntype == "hitl":
            any_hitl = True
            h_node: Dict[str, Any] = {
                "id": nid,
                "type": NODE_TYPE_HITL,
                "name": str(node.get("name") or "Human Review").strip(),
                "message": str(node.get("message") or "Please review and approve before continuing.").strip(),
                "gate_mode": str(node.get("gate_mode") or "after_parents").strip(),
                "execution_level": lvl,
                "depends_on": deps,
                "inputs_map": imap,
            }
            if when_route:
                h_node["when_route"] = when_route
            raw_graph.append(h_node)
            continue

        if ntype == "input":
            in_node: Dict[str, Any] = {
                "id": nid,
                "type": NODE_TYPE_INPUT,
                "name": str(node.get("name") or "Workflow Inputs").strip(),
                "keys": [str(k).strip() for k in (node.get("keys") or []) if str(k).strip()],
                "execution_level": lvl,
                "depends_on": [],
                "inputs_map": {},
            }
            raw_graph.append(in_node)
            continue

        if ntype == "export":
            exp_list = [str(e).strip() for e in (node.get("exports") or []) if str(e).strip()]
            ex_node: Dict[str, Any] = {
                "id": nid,
                "type": NODE_TYPE_EXPORT,
                "name": str(node.get("name") or "Export Results").strip(),
                "exports": exp_list,
                "export_instructions": str(node.get("export_instructions") or "").strip(),
                "execution_level": lvl,
                "depends_on": deps,
                "inputs_map": imap,
            }
            if when_route:
                ex_node["when_route"] = when_route
            raw_graph.append(ex_node)
            continue

        # Default: "task" node persisted in Pass 1
        if nid in batch_consumed_map:
            continue
        tid = node_id_to_task_id.get(nid)
        if tid is None or tid not in task_map:
            continue
        t_record = task_map[tid]
        t_node: Dict[str, Any] = {
            "id": nid,
            "type": NODE_TYPE_TASK,
            "task_id": tid,
            "execution_level": lvl,
            "depends_on": deps,
            "inputs_map": imap,
        }
        if isinstance(node.get("handoff_contract"), dict):
            t_node["handoff_contract"] = dict(node["handoff_contract"])
        if when_route:
            t_node["when_route"] = when_route
        t_node = attach_snapshot(t_node, t_record, task_id=tid)
        raw_graph.append(t_node)

    exports = [str(e) for e in (blueprint.get("expected_exports") or []) if str(e).strip()]
    if not exports:
        for rn in raw_graph:
            if rn.get("type") == NODE_TYPE_EXPORT:
                for ex in (rn.get("exports") or []):
                    if ex and ex not in exports:
                        exports.append(ex)
    normalized = normalize_graph(
        raw_graph,
        expected_exports=exports,
        materialize_export=bool(exports),
    )
    hydrated = hydrate_graph_for_canvas(normalized, task_id_map=task_map, force_layout=True)

    errors, warnings = validate_graph(hydrated, task_id_map=task_map)

    workflow_id = None
    if save_workflow:
        workflow_id = int(
            db.create_workflow(
                name=str(blueprint.get("workflow_name") or "AI Team Workflow").strip(),
                task_ids=hydrated,
                requires_human_check=any_hitl,
                expected_exports=exports,
                export_instructions=str(blueprint.get("description") or ""),
            )
        )

    return {
        "graph": hydrated,
        "agent_map": agent_key_to_id,
        "created_agent_ids": created_agent_ids,
        "reused_agent_ids": reused_agent_ids,
        "created_task_ids": created_task_ids,
        "workflow_id": workflow_id,
        "errors": errors,
        "warnings": warnings,
    }


__all__ = [
    "AGENT_ARCHETYPES",
    "DECISION_GATE_TEMPLATES",
    "JEV_DECISION_MODELS",
    "RESERVED_TEMPLATE_VARS",
    "TASK_ARCHETYPES",
    "TEAM_ARCHITECT_PROMPT",
    "build_task_spec_from_archetype",
    "extract_template_variables",
    "generate_assisted_task_spec",
    "generate_team_and_workflow_blueprint",
    "get_available_tools_catalog",
    "match_existing_agent",
    "materialize_blueprint_to_db",
    "recommend_models_for_tiers",
    "resolve_orchestrator_model_info",
]
