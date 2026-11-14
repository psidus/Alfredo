"""
Workflow Assembler — Canvas mode (drag/drop via streamlit-flow) + task write-through editor.

UX:
- Level: droppable column; blocks dragged in belong to that level and auto-stack.
- Resize blocks from bottom-right corner grip (CSS resize; persisted in canvas_ui).
- Loop select: marquee board (L→R full / R→L partial), then create frame with while/for.
"""

from __future__ import annotations

from typing import Any, Dict, List

import streamlit as st

from core.workflow_canvas import (
    assisted_chain_edges,
    auto_stack_all_levels,
    canvas_geometry_fingerprint,
    flow_state_to_graph_updates,
    get_canvas_ui,
    graph_to_flow_state,
    hydrate_graph_for_canvas,
    is_canvas_decorator_id,
    run_streamlit_flow_preserving_size,
    set_canvas_ui,
    snap_assisted_positions,
    strip_decorator_nodes_from_state,
)
from core.workflow_graph import (
    CONTENT_TYPES,
    LOOP_MODE_FOR_EACH,
    LOOP_MODE_WHILE,
    LOOP_SCOPE_NODES,
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
    compute_loop_frame_ui,
    make_level_node,
    new_node_id,
    next_level_number,
    normalize_graph,
    remove_node,
)
from ui.marquee_select import render_marquee_select


def _check_streamlit_flow() -> bool:
    try:
        from streamlit_flow.elements import StreamlitFlowNode  # noqa: F401
        return True
    except ImportError:
        return False


def _get_agent_avatar_url(agent: dict) -> str:
    name = (agent or {}).get("name") or "agent"
    return f"https://api.dicebear.com/7.x/bottts/svg?seed={name}"


def _default_level_for_new(graph: List[dict]) -> int:
    levels = [int(n.get("level") or 1) for n in graph if n.get("type") == NODE_TYPE_LEVEL]
    return min(levels) if levels else 1


def _ensure_level_exists(graph: List[dict], level: int) -> List[dict]:
    if any(n.get("type") == NODE_TYPE_LEVEL and int(n.get("level") or 0) == level for n in graph):
        return graph
    graph = list(graph)
    graph.append(make_level_node(level))
    return graph


