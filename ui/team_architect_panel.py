"""
Guided AI Team & Workflow Architect panel for Workflow Assembler (`ui/dashboard.py`).

Lets the user describe an objective in natural language, inspect/edit the
Orchestrator-proposed Team of Agents (reusing or creating), Tasks with automatic
structured handoff contracts, and Binary Decision (Sì/No) branching nodes, and
materialize everything into DB + Canvas with one click.
"""

from __future__ import annotations

import copy
from typing import Any, Callable, Dict, List, Optional

import streamlit as st

from core.team_architect import (
    AGENT_ARCHETYPES,
    DECISION_GATE_TEMPLATES,
    JEV_DECISION_MODELS,
    generate_team_and_workflow_blueprint,
    materialize_blueprint_to_db,
    recommend_models_for_tiers,
)


def render_ai_team_architect_wizard(
    db: Any,
    *,
    available_exports: Optional[List[str]] = None,
    sanitize_input: Optional[Callable[[str], str]] = None,
) -> None:
    """Render the collapsible Guided AI Team & Workflow Architect inside Workflow Assembler."""
    available_exports = list(available_exports or [])
    existing_agents = db.read_all_agents() or []
    models = db.read_all_models() or []

    has_blueprint = bool(st.session_state.get("wf_architect_blueprint"))
    with st.expander(
        "🪄 AI Team & Workflow Architect — Creazione Guidata da Obiettivo (Team + Task + Decisioni Sì/No)",
        expanded=has_blueprint or not (db.read_all_tasks() or []),
    ):
        st.caption(
            "Descrivi cosa vuoi ottenere: l'Orchestratore progetta automaticamente il Team di Agenti, "
            "i Task con contratti di handoff strutturati e i Nodi Decisionali Binari (Sì/No). "
            "Puoi modificare tutto prima di generare il Canvas."
        )

        if st.session_state.get("wf_architect_last_msg"):
            st.success(st.session_state.pop("wf_architect_last_msg"))

        objective = st.text_area(
            "Obiettivo del Workflow",
            placeholder=(
                "Es: Analizza i dati di mercato di un prodotto, valuta con un gate Sì/No "
                "se le fonti sono complete (se No integra la ricerca, se Sì prepara il report esecutivo)."
            ),
            height=85,
            key="wf_arch_objective",
        )

        opt1, opt2, opt3, opt4 = st.columns([1.2, 1.2, 1.1, 1.3])
        with opt1:
            include_decisions = st.checkbox(
                "⚖️ Includi Nodi Sì/No",
                value=True,
                key="wf_arch_inc_dec",
                help="Inserisce almeno un modello decisionale binario Sì/No con branching condizionale.",
            )
        with opt2:
            reuse_agents = st.checkbox(
                "♻️ Riutilizza Agenti esistenti",
                value=True,
                key="wf_arch_reuse",
            )
        with opt3:
            prefer_local = st.checkbox(
                "🏠 Preferisci Modelli Locali",
                value=True,
                key="wf_arch_local",
            )
        with opt4:
            exp_exports = st.multiselect(
                "Export attesi (opz.)",
                options=available_exports,
                default=[],
                key="wf_arch_exports",
            )

        gen_col, clear_col = st.columns([2, 1])
        with gen_col:
            if st.button(
                "🧠 Progetta Team, Task e Decisioni con Orchestratore",
                type="primary",
                use_container_width=True,
                key="wf_arch_generate_btn",
            ):
                if not (objective or "").strip():
                    st.warning("Descrivi prima l'obiettivo del workflow.")
                else:
                    clean_obj = sanitize_input(objective) if sanitize_input else objective.strip()
                    with st.spinner("L'Orchestratore sta progettando Team, Contratti I/O e Nodi Sì/No..."):
                        bp = generate_team_and_workflow_blueprint(
                            clean_obj,
                            db,
                            include_decision_gates=include_decisions,
                            prefer_local_models=prefer_local,
                            reuse_existing_agents=reuse_agents,
                            expected_exports=exp_exports,
                        )
                    st.session_state.wf_architect_blueprint = bp
                    st.session_state.wf_architect_ver = int(st.session_state.get("wf_architect_ver", 0)) + 1
                    st.rerun()
        with clear_col:
            if has_blueprint and st.button(
                "🗑️ Chiudi Bozza",
                use_container_width=True,
                key="wf_arch_clear_btn",
            ):
                st.session_state.pop("wf_architect_blueprint", None)
                st.rerun()

        bp = st.session_state.get("wf_architect_blueprint")
        if not bp:
            return

        ver = int(st.session_state.get("wf_architect_ver", 0))
        st.markdown("---")
        src_badge = "🤖 Progetto AI Orchestrator" if bp.get("source") == "llm" else "⚡ Progetto Guidato Strutturato"
        st.markdown(f"#### Anteprima Modificabile ({src_badge})")

        wf_h1, wf_h2 = st.columns([1.4, 2.0])
        with wf_h1:
            bp["workflow_name"] = st.text_input(
                "Nome Workflow",
                value=bp.get("workflow_name") or "AI Team Workflow",
                key=f"wf_arch_wfname_{ver}",
            )
        with wf_h2:
            bp["description"] = st.text_input(
                "Descrizione breve",
                value=bp.get("description") or "",
                key=f"wf_arch_wfdesc_{ver}",
            )

        # Build model select options
        model_opts: Dict[str, Optional[int]] = {"🤖 Default di sistema": None}
        for m in models:
            lbl = f"{m.get('provider')}/{m.get('model_name')} ({m.get('capability_tier', 'standard')})"
            model_opts[lbl] = int(m["id"])
        model_labels = list(model_opts.keys())

        existing_agent_opts: Dict[str, Optional[int]] = {"➕ Crea nuovo agente": None}
        for ea in existing_agents:
            existing_agent_opts[f"♻️ Riusa: {ea.get('name')} ({ea.get('role')})"] = int(ea["id"])
        existing_agent_labels = list(existing_agent_opts.keys())

        # --- Section 1: Team of Agents ---
        st.markdown("##### 👥 1. Team di Agenti Proposto")
        agents_list: List[Dict[str, Any]] = bp.get("agents") or []
        for idx, ag in enumerate(agents_list):
            akey = ag.get("agent_key") or f"ag_{idx}"
            with st.container(border=True):
                c1, c2, c3, c4 = st.columns([1.3, 1.1, 1.3, 1.3])
                with c1:
                    cur_reuse = ag.get("reuse_agent_id")
                    r_idx = 0
                    for ir, rv in enumerate(existing_agent_opts.values()):
                        if cur_reuse is not None and rv == int(cur_reuse):
                            r_idx = ir
                            break
                    picked_reuse = st.selectbox(
                        f"Origine ({akey})",
                        existing_agent_labels,
                        index=r_idx,
                        key=f"arch_ag_reuse_{ver}_{idx}",
                    )
                    ag["reuse_agent_id"] = existing_agent_opts[picked_reuse]
                with c2:
                    ag["name"] = st.text_input(
                        "Nome Agente",
                        value=ag.get("name") or akey,
                        disabled=ag["reuse_agent_id"] is not None,
                        key=f"arch_ag_name_{ver}_{idx}",
                    )
                with c3:
                    ag["role"] = st.text_input(
                        "Ruolo",
                        value=ag.get("role") or "",
                        disabled=ag["reuse_agent_id"] is not None,
                        key=f"arch_ag_role_{ver}_{idx}",
                    )
                with c4:
                    cur_mid = ag.get("model_id")
                    m_idx = 0
                    for im, vm in enumerate(model_opts.values()):
                        if cur_mid is not None and vm == int(cur_mid):
                            m_idx = im
                            break
                    picked_m = st.selectbox(
                        f"Modello LLM (tier: {ag.get('recommended_tier', 'standard')})",
                        model_labels,
                        index=m_idx,
                        disabled=ag["reuse_agent_id"] is not None,
                        key=f"arch_ag_mod_{ver}_{idx}",
                    )
                    ag["model_id"] = model_opts[picked_m]

                if ag["reuse_agent_id"] is None:
                    ag["goal"] = st.text_input(
                        "Obiettivo agente (Goal)",
                        value=ag.get("goal") or "",
                        key=f"arch_ag_goal_{ver}_{idx}",
                    )

        # --- Section 2: Tasks & Binary Decision Gates (Sì/No) ---
        st.markdown("##### 🔀 2. Sequenza di Task, Handoff Strutturati e Nodi Decisionali (Sì/No)")
        nodes_list: List[Dict[str, Any]] = bp.get("nodes") or []
        agent_key_opts = [a["agent_key"] for a in agents_list if a.get("agent_key")] or ["lead_agent"]
        decision_nodes = [n for n in nodes_list if n.get("type") in ("decision", "router")]

        remove_node_idx: Optional[int] = None
        for idx, node in enumerate(nodes_list):
            nid = node.get("id") or f"step_{idx + 1}"
            ntype = node.get("type") or "task"
            is_dec = ntype in ("decision", "router")
            badge = "⚖️ Decisione Sì/No" if is_dec else "🧩 Task Agente"
            with st.container(border=True):
                h1, h2, h3, h4, h5 = st.columns([0.9, 1.5, 0.7, 1.3, 0.45])
                with h1:
                    new_type_lbl = st.selectbox(
                        "Tipo",
                        ["🧩 Task Agente", "⚖️ Decisione Sì/No"],
                        index=1 if is_dec else 0,
                        key=f"arch_nd_type_{ver}_{idx}",
                    )
                    node["type"] = "decision" if new_type_lbl.startswith("⚖️") else "task"
                    is_dec = node["type"] == "decision"
                with h2:
                    node["name"] = st.text_input(
                        f"Titolo ({nid})",
                        value=node.get("name") or nid,
                        key=f"arch_nd_name_{ver}_{idx}",
                    )
                with h3:
                    node["execution_level"] = int(
                        st.number_input(
                            "Livello",
                            min_value=1,
                            max_value=20,
                            value=int(node.get("execution_level") or (idx + 1)),
                            key=f"arch_nd_lvl_{ver}_{idx}",
                        )
                    )
                with h4:
                    if is_dec:
                        jev_preset_keys = list(JEV_DECISION_MODELS.keys())
                        cur_jev = str(node.get("jev_model") or "tev1").strip()
                        if cur_jev not in jev_preset_keys:
                            jev_preset_keys = [cur_jev] + jev_preset_keys
                        node["jev_model"] = st.selectbox(
                            "Modello Jev / System One",
                            jev_preset_keys,
                            index=jev_preset_keys.index(cur_jev),
                            format_func=lambda k: JEV_DECISION_MODELS[k]["label"] if k in JEV_DECISION_MODELS else k,
                            key=f"arch_nd_jmod_{ver}_{idx}",
                        )
                        node["decision_engine"] = "jev"
                    else:
                        cur_akey = node.get("agent_key")
                        ak_idx = agent_key_opts.index(cur_akey) if cur_akey in agent_key_opts else 0
                        node["agent_key"] = st.selectbox(
                            "Agente assegnato",
                            agent_key_opts,
                            index=ak_idx,
                            key=f"arch_nd_ak_{ver}_{idx}",
                        )
                with h5:
                    st.write("")
                    if st.button("🗑", key=f"arch_nd_del_{ver}_{idx}", help="Rimuovi nodo"):
                        remove_node_idx = idx

                if is_dec:
                    dq1, dq2, dq3 = st.columns([1.8, 0.8, 0.9])
                    with dq1:
                        node["question"] = st.text_area(
                            "Domanda Decisionale Binaria (Jev noul Sì/No)",
                            value=node.get("question") or node.get("prompt") or "I criteri sono soddisfatti?",
                            height=68,
                            key=f"arch_nd_q_{ver}_{idx}",
                        )
                    with dq2:
                        prim_opts = ["noul", "choice", "score"]
                        cur_p = str(node.get("decision_primitive") or "noul").lower()
                        node["decision_primitive"] = st.selectbox(
                            "Primitiva Jev",
                            prim_opts,
                            index=prim_opts.index(cur_p) if cur_p in prim_opts else 0,
                            key=f"arch_nd_prim_{ver}_{idx}",
                        )
                    with dq3:
                        try:
                            cur_t = float(0.5 if node.get("threshold") in (None, "") else node.get("threshold"))
                        except (TypeError, ValueError):
                            cur_t = 0.5
                        node["threshold"] = st.slider(
                            "Soglia (threshold)",
                            0.05,
                            0.95,
                            max(0.05, min(0.95, cur_t)),
                            0.05,
                            key=f"arch_nd_thr_{ver}_{idx}",
                        )
                    node["routes"] = ["yes", "no"]
                    node["decision_mode"] = True
                else:
                    d1, d2 = st.columns([1.4, 1.0])
                    with d1:
                        node["description"] = st.text_area(
                            "Istruzioni Task",
                            value=node.get("description") or "",
                            height=68,
                            key=f"arch_nd_desc_{ver}_{idx}",
                        )
                    with d2:
                        node["expected_output"] = st.text_area(
                            "Output Atteso",
                            value=node.get("expected_output") or "",
                            height=68,
                            key=f"arch_nd_exp_{ver}_{idx}",
                        )

                # Branching selector (when_route)
                other_decisions = [d for d in decision_nodes if d.get("id") != nid]
                if other_decisions:
                    cur_gate = node.get("when_route") if isinstance(node.get("when_route"), dict) else {}
                    gate_opts: Dict[str, Optional[tuple[str, str]]] = {"— Esegui sempre —": None}
                    for dn in other_decisions:
                        dn_id = str(dn.get("id"))
                        gate_opts[f"Se [{dn.get('name') or dn_id}] == YES (Sì)"] = (dn_id, "yes")
                        gate_opts[f"Se [{dn.get('name') or dn_id}] == NO (No)"] = (dn_id, "no")
                    cur_pair = (str(cur_gate.get("from") or ""), str(cur_gate.get("equals") or ""))
                    g_labels = list(gate_opts.keys())
                    g_idx = 0
                    for ig, vg in enumerate(gate_opts.values()):
                        if vg is not None and vg == cur_pair:
                            g_idx = ig
                            break
                    picked_g = st.selectbox(
                        "Condizione di esecuzione (Branching Sì/No)",
                        g_labels,
                        index=g_idx,
                        key=f"arch_nd_gate_{ver}_{idx}",
                    )
                    sel_pair = gate_opts[picked_g]
                    if sel_pair is None:
                        node.pop("when_route", None)
                    else:
                        node["when_route"] = {"from": sel_pair[0], "equals": sel_pair[1]}
                        deps = list(node.get("depends_on") or [])
                        if sel_pair[0] not in deps:
                            deps.append(sel_pair[0])
                        node["depends_on"] = deps

        if remove_node_idx is not None and len(nodes_list) > 1:
            removed = nodes_list.pop(remove_node_idx)
            rid = removed.get("id")
            for n in nodes_list:
                n["depends_on"] = [d for d in (n.get("depends_on") or []) if d != rid]
                if isinstance(n.get("when_route"), dict) and n["when_route"].get("from") == rid:
                    n.pop("when_route", None)
            st.rerun()

        add_n1, add_n2 = st.columns(2)
        with add_n1:
            if st.button("➕ Aggiungi Task al Blueprint", use_container_width=True, key=f"arch_add_task_{ver}"):
                max_lvl = max((int(n.get("execution_level") or 1) for n in nodes_list), default=1)
                last_id = nodes_list[-1]["id"] if nodes_list else None
                nodes_list.append({
                    "id": f"step_{len(nodes_list) + 1}",
                    "type": "task",
                    "name": f"Nuovo Step {len(nodes_list) + 1}",
                    "agent_key": agent_key_opts[0],
                    "execution_level": max_lvl + 1,
                    "depends_on": [last_id] if last_id else [],
                    "description": "Esegui il passo successivo usando il contesto precedente.",
                    "expected_output": "Output strutturato.",
                    "tools": [],
                    "required_inputs": [],
                    "inputs_map": {"upstream": {"from": last_id, "key": "summary"}} if last_id else {},
                })
                st.rerun()
        with add_n2:
            if st.button("➕ Aggiungi Nodo Decisionale (Sì/No)", use_container_width=True, key=f"arch_add_dec_{ver}"):
                max_lvl = max((int(n.get("execution_level") or 1) for n in nodes_list), default=1)
                last_id = nodes_list[-1]["id"] if nodes_list else None
                rec = recommend_models_for_tiers(db, prefer_local=prefer_local)
                tpl = copy.deepcopy(DECISION_GATE_TEMPLATES["quality_check"])
                nodes_list.append({
                    "id": f"gate_{len(nodes_list) + 1}",
                    "type": "decision",
                    "name": tpl["name"],
                    "execution_level": max_lvl + 1,
                    "depends_on": [last_id] if last_id else [],
                    "question": tpl["question"],
                    "routes": ["yes", "no"],
                    "default_route": "no",
                    "decision_mode": True,
                    "decision_engine": "jev",
                    "decision_primitive": "noul",
                    "threshold": float(tpl.get("threshold", 0.5)),
                    "jev_model": str(rec.get("decision_model_name") or "tev1"),
                    "model_tier": "basic",
                    "model_id": rec.get("decision"),
                })
                st.rerun()

        # --- Section 3: Materialize 1-Click ---
        st.markdown("---")
        act1, act2 = st.columns(2)
        with act1:
            if st.button(
                "🚀 Crea Team, Task e Carica nel Canvas",
                type="primary",
                use_container_width=True,
                key=f"arch_mat_canvas_{ver}",
            ):
                res = materialize_blueprint_to_db(bp, db, save_workflow=False)
                if res.get("errors"):
                    st.error("Errori di validazione grafo: " + " | ".join(res["errors"]))
                else:
                    st.session_state.editing_workflow_id = None
                    st.session_state.last_editing_workflow_id = None
                    st.session_state.wf_name_input = bp.get("workflow_name") or "AI Team Workflow"
                    st.session_state.wf_expected_exports = list(bp.get("expected_exports") or [])
                    st.session_state.wf_export_instructions = bp.get("description") or ""
                    st.session_state.wf_selected_task_ids = res["graph"]
                    st.session_state.pop("wf_flow_state", None)
                    st.session_state.pop("wf_architect_blueprint", None)
                    st.session_state.wf_architect_last_msg = (
                        f"✅ Creati {len(res['created_agent_ids'])} nuovi agenti "
                        f"(riutilizzati {len(res['reused_agent_ids'])}), {len(res['created_task_ids'])} task "
                        "e caricato il workflow nel Canvas!"
                    )
                    st.rerun()
        with act2:
            if st.button(
                "💾 Crea Tutto e Salva Workflow nel DB",
                use_container_width=True,
                key=f"arch_mat_save_{ver}",
            ):
                res = materialize_blueprint_to_db(bp, db, save_workflow=True)
                if res.get("errors"):
                    st.error("Errori di validazione grafo: " + " | ".join(res["errors"]))
                else:
                    st.session_state.editing_workflow_id = res["workflow_id"]
                    st.session_state.last_editing_workflow_id = res["workflow_id"]
                    st.session_state.wf_name_input = bp.get("workflow_name") or "AI Team Workflow"
                    st.session_state.wf_expected_exports = list(bp.get("expected_exports") or [])
                    st.session_state.wf_export_instructions = bp.get("description") or ""
                    st.session_state.wf_selected_task_ids = res["graph"]
                    st.session_state.pop("wf_flow_state", None)
                    st.session_state.pop("wf_architect_blueprint", None)
                    st.session_state.wf_architect_last_msg = (
                        f"✅ Workflow salvato (ID {res['workflow_id']}) con {len(res['created_agent_ids'])} "
                        f"nuovi agenti e {len(res['created_task_ids'])} task!"
                    )
                    st.rerun()
