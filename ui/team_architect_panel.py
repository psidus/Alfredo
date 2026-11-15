"""
Guided AI Team & Workflow Architect panels for `Create a New Workflow` (`ui/dashboard.py`).

Provides:
- `render_ai_team_architect_wizard`: natural-language goal input embedded inside
  `Create a New Workflow` with a full-width loading spinner directly below the
  card controls; immediately populates native Workflow fields, Team agents, Tasks,
  Yes/No Decision gates, and the interactive Canvas.
- `render_workflow_team_and_blocks_editor`: live-synced Team + Task/Decision block
  editor inside `Create a New Workflow` (right above `### Canvas`), including inline
  Required Inputs (`{variable_name}` + Chat Prompt with auto-detection), Write Tool
  injection, Assigned Tools, Model Override, Specialization, HITL, Yes/No branching,
  and 1-click Assisted Task / Decision creation.
"""

from __future__ import annotations

import copy
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

import streamlit as st

from core.data_manager import DataManager
from core.task_library import attach_snapshot
from core.team_architect import (
    AGENT_ARCHETYPES,
    DECISION_GATE_TEMPLATES,
    JEV_DECISION_MODELS,
    TASK_ARCHETYPES,
    build_task_spec_from_archetype,
    extract_template_variables,
    generate_assisted_task_spec,
    generate_team_and_workflow_blueprint,
    get_available_tools_catalog,
    materialize_blueprint_to_db,
    recommend_models_for_tiers,
    resolve_orchestrator_model_info,
)
from core.workflow_canvas import auto_stack_all_levels, hydrate_graph_for_canvas
from core.workflow_contracts import (
    TOOL_PROFILE_FINAL_WRITER,
    TOOL_PROFILE_INTERMEDIATE,
    WRITE_TOOLS,
)
from core.workflow_graph import (
    NODE_TYPE_BATCH,
    NODE_TYPE_EXPORT,
    NODE_TYPE_HITL,
    NODE_TYPE_INPUT,
    NODE_TYPE_LEVEL,
    NODE_TYPE_LLM,
    NODE_TYPE_LOOP,
    NODE_TYPE_ROUTER,
    NODE_TYPE_TASK,
    NODE_TYPE_TOOL,
    make_level_node,
    new_node_id,
    normalize_graph,
    remove_node,
)

WRITE_TOOLS_INJECT_MAP: Dict[str, str] = {
    "write_file": "Save the final text output to a file using the 'write_file' tool. Specify a filename in the workspace.",
    "write_python_file": "Save the generated python code to a .py file using the 'write_python_file' tool.",
    "create_word_document": "Generate a well-formatted Word document using the 'create_word_document' tool.",
    "edit_word_document": "Append or edit an existing Word document using the 'edit_word_document' tool.",
    "create_excel_document": "Generate an Excel spreadsheet with the data using the 'create_excel_document' tool.",
}


def _default_available_tools() -> List[str]:
    try:
        return list(get_available_tools_catalog().keys())
    except Exception:
        return []


def _split_agent_goal_backstory(raw_backstory: str) -> Tuple[str, str]:
    raw = raw_backstory or ""
    if "Goal: " in raw and "\n\nBackstory: " in raw:
        parts = raw.split("\n\nBackstory: ", 1)
        return parts[0].replace("Goal: ", "", 1).strip(), parts[1].strip()
    return "", raw.strip()


def _join_agent_goal_backstory(goal: str, backstory: str) -> str:
    g = (goal or "").strip()
    b = (backstory or "").strip()
    if g and b:
        return f"Goal: {g}\n\nBackstory: {b}"
    if g:
        return f"Goal: {g}\n\nBackstory: {g}"
    return b


def _ensure_level_node(graph: List[Dict[str, Any]], level: int) -> List[Dict[str, Any]]:
    g = list(graph or [])
    if not any(
        isinstance(n, dict) and n.get("type") == NODE_TYPE_LEVEL and int(n.get("level") or 0) == int(level)
        for n in g
    ):
        g.append(make_level_node(int(level)))
    return g


def _restack_graph(graph: List[Dict[str, Any]], db: Any) -> List[Dict[str, Any]]:
    tasks = db.read_all_tasks() or []
    task_map = {int(t["id"]): t for t in tasks if t.get("id") is not None}
    norm = normalize_graph(graph, materialize_export=False)
    hydrated = hydrate_graph_for_canvas(norm, task_id_map=task_map, force_layout=False)
    return auto_stack_all_levels(hydrated, task_id_map=task_map)


def _persist_task_snapshot_edit(
    db: Any,
    task_rec: Dict[str, Any],
    node: Dict[str, Any],
    *,
    description: str,
    expected_output: str,
    agent_id: Optional[int],
    tools: List[str],
    required_inputs: List[Dict[str, str]],
    agent_specialization: Optional[str],
    name: Optional[str],
    model_id: Optional[int],
    human_validation: bool,
    tool_profile: str,
) -> Dict[str, Any]:
    """Update a DB task record and refresh its attached Canvas node snapshot."""
    tid = int(task_rec["id"])
    db.update_task(
        tid,
        description,
        expected_output,
        agent_id,
        tools,
        required_inputs,
        list(task_rec.get("vector_dbs") or []),
        agent_specialization,
        name,
        model_id,
        bool(human_validation),
        int(task_rec.get("max_input_context") or 0),
        int(task_rec.get("max_output_tokens") or 0),
        task_rec.get("output_pydantic"),
        tool_profile or "",
    )
    fresh_t = db.read_task(tid) or task_rec
    attach_snapshot(node, fresh_t, task_id=tid)
    return fresh_t


def _apply_graph_to_canvas_session(
    bp: Dict[str, Any],
    res: Dict[str, Any],
    *,
    available_exports: Optional[List[str]] = None,
    workflow_id: Optional[int] = None,
    message: Optional[str] = None,
) -> None:
    """Populate native `Create a New Workflow` & Canvas session state from a materialized blueprint."""
    exports = list(bp.get("expected_exports") or [])
    if available_exports is not None:
        exports = [e for e in exports if e in available_exports]
    st.session_state.editing_workflow_id = workflow_id
    st.session_state.last_editing_workflow_id = workflow_id
    st.session_state.wf_name_input = bp.get("workflow_name") or "AI Team Workflow"
    st.session_state.wf_expected_exports = exports
    st.session_state.wf_expected_exports_multiselect = list(exports)
    st.session_state.wf_export_instructions = bp.get("description") or ""
    st.session_state.wf_human_check = bool(
        any(
            bool(n.get("human_validation")) or str(n.get("type") or "").lower() == "hitl"
            for n in (bp.get("nodes") or [])
            if isinstance(n, dict)
        )
    )
    st.session_state.wf_selected_task_ids = res["graph"]
    st.session_state.pop("wf_flow_state", None)
    if message:
        st.session_state.wf_architect_last_msg = message