def render_canvas_palette_and_board(
    *,
    db,
    tasks: List[dict],
    agents: List[dict],
    available_exports: List[str],
    sanitize_input,
) -> None:
    """Render palette + streamlit-flow board + selection inspector into session draft."""
    task_id_map = {t["id"]: t for t in tasks}
    agent_id_map = {a["id"]: a for a in agents}
    tasks_by_agent = {a["id"]: [] for a in agents}
    for t in tasks:
        if t.get("agent_id") in tasks_by_agent:
            tasks_by_agent[t["agent_id"]].append(t)

    if "wf_selected_task_ids" not in st.session_state:
        st.session_state.wf_selected_task_ids = []
    st.session_state.wf_selected_task_ids = normalize_graph(
        st.session_state.wf_selected_task_ids, materialize_export=False
    )
    # Lanes drafts / legacy JSON may lack Level columns — materialize for canvas
    _g = st.session_state.wf_selected_task_ids
    _has_content = any(
        n.get("type") in CONTENT_TYPES or n.get("type") == NODE_TYPE_LOOP for n in _g
    )
    _has_levels = any(n.get("type") == NODE_TYPE_LEVEL for n in _g)
    if _has_content and not _has_levels:
        st.session_state.wf_selected_task_ids = hydrate_graph_for_canvas(
            _g, task_id_map=task_id_map, force_layout=True
        )
        st.session_state.pop("wf_flow_state", None)
    if "wf_loop_select_mode" not in st.session_state:
        st.session_state.wf_loop_select_mode = False
    if "wf_loop_select_ids" not in st.session_state:
        st.session_state.wf_loop_select_ids = []

    # Determine active block selection
    selected = getattr(st.session_state.get("wf_flow_state"), "selected_id", None)
    if not selected:
        selected = st.session_state.get("canvas_selected_node")
    user_picked = st.session_state.get("canvas_inspect_pick")
    if user_picked and user_picked != "—":
        selected = user_picked

    left, right = st.columns([1.15, 2.35], gap="medium")

    with left:
        insp_badge = f"🔍 Inspector ({selected})" if selected else "🔍 Inspector"
        tab_palette, tab_inspector, tab_loop, tab_actions = st.tabs(
            ["🧱 Palette", insp_badge, "🔁 Loop Frame", "⚙️ Actions"]
        )

        with tab_palette:
            st.markdown("#### Add Block")
            st.caption(
                "1) Add **Level** columns · 2) Drop blocks inside · "
                "3) Resize borders · 4) Wire data between dots"
            )
            add_kind = st.selectbox(
                "Add block",
                ["Level", "Task", "Decision (Sì/No)", "Batch", "HITL", "Export", "Input", "LLM", "Tool", "Router"],
                key="canvas_add_kind",
            )

            if add_kind == "Level":
                nxt = next_level_number(st.session_state.wf_selected_task_ids)
                st.write(f"Next level number: **{nxt}**")
                if st.button("Add Level column", key="canvas_btn_level"):
                    st.session_state.wf_selected_task_ids.append(make_level_node(nxt))
                    st.session_state.pop("wf_flow_state", None)
                    st.rerun()

            elif add_kind == "Decision (Sì/No)":
                from core.team_architect import DECISION_GATE_TEMPLATES, recommend_models_for_tiers

                tpl_keys = ["custom"] + list(DECISION_GATE_TEMPLATES.keys())
                tpl_labels = {"custom": "✏️ Domanda personalizzata (Sì/No)"}
                tpl_labels.update({k: v["label"] for k, v in DECISION_GATE_TEMPLATES.items()})
                chosen_tpl = st.selectbox(
                    "Template Decisione",
                    tpl_keys,
                    format_func=lambda k: tpl_labels.get(k, k),
                    key="canvas_dec_tpl",
                )
                default_q = (
                    DECISION_GATE_TEMPLATES[chosen_tpl]["question"]
                    if chosen_tpl != "custom"
                    else "L'output precedente soddisfa tutti i criteri richiesti?"
                )
                default_name = (
                    DECISION_GATE_TEMPLATES[chosen_tpl]["name"]
                    if chosen_tpl != "custom"
                    else "Decision Gate (Sì/No)"
                )
                dec_name = st.text_input("Nome nodo", value=default_name, key="canvas_dec_name")
                dec_q = st.text_area("Criterio / Domanda binaria (Sì/No)", value=default_q, key="canvas_dec_q", height=90)

                from core.team_architect import JEV_DECISION_MODELS

                c_eng, c_prim, c_thr = st.columns([1.0, 1.0, 1.1])
                with c_eng:
                    dec_engine = st.selectbox(
                        "Motore Decisionale",
                        ["jev", "auto", "llm"],
                        index=0,
                        format_func=lambda x: {
                            "jev": "⚡ Jev / System One (/v1/systemone)",
                            "auto": "🤖 Auto (Jev se disponibile, altrimenti LLM)",
                            "llm": "💬 LLM Generativo",
                        }.get(x, x),
                        key="canvas_dec_engine",
                    )
                with c_prim:
                    dec_primitive = st.selectbox(
                        "Primitiva Jev",
                        ["noul", "choice", "score"],
                        index=0,
                        key="canvas_dec_primitive",
                        help="noul = probabilità Sì/No (0..1); choice = selezione classe; score = rubrica.",
                    )
                with c_thr:
                    dec_threshold = st.slider(
                        "Soglia Probabilità (threshold)",
                        min_value=0.05,
                        max_value=0.95,
                        value=float(
                            DECISION_GATE_TEMPLATES.get(chosen_tpl, {}).get("threshold", 0.5)
                            if chosen_tpl != "custom"
                            else 0.5
                        ),
                        step=0.05,
                        key="canvas_dec_thr",
                        help="Se p(noul='yes') >= threshold segue il ramo Sì ('yes'), altrimenti No ('no').",
                    )

                rec_tiers = recommend_models_for_tiers(db)
                default_jev = str(rec_tiers.get("decision_model_name") or "tev1")
                jev_keys = list(JEV_DECISION_MODELS.keys())
                jev_idx = jev_keys.index(default_jev) if default_jev in jev_keys else 0
                jev_model = st.selectbox(
                    "Modello Jev / System One",
                    jev_keys,
                    index=jev_idx,
                    format_func=lambda k: JEV_DECISION_MODELS[k]["label"],
                    key="canvas_dec_jev_model",
                )

                dec_def = st.selectbox("Rotta di default (fallback)", ["no", "yes"], key="canvas_dec_def")

                models_list = db.read_all_models() or []
                rec_mid = rec_tiers.get("decision")
                m_opts: Dict[str, Any] = {"🤖 Automatico (modello System One / fallback DB)": None}
                def_m_idx = 0
                for idx_m, m in enumerate(models_list, start=1):
                    lbl = f"{m.get('provider')}/{m.get('model_name')} ({m.get('capability_tier', 'standard')})"
                    m_opts[lbl] = m["id"]
                    if rec_mid is not None and int(m["id"]) == int(rec_mid):
                        def_m_idx = idx_m
                picked_m_lbl = st.selectbox(
                    "Record Modello DB (per credenziali/endpoint o fallback LLM)",
                    list(m_opts.keys()),
                    index=def_m_idx,
                    key="canvas_dec_model",
                )
                dec_mid = m_opts[picked_m_lbl]
                lvl = _default_level_for_new(st.session_state.wf_selected_task_ids)
                if st.button("⚖️ Add Decision (Sì/No)", key="canvas_btn_dec"):
                    g = _ensure_level_exists(st.session_state.wf_selected_task_ids, lvl)
                    node_payload: Dict[str, Any] = {
                        "id": new_node_id("dec"),
                        "type": NODE_TYPE_ROUTER,
                        "name": (dec_name or "Decisione Sì/No").strip(),
                        "decision_mode": True,
                        "decision_engine": dec_engine,
                        "decision_primitive": dec_primitive,
                        "threshold": float(dec_threshold),
                        "jev_model": jev_model,
                        "routes": ["yes", "no"],
                        "rules": [],
                        "router_mode": "auto",
                        "default_route": dec_def,
                        "question": (dec_q or "").strip() or "Procedere?",
                        "prompt": (dec_q or "").strip() or "Procedere?",
                        "depends_on": [],
                        "execution_level": lvl,
                        "model_tier": "basic",
                        "inputs_map": {},
                    }
                    if dec_mid is not None:
                        node_payload["model_id"] = int(dec_mid)
                    g.append(node_payload)
                    st.session_state.wf_selected_task_ids = auto_stack_all_levels(
                        g, task_id_map=task_id_map
                    )
                    st.session_state.pop("wf_flow_state", None)
                    st.rerun()

            elif add_kind == "Task":
                if agents:
                    a_opts = {f"{a['name']} — {a['role']}": a["id"] for a in agents}
                    a_lab = st.selectbox("Agent", list(a_opts.keys()), key="canvas_add_agent")
                    atasks = tasks_by_agent.get(a_opts[a_lab], [])
                    if atasks:
                        t_opts = {
                            f"{t.get('name') or t['id']}: {(t.get('description') or '')[:40]}": t["id"]
                            for t in atasks
                        }
                        t_lab = st.selectbox("Task", list(t_opts.keys()), key="canvas_add_task")
                        lvl = _default_level_for_new(st.session_state.wf_selected_task_ids)
                        if st.button("Add Task to canvas", key="canvas_btn_task"):
                            g = _ensure_level_exists(st.session_state.wf_selected_task_ids, lvl)
                            g.append({
                                "id": new_node_id(),
                                "type": NODE_TYPE_TASK,
                                "task_id": t_opts[t_lab],
                                "depends_on": [],
                                "execution_level": lvl,
                                "model_tier": "default",
                                "inputs_map": {},
                            })
                            st.session_state.wf_selected_task_ids = auto_stack_all_levels(
                                g, task_id_map=task_id_map
                            )
                            st.session_state.pop("wf_flow_state", None)
                            st.rerun()
                    else:
                        st.info("No tasks for this agent.")

                    with st.expander("✨ Crea Nuovo Task al volo", expanded=not bool(atasks)):
                        qt_name = st.text_input("Nome nuovo task", key="canvas_quick_t_name")
                        qt_desc = st.text_area("Istruzioni / Prompt", key="canvas_quick_t_desc", height=80)
                        qt_exp = st.text_input("Output atteso", value="Riepilogo strutturato con punti chiave.", key="canvas_quick_t_exp")
                        if st.button("➕ Crea e Aggiungi al Canvas", key="canvas_btn_quick_task"):
                            if not (qt_name.strip() and qt_desc.strip()):
                                st.error("Inserisci nome e istruzioni del task.")
                            else:
                                sel_aid = a_opts[a_lab]
                                new_tid = db.create_task(
                                    description=sanitize_input(qt_desc) if sanitize_input else qt_desc.strip(),
                                    expected_output=sanitize_input(qt_exp) if sanitize_input else qt_exp.strip(),
                                    agent_id=sel_aid,
                                    tools=[],
                                    required_inputs=[],
                                    name=qt_name.strip(),
                                )
                                new_trec = db.read_task(new_tid)
                                local_map = dict(task_id_map)
                                if new_trec:
                                    local_map[new_tid] = new_trec
                                lvl = _default_level_for_new(st.session_state.wf_selected_task_ids)
                                g = _ensure_level_exists(st.session_state.wf_selected_task_ids, lvl)
                                g.append({
                                    "id": new_node_id(),
                                    "type": NODE_TYPE_TASK,
                                    "task_id": new_tid,
                                    "depends_on": [],
                                    "execution_level": lvl,
                                    "model_tier": "default",
                                    "inputs_map": {},
                                })
                                st.session_state.wf_selected_task_ids = auto_stack_all_levels(
                                    g, task_id_map=local_map
                                )
                                st.session_state.pop("wf_flow_state", None)
                                st.rerun()
                else:
                    st.info("Create agents first.")

            elif add_kind == "Batch":
                b_size = st.number_input("Batch size", 1, 100, 5, key="canvas_b_size")
                b_src = st.text_input("Source", "{previous_result}", key="canvas_b_src")
                all_t = {f"{t.get('name') or t['id']}": t["id"] for t in tasks}
                inner = st.multiselect("Inner tasks", list(all_t.keys()), key="canvas_b_inner")
                lvl = _default_level_for_new(st.session_state.wf_selected_task_ids)
                if st.button("Add Batch", key="canvas_btn_batch") and inner:
                    g = _ensure_level_exists(st.session_state.wf_selected_task_ids, lvl)
                    g.append({
                        "id": new_node_id("batch"),
                        "type": NODE_TYPE_BATCH,
                        "task_ids": [all_t[x] for x in inner],
                        "batch_size": int(b_size),
                        "source_variable": b_src,
                        "depends_on": [],
                        "execution_level": lvl,
                        "inputs_map": {},
                    })
                    st.session_state.wf_selected_task_ids = auto_stack_all_levels(
                        g, task_id_map=task_id_map
                    )
                    st.session_state.pop("wf_flow_state", None)
                    st.rerun()

            elif add_kind == "HITL":
                msg = st.text_input("Message", "Please review before continuing.", key="canvas_hitl_msg")
                lvl = _default_level_for_new(st.session_state.wf_selected_task_ids)
                if st.button("Add HITL", key="canvas_btn_hitl"):
                    g = _ensure_level_exists(st.session_state.wf_selected_task_ids, lvl)
                    g.append({
                        "id": new_node_id("hitl"),
                        "type": NODE_TYPE_HITL,
                        "depends_on": [],
                        "execution_level": lvl,
                        "inputs_map": {},
                        "message": msg,
                        "gate_mode": "after_parents",
                    })
                    st.session_state.wf_selected_task_ids = auto_stack_all_levels(
                        g, task_id_map=task_id_map
                    )
                    st.session_state.pop("wf_flow_state", None)
                    st.rerun()

            elif add_kind == "Export":
                ex = st.multiselect("Formats", available_exports, key="canvas_ex")
                instr = st.text_area("Instructions", key="canvas_ex_instr", height=60)
                lvl = _default_level_for_new(st.session_state.wf_selected_task_ids)
                if st.button("Add Export", key="canvas_btn_ex"):
                    g = _ensure_level_exists(st.session_state.wf_selected_task_ids, lvl)
                    g.append({
                        "id": new_node_id("export"),
                        "type": NODE_TYPE_EXPORT,
                        "depends_on": [],
                        "execution_level": lvl,
                        "inputs_map": {},
                        "exports": ex,
                        "export_instructions": instr or "",
                    })
                    st.session_state.wf_selected_task_ids = auto_stack_all_levels(
                        g, task_id_map=task_id_map
                    )
                    st.session_state.pop("wf_flow_state", None)
                    st.rerun()

            elif add_kind == "Input":
                keys = st.text_input("Keys", "user_input", key="canvas_in_keys")
                lvl = _default_level_for_new(st.session_state.wf_selected_task_ids)
                if st.button("Add Input", key="canvas_btn_in"):
                    g = _ensure_level_exists(st.session_state.wf_selected_task_ids, lvl)
                    g.append({
                        "id": new_node_id("input"),
                        "type": NODE_TYPE_INPUT,
                        "depends_on": [],
                        "execution_level": lvl,
                        "inputs_map": {},
                        "keys": [k.strip() for k in keys.split(",") if k.strip()] or ["user_input"],
                    })
                    st.session_state.wf_selected_task_ids = auto_stack_all_levels(
                        g, task_id_map=task_id_map
                    )
                    st.session_state.pop("wf_flow_state", None)
                    st.rerun()

            elif add_kind == "LLM":
                prompt = st.text_area("Prompt", "{previous_result}", key="canvas_llm_prompt", height=90)
                fmt = st.selectbox("Output format", ["text", "json"], key="canvas_llm_fmt")
                lvl = _default_level_for_new(st.session_state.wf_selected_task_ids)
                if st.button("Add LLM", key="canvas_btn_llm"):
                    g = _ensure_level_exists(st.session_state.wf_selected_task_ids, lvl)
                    g.append({
                        "id": new_node_id("llm"),
                        "type": NODE_TYPE_LLM,
                        "prompt": (prompt or "").strip() or "{previous_result}",
                        "output_format": fmt,
                        "temperature": 0.2,
                        "depends_on": [],
                        "execution_level": lvl,
                        "model_tier": "default",
                        "inputs_map": {},
                    })
                    st.session_state.wf_selected_task_ids = auto_stack_all_levels(
                        g, task_id_map=task_id_map
                    )
                    st.session_state.pop("wf_flow_state", None)
                    st.rerun()

            elif add_kind == "Tool":
                try:
                    from core.crew_builder import ALLOWED_TOOLS
                    tool_names = sorted(ALLOWED_TOOLS.keys())
                except Exception:
                    tool_names = []
                if tool_names:
                    tool_name = st.selectbox("Tool", tool_names, key="canvas_tool_name")
                else:
                    tool_name = st.text_input("Tool name", key="canvas_tool_name_txt")
                lvl = _default_level_for_new(st.session_state.wf_selected_task_ids)
                if st.button("Add Tool", key="canvas_btn_tool") and (tool_name or "").strip():
                    g = _ensure_level_exists(st.session_state.wf_selected_task_ids, lvl)
                    g.append({
                        "id": new_node_id("tool"),
                        "type": NODE_TYPE_TOOL,
                        "tool_name": str(tool_name).strip(),
                        "tool_args": {},
                        "depends_on": [],
                        "execution_level": lvl,
                        "model_tier": "default",
                        "inputs_map": {},
                    })
                    st.session_state.wf_selected_task_ids = auto_stack_all_levels(
                        g, task_id_map=task_id_map
                    )
                    st.session_state.pop("wf_flow_state", None)
                    st.rerun()

            elif add_kind == "Router":
                routes_str = st.text_input("Routes", "yes, no", key="canvas_router_routes")
                routes = [r.strip() for r in (routes_str or "").split(",") if r.strip()]
                mode = st.selectbox("Mode", ["auto", "rules", "llm"], key="canvas_router_mode")
                lvl = _default_level_for_new(st.session_state.wf_selected_task_ids)
                if st.button("Add Router", key="canvas_btn_router"):
                    if len(set(routes)) < 2:
                        st.error("A router needs at least two distinct routes.")
                    else:
                        g = _ensure_level_exists(st.session_state.wf_selected_task_ids, lvl)
                        g.append({
                            "id": new_node_id("router"),
                            "type": NODE_TYPE_ROUTER,
                            "routes": routes,
                            "rules": [],
                            "router_mode": mode,
                            "default_route": routes[0],
                            "depends_on": [],
                            "execution_level": lvl,
                            "model_tier": "default",
                            "inputs_map": {},
                        })
                        st.session_state.wf_selected_task_ids = auto_stack_all_levels(
                            g, task_id_map=task_id_map
                        )
                        st.session_state.pop("wf_flow_state", None)
                        st.rerun()

        with tab_inspector:
            st.markdown("#### Block Inspector")
            node_ids = [n["id"] for n in st.session_state.wf_selected_task_ids]
            pick_opts = ["—"] + node_ids
            idx = 0
            if selected in node_ids:
                idx = node_ids.index(selected) + 1
            pick_row, trash_row = st.columns([4, 1])
            with pick_row:
                pick = st.selectbox("Selected block", options=pick_opts, index=idx, key="canvas_inspect_pick")
            with trash_row:
                st.write("")  # align with selectbox
                if st.button("🗑", key="canvas_trash_selected", help="Delete selected block (Delete/Backspace on canvas)"):
                    target = pick if pick and pick != "—" else selected
                    if target and target in node_ids:
                        st.session_state.wf_selected_task_ids = remove_node(
                            st.session_state.wf_selected_task_ids, target
                        )
                        st.session_state.pop("wf_flow_state", None)
                        st.rerun()
                    else:
                        st.toast("Select a block to delete")

            active_id = pick if (pick and pick != "—") else selected
            if active_id and active_id in node_ids:
                _render_node_inspector(
                    db=db,
                    node_id=active_id,
                    task_id_map=task_id_map,
                    agent_id_map=agent_id_map,
                    agents=agents,
                    available_exports=available_exports,
                    sanitize_input=sanitize_input,
                )
            else:
                st.info("👆 Click any block on the canvas or pick one from the dropdown above to inspect and edit its properties.")

        with tab_loop:
            st.markdown("#### Loop Select & Frame")
            mode_on = st.toggle(
                "Loop select mode",
                value=bool(st.session_state.wf_loop_select_mode),
                key="canvas_loop_toggle",
                help="Draw a rectangle on the marquee board: L→R full, R→L partial.",
            )
            st.session_state.wf_loop_select_mode = mode_on
            selected_ids = list(st.session_state.wf_loop_select_ids or [])
            if selected_ids:
                st.caption(f"Selected: `{', '.join(selected_ids)}`")
            else:
                st.caption("No blocks selected yet — draw on the marquee board (right).")
            if st.button("Clear loop selection", key="canvas_loop_clear"):
                st.session_state.wf_loop_select_ids = []
                st.session_state.pop("wf_flow_state", None)
                st.rerun()

            loop_mode = st.selectbox(
                "Loop mode (on frame)",
                [LOOP_MODE_WHILE, LOOP_MODE_FOR_EACH],
                key="canvas_loop_mode",
            )
            max_it = st.number_input("max_iterations", 1, 100, 5, key="canvas_loop_max")
            exit_hitl = st.checkbox("Exit on HITL approve", value=True, key="canvas_loop_hitl")
            cond_pat = st.text_input("Exit output regex (optional)", "", key="canvas_loop_cond_pat")
            if st.button("Create loop frame from selection", key="canvas_btn_loop_frame"):
                body = [
                    n for n in st.session_state.wf_selected_task_ids
                    if n.get("id") in selected_ids and n.get("type") in (NODE_TYPE_TASK, NODE_TYPE_BATCH, NODE_TYPE_HITL)
                ]
                if len(body) < 1:
                    st.error("Select at least one Task/Batch/HITL with the marquee (L→R or R→L).")
                else:
                    frame_ui = compute_loop_frame_ui(body)
                    levels = {int(n.get("execution_level") or 1) for n in body}
                    loop_lvl = min(levels)
                    cond_src = body[-1]["id"]
                    st.session_state.wf_selected_task_ids.append({
                        "id": new_node_id("loop"),
                        "type": NODE_TYPE_LOOP,
                        "loop_mode": loop_mode,
                        "scope": LOOP_SCOPE_NODES,
                        "body_node_ids": [n["id"] for n in body],
                        "body_levels": [],
                        "source_variable": "{previous_result}",
                        "max_iterations": int(max_it),
                        "exit_on_hitl": bool(exit_hitl),
                        "exit_condition": {
                            "output_match": {
                                "source_node": cond_src if cond_pat.strip() else "",
                                "pattern": cond_pat.strip(),
                            }
                        },
                        "depends_on": [],
                        "execution_level": loop_lvl,
                        "inputs_map": {},
                        "canvas_ui": frame_ui,
                    })
                    st.session_state.wf_loop_select_ids = []
                    st.session_state.wf_loop_select_mode = False
                    st.session_state.pop("wf_flow_state", None)
                    st.rerun()

        with tab_actions:
            st.markdown("#### Canvas Tools & Layout")
            if st.button("Snap & restack in levels", key="canvas_restack", use_container_width=True):
                st.session_state.wf_selected_task_ids = snap_assisted_positions(
                    st.session_state.wf_selected_task_ids, task_id_map=task_id_map
                )
                st.session_state.pop("wf_flow_state", None)
                st.rerun()
            if st.button("Auto-link by levels", key="canvas_assist", use_container_width=True):
                st.session_state.wf_selected_task_ids = assisted_chain_edges(
                    st.session_state.wf_selected_task_ids
                )
                st.session_state.pop("wf_flow_state", None)
                st.rerun()
            if st.button("Rebuild canvas layout", key="canvas_rebuild", use_container_width=True):
                st.session_state.pop("wf_flow_state", None)
                st.rerun()

            st.markdown("---")
            st.markdown("##### 💡 Canvas Tips")
            st.markdown(
                "- **Columns**: Blocks snap inside Level columns (execution phases).\n"
                "- **Wiring**: Drag from a block's right dot to a target block's left dot.\n"
                "- **Delete**: Select a block and press **Backspace** / **Delete**, or click 🗑 in the Inspector.\n"
                "- **Inspector**: Click any block on the canvas to immediately inspect its properties on the left."
            )

    with right:
        if not _check_streamlit_flow():
            st.error(
                "Install `streamlit-flow-component` to use the drag-and-drop canvas "
                "(`pip install streamlit-flow-component`)."
            )
            return

        from streamlit_flow.layouts import ManualLayout

        graph = st.session_state.wf_selected_task_ids
        highlight = st.session_state.wf_loop_select_ids if st.session_state.wf_loop_select_mode else []

        if st.session_state.wf_loop_select_mode:
            st.info(
                "**Loop marquee** — drag on the board below. "
                "Left→right = only fully enclosed · Right→left = also partial hits."
            )
            marquee_nodes = []
            for n in graph:
                if n.get("type") not in (NODE_TYPE_TASK, NODE_TYPE_BATCH, NODE_TYPE_HITL):
                    continue
                ui = get_canvas_ui(n)
                label = n.get("type", "?")
                if n.get("type") == NODE_TYPE_TASK and n.get("task_id") is not None:
                    t = task_id_map.get(int(n["task_id"]))
                    label = (t or {}).get("name") or f"task {n['task_id']}"
                marquee_nodes.append({
                    "id": n["id"],
                    "label": str(label),
                    "x": float(ui.get("x") or 0),
                    "y": float(ui.get("y") or 0),
                    "w": float(ui.get("width") or 220),
                    "h": float(ui.get("height") or 120),
                    "color": {
                        NODE_TYPE_TASK: "#2196F3",
                        NODE_TYPE_BATCH: "#9C27B0",
                        NODE_TYPE_HITL: "#FF9800",
                    }.get(n.get("type"), "#607D8B"),
                })
            result = render_marquee_select(marquee_nodes, key="loop_marquee_board", height=380)
            if result and result.get("ids") is not None:
                new_ids = list(result.get("ids") or [])
                if new_ids != list(st.session_state.wf_loop_select_ids or []):
                    st.session_state.wf_loop_select_ids = new_ids
                    st.session_state.pop("wf_flow_state", None)
                    st.rerun()
            if st.session_state.wf_loop_select_ids:
                direction = (result or {}).get("direction") or "?"
                st.success(
                    f"Selection ({direction}): " + ", ".join(st.session_state.wf_loop_select_ids)
                )

        if "wf_flow_state" in st.session_state:
            st.session_state.wf_flow_state = strip_decorator_nodes_from_state(
                st.session_state.wf_flow_state
            )
            # Force rebuild if stale agent-label nodes somehow remain
            for sn in getattr(st.session_state.wf_flow_state, "nodes", []) or []:
                nid = getattr(sn, "id", None) or (sn.get("id") if isinstance(sn, dict) else None)
                if nid and is_canvas_decorator_id(str(nid)):
                    st.session_state.pop("wf_flow_state", None)
                    break

        if "wf_flow_state" not in st.session_state:
            st.session_state.wf_flow_state = graph_to_flow_state(
                graph,
                task_id_map=task_id_map,
                agent_id_map=agent_id_map,
                highlight_ids=highlight,
            )

        before_fp = canvas_geometry_fingerprint(st.session_state.wf_selected_task_ids)
        before_ids = {n["id"] for n in st.session_state.wf_selected_task_ids}

        st.session_state.wf_flow_state = run_streamlit_flow_preserving_size(
            "alfredo_wf_canvas",
            st.session_state.wf_flow_state,
            height=680,
            fit_view=True,
            show_controls=True,
            show_minimap=True,
            allow_new_edges=True,
            animate_new_edges=True,
            get_node_on_click=True,
            enable_node_menu=True,
            enable_edge_menu=True,
            enable_pane_menu=False,
            hide_watermark=True,
            pan_on_drag=not bool(st.session_state.wf_loop_select_mode),
            layout=ManualLayout(),
        )

        try:
            before_levels = {
                n["id"]: int(n.get("execution_level") or 1)
                for n in st.session_state.wf_selected_task_ids
                if n.get("type") in CONTENT_TYPES
            }
            updated = flow_state_to_graph_updates(
                st.session_state.wf_flow_state,
                st.session_state.wf_selected_task_ids,
                task_id_map=task_id_map,
                restack=False,
            )
            # Always snap content into level columns so cards stay aligned
            updated = snap_assisted_positions(updated, task_id_map=task_id_map)
            after_levels = {
                n["id"]: int(n.get("execution_level") or 1)
                for n in updated
                if n.get("type") in CONTENT_TYPES
            }
            after_fp = canvas_geometry_fingerprint(updated)
            after_ids = {n["id"] for n in updated}
            st.session_state.wf_selected_task_ids = updated
            if (
                before_levels != after_levels
                or before_fp != after_fp
                or before_ids != after_ids
            ):
                st.session_state.pop("wf_flow_state", None)
                st.rerun()
        except Exception as e:
            st.warning(f"Could not sync canvas: {e}")

        # Keyboard Delete/Canc when focus is outside the canvas iframe
        import streamlit.components.v1 as components

        components.html(
            """
            <script>
            (function () {
              const w = window.parent;
              if (!w || w.__alfredoDeleteBound) return;
              w.__alfredoDeleteBound = true;
              w.document.addEventListener('keydown', function (e) {
                if (e.key !== 'Delete' && e.key !== 'Backspace') return;
                const t = e.target;
                const tag = (t && t.tagName) ? t.tagName.toUpperCase() : '';
                if (tag === 'INPUT' || tag === 'TEXTAREA' || (t && t.isContentEditable)) return;
                const buttons = w.document.querySelectorAll('button');
                for (const b of buttons) {
                  if ((b.getAttribute('kind') || '') === 'secondary') { /* skip */ }
                  const title = (b.getAttribute('title') || '') + ' ' + (b.innerText || '');
                  if (title.indexOf('Delete selected') >= 0 || title.indexOf('Elimina selezionato') >= 0 || (b.innerText || '').trim() === '🗑') {
                    e.preventDefault();
                    b.click();
                    break;
                  }
                }
              }, true);
            })();
            </script>
            """,
            height=0,
        )

