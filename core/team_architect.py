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
import re
from typing import Any, Callable, Dict, List, Optional

from core.node_executor import is_jev_decision_model
from core.task_library import attach_snapshot
from core.workflow_canvas import hydrate_graph_for_canvas
from core.workflow_contracts import WRITE_TOOLS, TOOL_PROFILE_FINAL_WRITER, TOOL_PROFILE_INTERMEDIATE
from core.workflow_graph import (
    NODE_TYPE_ROUTER,
    NODE_TYPE_TASK,
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
        "label": "🔍 Ricercatore & Fonti (Web / Letteratura)",
        "name": "Ricercatore Fonti",
        "role": "Senior Information Researcher",
        "goal": "Raccogliere dati verificati, fonti primarie ed evidenze oggettive sull'obiettivo assegnato.",
        "backstory": (
            "Analista investigativo esperto nel recupero di informazioni verificabili da fonti "
            "web, documentali e scientifiche. Non inventa mai dati mancanti: separa sempre i "
            "fatti verificati dalle ipotesi e riporta riferimenti precisi."
        ),
        "tools": ["web_search"],
        "recommended_tier": "standard",
    },
    "data_analyst": {
        "label": "📊 Analista Dati & Metriche",
        "name": "Analista Dati",
        "role": "Senior Data & Quantitative Analyst",
        "goal": "Trasformare dati grezzi in metriche strutturate, confronti quantitativi e insight azionabili.",
        "backstory": (
            "Specialista in analisi quantitativa, normalizzazione di tabelle e individuazione "
            "di anomalie o trend. Produce output strutturati facilmente consumabili dagli step successivi."
        ),
        "tools": [],
        "recommended_tier": "standard",
    },
    "strategist": {
        "label": "🧭 Strategist & Pianificatore",
        "name": "Strategist Operativo",
        "role": "Lead Strategy & Operations Architect",
        "goal": "Definire piani d'azione concreti, priorità, rischi e raccomandazioni decisionali.",
        "backstory": (
            "Consulente strategico orientato al risultato. Sintetizza analisi complesse in "
            "opzioni operative chiare, evidenziando costi, benefici, rischi e criteri Go/No-Go."
        ),
        "tools": [],
        "recommended_tier": "advanced",
    },
    "tech_architect": {
        "label": "⚙️ Architetto Tecnico / Sviluppatore",
        "name": "Architetto Tecnico",
        "role": "Principal Software & Systems Architect",
        "goal": "Progettare soluzioni tecniche solide, specifiche implementative e codice pulito.",
        "backstory": (
            "Ingegnere software senior focalizzato su affidabilità, contratti I/O chiari, "
            "sicurezza e manutenibilità. Verifica sempre i requisiti prima di produrre artefatti."
        ),
        "tools": ["read_file", "write_file"],
        "recommended_tier": "advanced",
    },
    "writer_editor": {
        "label": "✍️ Copywriter & Redattore Finale",
        "name": "Redattore Esecutivo",
        "role": "Executive Technical Writer & Editor",
        "goal": "Redigere il documento finale chiaro, coerente e pronto per la consegna o esportazione.",
        "backstory": (
            "Editor esperto nella trasformazione di appunti tecnici e dati strutturati in "
            "report esecutivi impeccabili, fedeli ai dati raccolti a monte senza allucinazioni."
        ),
        "tools": ["write_file"],
        "recommended_tier": "standard",
    },
    "qa_critic": {
        "label": "🛡️ Revisore Qualità & Fact-Checker",
        "name": "Revisore Qualità",
        "role": "Quality Assurance & Fact-Checking Reviewer",
        "goal": "Verificare completezza, coerenza fattuale e rispetto dei vincoli prima del rilascio.",
        "backstory": (
            "Auditor rigoroso incaricato di individuare lacune, contraddizioni o affermazioni "
            "non supportate dai dati precedenti."
        ),
        "tools": [],
        "recommended_tier": "basic",
    },
}


