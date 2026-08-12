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
    NODE_TYPE_LOOP,
    NODE_TYPE_TASK,
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

    left, right = st.columns([1, 3])

    with left:
        st.markdown("### Palette")
        st.caption(
            "1) Add **Level** columns · 2) Drop blocks inside · "
            "3) Resize borders · 4) Loop-select to frame a group"
        )

        add_kind = st.selectbox(
            "Add block",
            ["Level", "Task", "Batch", "HITL", "Export", "Input"],
            key="canvas_add_kind",
        )

        if add_kind == "Level":
            nxt = next_level_number(st.session_state.wf_selected_task_ids)
            st.write(f"Next level number: **{nxt}**")
            if st.button("Add Level column", key="canvas_btn_level"):
                st.session_state.wf_selected_task_ids.append(make_level_node(nxt))
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

        st.markdown("---")
        st.markdown("### Loop select")
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

        st.markdown("---")
        if st.button("Snap & restack in levels", key="canvas_restack"):
            st.session_state.wf_selected_task_ids = snap_assisted_positions(
                st.session_state.wf_selected_task_ids, task_id_map=task_id_map
            )
            st.session_state.pop("wf_flow_state", None)
            st.rerun()
        if st.button("Auto-link by levels", key="canvas_assist"):
            st.session_state.wf_selected_task_ids = assisted_chain_edges(
                st.session_state.wf_selected_task_ids
            )
            st.session_state.pop("wf_flow_state", None)
            st.rerun()
        if st.button("Rebuild canvas layout", key="canvas_rebuild"):
            st.session_state.pop("wf_flow_state", None)
            st.rerun()

    with right:
        st.markdown("### Canvas")
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

        st.caption(
            "Blocks auto-fit text and snap inside Level columns (75% width). "
            "Agent name is a banner on the first task — no separate agent chips. "
            "Delete: 🗑 below, **Backspace** on the canvas, or node menu. "
            "Task left/right dots are for wiring; Level dots do nothing."
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
            height=620,
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

        selected = getattr(st.session_state.wf_flow_state, "selected_id", None) if "wf_flow_state" in st.session_state else None
        if not selected:
            selected = st.session_state.get("canvas_selected_node")

        st.markdown("---")
        st.markdown("### Inspector")
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
            if st.button("🗑", key="canvas_trash_selected", help="Elimina selezionato (Canc/Backspace sul canvas)"):
                target = pick if pick and pick != "—" else selected
                if target and target in node_ids:
                    st.session_state.wf_selected_task_ids = remove_node(
                        st.session_state.wf_selected_task_ids, target
                    )
                    st.session_state.pop("wf_flow_state", None)
                    st.rerun()
                else:
                    st.toast("Seleziona un blocco da eliminare")
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
                  if (title.indexOf('Elimina selezionato') >= 0 || (b.innerText || '').trim() === '🗑') {
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
        if pick and pick != "—":
            _render_node_inspector(
                db=db,
                node_id=pick,
                task_id_map=task_id_map,
                agent_id_map=agent_id_map,
                agents=agents,
                available_exports=available_exports,
                sanitize_input=sanitize_input,
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

    elif ntype == NODE_TYPE_BATCH:
        node["batch_size"] = st.number_input(
            "Batch size", 1, 100, int(node.get("batch_size") or 5), key=f"insp_bs_{node_id}"
        )
        node["source_variable"] = st.text_input(
            "Source", value=node.get("source_variable") or "{previous_result}", key=f"insp_bsrc_{node_id}"
        )

    if st.button("🗑 Elimina dal workflow", key=f"insp_rm_{node_id}"):
        st.session_state.wf_selected_task_ids = remove_node(graph, node_id)
        st.session_state.pop("wf_flow_state", None)
        st.rerun()