def render_ai_team_architect_wizard(
    db: Any,
    *,
    available_exports: Optional[List[str]] = None,
    sanitize_input: Optional[Callable[[str], str]] = None,
) -> None:
    """
    Render the AI Team & Workflow Architect inside `Create a New Workflow`.
    Places the loading spinner full-width directly below the controls card and populates
    native Workflow inputs, DB Agents/Tasks, Yes/No Decision gates, and the Canvas.
    """
    available_exports = list(available_exports or [])
    has_active_blocks = bool(
        [n for n in (st.session_state.get("wf_selected_task_ids") or []) if isinstance(n, dict) and n.get("type") != NODE_TYPE_LEVEL]
    )

    if st.session_state.get("wf_architect_last_msg"):
        st.success(st.session_state.pop("wf_architect_last_msg"))

    with st.expander(
        "🪄 AI Team & Workflow Architect — Generate Workflow, Team & Yes/No Decisions from Objective",
        expanded=not has_active_blocks,
    ):
        st.caption(
            "Describe what you want to achieve: the Orchestrator automatically proposes the Agent Team, "
            "structured Tasks, I/O contracts, and Binary Yes/No Decision Gates (Jev / System One), "
            "populating the Workflow fields, Block Editor, and interactive Canvas below."
        )

        objective = st.text_area(
            "Workflow Objective",
            placeholder=(
                "Example: Analyze market data for a product, evaluate with a Yes/No decision gate "
                "whether sources are complete (if No deepen research, if Yes draft the executive report)."
            ),
            height=82,
            key="wf_arch_objective",
        )

        c_opt1, c_opt2, c_opt3 = st.columns(3)
        with c_opt1:
            include_decisions = st.checkbox(
                "⚖️ Include Yes/No Gates",
                value=True,
                key="wf_arch_inc_dec",
                help="Inserts at least one binary Yes/No Jev/System One decision model with conditional branching.",
            )
        with c_opt2:
            reuse_agents = st.checkbox(
                "♻️ Reuse Existing Agents",
                value=True,
                key="wf_arch_reuse",
            )
        with c_opt3:
            prefer_local = st.checkbox(
                "🏠 Prefer Local Models",
                value=True,
                key="wf_arch_local",
            )

        orch_info = resolve_orchestrator_model_info(db)
        try:
            all_db_models = db.read_all_models() or []
        except Exception:
            all_db_models = []
        gen_db_models = [
            m
            for m in all_db_models
            if not any(
                tag in f"{m.get('provider', '')} {m.get('model_name', '')}".lower()
                for tag in ("jev", "tev", "systemone", "system_one")
            )
        ]
        auto_orch_label = f"Auto — {orch_info.get('label') or orch_info.get('model_string') or 'default'}"
        orch_model_opts: Dict[str, Optional[int]] = {
            auto_orch_label: None
        }
        for gm in gen_db_models:
            gid = gm.get("id")
            if gid is None:
                continue
            lbl = f"{gm.get('provider')}/{gm.get('model_name')} ({gm.get('capability_tier', 'standard')}, ID {gid})"
            orch_model_opts[lbl] = int(gid)
        orch_labels = list(orch_model_opts.keys())
        saved_orch_id = st.session_state.get("wf_arch_selected_orch_model_id")
        orch_idx = 0
        if saved_orch_id is not None:
            for idx_o, val_o in enumerate(orch_model_opts.values()):
                if val_o == saved_orch_id:
                    orch_idx = idx_o
                    break

        orch_col, gen_col = st.columns([1.4, 2.2], vertical_alignment="bottom")
        with orch_col:
            picked_orch_lbl = st.selectbox(
                "🤖 Orchestrator Model (Master AI)",
                options=orch_labels,
                index=orch_idx,
                key="wf_arch_orch_model",
                help=orch_info.get("note") or "Generative LLM used by Master AI to design teams, workflows, loops, tools, and decisions.",
            )
            picked_orch_model_id = orch_model_opts[picked_orch_lbl]
            st.session_state["wf_arch_selected_orch_model_id"] = picked_orch_model_id
        with gen_col:
            run_generate = st.button(
                "🧠 Design Team, Tasks & Decisions",
                type="primary",
                use_container_width=True,
                key="wf_arch_generate_btn",
            )

    # Full-width status & spinner directly below the Architect card (outside narrow columns)
    status_slot = st.container()
    if run_generate:
        if not (objective or "").strip():
            status_slot.warning("Please describe the workflow objective first.")
        else:
            clean_obj = sanitize_input(objective) if sanitize_input else objective.strip()
            exp_exports = list(
                st.session_state.get("wf_expected_exports_multiselect")
                or st.session_state.get("wf_expected_exports")
                or []
            )
            with status_slot:
                with st.spinner("Orchestrator is designing the Team, I/O Contracts, and Yes/No Gates..."):
                    bp = generate_team_and_workflow_blueprint(
                        clean_obj,
                        db,
                        include_decision_gates=include_decisions,
                        prefer_local_models=prefer_local,
                        reuse_existing_agents=reuse_agents,
                        expected_exports=exp_exports,
                        orchestrator_model_id=picked_orch_model_id,
                    )
                    res = materialize_blueprint_to_db(bp, db, save_workflow=False)
            if res.get("errors"):
                status_slot.error("Graph validation errors: " + " | ".join(res["errors"]))
            else:
                g_nodes = [n for n in (res.get("graph") or []) if isinstance(n, dict) and n.get("type") != NODE_TYPE_LEVEL]
                n_dec = sum(1 for n in g_nodes if n.get("type") == NODE_TYPE_ROUTER)
                n_loops = sum(1 for n in g_nodes if n.get("type") == NODE_TYPE_LOOP)
                orch_used = bp.get("orchestrator_model") or orch_info.get("model_string") or "auto"
                _apply_graph_to_canvas_session(
                    bp,
                    res,
                    available_exports=available_exports,
                    workflow_id=None,
                    message=(
                        f"✅ Generated workflow **{bp.get('workflow_name')}** via `{orch_used}`: "
                        f"created {len(res['created_agent_ids'])} new agents (reused {len(res['reused_agent_ids'])}), "
                        f"{len(res['created_task_ids'])} tasks, {n_dec} Yes/No decision(s), {n_loops} loop(s), "
                        "and loaded everything into `Create a New Workflow` & the Canvas below!"
                    ),
                )
                st.rerun()