def _render_node_inspector(
    *,
    db,
    node_id: str,
    task_id_map: Dict[int, Any],
    agent_id_map: Dict[int, Any],
    agents: List[dict],
    available_exports: List[str],
    sanitize_input,
) -> None:
    graph = st.session_state.wf_selected_task_ids
    node = next((n for n in graph if n.get("id") == node_id), None)
    if not node:
        st.warning("Node not found in draft.")
        return

    ntype = node.get("type")
    st.write(f"**Type:** `{ntype}` · **id:** `{node_id}`")

    ui = get_canvas_ui(node)
    cw, ch = st.columns(2)
    with cw:
        new_w = st.number_input(
            "Block width (px)", 80, 900, int(ui.get("width") or 220), key=f"insp_w_{node_id}"
        )
    with ch:
        new_h = st.number_input(
            "Block height (px)", 60, 900, int(ui.get("height") or 120), key=f"insp_h_{node_id}"
        )
    set_canvas_ui(node, width=int(new_w), height=int(new_h))

    if ntype == NODE_TYPE_LEVEL:
        node["level"] = st.number_input(
            "Level number", min_value=1, value=int(node.get("level") or 1), key=f"insp_levelnum_{node_id}"
        )
        node["execution_level"] = int(node["level"])
        st.caption("Blocks dropped into this column get this execution level and auto-stack.")
        if st.button("Restack this column", key=f"insp_restack_{node_id}"):
            from core.workflow_canvas import auto_stack_level
            st.session_state.wf_selected_task_ids = auto_stack_level(
                list(graph), int(node["level"]), task_id_map=task_id_map
            )
            st.session_state.pop("wf_flow_state", None)
            st.rerun()

    elif ntype != NODE_TYPE_LOOP:
        node["execution_level"] = st.number_input(
            "Execution level",
            min_value=1,
            value=int(node.get("execution_level") or 1),
            key=f"insp_lvl_{node_id}",
        )

    if ntype == NODE_TYPE_TASK:
        tid = node.get("task_id")
        task = task_id_map.get(int(tid)) if tid is not None else None
        if not task:
            st.error(f"Task {tid} missing.")
            return
        agent = agent_id_map.get(task.get("agent_id"))
        if agent:
            st.image(_get_agent_avatar_url(agent), width=64)
        st.info(
            "Edits below write through to the **shared task** (Task Builder). "
            "Model and deletion are locked here."
        )
        name = st.text_input("Task name", value=task.get("name") or "", key=f"insp_name_{node_id}")
        desc = st.text_area(
            "Description / output prompt context",
            value=task.get("description") or "",
            key=f"insp_desc_{node_id}",
            height=100,
        )
        expected = st.text_area(
            "Expected output", value=task.get("expected_output") or "", key=f"insp_exp_{node_id}", height=80
        )
        spec = st.text_input(
            "Specialization", value=task.get("agent_specialization") or "", key=f"insp_spec_{node_id}"
        )

        a_opts = {f"{a['name']} — {a['role']}": a["id"] for a in agents}
        cur_a = task.get("agent_id")
        a_labels = list(a_opts.keys())
        try:
            a_idx = list(a_opts.values()).index(cur_a) if cur_a in a_opts.values() else 0
        except Exception:
            a_idx = 0
        a_lab = st.selectbox("Agent (global)", a_labels, index=a_idx, key=f"insp_agent_{node_id}")
        new_agent_id = a_opts[a_lab]

        try:
            from core.crew_builder import ALLOWED_TOOLS
            tool_names = sorted(ALLOWED_TOOLS.keys())
        except Exception:
            tool_names = list(task.get("tools") or [])
        cur_tools = list(task.get("tools") or [])
        tools = st.multiselect(
            "Tools (global)",
            tool_names,
            default=[t for t in cur_tools if t in tool_names],
            key=f"insp_tools_{node_id}",
        )

        req = task.get("required_inputs") or []
        req_text = "\n".join(
            f"{(r.get('key') if isinstance(r, dict) else r)}|{(r.get('prompt') if isinstance(r, dict) else '')}"
            for r in req
        )
        req_edit = st.text_area(
            "Required inputs (one per line: key|prompt)",
            value=req_text,
            key=f"insp_req_{node_id}",
            height=80,
        )
        hitl = st.checkbox(
            "Human validation (HITL) on this task",
            value=bool(task.get("human_validation")),
            key=f"insp_hitl_{node_id}",
        )
        st.caption(f"Model locked: `{task.get('model_id')}` — change it in Task Builder.")

        if st.button("Save task (global write-through)", key=f"insp_save_task_{node_id}"):
            input_rows = []
            for line in (req_edit or "").splitlines():
                line = line.strip()
                if not line:
                    continue
                if "|" in line:
                    k, p = line.split("|", 1)
                else:
                    k, p = line, ""
                k = sanitize_input(k.strip()) if sanitize_input else k.strip()
                if k:
                    input_rows.append({"key": k, "prompt": p.strip()})
            db.update_task(
                int(tid),
                sanitize_input(desc) if sanitize_input else desc,
                sanitize_input(expected) if sanitize_input else expected,
                new_agent_id,
                tools,
                input_rows,
                task.get("vector_dbs") or [],
                (spec or "").strip() or None,
                (name or "").strip() or None,
                task.get("model_id"),
                bool(hitl),
                int(task.get("max_input_context") or 0),
                int(task.get("max_output_tokens") or 0),
                task.get("output_pydantic"),
                task.get("tool_profile") or "",
            )
            st.success("Task updated globally.")
            st.session_state.pop("wf_flow_state", None)
            st.rerun()

        with st.expander("Node data wiring"):
            st.json(node.get("inputs_map") or {})

    elif ntype == NODE_TYPE_LOOP:
        st.info("While/for live **on this frame**. Connect downstream blocks to this loop's output handle.")
        node["loop_mode"] = st.selectbox(
            "Loop mode",
            [LOOP_MODE_WHILE, LOOP_MODE_FOR_EACH],
            index=0 if node.get("loop_mode") != LOOP_MODE_FOR_EACH else 1,
            key=f"insp_lmode_{node_id}",
        )
        opts = {
            f"{n.get('type')} · {n['id']}": n["id"]
            for n in graph
            if n["id"] != node_id and n.get("type") in (NODE_TYPE_TASK, NODE_TYPE_BATCH, NODE_TYPE_HITL)
        }
        cur = [k for k, v in opts.items() if v in (node.get("body_node_ids") or [])]
        picked = st.multiselect("Body nodes", list(opts.keys()), default=cur, key=f"insp_lbody_{node_id}")
        node["body_node_ids"] = [opts[p] for p in picked]
        node["scope"] = LOOP_SCOPE_NODES
        node["source_variable"] = st.text_input(
            "for_each source",
            value=node.get("source_variable") or "{previous_result}",
            key=f"insp_lsrc_{node_id}",
        )
        node["max_iterations"] = st.number_input(
            "max_iterations", 1, 100, int(node.get("max_iterations") or 5), key=f"insp_lmax_{node_id}"
        )
        node["exit_on_hitl"] = st.checkbox(
            "Exit when HITL in body approves",
            value=bool(node.get("exit_on_hitl", True)),
            key=f"insp_lhitl_{node_id}",
        )
        ec = node.get("exit_condition") if isinstance(node.get("exit_condition"), dict) else {}
        om = ec.get("output_match") if isinstance(ec.get("output_match"), dict) else {}
        body_opts = opts
        cur_src = om.get("source_node") or ""
        if body_opts:
            src_labels = list(body_opts.keys())
            src_idx = 0
            for i, (_, vid) in enumerate(body_opts.items()):
                if vid == cur_src:
                    src_idx = i
                    break
            src_pick = st.selectbox(
                "Exit when output matches (source node)",
                ["—"] + src_labels,
                index=(src_idx + 1) if cur_src in body_opts.values() else 0,
                key=f"insp_lcond_src_{node_id}",
            )
            src_node = body_opts[src_pick] if src_pick != "—" else ""
        else:
            src_node = ""
        pat = st.text_input(
            "Exit output regex",
            value=om.get("pattern") or "",
            key=f"insp_lcond_pat_{node_id}",
        )
        node["exit_condition"] = {"output_match": {"source_node": src_node, "pattern": pat.strip()}}
        if st.button("Fit frame to body", key=f"insp_lfit_{node_id}"):
            body = [n for n in graph if n.get("id") in set(node.get("body_node_ids") or [])]
            set_canvas_ui(node, **compute_loop_frame_ui(body))
            st.session_state.pop("wf_flow_state", None)
            st.rerun()

    elif ntype == NODE_TYPE_EXPORT:
        cur = [x for x in (node.get("exports") or []) if x in available_exports]
        node["exports"] = st.multiselect("Formats", available_exports, default=cur, key=f"insp_ex_{node_id}")
        node["export_instructions"] = st.text_area(
            "Instructions", value=node.get("export_instructions") or "", key=f"insp_exi_{node_id}", height=70
        )

    elif ntype == NODE_TYPE_HITL:
        node["message"] = st.text_input("Message", value=node.get("message") or "", key=f"insp_hmsg_{node_id}")
        st.caption("At runtime HITL desugars onto upstream task human_validation.")

    elif ntype == NODE_TYPE_INPUT:
        keys = ", ".join(node.get("keys") or [])
        edited = st.text_input("Keys", value=keys, key=f"insp_ik_{node_id}")
        node["keys"] = [k.strip() for k in edited.split(",") if k.strip()]

    elif ntype == NODE_TYPE_LLM:
        node["prompt"] = st.text_area(
            "Prompt",
            value=node.get("prompt") or "",
            key=f"insp_llm_prompt_{node_id}",
            height=140,
            help="{previous_result}, {input.key}, {node_id}, {node_id.section}.",
        )
        node["system_prompt"] = st.text_input(
            "System prompt (optional)",
            value=node.get("system_prompt") or "",
            key=f"insp_llm_sys_{node_id}",
        )
        fmt_opts = ["text", "json"]
        cur_fmt = node.get("output_format") if node.get("output_format") in fmt_opts else "text"
        node["output_format"] = st.selectbox(
            "Output format",
            fmt_opts,
            index=fmt_opts.index(cur_fmt),
            key=f"insp_llm_fmt_{node_id}",
        )
        try:
            cur_temp = float(node.get("temperature", 0.2))
        except (TypeError, ValueError):
            cur_temp = 0.2
        node["temperature"] = st.slider(
            "Temperature", 0.0, 1.0, max(0.0, min(1.0, cur_temp)), 0.05,
            key=f"insp_llm_temp_{node_id}",
        )
        with st.expander("Node data wiring"):
            st.json(node.get("inputs_map") or {})

    elif ntype == NODE_TYPE_TOOL:
        node["tool_name"] = st.text_input(
            "Tool name", value=node.get("tool_name") or "", key=f"insp_tool_name_{node_id}"
        )
        import json as _json
        args_json = _json.dumps(node.get("tool_args") or {}, ensure_ascii=False)
        edited_args = st.text_area(
            "Arguments (JSON object)",
            value=args_json,
            key=f"insp_tool_args_{node_id}",
            height=100,
        )
        try:
            parsed_args = _json.loads(edited_args or "{}")
            if isinstance(parsed_args, dict):
                node["tool_args"] = parsed_args
            else:
                st.caption("⚠️ Arguments must be a JSON object.")
        except _json.JSONDecodeError:
            st.caption("⚠️ Invalid JSON (keeping the last valid value).")
        with st.expander("Node data wiring"):
            st.json(node.get("inputs_map") or {})

    elif ntype == NODE_TYPE_ROUTER:
        cur_binary = bool(node.get("decision_mode")) or (
            {str(r).strip().lower() for r in (node.get("routes") or [])} == {"yes", "no"}
        )
        is_binary = st.checkbox(
            "⚖️ Modalità Decisionale Binaria (Sì / No)",
            value=cur_binary,
            key=f"insp_router_binary_{node_id}",
            help="Valuta una domanda binaria e instrada il flusso sui rami 'yes' (Sì) o 'no' (No).",
        )
        node["decision_mode"] = bool(is_binary)
        if is_binary:
            node["routes"] = ["yes", "no"]
            node["name"] = st.text_input(
                "Nome nodo decisionale",
                value=node.get("name") or "Decisione Sì/No",
                key=f"insp_dec_name_{node_id}",
            )
            cur_q = str(node.get("question") or node.get("prompt") or "")
            new_q = st.text_area(
                "Domanda / Criterio Sì/No",
                value=cur_q,
                key=f"insp_dec_q_{node_id}",
                height=100,
                help="Es: I dati raccolti sono sufficienti per procedere? Risponde JSON {decision: yes|no, reason}.",
            )
            node["question"] = new_q
            node["prompt"] = new_q
            def_opts = ["no", "yes"]
            cur_def = node.get("default_route") if node.get("default_route") in def_opts else "no"
            node["default_route"] = st.selectbox(
                "Rotta fallback (se contesto vuoto)",
                def_opts,
                index=def_opts.index(cur_def),
                key=f"insp_dec_def_{node_id}",
            )
        else:
            routes_str = st.text_input(
                "Routes (comma-separated)",
                value=", ".join(str(r) for r in (node.get("routes") or [])),
                key=f"insp_router_routes_{node_id}",
            )
            node["routes"] = [r.strip() for r in routes_str.split(",") if r.strip()]
            if node["routes"]:
                cur_def = node.get("default_route")
                if cur_def not in node["routes"]:
                    cur_def = node["routes"][0]
                node["default_route"] = st.selectbox(
                    "Default route",
                    node["routes"],
                    index=node["routes"].index(cur_def),
                    key=f"insp_router_default_{node_id}",
                )

        # Jev / System One Decision Engine & Threshold controls
        from core.team_architect import JEV_DECISION_MODELS

        eng_opts = ["jev", "auto", "llm"]
        cur_eng = str(node.get("decision_engine") or ("jev" if is_binary else "auto")).strip().lower()
        if cur_eng not in eng_opts:
            cur_eng = "jev" if is_binary else "auto"
        node["decision_engine"] = st.selectbox(
            "Motore Decisionale (Jev / System One)",
            eng_opts,
            index=eng_opts.index(cur_eng),
            format_func=lambda x: {
                "jev": "⚡ Jev / System One (/v1/systemone)",
                "auto": "🤖 Auto (Jev o LLM)",
                "llm": "💬 LLM Generativo",
            }.get(x, x),
            key=f"insp_router_eng_{node_id}",
        )

        prim_opts = ["noul", "choice", "score"]
        cur_prim = str(node.get("decision_primitive") or ("noul" if is_binary else "choice")).strip().lower()
        if cur_prim not in prim_opts:
            cur_prim = "noul" if is_binary else "choice"
        node["decision_primitive"] = st.selectbox(
            "Primitiva Jev",
            prim_opts,
            index=prim_opts.index(cur_prim),
            key=f"insp_router_prim_{node_id}",
        )

        try:
            cur_thr = float(0.5 if node.get("threshold") in (None, "") else node.get("threshold"))
        except (TypeError, ValueError):
            cur_thr = 0.5
        node["threshold"] = st.slider(
            "Soglia Probabilità (threshold)",
            min_value=0.05,
            max_value=0.95,
            value=max(0.05, min(0.95, cur_thr)),
            step=0.05,
            key=f"insp_router_thr_{node_id}",
        )

        jev_keys = ["(usa modello DB)"] + list(JEV_DECISION_MODELS.keys())
        cur_jev = str(node.get("jev_model") or "").strip()
        j_idx = jev_keys.index(cur_jev) if cur_jev in jev_keys else (1 if node["decision_engine"] == "jev" and len(jev_keys) > 1 else 0)
        picked_jev = st.selectbox(
            "Preset Modello Jev / System One",
            jev_keys,
            index=j_idx,
            format_func=lambda k: JEV_DECISION_MODELS[k]["label"] if k in JEV_DECISION_MODELS else k,
            key=f"insp_router_jev_{node_id}",
        )
        if picked_jev in JEV_DECISION_MODELS:
            node["jev_model"] = picked_jev
        else:
            node.pop("jev_model", None)

        # Model override selector (DB record for System One endpoint/credentials or fallback LLM)
        all_models = db.read_all_models() or []
        m_opts: Dict[str, Any] = {"🤖 Automatico (MasterAI default)": None}
        for m in all_models:
            lbl = f"{m.get('provider')}/{m.get('model_name')} ({m.get('capability_tier', 'standard')})"
            m_opts[lbl] = int(m["id"])
        cur_mid = node.get("model_id")
        m_labels = list(m_opts.keys())
        m_idx = 0
        if cur_mid is not None:
            for i_m, v_m in enumerate(m_opts.values()):
                if v_m is not None and int(v_m) == int(cur_mid):
                    m_idx = i_m
                    break
        picked_m = st.selectbox("Modello LLM per Router / Decisione", m_labels, index=m_idx, key=f"insp_router_mid_{node_id}")
        if m_opts[picked_m] is None:
            node.pop("model_id", None)
        else:
            node["model_id"] = int(m_opts[picked_m])

        mode_opts = ["auto", "rules", "llm"]
        cur_mode = node.get("router_mode") if node.get("router_mode") in mode_opts else "auto"
        node["router_mode"] = st.selectbox(
            "Mode", mode_opts, index=mode_opts.index(cur_mode), key=f"insp_router_mode_{node_id}"
        )
        rules_text = "\n".join(
            f"{r.get('route')} | {r.get('pattern', '')}"
            for r in (node.get("rules") or [])
            if isinstance(r, dict)
        )
        edited_rules = st.text_area(
            "Rules (opzionali: route | regex per line)",
            value=rules_text,
            key=f"insp_router_rules_{node_id}",
            height=80,
        )
        new_rules = []
        for line in edited_rules.splitlines():
            line = line.strip()
            if not line:
                continue
            route_part, _, pattern = line.partition("|")
            if route_part.strip() and pattern.strip():
                new_rules.append({"route": route_part.strip(), "pattern": pattern.strip()})
        node["rules"] = new_rules
        with st.expander("Node data wiring"):
            st.json(node.get("inputs_map") or {})

    elif ntype == NODE_TYPE_BATCH:
        node["batch_size"] = st.number_input(
            "Batch size", 1, 100, int(node.get("batch_size") or 5), key=f"insp_bs_{node_id}"
        )
        node["source_variable"] = st.text_input(
            "Source", value=node.get("source_variable") or "{previous_result}", key=f"insp_bsrc_{node_id}"
        )

    # Universal Conditional Branching (when_route) selector across executable/export blocks
    if ntype not in (NODE_TYPE_LEVEL, NODE_TYPE_INPUT, NODE_TYPE_LOOP):
        routers = [n for n in graph if n.get("type") == NODE_TYPE_ROUTER and n.get("id") != node_id]
        if routers:
            with st.expander("🔀 Condizione Branch (Sì/No — when_route)", expanded=bool(node.get("when_route"))):
                cur_gate = node.get("when_route") if isinstance(node.get("when_route"), dict) else {}
                cur_src = str(cur_gate.get("from") or "")
                cur_eq = str(cur_gate.get("equals") or "yes")

                r_opts = {"— Nessuna condizione (esegui sempre) —": ""}
                for r in routers:
                    r_name = r.get("name") or r.get("id")
                    r_opts[f"⚖️ {r_name} ({r['id']})"] = r["id"]
                r_labels = list(r_opts.keys())
                r_idx = 0
                for ir, rv in enumerate(r_opts.values()):
                    if rv and rv == cur_src:
                        r_idx = ir
                        break
                picked_r_lbl = st.selectbox("Dipende dalla decisione di", r_labels, index=r_idx, key=f"insp_wr_src_{node_id}")
                new_src = r_opts[picked_r_lbl]
                if new_src:
                    src_node = next((r for r in routers if r["id"] == new_src), {})
                    avail_routes = [str(x) for x in (src_node.get("routes") or ["yes", "no"])]
                    eq_idx = avail_routes.index(cur_eq) if cur_eq in avail_routes else 0
                    new_eq = st.selectbox("Esegui solo se la rotta scelta è", avail_routes, index=eq_idx, key=f"insp_wr_eq_{node_id}")
                    if st.button("Applica Branch Condizionale", key=f"insp_wr_apply_{node_id}"):
                        node["when_route"] = {"from": new_src, "equals": new_eq}
                        deps = list(node.get("depends_on") or [])
                        if new_src not in deps:
                            deps.append(new_src)
                        node["depends_on"] = deps
                        st.session_state.pop("wf_flow_state", None)
                        st.rerun()
                elif cur_gate:
                    if st.button("Rimuovi condizione branch", key=f"insp_wr_clear_{node_id}"):
                        node.pop("when_route", None)
                        st.session_state.pop("wf_flow_state", None)
                        st.rerun()

    if st.button("🗑 Delete from workflow", key=f"insp_rm_{node_id}"):
        st.session_state.wf_selected_task_ids = remove_node(graph, node_id)
        st.session_state.pop("wf_flow_state", None)
        st.rerun()