JEV_DECISION_MODELS: Dict[str, Dict[str, Any]] = {
    "tev1": {
        "label": "Ollama tev1 (~4B · System One Locale)",
        "provider": "ollama",
        "default_primitive": "noul",
    },
    "tev1:0.8b": {
        "label": "Ollama tev1:0.8b (Ultra-rapido · System One)",
        "provider": "ollama",
        "default_primitive": "noul",
    },
    "jev": {
        "label": "Jev Locale (Ollama /v1/systemone)",
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
        "label": "✅ Controllo Qualità & Completezza (Jev Noul Sì/No)",
        "name": "Quality Gate (Sì/No)",
        "question": (
            "L'output dello step precedente è completo, coerente con l'obiettivo e privo di lacune critiche? "
            "Sì (noul >= threshold) se è pronto per procedere, No se servono integrazioni o correzioni."
        ),
        "default_route": "no",
        "decision_engine": "jev",
        "decision_primitive": "noul",
        "threshold": 0.5,
    },
    "data_sufficiency": {
        "label": "📦 Verifica Sufficienza Dati (Jev Noul Sì/No)",
        "name": "Dati Sufficienti? (Sì/No)",
        "question": (
            "I dati raccolti contengono informazioni sufficienti e verificabili per produrre il risultato finale? "
            "Sì se sufficienti, No se occorre una ricerca o integrazione aggiuntiva."
        ),
        "default_route": "no",
        "decision_engine": "jev",
        "decision_primitive": "noul",
        "threshold": 0.5,
    },
    "feasibility_go_nogo": {
        "label": "🚦 Valutazione Go / No-Go (Jev Noul Sì/No)",
        "name": "Decisione Go/No-Go (Sì/No)",
        "question": (
            "Sulla base dei vincoli, rischi e costi analizzati, la proposta è approvata (GO = Sì) "
            "oppure respinta / da rivedere (NO-GO = No)?"
        ),
        "default_route": "no",
        "decision_engine": "jev",
        "decision_primitive": "noul",
        "threshold": 0.5,
    },
    "compliance_gate": {
        "label": "🔒 Verifica Conformità / Sicurezza (Jev Noul Sì/No)",
        "name": "Compliance Gate (Sì/No)",
        "question": (
            "L'output rispetta tutti i requisiti di sicurezza, policy e accuratezza senza rischi bloccanti? "
            "Sì se conforme, No altrimenti."
        ),
        "default_route": "no",
        "decision_engine": "jev",
        "decision_primitive": "noul",
        "threshold": 0.65,
    },
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
    target_tokens = _tokenize_role(
        f"{proposed_agent.get('name', '')} {proposed_agent.get('role', '')} {proposed_agent.get('goal', '')}"
    )
    best_agent = None
    best_score = 0.0

    for ag in existing_agents:
        ex_name = str(ag.get("name") or "").strip().lower()
        if target_name and ex_name == target_name:
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
Given the user's objective, design a complete, cohesive multi-agent team and executable DAG workflow.

EXISTING AGENTS IN DATABASE (reuse compatible ones via "reuse_agent_id" when appropriate, or set null to create new):
{existing_agents_json}

AVAILABLE LLM MODELS IN DATABASE:
{models_json}

RECOMMENDED MODEL IDS BY TIER:
{recommended_models_json}

OPTIONS:
- Include binary Yes/No decision gates (`decision` nodes): {include_decision_gates}
- Expected exports: {expected_exports_json}

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
      "tools": ["web_search"],
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
      "execution_level": 1,
      "depends_on": [],
      "description": "Exact instructions. Can reference {{input.user_input}} or upstream ports.",
      "expected_output": "Exact structured deliverable expected from this agent.",
      "tools": [],
      "required_inputs": [],
      "inputs_map": {{}},
      "handoff_contract": {{
        "required_fields": ["summary", "key_facts"],
        "output_instruction": "Concludi con un riepilogo puntato chiaro dei fatti chiave."
      }}
    }},
    {{
      "id": "gate_1",
      "type": "decision",
      "name": "Controllo Qualità Sì/No",
      "execution_level": 2,
      "depends_on": ["step_1"],
      "question": "Clear binary YES/NO evaluation question on step_1 output",
      "routes": ["yes", "no"],
      "default_route": "no",
      "model_tier": "basic",
      "model_id": {decision_model_id}
    }},
    {{
      "id": "step_yes",
      "type": "task",
      "name": "Deliverable Finale (Ramo Sì)",
      "agent_key": "writer_editor",
      "execution_level": 3,
      "depends_on": ["gate_1", "step_1"],
      "when_route": {{"from": "gate_1", "equals": "yes"}},
      "description": "Usa {{source_summary}} per generare l'output finale.",
      "expected_output": "Documento/risultato finale completo.",
      "inputs_map": {{"source_summary": {{"from": "step_1", "key": "summary"}}}},
      "tools": [],
      "required_inputs": []
    }}
  ]
}}
2. Design principles:
- Keep teams lean (2-4 agents unless the objective clearly needs more).
- If `include_decision_gates` is true, include at least 1 binary `decision` node (`routes: ["yes", "no"]`) using the lightweight `decision_model_id` and branch downstream tasks cleanly using `when_route: {{"from": "<decision_id>", "equals": "yes"|"no"}}`.
- Wire `inputs_map` deterministically between tasks using `{{"from": "<parent_id>", "key": "summary"}}` or `"key_facts"` so downstream models automatically receive structured upstream context.
- Every task node id referenced in `depends_on`, `when_route.from`, or `inputs_map.*.from` must exist in `nodes` (or be `"input"`).
"""


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
    clean_obj = (objective or "Esecuzione workflow operativo").strip()
    low = clean_obj.lower()
    exports = list(expected_exports or [])

    # Detect needed archetypes from objective keywords
    need_research = any(k in low for k in ("cerca", "ricerc", "analisi", "competitor", "mercato", "fonti", "dati", "paper", "web"))
    need_tech = any(k in low for k in ("codice", "script", "python", "api", "app", "architett", "bug", "test", "sql", "software"))
    need_data = any(k in low for k in ("metriche", "excel", "csv", "calcol", "numeri", "finanzi", "budget", "kpi"))

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
            "tools": list(arch.get("tools") or []),
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
            "name": "Raccolta Dati e Analisi Iniziale",
            "agent_key": first_agent_key,
            "execution_level": 1,
            "depends_on": [],
            "description": (
                f"Analizza in dettaglio l'obiettivo seguente e raccogli tutte le informazioni essenziali:\n"
                f"{clean_obj}\n\n"
                "Contesto aggiuntivo dell'utente: {user_input}"
            ),
            "expected_output": (
                "Riepilogo strutturato con fatti verificati, punti chiave numerati (Key Facts) "
                "e segnalazione esplicita di eventuali informazioni mancanti."
            ),
            "tools": list(agents[0].get("tools") or []),
            "required_inputs": [],
            "inputs_map": {"richiesta": {"from": "input", "key": "user_input"}},
            "handoff_contract": {
                "required_fields": ["summary", "key_facts"],
                "output_instruction": "Includi una sezione di sintesi e un elenco puntato dei punti chiave.",
            },
        }
    ]

    if include_decision_gates:
        gate_tpl = DECISION_GATE_TEMPLATES["data_sufficiency"]
        nodes.append(
            {
                "id": "gate_1",
                "type": "decision",
                "name": gate_tpl["name"],
                "execution_level": 2,
                "depends_on": ["step_1"],
                "question": (
                    f"In relazione all'obiettivo '{clean_obj[:140]}', i dati e l'analisi prodotti da step_1 "
                    "sono completi e sufficienti per redigere direttamente il deliverable finale ('yes') "
                    "oppure richiedono un approfondimento integrativo ('no')?"
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
                "name": "Approfondimento & Integrazione (Ramo NO)",
                "agent_key": analyst_agent_key,
                "execution_level": 3,
                "depends_on": ["gate_1", "step_1"],
                "when_route": {"from": "gate_1", "equals": "no"},
                "description": (
                    "Il gate decisionale ha rilevato lacune nella prima analisi ({gate_reason}).\n"
                    "Partendo dai dati preliminari ({step1_summary}), colma le lacune critiche e "
                    "struttura un quadro completo per l'obiettivo:\n"
                    f"{clean_obj}"
                ),
                "expected_output": "Analisi integrativa completa con metriche, rischi e raccomandazioni.",
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
                "name": "Report Finale & Deliverable (Ramo SÌ)",
                "agent_key": writer_agent_key,
                "execution_level": 3,
                "depends_on": ["gate_1", "step_1"],
                "when_route": {"from": "gate_1", "equals": "yes"},
                "description": (
                    "Il controllo qualità ha approvato i dati raccolti ({step1_facts}).\n"
                    f"Redigi il deliverable finale completo e pronto all'uso per:\n{clean_obj}"
                ),
                "expected_output": "Documento finale ordinato, verificato e direttamente utilizzabile.",
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
                "name": "Consolidamento Finale dopo Integrazione (Ramo NO)",
                "agent_key": writer_agent_key,
                "execution_level": 4,
                "depends_on": ["step_deepen"],
                "when_route": {"from": "gate_1", "equals": "no"},
                "description": (
                    "Consolida l'integrazione ricevuta ({deep_summary}) nel deliverable finale per:\n"
                    f"{clean_obj}"
                ),
                "expected_output": "Documento finale consolidato comprensivo delle integrazioni.",
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
    else:
        nodes.append(
            {
                "id": "step_2",
                "type": "task",
                "name": "Analisi e Strategia Operativa",
                "agent_key": analyst_agent_key,
                "execution_level": 2,
                "depends_on": ["step_1"],
                "description": (
                    "Elabora i dati ricevuti ({step1_summary}) trasformandoli in valutazione tecnica/strategica "
                    f"e piano d'azione per:\n{clean_obj}"
                ),
                "expected_output": "Valutazione strutturata con pro/contro, rischi e passi operativi.",
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
                "name": "Sintesi e Deliverable Finale",
                "agent_key": writer_agent_key,
                "execution_level": 3,
                "depends_on": ["step_2", "step_1"],
                "description": (
                    "Redigi il deliverable finale combinando analisi ({step2_summary}) ed evidenze iniziali "
                    f"({{step1_facts}}) per soddisfare:\n{clean_obj}"
                ),
                "expected_output": "Output finale strutturato e pronto per l'utente.",
                "tools": ["write_file"] if exports else [],
                "required_inputs": [],
                "inputs_map": {
                    "step2_summary": {"from": "step_2", "key": "summary"},
                    "step1_facts": {"from": "step_1", "key": "key_facts"},
                },
            }
        )

    title_words = [w for w in re.findall(r"[A-Za-zÀ-ÿ0-9]+", clean_obj)][:6]
    wf_name = "Workflow: " + (" ".join(title_words) if title_words else "Team Guidato")

    return {
        "workflow_name": wf_name,
        "description": clean_obj[:240],
        "expected_exports": exports,
        "agents": agents,
        "nodes": nodes,
        "source": "heuristic",
    }


def _normalize_blueprint(
    raw_bp: Dict[str, Any],
    *,
    objective: str,
    existing_agents: List[Dict[str, Any]],
    model_tiers: Dict[str, Optional[int]],
    reuse_existing_agents: bool = True,
    expected_exports: Optional[List[str]] = None,
) -> Dict[str, Any]:
    bp = copy.deepcopy(raw_bp or {})
    bp["workflow_name"] = str(bp.get("workflow_name") or "AI Team Workflow").strip()
    bp["description"] = str(bp.get("description") or objective or "").strip()
    bp["expected_exports"] = list(bp.get("expected_exports") or expected_exports or [])

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
            "name": str(ag.get("name") or f"Agente {idx + 1}").strip(),
            "role": str(ag.get("role") or "Workflow Specialist").strip(),
            "goal": str(ag.get("goal") or "").strip(),
            "backstory": str(ag.get("backstory") or "").strip(),
            "tools": [str(t) for t in (ag.get("tools") or []) if str(t).strip()],
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

    clean_nodes: List[Dict[str, Any]] = []
    seen_nids: set[str] = set()
    for idx, node in enumerate(bp.get("nodes") or []):
        if not isinstance(node, dict):
            continue
        ntype = str(node.get("type") or "task").strip().lower()
        if ntype not in ("task", "decision", "router"):
            ntype = "task"
        nid = str(node.get("id") or f"node_{idx + 1}").strip()
        if nid in seen_nids:
            nid = f"{nid}_{idx + 1}"
        seen_nids.add(nid)

        try:
            lvl = max(1, int(node.get("execution_level") or 1))
        except (TypeError, ValueError):
            lvl = 1

        deps = [str(d) for d in (node.get("depends_on") or []) if str(d).strip()]
        item: Dict[str, Any] = {
            "id": nid,
            "type": "decision" if ntype in ("decision", "router") else "task",
            "name": str(node.get("name") or f"Step {idx + 1}").strip(),
            "execution_level": lvl,
            "depends_on": deps,
        }
        if isinstance(node.get("when_route"), dict) and node["when_route"].get("from"):
            item["when_route"] = {
                "from": str(node["when_route"]["from"]).strip(),
                "equals": str(node["when_route"].get("equals") or "yes").strip().lower(),
            }
            if item["when_route"]["from"] not in item["depends_on"]:
                item["depends_on"].append(item["when_route"]["from"])

        if item["type"] == "decision":
            q = str(node.get("question") or node.get("prompt") or "I criteri per procedere sono soddisfatti?").strip()
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
        else:
            akey = str(node.get("agent_key") or "").strip()
            item["agent_key"] = akey if akey in valid_agent_keys else default_agent_key
            item["description"] = str(node.get("description") or objective or "").strip()
            item["expected_output"] = str(node.get("expected_output") or "Risultato strutturato e verificato.").strip()
            item["tools"] = [str(t) for t in (node.get("tools") or []) if str(t).strip()]
            item["required_inputs"] = list(node.get("required_inputs") or [])
            item["inputs_map"] = dict(node.get("inputs_map") or {}) if isinstance(node.get("inputs_map"), dict) else {}
            item["human_validation"] = bool(node.get("human_validation", False))
            if isinstance(node.get("handoff_contract"), dict):
                item["handoff_contract"] = dict(node["handoff_contract"])

        clean_nodes.append(item)

    # Prune dangling dependency references
    node_ids_set = {n["id"] for n in clean_nodes}
    for n in clean_nodes:
        n["depends_on"] = [d for d in n["depends_on"] if d in node_ids_set and d != n["id"]]
        if isinstance(n.get("when_route"), dict) and n["when_route"].get("from") not in node_ids_set:
            n.pop("when_route", None)
        if isinstance(n.get("inputs_map"), dict):
            n["inputs_map"] = {
                p: b
                for p, b in n["inputs_map"].items()
                if isinstance(b, dict) and b.get("from") in (node_ids_set | {"input"})
            }

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
    llm_callable: Optional[Callable[..., str]] = None,
) -> Dict[str, Any]:
    """
    Use MasterAI (or supplied `llm_callable`) + structured JSON repair to design a team
    and branched DAG workflow blueprint for `objective`. Falls back cleanly to a
    domain-aware heuristic blueprint when offline or unconfigured.
    """
    existing_agents: List[Dict[str, Any]] = []
    models: List[Dict[str, Any]] = []
    if db is not None:
        try:
            existing_agents = db.read_all_agents() or []
        except Exception:
            existing_agents = []
        try:
            models = db.read_all_models() or []
        except Exception:
            models = []

    model_tiers = recommend_models_for_tiers(db, prefer_local=prefer_local_models)

    compact_agents = [
        {
            "id": a.get("id"),
            "name": a.get("name"),
            "role": a.get("role"),
            "tools": a.get("tools") or [],
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

    sys_prompt = TEAM_ARCHITECT_PROMPT.format(
        existing_agents_json=json.dumps(compact_agents, ensure_ascii=False),
        models_json=json.dumps(compact_models, ensure_ascii=False),
        recommended_models_json=json.dumps(model_tiers, ensure_ascii=False),
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
    try:
        if llm_callable is not None:
            from core.structured_llm import validate_or_repair_json

            first = llm_callable(model="custom", messages=messages, temperature=0.2, force_json_mode=True)
            raw_bp = validate_or_repair_json(first, _sanitize, llm_callable, "custom", messages, max_repairs=1)
        elif models:
            from core.master_ai import MasterAI
            from core.structured_llm import validate_or_repair_json

            master = MasterAI()
            m_str = master._get_model_string()
            first = master._call_llm_with_retry(model=m_str, messages=messages, temperature=0.2, force_json_mode=True)
            raw_bp = validate_or_repair_json(first, _sanitize, master._call_llm_with_retry, m_str, messages, max_repairs=1)
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

    return _normalize_blueprint(
        raw_bp,
        objective=objective,
        existing_agents=existing_agents,
        model_tiers=model_tiers,
        reuse_existing_agents=reuse_existing_agents,
        expected_exports=expected_exports,
    )


# ---------------------------------------------------------------------------
# Materialization to DB + Hydrated Canvas Graph
# ---------------------------------------------------------------------------


def _unique_agent_name(base_name: str, existing_names: set[str]) -> str:
    candidate = (base_name or "Agente").strip()
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
        if goal and goal not in backstory:
            full_backstory = f"Obiettivo principale: {goal}\n\n{backstory}".strip()
        else:
            full_backstory = backstory or goal or role
        mid = int(ag["model_id"]) if ag.get("model_id") not in (None, "") else None
        tools = [str(t) for t in (ag.get("tools") or []) if str(t).strip()]
        aid = int(db.create_agent(name=name, role=role, backstory=full_backstory, model_id=mid, tools=tools))
        agent_key_to_id[akey] = aid
        agent_key_to_model[akey] = mid
        created_agent_ids.append(aid)

    created_task_ids: List[int] = []
    task_map: Dict[int, Dict[str, Any]] = {}
    raw_graph: List[Dict[str, Any]] = []
    any_hitl = False

    for node in blueprint.get("nodes") or []:
        nid = str(node.get("id") or new_node_id("step"))
        ntype = str(node.get("type") or "task").strip().lower()
        lvl = max(1, int(node.get("execution_level") or 1))
        deps = [str(d) for d in (node.get("depends_on") or []) if str(d).strip()]
        when_route = node.get("when_route") if isinstance(node.get("when_route"), dict) else None

        if ntype in ("decision", "router"):
            q = str(node.get("question") or node.get("prompt") or "I criteri sono soddisfatti?").strip()
            try:
                thr = float(0.5 if node.get("threshold") in (None, "") else node.get("threshold"))
            except (TypeError, ValueError):
                thr = 0.5
            d_node: Dict[str, Any] = {
                "id": nid,
                "type": NODE_TYPE_ROUTER,
                "name": str(node.get("name") or "Decisione Sì/No").strip(),
                "decision_mode": True,
                "decision_engine": str(node.get("decision_engine") or "jev").strip().lower(),
                "decision_primitive": str(node.get("decision_primitive") or "noul").strip().lower(),
                "threshold": max(0.0, min(1.0, thr)),
                "routes": ["yes", "no"],
                "default_route": str(node.get("default_route") or "no"),
                "router_mode": "auto",
                "question": q,
                "prompt": q,
                "model_tier": "basic",
                "execution_level": lvl,
                "depends_on": deps,
                "inputs_map": dict(node.get("inputs_map") or {}),
            }
            if node.get("jev_model") not in (None, ""):
                d_node["jev_model"] = str(node["jev_model"]).strip()
            if node.get("model_id") not in (None, ""):
                d_node["model_id"] = int(node["model_id"])
            if when_route and when_route.get("from"):
                d_node["when_route"] = dict(when_route)
            raw_graph.append(d_node)
            continue

        akey = str(node.get("agent_key") or "")
        aid = agent_key_to_id.get(akey)
        if aid is None and agent_key_to_id:
            aid = next(iter(agent_key_to_id.values()))
        t_tools = [str(t) for t in (node.get("tools") or []) if str(t).strip()]
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
        for r in req_inputs:
            if isinstance(r, dict) and r.get("key"):
                norm_req_inputs.append({"key": str(r["key"]), "question": str(r.get("question") or f"Inserisci {r['key']}:")})
            elif isinstance(r, str) and r.strip():
                norm_req_inputs.append({"key": r.strip(), "question": f"Inserisci {r.strip()}:"})

        tid = int(
            db.create_task(
                description=str(node.get("description") or "").strip(),
                expected_output=str(node.get("expected_output") or "Risultato completo e strutturato.").strip(),
                agent_id=aid,
                tools=t_tools,
                required_inputs=norm_req_inputs,
                vector_dbs=[],
                agent_specialization=None,
                name=str(node.get("name") or nid).strip(),
                model_id=int(t_model_id) if t_model_id not in (None, "") else None,
                human_validation=is_hitl,
                tool_profile=t_profile,
            )
        )
        created_task_ids.append(tid)
        t_record = db.read_task(tid) or {
            "id": tid,
            "name": str(node.get("name") or nid).strip(),
            "description": str(node.get("description") or "").strip(),
            "expected_output": str(node.get("expected_output") or "").strip(),
            "agent_id": aid,
            "tools": t_tools,
            "required_inputs": norm_req_inputs,
            "human_validation": int(is_hitl),
            "tool_profile": t_profile,
        }
        task_map[tid] = t_record

        t_node: Dict[str, Any] = {
            "id": nid,
            "type": NODE_TYPE_TASK,
            "task_id": tid,
            "execution_level": lvl,
            "depends_on": deps,
            "inputs_map": dict(node.get("inputs_map") or {}),
        }
        if isinstance(node.get("handoff_contract"), dict):
            t_node["handoff_contract"] = dict(node["handoff_contract"])
        if when_route and when_route.get("from"):
            t_node["when_route"] = dict(when_route)
        t_node = attach_snapshot(t_node, t_record, task_id=tid)
        raw_graph.append(t_node)

    exports = [str(e) for e in (blueprint.get("expected_exports") or []) if str(e).strip()]
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
    "TEAM_ARCHITECT_PROMPT",
    "generate_team_and_workflow_blueprint",
    "match_existing_agent",
    "materialize_blueprint_to_db",
    "recommend_models_for_tiers",
]