def render_workflow_team_and_blocks_editor(
    db: Any,
    *,
    available_exports: Optional[List[str]] = None,
    available_tools: Optional[List[str]] = None,
    sanitize_input: Optional[Callable[[str], str]] = None,
) -> None:
    """
    Render the live-synced Team, Task Blocks (with Required Inputs, Tools, Write-Tool
    injection, Specialization & HITL), Yes/No Decision Gates, and Assisted Task Creator
    inside `Create a New Workflow` right above the Canvas.
    """
    available_tools = list(available_tools) if available_tools is not None else _default_available_tools()
    graph: List[Dict[str, Any]] = list(st.session_state.get("wf_selected_task_ids") or [])
    all_agents = db.read_all_agents() or []
    all_models = db.read_all_models() or []
    all_tasks = db.read_all_tasks() or []

    agents_by_id = {int(a["id"]): a for a in all_agents if a.get("id") is not None}
    tasks_by_id = {int(t["id"]): t for t in all_tasks if t.get("id") is not None}

    active_nodes = [
        n for n in graph if isinstance(n, dict) and n.get("type") not in (None, NODE_TYPE_LEVEL)
    ]

    with st.expander(
        f"👥 Team, Tasks & Yes/No Decisions Editor ({len(active_nodes)} active block(s) — Synced with Canvas)",
        expanded=bool(active_nodes),
    ):
        if st.session_state.get("wf_editor_last_msg"):
            st.success(st.session_state.pop("wf_editor_last_msg"))

        # --- Model & Agent options ---
        model_opts: Dict[str, Optional[int]] = {"None / Use Default": None}
        for m in all_models:
            model_opts[f"{m.get('provider')}/{m.get('model_name')} ({m.get('capability_tier', 'standard')})"] = int(m["id"])
        model_labels = list(model_opts.keys())

        agent_opts: Dict[str, Optional[int]] = {}
        for a in all_agents:
            agent_opts[f"{a.get('name')} ({a.get('role')})"] = int(a["id"])
        agent_labels = list(agent_opts.keys())

        # Collect agents referenced by active Task nodes
        referenced_agent_ids: List[int] = []
        for n in active_nodes:
            if n.get("type") != NODE_TYPE_TASK:
                continue
            tid = n.get("task_id")
            trec = tasks_by_id.get(int(tid)) if tid is not None else None
            aid = (trec or {}).get("agent_id") or (n.get("task_snapshot") or {}).get("agent_id")
            if aid is not None and int(aid) in agents_by_id and int(aid) not in referenced_agent_ids:
                referenced_agent_ids.append(int(aid))

        # --- Part A: Assigned Agents Quick Editor + Quick Create Agent ---
        st.markdown("##### 👥 Active Workflow Team")
        if referenced_agent_ids:
            for aid in referenced_agent_ids:
                ag = agents_by_id[aid]
                cur_goal, cur_backstory = _split_agent_goal_backstory(ag.get("backstory") or "")
                with st.container(border=True):
                    ac1, ac2, ac3, ac4 = st.columns([1.0, 1.3, 1.4, 1.3])
                    with ac1:
                        new_aname = st.text_input(
                            f"Agent Name (#{aid})",
                            value=ag.get("name") or "",
                            key=f"wf_ed_ag_name_{aid}",
                        )
                    with ac2:
                        new_arole = st.text_input(
                            "Role",
                            value=ag.get("role") or "",
                            key=f"wf_ed_ag_role_{aid}",
                        )
                    with ac3:
                        new_agoal = st.text_input(
                            "Goal",
                            value=cur_goal,
                            key=f"wf_ed_ag_goal_{aid}",
                        )
                    with ac4:
                        cur_amid = ag.get("model_id")
                        am_idx = 0
                        for im, vm in enumerate(model_opts.values()):
                            if cur_amid is not None and vm == int(cur_amid):
                                am_idx = im
                                break
                        picked_am = st.selectbox(
                            "Agent LLM Model",
                            model_labels,
                            index=am_idx,
                            key=f"wf_ed_ag_mod_{aid}",
                        )
                        new_amid = model_opts[picked_am]

                    new_full_bs = _join_agent_goal_backstory(new_agoal, cur_backstory)
                    if (
                        new_aname.strip() != (ag.get("name") or "").strip()
                        or new_arole.strip() != (ag.get("role") or "").strip()
                        or new_full_bs.strip() != (ag.get("backstory") or "").strip()
                        or new_amid != ag.get("model_id")
                    ):
                        db.update_agent(
                            aid,
                            new_aname.strip() or ag.get("name") or "Agent",
                            new_arole.strip() or ag.get("role") or "Specialist",
                            new_full_bs,
                            new_amid,
                            list(ag.get("tools") or []),
                        )
                        all_agents = db.read_all_agents() or []
                        agents_by_id = {int(a["id"]): a for a in all_agents if a.get("id") is not None}
        else:
            st.caption("No agents assigned to active workflow tasks yet. Generate a workflow above or quick-create an agent below.")

        with st.expander("➕ Quick Add Agent from Role Archetype", expanded=not bool(all_agents)):
            qa1, qa2 = st.columns([2.4, 1.0], vertical_alignment="bottom")
            arch_keys = list(AGENT_ARCHETYPES.keys())
            with qa1:
                picked_ag_arch = st.selectbox(
                    "Role Archetype",
                    arch_keys,
                    format_func=lambda k: AGENT_ARCHETYPES[k]["label"],
                    key="wf_ed_quick_ag_arch",
                )
            with qa2:
                if st.button("➕ Create Agent", use_container_width=True, key="wf_ed_quick_ag_btn"):
                    tpl = AGENT_ARCHETYPES[picked_ag_arch]
                    rec_tiers = recommend_models_for_tiers(db, prefer_local=True)
                    tier_mid = rec_tiers.get(tpl.get("recommended_tier", "standard")) or rec_tiers.get("standard")
                    existing_names = {str(a.get("name") or "") for a in all_agents}
                    cname = tpl["name"]
                    idx_u = 2
                    while cname in existing_names:
                        cname = f"{tpl['name']} #{idx_u}"
                        idx_u += 1
                    full_bs = _join_agent_goal_backstory(tpl["goal"], tpl["backstory"])
                    new_aid = db.create_agent(
                        name=cname,
                        role=tpl["role"],
                        backstory=full_bs,
                        model_id=tier_mid,
                        tools=[t for t in (tpl.get("tools") or []) if not available_tools or t in available_tools],
                    )
                    st.session_state.wf_editor_last_msg = f"✅ Created agent **{cname}** (ID {new_aid})."
                    st.rerun()

        # --- Part B: Active Workflow Blocks (Tasks, Decisions, Loops & Typed Blocks) ---
        st.markdown("##### 🔀 Active Workflow Blocks (Tasks, Decisions & Loops)")
        decision_nodes = [
            n
            for n in active_nodes
            if n.get("type") == NODE_TYPE_ROUTER
            and (bool(n.get("decision_mode")) or set(n.get("routes") or []) <= {"yes", "no"})
        ]

        _type_labels = {
            NODE_TYPE_ROUTER: "⚖️ Decision (Yes/No)",
            NODE_TYPE_TASK: "🧩 Agent Task",
            NODE_TYPE_LOOP: "🔁 Loop Frame",
            NODE_TYPE_BATCH: "📦 Batch Loop",
            NODE_TYPE_TOOL: "🛠️ Tool Block",
            NODE_TYPE_LLM: "💬 Direct LLM",
            NODE_TYPE_HITL: "✋ HITL Gate",
            NODE_TYPE_INPUT: "🔑 Input Block",
            NODE_TYPE_EXPORT: "📤 Export Block",
        }

        graph_mutated = False
        for node_pos, node in enumerate(active_nodes):
            nid = str(node.get("id") or f"node_{node_pos}")
            rev = int(st.session_state.get(f"wf_blk_rev_{nid}", 0))
            ntype = str(node.get("type") or NODE_TYPE_TASK)
            is_dec = ntype == NODE_TYPE_ROUTER
            is_binary_dec = is_dec and (bool(node.get("decision_mode")) or set(node.get("routes") or []) <= {"yes", "no"})
            is_task = ntype == NODE_TYPE_TASK

            tid_raw = node.get("task_id")
            task_rec = tasks_by_id.get(int(tid_raw)) if (is_task and tid_raw is not None) else None

            with st.container(border=True):
                h1, h2, h3, h4, h5 = st.columns([0.9, 1.5, 0.7, 1.4, 0.45], vertical_alignment="bottom")
                with h1:
                    st.text_input(
                        "Type",
                        value=_type_labels.get(ntype, f"🔹 {ntype}"),
                        disabled=True,
                        key=f"wf_blk_type_{nid}_{rev}",
                    )
                with h2:
                    if is_task:
                        new_title = st.text_input(
                            f"Task Name ({nid})",
                            value=str((task_rec or {}).get("name") or (node.get("task_snapshot") or {}).get("name") or nid),
                            key=f"wf_blk_name_{nid}_{rev}",
                        )
                    else:
                        default_lbl = _type_labels.get(ntype, ntype).split(" ", 1)[-1]
                        new_title = st.text_input(
                            f"Title ({nid})",
                            value=str(node.get("name") or default_lbl),
                            key=f"wf_blk_name_{nid}_{rev}",
                        )
                with h3:
                    cur_lvl = max(1, int(node.get("execution_level") or 1))
                    new_lvl = int(
                        st.number_input(
                            "Level",
                            min_value=1,
                            max_value=20,
                            value=cur_lvl,
                            key=f"wf_blk_lvl_{nid}_{rev}",
                        )
                    )
                with h4:
                    if is_binary_dec:
                        jev_preset_keys = list(JEV_DECISION_MODELS.keys())
                        cur_jev = str(node.get("jev_model") or "tev1").strip()
                        if cur_jev and cur_jev not in jev_preset_keys:
                            jev_preset_keys = [cur_jev] + jev_preset_keys
                        new_jev = st.selectbox(
                            "Jev / System One Model",
                            jev_preset_keys,
                            index=jev_preset_keys.index(cur_jev) if cur_jev in jev_preset_keys else 0,
                            format_func=lambda k: JEV_DECISION_MODELS[k]["label"] if k in JEV_DECISION_MODELS else k,
                            key=f"wf_blk_jmod_{nid}_{rev}",
                        )
                    elif is_task:
                        cur_aid = (task_rec or {}).get("agent_id")
                        ak_idx = 0
                        for ia, va in enumerate(agent_opts.values()):
                            if cur_aid is not None and va == int(cur_aid):
                                ak_idx = ia
                                break
                        if agent_labels:
                            picked_ag_lbl = st.selectbox(
                                "Assigned Agent",
                                agent_labels,
                                index=ak_idx,
                                key=f"wf_blk_ag_{nid}_{rev}",
                            )
                            new_aid = agent_opts[picked_ag_lbl]
                        else:
                            st.info("Create an agent above")
                            new_aid = None
                    else:
                        if ntype == NODE_TYPE_LOOP:
                            summary_txt = f"{node.get('loop_mode', 'while')} · {len(node.get('body_node_ids') or [])} node(s)"
                        elif ntype == NODE_TYPE_BATCH:
                            summary_txt = f"size={node.get('batch_size', 5)} · {len(node.get('task_ids') or [])} task(s)"
                        elif ntype == NODE_TYPE_TOOL:
                            summary_txt = str(node.get("tool_name") or "tool")
                        elif ntype == NODE_TYPE_LLM:
                            summary_txt = f"{node.get('output_format', 'text')} · {node.get('model_tier', 'default')}"
                        elif ntype == NODE_TYPE_ROUTER:
                            summary_txt = f"routes: {', '.join(node.get('routes') or [])}"
                        elif ntype == NODE_TYPE_INPUT:
                            summary_txt = f"keys: {', '.join(node.get('keys') or [])}"
                        elif ntype == NODE_TYPE_EXPORT:
                            summary_txt = ", ".join(node.get("exports") or []) or "export"
                        else:
                            summary_txt = str(node.get("gate_mode") or "checkpoint")
                        st.text_input(
                            "Config",
                            value=summary_txt,
                            disabled=True,
                            key=f"wf_blk_cfg_{nid}_{rev}",
                        )
                with h5:
                    if st.button("🗑", key=f"wf_blk_del_{nid}_{rev}", use_container_width=True, help="Remove block from workflow"):
                        st.session_state.wf_selected_task_ids = remove_node(graph, nid)
                        st.session_state.pop("wf_flow_state", None)
                        st.rerun()

                if new_lvl != cur_lvl:
                    node["execution_level"] = new_lvl
                    graph = _ensure_level_node(graph, new_lvl)
                    graph_mutated = True

                # Middle body
                if is_binary_dec:
                    dq1, dq2, dq3, dq4 = st.columns([1.8, 0.8, 0.9, 0.7])
                    with dq1:
                        new_q = st.text_area(
                            "Binary Decision Question (Jev noul Yes/No)",
                            value=str(node.get("question") or node.get("prompt") or "Are the criteria satisfied?"),
                            height=70,
                            key=f"wf_blk_q_{nid}_{rev}",
                        )
                    with dq2:
                        prim_opts = ["noul", "choice", "score"]
                        cur_p = str(node.get("decision_primitive") or "noul").lower()
                        new_prim = st.selectbox(
                            "Jev Primitive",
                            prim_opts,
                            index=prim_opts.index(cur_p) if cur_p in prim_opts else 0,
                            key=f"wf_blk_prim_{nid}_{rev}",
                        )
                    with dq3:
                        try:
                            cur_t = float(0.5 if node.get("threshold") in (None, "") else node.get("threshold"))
                        except (TypeError, ValueError):
                            cur_t = 0.5
                        new_thr = st.slider(
                            "Probability Threshold",
                            0.05,
                            0.95,
                            max(0.05, min(0.95, cur_t)),
                            0.05,
                            key=f"wf_blk_thr_{nid}_{rev}",
                        )
                    with dq4:
                        def_opts = ["no", "yes"]
                        cur_def = str(node.get("default_route") or "no").lower()
                        new_def = st.selectbox(
                            "Fallback",
                            def_opts,
                            index=def_opts.index(cur_def) if cur_def in def_opts else 0,
                            key=f"wf_blk_def_{nid}_{rev}",
                        )

                    if (
                        new_title.strip() != str(node.get("name") or "").strip()
                        or new_q.strip() != str(node.get("question") or "").strip()
                        or new_jev != str(node.get("jev_model") or "")
                        or new_prim != str(node.get("decision_primitive") or "noul")
                        or abs(float(new_thr) - float(node.get("threshold") or 0.5)) > 1e-6
                        or new_def != str(node.get("default_route") or "no")
                    ):
                        node["name"] = new_title.strip() or "Decision (Yes/No)"
                        node["question"] = new_q.strip()
                        node["prompt"] = new_q.strip()
                        node["jev_model"] = new_jev
                        node["decision_engine"] = "jev"
                        node["decision_mode"] = True
                        node["decision_primitive"] = new_prim
                        node["threshold"] = float(new_thr)
                        node["default_route"] = new_def
                        node["routes"] = ["yes", "no"]
                        graph_mutated = True

                elif task_rec is not None:
                    d1, d2 = st.columns([1.3, 1.1])
                    with d1:
                        new_desc = st.text_area(
                            "Task Description",
                            value=str(task_rec.get("description") or ""),
                            height=82,
                            key=f"wf_blk_desc_{nid}_{rev}",
                            help="Use `{variable_name}` for Required Inputs, `{previous_result}` for upstream outputs.",
                        )
                    with d2:
                        new_out = st.text_area(
                            "Expected Output",
                            value=str(task_rec.get("expected_output") or ""),
                            height=82,
                            key=f"wf_blk_out_{nid}_{rev}",
                        )

                    # Expandable Task Builder Fields: Required Inputs, Tools, Write-Tool Inject, Spec, Model, HITL
                    cur_req_raw = list(task_rec.get("required_inputs") or [])
                    draft_state_key = f"wf_blk_req_list_{nid}_{rev}"
                    if draft_state_key not in st.session_state:
                        st.session_state[draft_state_key] = [
                            {
                                "key": str(item.get("key") or ""),
                                "prompt": str(item.get("prompt") or item.get("question") or ""),
                            }
                            for item in cur_req_raw
                            if isinstance(item, dict)
                        ]

                    with st.expander("⚙️ Inputs, Tools & Advanced Settings", expanded=False):
                        st.markdown("**🔑 Required Inputs** (`{variable_name}` + Chat Prompt)")
                        ri_bar1, ri_bar2 = st.columns(2)
                        with ri_bar1:
                            if st.button("🔍 Auto-Detect {Variables}", key=f"wf_blk_autovar_{nid}_{rev}", use_container_width=True):
                                existing_list = st.session_state[draft_state_key]
                                existing_keys = {r["key"].strip() for r in existing_list if r.get("key", "").strip()}
                                wired = set((node.get("inputs_map") or {}).keys())
                                detected = extract_template_variables(
                                    f"{new_desc}\n{new_out}",
                                    existing_inputs=existing_list,
                                    excluded_vars=wired,
                                )
                                added_rows = [
                                    {"key": d["key"], "prompt": d["prompt"], "question": d["prompt"]}
                                    for d in detected
                                    if d["key"] not in existing_keys
                                ]
                                merged_rows = [
                                    {
                                        "key": r["key"].strip(),
                                        "prompt": r["prompt"].strip(),
                                        "question": r["prompt"].strip(),
                                    }
                                    for r in existing_list
                                    if r.get("key", "").strip() and r.get("prompt", "").strip()
                                ] + added_rows
                                if merged_rows != cur_req_raw:
                                    _persist_task_snapshot_edit(
                                        db,
                                        task_rec,
                                        node,
                                        description=new_desc.strip(),
                                        expected_output=new_out.strip(),
                                        agent_id=new_aid,
                                        tools=list(task_rec.get("tools") or []),
                                        required_inputs=merged_rows,
                                        agent_specialization=task_rec.get("agent_specialization"),
                                        name=new_title.strip() or None,
                                        model_id=task_rec.get("model_id"),
                                        human_validation=bool(task_rec.get("human_validation")),
                                        tool_profile=task_rec.get("tool_profile") or "",
                                    )
                                    st.session_state.wf_selected_task_ids = graph
                                    st.session_state.pop("wf_flow_state", None)
                                st.session_state[f"wf_blk_rev_{nid}"] = rev + 1
                                st.rerun()
                        with ri_bar2:
                            if st.button("➕ Add Input Variable", key=f"wf_blk_addvar_{nid}_{rev}", use_container_width=True):
                                st.session_state[draft_state_key].append({"key": "", "prompt": ""})
                                st.rerun()

                        draft_rows = st.session_state[draft_state_key]
                        remove_var_idx: Optional[int] = None
                        updated_req_rows: List[Dict[str, str]] = []
                        for r_idx, r_item in enumerate(draft_rows):
                            rc1, rc2, rc3 = st.columns([1.0, 2.0, 0.35], vertical_alignment="bottom")
                            with rc1:
                                rk = st.text_input(
                                    "Variable Name" if r_idx == 0 else f"Var {r_idx}",
                                    value=r_item.get("key") or "",
                                    placeholder="e.g. topic",
                                    label_visibility="visible" if r_idx == 0 else "collapsed",
                                    key=f"wf_blk_rk_{nid}_{rev}_{r_idx}",
                                )
                            with rc2:
                                rp = st.text_input(
                                    "Chat Prompt" if r_idx == 0 else f"Prompt {r_idx}",
                                    value=r_item.get("prompt") or "",
                                    placeholder="e.g. What topic should be analyzed?",
                                    label_visibility="visible" if r_idx == 0 else "collapsed",
                                    key=f"wf_blk_rp_{nid}_{rev}_{r_idx}",
                                )
                            with rc3:
                                if st.button("🗑️", key=f"wf_blk_rdel_{nid}_{rev}_{r_idx}", help="Remove variable"):
                                    remove_var_idx = r_idx
                            draft_rows[r_idx] = {"key": rk, "prompt": rp}
                            if rk.strip() and rp.strip():
                                updated_req_rows.append({"key": rk.strip(), "prompt": rp.strip(), "question": rp.strip()})

                        if remove_var_idx is not None:
                            draft_rows.pop(remove_var_idx)
                            cleaned_after = [
                                {"key": r["key"].strip(), "prompt": r["prompt"].strip(), "question": r["prompt"].strip()}
                                for r in draft_rows
                                if r.get("key", "").strip() and r.get("prompt", "").strip()
                            ]
                            _persist_task_snapshot_edit(
                                db,
                                task_rec,
                                node,
                                description=new_desc.strip(),
                                expected_output=new_out.strip(),
                                agent_id=new_aid,
                                tools=list(task_rec.get("tools") or []),
                                required_inputs=cleaned_after,
                                agent_specialization=task_rec.get("agent_specialization"),
                                name=new_title.strip() or None,
                                model_id=task_rec.get("model_id"),
                                human_validation=bool(task_rec.get("human_validation")),
                                tool_profile=task_rec.get("tool_profile") or "",
                            )
                            st.session_state[f"wf_blk_rev_{nid}"] = rev + 1
                            st.session_state.wf_selected_task_ids = graph
                            st.session_state.pop("wf_flow_state", None)
                            st.rerun()

                        st.markdown("---")
                        cur_tools = list(task_rec.get("tools") or [])
                        tool_choices = list(dict.fromkeys(list(available_tools) + cur_tools))

                        inj1, inj2 = st.columns([2.2, 1.0], vertical_alignment="bottom")
                        with inj1:
                            picked_wtool = st.selectbox(
                                "🪄 Quick Action: Assign Write Tool & Inject Instructions",
                                options=["None"] + list(WRITE_TOOLS_INJECT_MAP.keys()),
                                format_func=lambda x: "Select a write tool to inject..." if x == "None" else x,
                                key=f"wf_blk_wtool_{nid}_{rev}",
                            )
                        with inj2:
                            if st.button("Inject Write Tool", key=f"wf_blk_wtool_btn_{nid}_{rev}", use_container_width=True):
                                if picked_wtool != "None":
                                    instr = WRITE_TOOLS_INJECT_MAP[picked_wtool]
                                    mod_out = new_out
                                    if instr not in mod_out:
                                        mod_out = (mod_out.rstrip() + f"\n\n[TOOL INSTRUCTION]: {instr}").strip()
                                    mod_tools = list(dict.fromkeys(cur_tools + [picked_wtool]))
                                    _persist_task_snapshot_edit(
                                        db,
                                        task_rec,
                                        node,
                                        description=new_desc.strip(),
                                        expected_output=mod_out,
                                        agent_id=new_aid,
                                        tools=mod_tools,
                                        required_inputs=updated_req_rows,
                                        agent_specialization=task_rec.get("agent_specialization"),
                                        name=new_title.strip() or None,
                                        model_id=task_rec.get("model_id"),
                                        human_validation=bool(task_rec.get("human_validation")),
                                        tool_profile=TOOL_PROFILE_FINAL_WRITER,
                                    )
                                    st.session_state[f"wf_blk_rev_{nid}"] = rev + 1
                                    st.session_state.wf_selected_task_ids = graph
                                    st.session_state.pop("wf_flow_state", None)
                                    st.rerun()

                        new_tools = st.multiselect(
                            "🛠️ Assigned Tools",
                            options=tool_choices,
                            default=cur_tools,
                            key=f"wf_blk_tools_{nid}_{rev}",
                        )

                        adv1, adv2 = st.columns(2)
                        with adv1:
                            t_model_opts = {"None / Use Agent Default": None}
                            for m in all_models:
                                t_model_opts[f"{m.get('provider')}/{m.get('model_name')}"] = int(m["id"])
                            t_model_labels = list(t_model_opts.keys())
                            cur_tmid = task_rec.get("model_id")
                            tm_idx = 0
                            for itm, vtm in enumerate(t_model_opts.values()):
                                if cur_tmid is not None and vtm == int(cur_tmid):
                                    tm_idx = itm
                                    break
                            picked_tm = st.selectbox(
                                "Model Override (Optional)",
                                t_model_labels,
                                index=tm_idx,
                                key=f"wf_blk_tmid_{nid}_{rev}",
                            )
                            new_tmid = t_model_opts[picked_tm]

                            new_spec = st.text_input(
                                "Agent Specialization (Optional)",
                                value=str(task_rec.get("agent_specialization") or ""),
                                placeholder="e.g. financial benchmarking",
                                key=f"wf_blk_spec_{nid}_{rev}",
                            )
                        with adv2:
                            prof_opts = ["", TOOL_PROFILE_INTERMEDIATE, TOOL_PROFILE_FINAL_WRITER]
                            cur_prof = str(task_rec.get("tool_profile") or "")
                            if cur_prof not in prof_opts:
                                cur_prof = ""
                            new_prof = st.selectbox(
                                "Tool Profile",
                                prof_opts,
                                index=prof_opts.index(cur_prof),
                                format_func=lambda x: {
                                    "": "Auto (infer from write tools)",
                                    TOOL_PROFILE_INTERMEDIATE: "Intermediate — no durable write tools",
                                    TOOL_PROFILE_FINAL_WRITER: "Final writer — write tools allowed",
                                }.get(x, x),
                                key=f"wf_blk_prof_{nid}_{rev}",
                            )
                            new_hitl = st.checkbox(
                                "Pause for Human Validation (HITL)",
                                value=bool(task_rec.get("human_validation")),
                                key=f"wf_blk_hitl_{nid}_{rev}",
                            )

                    # Compare against persisted DB record and write-through on change
                    norm_cur_req = [
                        {
                            "key": str(r.get("key") or "").strip(),
                            "prompt": str(r.get("prompt") or r.get("question") or "").strip(),
                            "question": str(r.get("question") or r.get("prompt") or "").strip(),
                        }
                        for r in cur_req_raw
                        if isinstance(r, dict) and r.get("key") and (r.get("prompt") or r.get("question"))
                    ]
                    if (
                        new_title.strip() != str(task_rec.get("name") or "").strip()
                        or new_desc.strip() != str(task_rec.get("description") or "").strip()
                        or new_out.strip() != str(task_rec.get("expected_output") or "").strip()
                        or new_aid != task_rec.get("agent_id")
                        or new_tmid != task_rec.get("model_id")
                        or new_spec.strip() != str(task_rec.get("agent_specialization") or "").strip()
                        or new_tools != cur_tools
                        or updated_req_rows != norm_cur_req
                        or bool(new_hitl) != bool(task_rec.get("human_validation"))
                        or new_prof != str(task_rec.get("tool_profile") or "")
                    ):
                        fresh_t = _persist_task_snapshot_edit(
                            db,
                            task_rec,
                            node,
                            description=sanitize_input(new_desc) if sanitize_input else new_desc.strip(),
                            expected_output=sanitize_input(new_out) if sanitize_input else new_out.strip(),
                            agent_id=new_aid,
                            tools=new_tools,
                            required_inputs=updated_req_rows,
                            agent_specialization=new_spec.strip() or None,
                            name=new_title.strip() or None,
                            model_id=new_tmid,
                            human_validation=bool(new_hitl),
                            tool_profile=new_prof or "",
                        )
                        tasks_by_id[int(task_rec["id"])] = fresh_t
                        graph_mutated = True

                elif ntype == NODE_TYPE_LOOP:
                    lp1, lp2, lp3 = st.columns([1.2, 0.8, 1.5])
                    with lp1:
                        st.caption(f"Body nodes: `{', '.join(node.get('body_node_ids') or []) or 'none'}`")
                    with lp2:
                        cur_max = max(1, int(node.get("max_iterations") or 3))
                        new_max = int(
                            st.number_input(
                                "Max Iterations",
                                min_value=1,
                                max_value=50,
                                value=cur_max,
                                key=f"wf_blk_lpmax_{nid}_{rev}",
                            )
                        )
                        if new_max != cur_max:
                            node["max_iterations"] = new_max
                            graph_mutated = True
                    with lp3:
                        om = ((node.get("exit_condition") or {}).get("output_match") or {})
                        st.caption(
                            f"Exit: HITL={'Yes' if node.get('exit_on_hitl') else 'No'}"
                            + (f" | match `{om.get('pattern')}` on `{om.get('source_node')}`" if om.get("pattern") else "")
                        )
                    if new_title.strip() != str(node.get("name") or "Loop Frame").strip():
                        node["name"] = new_title.strip()
                        graph_mutated = True

                elif ntype == NODE_TYPE_HITL:
                    cur_msg = str(node.get("message") or "")
                    new_msg = st.text_input(
                        "HITL Approval Message",
                        value=cur_msg,
                        key=f"wf_blk_hitlmsg_{nid}_{rev}",
                    )
                    if new_msg.strip() != cur_msg.strip() or new_title.strip() != str(node.get("name") or "HITL Gate").strip():
                        node["message"] = new_msg.strip() or "Please review and approve before continuing."
                        node["name"] = new_title.strip() or "HITL Gate"
                        graph_mutated = True

                elif ntype in (NODE_TYPE_ROUTER, NODE_TYPE_TOOL, NODE_TYPE_LLM, NODE_TYPE_BATCH, NODE_TYPE_INPUT, NODE_TYPE_EXPORT):
                    if ntype == NODE_TYPE_TOOL:
                        st.caption(f"Deterministic tool `{node.get('tool_name')}` · args `{json.dumps(node.get('tool_args') or {})}`")
                    elif ntype == NODE_TYPE_LLM:
                        preview = str(node.get("prompt") or "")[:140]
                        st.caption(f"Prompt: {preview}{'...' if len(str(node.get('prompt') or '')) > 140 else ''}")
                    elif ntype == NODE_TYPE_ROUTER:
                        st.caption(f"Multi-route ({node.get('router_mode', 'auto')}): {', '.join(node.get('routes') or [])}")
                    elif ntype == NODE_TYPE_BATCH:
                        st.caption(f"Batch tasks: {node.get('task_ids') or []} · source `{node.get('source_variable') or ''}`")
                    elif ntype == NODE_TYPE_INPUT:
                        st.caption(f"Declared keys: {', '.join(node.get('keys') or [])}")
                    elif ntype == NODE_TYPE_EXPORT:
                        st.caption(f"Exports: {', '.join(node.get('exports') or [])}")
                    if new_title.strip() != str(node.get("name") or "").strip():
                        node["name"] = new_title.strip()
                        graph_mutated = True

                # Conditional Yes/No Branching selector (when_route)
                other_decisions = [d for d in decision_nodes if d.get("id") != nid]
                if other_decisions and ntype != NODE_TYPE_INPUT:
                    cur_gate = node.get("when_route") if isinstance(node.get("when_route"), dict) else {}
                    gate_opts: Dict[str, Optional[Tuple[str, str]]] = {"— Always run —": None}
                    for dn in other_decisions:
                        dn_id = str(dn.get("id"))
                        gate_opts[f"If [{dn.get('name') or dn_id}] == YES"] = (dn_id, "yes")
                        gate_opts[f"If [{dn.get('name') or dn_id}] == NO"] = (dn_id, "no")
                    cur_pair = (str(cur_gate.get("from") or ""), str(cur_gate.get("equals") or ""))
                    g_labels = list(gate_opts.keys())
                    g_idx = 0
                    for ig, vg in enumerate(gate_opts.values()):
                        if vg is not None and vg == cur_pair:
                            g_idx = ig
                            break
                    picked_g = st.selectbox(
                        "Execution Condition (Yes/No Branching)",
                        g_labels,
                        index=g_idx,
                        key=f"wf_blk_gate_{nid}_{rev}",
                    )
                    sel_pair = gate_opts[picked_g]
                    if sel_pair is None and cur_gate:
                        node.pop("when_route", None)
                        graph_mutated = True
                    elif sel_pair is not None and sel_pair != cur_pair:
                        node["when_route"] = {"from": sel_pair[0], "equals": sel_pair[1]}
                        deps = list(node.get("depends_on") or [])
                        if sel_pair[0] not in deps:
                            deps.append(sel_pair[0])
                        node["depends_on"] = deps
                        graph_mutated = True

        if graph_mutated:
            st.session_state.wf_selected_task_ids = _restack_graph(graph, db)
            st.session_state.pop("wf_flow_state", None)

        # --- Part C: Assisted Block Creation Bar inside Create a New Workflow ---
        with st.container(border=True):
            st.markdown("##### ➕ Add Assisted Block to Workflow")
            ab1, ab2, ab3 = st.columns([1.1, 1.8, 1.0], vertical_alignment="bottom")
            task_tpl_keys = ["ai_custom"] + list(TASK_ARCHETYPES.keys())
            with ab1:
                picked_task_tpl = st.selectbox(
                    "Task Template / Mode",
                    task_tpl_keys,
                    format_func=lambda k: "🤖 AI Custom from Prompt" if k == "ai_custom" else TASK_ARCHETYPES[k]["label"],
                    key="wf_ed_add_tpl",
                )
            with ab2:
                add_goal = st.text_input(
                    "Describe Task to Generate & Add",
                    placeholder="e.g., Extract competitor pricing into an Excel sheet for {product}",
                    key="wf_ed_add_goal",
                )
            with ab3:
                run_add_task = st.button("➕ Generate & Add Task", use_container_width=True, key="wf_ed_add_task_btn")

            # Full-width spinner below bar (outside columns)
            add_status_slot = st.container()
            if run_add_task:
                if picked_task_tpl == "ai_custom" and not (add_goal or "").strip():
                    add_status_slot.warning("Enter a task description or select a template.")
                else:
                    with add_status_slot:
                        with st.spinner("AI Co-Pilot is designing the task, tools, and required inputs..."):
                            if picked_task_tpl == "ai_custom" or (add_goal or "").strip():
                                seed_goal = (
                                    f"{add_goal.strip()} (Template hint: {TASK_ARCHETYPES[picked_task_tpl]['label']})"
                                    if (picked_task_tpl != "ai_custom" and (add_goal or "").strip())
                                    else add_goal.strip()
                                )
                                spec = generate_assisted_task_spec(
                                    seed_goal,
                                    db,
                                    available_tools=available_tools,
                                    orchestrator_model_id=st.session_state.get("wf_arch_selected_orch_model_id"),
                                )
                            else:
                                spec = build_task_spec_from_archetype(
                                    picked_task_tpl,
                                    db,
                                    available_tools=available_tools,
                                )

                    max_lvl = max((int(n.get("execution_level") or 1) for n in active_nodes), default=0)
                    new_lvl = max(1, max_lvl + 1 if active_nodes else 1)
                    last_id = active_nodes[-1].get("id") if active_nodes else None
                    tid = int(
                        db.create_task(
                            description=sanitize_input(spec["description"]) if sanitize_input else spec["description"],
                            expected_output=sanitize_input(spec["expected_output"]) if sanitize_input else spec["expected_output"],
                            agent_id=spec.get("agent_id"),
                            tools=spec.get("tools") or [],
                            required_inputs=spec.get("required_inputs") or [],
                            vector_dbs=[],
                            agent_specialization=spec.get("agent_specialization") or None,
                            name=spec.get("name") or None,
                            model_id=spec.get("model_id"),
                            human_validation=bool(spec.get("human_validation", False)),
                            tool_profile=spec.get("tool_profile") or "",
                        )
                    )
                    new_trec = db.read_task(tid) or {"id": tid, **spec}
                    t_node: Dict[str, Any] = {
                        "id": new_node_id("step"),
                        "type": NODE_TYPE_TASK,
                        "task_id": tid,
                        "execution_level": new_lvl,
                        "depends_on": [last_id] if last_id else [],
                        "inputs_map": {"upstream": {"from": last_id, "key": "summary"}} if last_id else {},
                    }
                    t_node = attach_snapshot(t_node, new_trec, task_id=tid)
                    g2 = _ensure_level_node(graph, new_lvl)
                    g2.append(t_node)
                    st.session_state.wf_selected_task_ids = _restack_graph(g2, db)
                    st.session_state.pop("wf_flow_state", None)
                    st.session_state.wf_editor_last_msg = f"✅ Created and added task **{new_trec.get('name') or tid}** to Level {new_lvl} & Canvas!"
                    st.rerun()

            if st.button("➕ Add Decision Gate (Yes/No)", use_container_width=True, key="wf_ed_add_dec_btn"):
                max_lvl = max((int(n.get("execution_level") or 1) for n in active_nodes), default=0)
                new_lvl = max(1, max_lvl + 1 if active_nodes else 1)
                last_id = active_nodes[-1].get("id") if active_nodes else None
                rec = recommend_models_for_tiers(db, prefer_local=True)
                tpl = copy.deepcopy(DECISION_GATE_TEMPLATES["quality_check"])
                d_node: Dict[str, Any] = {
                    "id": new_node_id("dec"),
                    "type": NODE_TYPE_ROUTER,
                    "name": tpl["name"],
                    "execution_level": new_lvl,
                    "depends_on": [last_id] if last_id else [],
                    "question": tpl["question"],
                    "prompt": tpl["question"],
                    "routes": ["yes", "no"],
                    "default_route": "no",
                    "router_mode": "auto",
                    "decision_mode": True,
                    "decision_engine": "jev",
                    "decision_primitive": "noul",
                    "threshold": float(tpl.get("threshold", 0.5)),
                    "jev_model": str(rec.get("decision_model_name") or "tev1"),
                    "model_tier": "basic",
                    "inputs_map": {},
                }
                if rec.get("decision") is not None:
                    d_node["model_id"] = int(rec["decision"])
                g2 = _ensure_level_node(graph, new_lvl)
                g2.append(d_node)
                st.session_state.wf_selected_task_ids = _restack_graph(g2, db)
                st.session_state.pop("wf_flow_state", None)
                st.session_state.wf_editor_last_msg = f"✅ Added Yes/No Decision Gate **{tpl['name']}** at Level {new_lvl} & Canvas!"
                st.rerun()
