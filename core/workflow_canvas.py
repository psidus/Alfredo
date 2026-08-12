"""
Bridge Alfredo typed workflow graphs <-> streamlit-flow (React Flow) canvas.

Keeps Alfredo JSON (`task_ids_json`) as source of truth; canvas is a view/editor.

UX model:
- Level nodes are droppable columns; content blocks snapped into a column get that level
  and are auto-stacked vertically.
- Blocks are resizable (borders); size persisted in canvas_ui.
- Loop is a visual frame over selected body blocks; while/for lives on the frame;
  the frame exposes the outgoing output handle.
"""

from __future__ import annotations

import html
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.workflow_graph import (
    CONTENT_TYPES,
    NODE_TYPE_BATCH,
    NODE_TYPE_EXPORT,
    NODE_TYPE_HITL,
    NODE_TYPE_INPUT,
    NODE_TYPE_LEVEL,
    NODE_TYPE_LOOP,
    NODE_TYPE_TASK,
    compute_loop_frame_ui,
    make_level_node,
    normalize_graph,
    remove_node,
    resolve_loop_body_ids,
)

# Soft dependency — UI falls back if missing
try:
    from streamlit_flow.elements import StreamlitFlowEdge, StreamlitFlowNode
    from streamlit_flow.state import StreamlitFlowState

    HAS_STREAMLIT_FLOW = True
except Exception:  # pragma: no cover
    HAS_STREAMLIT_FLOW = False
    StreamlitFlowNode = object  # type: ignore
    StreamlitFlowEdge = object  # type: ignore
    StreamlitFlowState = object  # type: ignore

COL_X_GAP = 300.0
DEFAULT_LEVEL_WIDTH = 280
DEFAULT_LEVEL_HEIGHT = 520
LANE_PADDING_TOP = 56.0
LANE_PADDING_SIDE = 16.0
ROW_GAP = 14.0
AGENT_BAND_GAP = 26.0
DEFAULT_NODE_WIDTH = 220
DEFAULT_NODE_HEIGHT = 120
CONTENT_WIDTH_RATIO = 0.75  # task width = 75% of level column
MIN_CONTENT_HEIGHT = 72
LINE_HEIGHT_PX = 16
AGENT_LABEL_PREFIX = "__agent_label_"

# Keep level columns strictly behind tasks / loops (no separate agent nodes)
Z_LEVEL = -100
Z_LOOP = 2
Z_CONTENT = 20

_TYPE_COLORS = {
    NODE_TYPE_INPUT: "#4CAF50",
    NODE_TYPE_TASK: "#2196F3",
    NODE_TYPE_BATCH: "#9C27B0",
    NODE_TYPE_HITL: "#FF9800",
    NODE_TYPE_EXPORT: "#607D8B",
    NODE_TYPE_LOOP: "#E91E63",
    NODE_TYPE_LEVEL: "#90CAF9",
}

_DEFAULT_SIZES = {
    NODE_TYPE_TASK: (220, 130),
    NODE_TYPE_BATCH: (200, 110),
    NODE_TYPE_HITL: (200, 100),
    NODE_TYPE_EXPORT: (200, 100),
    NODE_TYPE_INPUT: (180, 90),
    NODE_TYPE_LOOP: (320, 220),
    NODE_TYPE_LEVEL: (DEFAULT_LEVEL_WIDTH, DEFAULT_LEVEL_HEIGHT),
}


def _parse_dim(value: Any, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower().replace("px", "")
    try:
        return float(text)
    except ValueError:
        return default


def get_canvas_ui(node: Dict[str, Any]) -> Dict[str, Any]:
    ui = node.get("canvas_ui")
    return dict(ui) if isinstance(ui, dict) else {}


def set_canvas_ui(node: Dict[str, Any], **kwargs: Any) -> None:
    ui = get_canvas_ui(node)
    ui.update({k: v for k, v in kwargs.items() if v is not None})
    node["canvas_ui"] = ui


def is_canvas_decorator_id(node_id: str) -> bool:
    return str(node_id).startswith(AGENT_LABEL_PREFIX)


def _agent_key_for_node(node: Dict[str, Any], task_id_map: Dict[int, Any]) -> str:
    if node.get("type") == NODE_TYPE_TASK:
        tid = node.get("task_id")
        task = task_id_map.get(int(tid)) if tid is not None else None
        aid = (task or {}).get("agent_id")
        return f"agent:{aid}" if aid is not None else "agent:?"
    return f"type:{node.get('type') or 'other'}"


def _agent_label(agent_key: str, agent_id_map: Dict[int, Any]) -> str:
    if agent_key.startswith("agent:"):
        raw = agent_key.split(":", 1)[1]
        if raw == "?":
            return "Unassigned"
        try:
            agent = agent_id_map.get(int(raw))
            return (agent or {}).get("name") or f"Agent {raw}"
        except (TypeError, ValueError):
            return raw
    return agent_key.split(":", 1)[-1].upper()


def _level_nodes(graph: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [n for n in graph if n.get("type") == NODE_TYPE_LEVEL]


def _content_nodes(graph: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [n for n in graph if n.get("type") in CONTENT_TYPES or n.get("type") == NODE_TYPE_LOOP]


def find_level_for_x(graph: Sequence[Dict[str, Any]], x: float, *, width_hint: float = 0) -> Optional[int]:
    """Return level for a block: prefer partial overlap with a column, else nearest."""
    return find_level_for_block(graph, x=x, y=0, w=width_hint or DEFAULT_NODE_WIDTH, h=1)


def find_level_for_block(
    graph: Sequence[Dict[str, Any]],
    *,
    x: float,
    y: float,
    w: float,
    h: float,
) -> Optional[int]:
    """
    Assign level when a block intersects a level column (even partially).
    Picks the column with the largest horizontal overlap.
    """
    levels = _level_nodes(graph)
    if not levels:
        return None
    bx1, bx2 = float(x), float(x) + max(float(w), 1.0)
    best_lvl = None
    best_overlap = 0.0
    best_dist = None
    for ln in levels:
        ui = get_canvas_ui(ln)
        lx = float(ui.get("x") or 0)
        lw = float(ui.get("width") or DEFAULT_LEVEL_WIDTH)
        lx2 = lx + lw
        overlap = max(0.0, min(bx2, lx2) - max(bx1, lx))
        lvl = int(ln.get("level") or 1)
        if overlap > best_overlap:
            best_overlap = overlap
            best_lvl = lvl
        mid = lx + lw / 2.0
        cx = (bx1 + bx2) / 2.0
        dist = abs(cx - mid)
        if best_dist is None or dist < best_dist:
            best_dist = dist
            if best_lvl is None:
                best_lvl = lvl
    if best_overlap > 0:
        return best_lvl
    return best_lvl


def estimate_block_height(
    node: Dict[str, Any],
    *,
    width: float,
    task: Optional[Dict[str, Any]] = None,
    n_tasks_in_level: int = 1,
) -> int:
    """
    Fit height to card text (no scrollbar). Also scales slightly with how many
    tasks share the level (level grows; each card stays readable).
    """
    ntype = node.get("type") or NODE_TYPE_TASK
    chars_per_line = max(12, int(float(width) / 7.2))

    def lines_for(text: str, min_lines: int = 1) -> int:
        t = (text or "").strip()
        if not t:
            return min_lines
        return max(min_lines, (len(t) + chars_per_line - 1) // chars_per_line)

    if ntype == NODE_TYPE_TASK:
        name = (task or {}).get("name") or f"Task #{node.get('task_id')}"
        agent = "?"
        # header + name + agent + IN + OUT + id
        n_lines = 2 + lines_for(str(name), 1) + 1 + 2 + 1
    elif ntype == NODE_TYPE_BATCH:
        n_lines = 4
    elif ntype == NODE_TYPE_HITL:
        n_lines = 2 + lines_for(str(node.get("message") or ""), 1) + 1
    elif ntype == NODE_TYPE_EXPORT:
        n_lines = 3
    elif ntype == NODE_TYPE_INPUT:
        n_lines = 3
    else:
        n_lines = 4

    content_h = 12 + n_lines * LINE_HEIGHT_PX + 12  # padding
    # With few tasks give a bit more air; with many keep compact but never below content
    n = max(1, int(n_tasks_in_level))
    air = max(0, 18 - 2 * (n - 1))
    return max(MIN_CONTENT_HEIGHT, int(content_h + air))


def content_size_in_level(
    node: Dict[str, Any],
    *,
    level_width: float,
    n_tasks_in_level: int,
    task: Optional[Dict[str, Any]] = None,
) -> Tuple[int, int]:
    """Width = 75% of level; height fitted to text (+ task-count air)."""
    w = max(120, int(float(level_width) * CONTENT_WIDTH_RATIO))
    h = estimate_block_height(
        node, width=w, task=task, n_tasks_in_level=n_tasks_in_level
    )
    return w, h


def centered_x_in_level(level_x: float, level_width: float, block_width: float) -> float:
    return float(level_x) + max(0.0, (float(level_width) - float(block_width)) / 2.0)


def auto_stack_level(
    graph: List[Dict[str, Any]],
    level: int,
    *,
    task_id_map: Optional[Dict[int, Any]] = None,
) -> List[Dict[str, Any]]:
    """Center content at 75% level width, fit height to text, pack vertically; grow level."""
    task_id_map = task_id_map or {}
    level_node = next(
        (n for n in graph if n.get("type") == NODE_TYPE_LEVEL and int(n.get("level") or 0) == level),
        None,
    )
    if not level_node:
        return graph
    lui = get_canvas_ui(level_node)
    level_x = float(lui.get("x") or 0)
    col_w = float(lui.get("width") or DEFAULT_LEVEL_WIDTH)
    y_cursor = float(lui.get("y") or 0) + LANE_PADDING_TOP

    members = [
        n for n in graph
        if n.get("type") != NODE_TYPE_LEVEL
        and n.get("type") != NODE_TYPE_LOOP
        and int(n.get("execution_level") or 1) == level
    ]
    members.sort(key=lambda n: float(get_canvas_ui(n).get("y") or 0))
    n_tasks = len(members)

    groups: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    for n in members:
        key = _agent_key_for_node(n, task_id_map)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(n)

    for key in order:
        y_cursor += AGENT_BAND_GAP  # visual gap between agent groups (no extra RF node)
        for n in groups[key]:
            tid = n.get("task_id")
            task = task_id_map.get(int(tid)) if tid is not None else None
            w, h = content_size_in_level(
                n, level_width=col_w, n_tasks_in_level=max(1, n_tasks), task=task
            )
            x = centered_x_in_level(level_x, col_w, w)
            # Keep block fully inside level bounds
            max_x = level_x + col_w - w - 4
            x = min(max(level_x + 4, x), max(level_x + 4, max_x))
            set_canvas_ui(n, x=x, y=y_cursor, width=int(w), height=int(h))
            y_cursor += h + ROW_GAP

    # Level height depends on how many tasks (and their fitted heights)
    needed_h = max(
        DEFAULT_LEVEL_HEIGHT,
        int(y_cursor - float(lui.get("y") or 0) + 48),
        int(LANE_PADDING_TOP + n_tasks * (MIN_CONTENT_HEIGHT + ROW_GAP) + 80),
    )
    set_canvas_ui(level_node, height=needed_h)
    return graph


def snap_assisted_positions(
    graph: Sequence[Dict[str, Any]],
    *,
    task_id_map: Optional[Dict[int, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    After a free drag: assign level by X, then snap/stack all columns.
    Call this when the user finishes moving blocks between/within levels.
    """
    g = [dict(n) for n in normalize_graph(graph, materialize_export=False)]
    levels = _level_nodes(g)
    if not levels:
        return g
    for n in g:
        if n.get("type") in (NODE_TYPE_LEVEL, NODE_TYPE_LOOP):
            continue
        if n.get("type") not in CONTENT_TYPES:
            continue
        ui = get_canvas_ui(n)
        x = float(ui.get("x") or 0)
        y = float(ui.get("y") or 0)
        w = float(ui.get("width") or DEFAULT_NODE_WIDTH)
        h = float(ui.get("height") or DEFAULT_NODE_HEIGHT)
        lvl = find_level_for_block(g, x=x, y=y, w=w, h=h)
        if lvl is not None:
            n["execution_level"] = int(lvl)
    return auto_stack_all_levels(g, task_id_map=task_id_map)


def auto_stack_all_levels(
    graph: Sequence[Dict[str, Any]],
    *,
    task_id_map: Optional[Dict[int, Any]] = None,
) -> List[Dict[str, Any]]:
    g = [dict(n) for n in normalize_graph(graph, materialize_export=False)]
    levels = sorted({int(n.get("level") or 1) for n in g if n.get("type") == NODE_TYPE_LEVEL})
    for lvl in levels:
        g = auto_stack_level(g, lvl, task_id_map=task_id_map)
    # Refresh loop frames to wrap their bodies
    by_id = {n["id"]: n for n in g}
    for n in g:
        if n.get("type") != NODE_TYPE_LOOP:
            continue
        body_ids = resolve_loop_body_ids(g, n) or n.get("body_node_ids") or []
        body = [by_id[i] for i in body_ids if i in by_id]
        if body:
            set_canvas_ui(n, **compute_loop_frame_ui(body))
    return g


def _node_style(ntype: str, node: Dict[str, Any]) -> Dict[str, Any]:
    ui = get_canvas_ui(node)
    dw, dh = _DEFAULT_SIZES.get(ntype, (DEFAULT_NODE_WIDTH, DEFAULT_NODE_HEIGHT))
    w = int(ui.get("width") or dw)
    h = int(ui.get("height") or dh)
    color = _TYPE_COLORS.get(ntype, "#90A4AE")
    style: Dict[str, Any] = {
        "width": f"{w}px",
        "height": f"{h}px",
        "minWidth": "80px",
        "minHeight": f"{MIN_CONTENT_HEIGHT}px",
        "backgroundColor": color,
        "color": "#fff",
        "border": "1px solid #333",
        "borderRadius": "6px",
        "padding": "8px",
        "fontSize": "12px",
        "lineHeight": "1.35",
        "overflow": "hidden",  # no scrollbars — height is fitted to text
        "boxSizing": "border-box",
        "whiteSpace": "normal",
        "wordBreak": "break-word",
    }
    if ntype == NODE_TYPE_LEVEL:
        style.update(
            {
                "backgroundColor": "rgba(33, 150, 243, 0.10)",
                "border": "2px dashed rgba(21, 101, 192, 0.55)",
                "color": "#1565C0",
                "borderRadius": "10px",
                "overflow": "hidden",
                "resize": "both",
                "zIndex": Z_LEVEL,
                # Soften default RF handles (levels are not wired)
                "opacity": 1,
            }
        )
    elif ntype == NODE_TYPE_LOOP:
        style.update(
            {
                "backgroundColor": "rgba(233, 30, 99, 0.18)",
                "border": "2px dashed #E91E63",
                "color": "#880E4F",
                "borderRadius": "12px",
                "overflow": "hidden",
                "resize": "both",
            }
        )
    else:
        # Content cards: no CSS resize grip (keeps fit-to-text clean); use inspector if needed
        style["resize"] = "none"
    return style


def _task_ports_html(
    task: Optional[Dict[str, Any]],
    node: Dict[str, Any],
    *,
    exports_linked: bool = False,
) -> str:
    req = []
    if task:
        raw = task.get("required_inputs") or []
        if isinstance(raw, str):
            try:
                import json
                raw = json.loads(raw) or []
            except Exception:
                raw = []
        for row in raw:
            if isinstance(row, dict) and row.get("key"):
                req.append(str(row["key"]))
            elif isinstance(row, str):
                req.append(row)
    wired = list((node.get("inputs_map") or {}).keys())
    ins = wired or req or ["(previous_result)"]
    outs = ["result"]
    if task and task.get("output_pydantic"):
        outs = [s.strip() for s in str(task["output_pydantic"]).split(",") if s.strip()] or outs
    if task and task.get("human_validation"):
        outs.append("HITL✓")
    if exports_linked:
        outs.append("→ export file")
    return (
        f"<small><b>IN</b>: {html.escape(', '.join(ins[:6]))}<br/>"
        f"<b>OUT</b>: {html.escape(', '.join(outs[:6]))}</small>"
    )


def node_card_content(
    node: Dict[str, Any],
    *,
    task: Optional[Dict[str, Any]] = None,
    agent: Optional[Dict[str, Any]] = None,
    exports_linked: bool = False,
    member_count: int = 0,
    agent_band: Optional[str] = None,
) -> str:
    ntype = node.get("type") or NODE_TYPE_TASK
    nid = html.escape(str(node.get("id") or ""))
    lvl = node.get("execution_level", 1)
    band = ""
    if agent_band:
        band = (
            f"<div style='font-size:11px;opacity:0.95;margin-bottom:4px;"
            f"border-bottom:1px solid rgba(255,255,255,0.35);padding-bottom:3px'>"
            f"👤 <b>{html.escape(agent_band)}</b></div>"
        )

    if ntype == NODE_TYPE_LEVEL:
        agents = member_count
        return (
            f"<div style='padding:6px;overflow:hidden'>"
            f"<b style='font-size:15px'>LEVEL {int(node.get('level') or lvl)}</b><br/>"
            f"<small>Drop tasks here · {agents} block(s)</small><br/>"
            f"<code style='font-size:10px'>{nid}</code></div>"
        )
    if ntype == NODE_TYPE_TASK:
        tname = html.escape((task or {}).get("name") or f"Task #{node.get('task_id')}")
        aname = html.escape((agent or {}).get("name") or "?")
        ports = _task_ports_html(task, node, exports_linked=exports_linked)
        hitl = " · HITL" if (task or {}).get("human_validation") else ""
        return (
            f"<div style='overflow:hidden;word-break:break-word'>{band}"
            f"<b>TASK</b> L{lvl}{hitl}<br/>"
            f"<b>{tname}</b><br/><small>👤 {aname}</small><br/>{ports}<br/>"
            f"<code style='font-size:10px'>{nid}</code></div>"
        )
    if ntype == NODE_TYPE_BATCH:
        return (
            f"<div style='overflow:hidden'>{band}<b>BATCH</b> L{lvl}<br/>"
            f"size={node.get('batch_size')} · `{html.escape(str(node.get('source_variable') or ''))}`"
            f"<br/><code style='font-size:10px'>{nid}</code></div>"
        )
    if ntype == NODE_TYPE_HITL:
        return (
            f"<div style='overflow:hidden'>{band}<b>HITL</b> L{lvl}<br/>"
            f"{html.escape((node.get('message') or '')[:80])}"
            f"<br/><code style='font-size:10px'>{nid}</code></div>"
        )
    if ntype == NODE_TYPE_EXPORT:
        ex = ", ".join(node.get("exports") or []) or "formats?"
        return (
            f"<div style='overflow:hidden'>{band}<b>EXPORT</b> L{lvl}<br/>{html.escape(ex)}"
            f"<br/><code style='font-size:10px'>{nid}</code></div>"
        )
    if ntype == NODE_TYPE_INPUT:
        keys = ", ".join(node.get("keys") or ["user_input"])
        return (
            f"<div style='overflow:hidden'>{band}<b>INPUT</b> L{lvl}<br/>{html.escape(keys)}"
            f"<br/><code style='font-size:10px'>{nid}</code></div>"
        )
    if ntype == NODE_TYPE_LOOP:
        mode = html.escape(str(node.get("loop_mode") or "for_each"))
        n_body = len(node.get("body_node_ids") or []) or len(node.get("body_levels") or [])
        cond_bits = []
        if node.get("exit_on_hitl"):
            cond_bits.append("HITL✓")
        ec = node.get("exit_condition") or {}
        if isinstance(ec, dict) and (ec.get("output_match") or {}).get("pattern"):
            cond_bits.append("output match")
        until = ", ".join(cond_bits) or "max iter"
        return (
            f"<div style='overflow:hidden'><b>LOOP FRAME</b> · {mode}<br/>"
            f"body={n_body} · until: {html.escape(until)}<br/>"
            f"<small>Output → connect from this frame</small><br/>"
            f"<code style='font-size:10px'>{nid}</code></div>"
        )
    return f"<b>{html.escape(ntype)}</b><br/><code>{nid}</code>"


def graph_to_flow_state(
    graph: Sequence[Dict[str, Any]],
    *,
    task_id_map: Optional[Dict[int, Any]] = None,
    agent_id_map: Optional[Dict[int, Any]] = None,
    highlight_ids: Optional[Sequence[str]] = None,
) -> "StreamlitFlowState":
    """Build StreamlitFlowState: level columns, content blocks, loop frames."""
    if not HAS_STREAMLIT_FLOW:
        raise RuntimeError("streamlit-flow-component is not installed")

    task_id_map = task_id_map or {}
    agent_id_map = agent_id_map or {}
    highlight = set(highlight_ids or [])
    g = normalize_graph(graph, materialize_export=False)
    # Keep content cards inside their level columns (centers at 75% width)
    g = auto_stack_all_levels(g, task_id_map=task_id_map)

    export_parents: set = set()
    for n in g:
        if n.get("type") == NODE_TYPE_EXPORT:
            export_parents.update(n.get("depends_on") or [])

    members_by_level: Dict[int, int] = {}
    for n in g:
        if n.get("type") in CONTENT_TYPES:
            lvl = int(n.get("execution_level") or 1)
            members_by_level[lvl] = members_by_level.get(lvl, 0) + 1

    nodes: List[Any] = []

    # 1) Level columns (behind everything)
    for n in g:
        if n.get("type") != NODE_TYPE_LEVEL:
            continue
        ui = get_canvas_ui(n)
        x = float(ui.get("x") if ui.get("x") is not None else (int(n.get("level") or 1) - 1) * COL_X_GAP)
        y = float(ui.get("y") or 0)
        content = node_card_content(n, member_count=members_by_level.get(int(n.get("level") or 1), 0))
        nodes.append(
            StreamlitFlowNode(
                id=str(n["id"]),
                pos=(x, y),
                data={"content": content, "alfredo_type": NODE_TYPE_LEVEL, "alfredo_node": n},
                # "input" still shows one handle; default shows two — keep default but
                # connectable=False so level dots cannot create wires.
                node_type="default",
                source_position="right",
                target_position="left",
                draggable=True,
                selectable=True,
                connectable=False,
                resizing=True,
                deletable=True,
                z_index=Z_LEVEL,
                style=_node_style(NODE_TYPE_LEVEL, n),
            )
        )

    # 2) Loop frames (behind content, above levels)
    for n in g:
        if n.get("type") != NODE_TYPE_LOOP:
            continue
        enriched = dict(n)
        enriched["body_node_ids"] = resolve_loop_body_ids(g, enriched) or enriched.get("body_node_ids") or []
        ui = get_canvas_ui(enriched)
        if ui.get("x") is None or ui.get("width") is None:
            body = [x for x in g if x.get("id") in set(enriched["body_node_ids"])]
            set_canvas_ui(enriched, **compute_loop_frame_ui(body))
            ui = get_canvas_ui(enriched)
        x = float(ui.get("x") or 0)
        y = float(ui.get("y") or 0)
        content = node_card_content(enriched)
        style = _node_style(NODE_TYPE_LOOP, enriched)
        if enriched["id"] in highlight:
            style["boxShadow"] = "0 0 0 3px #FFEB3B"
        nodes.append(
            StreamlitFlowNode(
                id=str(enriched["id"]),
                pos=(x, y),
                data={"content": content, "alfredo_type": NODE_TYPE_LOOP, "alfredo_node": enriched},
                node_type="default",
                source_position="right",
                target_position="left",
                draggable=True,
                selectable=True,
                connectable=True,
                resizing=True,
                deletable=True,
                z_index=Z_LOOP,
                style=style,
            )
        )

    # 3) Content blocks inside levels (agent name is on the task card — no separate label nodes)
    by_level: Dict[int, List[Dict[str, Any]]] = {}
    for n in g:
        if n.get("type") in CONTENT_TYPES:
            by_level.setdefault(int(n.get("execution_level") or 1), []).append(n)

    for lvl, members in by_level.items():
        level_node = next((n for n in g if n.get("type") == NODE_TYPE_LEVEL and int(n.get("level") or 0) == lvl), None)
        lui = get_canvas_ui(level_node) if level_node else {}
        level_x = float(lui.get("x") or (lvl - 1) * COL_X_GAP)
        level_w = float(lui.get("width") or DEFAULT_LEVEL_WIDTH)
        default_y = float(lui.get("y") or 0) + LANE_PADDING_TOP

        groups: Dict[str, List[Dict[str, Any]]] = {}
        order: List[str] = []
        for n in sorted(members, key=lambda m: float(get_canvas_ui(m).get("y") or 0)):
            key = _agent_key_for_node(n, task_id_map)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(n)

        y_cursor = default_y
        for key in order:
            agent_label = _agent_label(key, agent_id_map)
            y_cursor += AGENT_BAND_GAP
            for i, n in enumerate(groups[key]):
                ui = get_canvas_ui(n)
                tid = n.get("task_id")
                task = task_id_map.get(int(tid)) if tid is not None else None
                agent = agent_id_map.get(task["agent_id"]) if task and task.get("agent_id") is not None else None
                if ui.get("x") is not None and ui.get("y") is not None:
                    x, y = float(ui["x"]), float(ui["y"])
                else:
                    tw = int(level_w * CONTENT_WIDTH_RATIO)
                    x = centered_x_in_level(level_x, level_w, tw)
                    y = y_cursor
                    y_cursor += float(ui.get("height") or DEFAULT_NODE_HEIGHT) + ROW_GAP

                content = node_card_content(
                    n,
                    task=task,
                    agent=agent,
                    exports_linked=n["id"] in export_parents,
                    agent_band=agent_label if i == 0 else None,
                )
                ntype = n.get("type") or NODE_TYPE_TASK
                style = _node_style(ntype, n)
                if n["id"] in highlight:
                    style["boxShadow"] = "0 0 0 3px #FFEB3B"
                    style["border"] = "2px solid #FBC02D"
                nodes.append(
                    StreamlitFlowNode(
                        id=str(n["id"]),
                        pos=(x, y),
                        data={"content": content, "alfredo_type": ntype, "alfredo_node": n},
                        node_type="default",
                        source_position="right",
                        target_position="left",
                        resizing=False,
                        selectable=True,
                        connectable=True,
                        draggable=True,
                        deletable=True,
                        z_index=Z_CONTENT,
                        style=style,
                    )
                )

    edges: List[Any] = []
    seen_e = set()
    for n in g:
        if n.get("type") == NODE_TYPE_LEVEL:
            continue
        for d in n.get("depends_on") or []:
            eid = f"ctrl_{d}_{n['id']}"
            if eid in seen_e:
                continue
            seen_e.add(eid)
            edges.append(
                StreamlitFlowEdge(
                    id=eid,
                    source=str(d),
                    target=str(n["id"]),
                    label="depends",
                    animated=False,
                    marker_end={"type": "arrowclosed"},
                )
            )
        for port, binding in (n.get("inputs_map") or {}).items():
            if not isinstance(binding, dict):
                continue
            src = binding.get("from")
            if not src or src == "input":
                continue
            eid = f"data_{src}_{n['id']}_{port}"
            if eid in seen_e:
                continue
            seen_e.add(eid)
            key = binding.get("key") or "*"
            edges.append(
                StreamlitFlowEdge(
                    id=eid,
                    source=str(src),
                    target=str(n["id"]),
                    label=f"{port}:{key}",
                    animated=True,
                    style={"strokeDasharray": "4 2"},
                    marker_end={"type": "arrowclosed"},
                )
            )

    return StreamlitFlowState(nodes, edges)


def _state_node_dict(sn: Any) -> Dict[str, Any]:
    if isinstance(sn, dict):
        return sn
    out: Dict[str, Any] = {}
    for attr in ("id", "data", "style", "position", "width", "height", "kwargs"):
        val = getattr(sn, attr, None)
        if val is not None:
            out[attr] = val
    # Some streamlit-flow builds stash extras in kwargs
    kwargs = out.get("kwargs") if isinstance(out.get("kwargs"), dict) else {}
    for k in ("width", "height"):
        if k not in out and k in kwargs:
            out[k] = kwargs[k]
    return out


def _extract_size(raw: Dict[str, Any], node: Dict[str, Any], default_w: float, default_h: float) -> Tuple[float, float]:
    """Prefer measured width/height, then style, then persisted canvas_ui."""
    ui = get_canvas_ui(node)
    style = raw.get("style") or {}
    w = raw.get("width")
    h = raw.get("height")
    if w is None:
        w = style.get("width")
    if h is None:
        h = style.get("height")
    return (
        _parse_dim(w, ui.get("width") or default_w),
        _parse_dim(h, ui.get("height") or default_h),
    )


def flow_state_to_graph_updates(
    state: Any,
    base_graph: Sequence[Dict[str, Any]],
    *,
    task_id_map: Optional[Dict[int, Any]] = None,
    restack: bool = True,
) -> List[Dict[str, Any]]:
    """
    Merge canvas edits back into Alfredo graph.
    - Persist width/height/x/y (resize + drag)
    - Drop into level column → execution_level + auto-stack
    - Edges → depends_on / inputs_map
    """
    base = {n["id"]: dict(n) for n in normalize_graph(base_graph, materialize_export=False)}

    # Canvas node list is authoritative for deletions (Backspace / node menu / trash)
    raw_nodes = getattr(state, "nodes", None)
    if isinstance(raw_nodes, list):
        present_ids = set()
        for sn in raw_nodes:
            raw = _state_node_dict(sn)
            nid = raw.get("id")
            if nid and not is_canvas_decorator_id(str(nid)):
                present_ids.add(str(nid))
        for nid in list(base.keys()):
            if nid not in present_ids:
                repaired = remove_node(list(base.values()), nid)
                base = {n["id"]: n for n in repaired}

    # First pass: capture positions/sizes from canvas
    for sn in getattr(state, "nodes", []) or []:
        raw = _state_node_dict(sn)
        nid = raw.get("id")
        if not nid or is_canvas_decorator_id(str(nid)):
            continue
        data = raw.get("data") or {}
        payload = data.get("alfredo_node") if isinstance(data, dict) else None
        if isinstance(payload, dict) and payload.get("id") and payload["id"] in base:
            # Keep type/identity from payload but prefer live canvas geometry
            merged = {**base[payload["id"]], **{k: v for k, v in payload.items() if k != "canvas_ui"}}
            base[payload["id"]] = merged

        if str(nid) not in base:
            continue

        pos = raw.get("position") or {}
        x = float(pos.get("x", 0))
        y = float(pos.get("y", 0))
        node = base[str(nid)]
        ntype = node.get("type")
        dw, dh = _DEFAULT_SIZES.get(ntype, (DEFAULT_NODE_WIDTH, DEFAULT_NODE_HEIGHT))
        w, h = _extract_size(raw, node, dw, dh)
        set_canvas_ui(node, width=int(max(80, w)), height=int(max(48, h)), x=x, y=y)

        if ntype == NODE_TYPE_LEVEL:
            # Keep level number; only geometry updates
            continue
        if ntype == NODE_TYPE_LOOP:
            continue
        # Content: assign when partially overlapping a level column
        lvl = find_level_for_block(list(base.values()), x=x, y=y, w=w, h=h)
        if lvl is not None:
            node["execution_level"] = int(lvl)

    # Wiring from edges
    for n in base.values():
        if n.get("type") == NODE_TYPE_LEVEL:
            n["depends_on"] = []
            n["inputs_map"] = {}
            continue
        n["depends_on"] = []
        old_imap = n.get("inputs_map") or {}
        kept = {
            k: v for k, v in old_imap.items()
            if isinstance(v, dict) and v.get("from") == "input"
        }
        n["inputs_map"] = kept

    for e in getattr(state, "edges", []) or []:
        src = getattr(e, "source", None) or (e.get("source") if isinstance(e, dict) else None)
        tgt = getattr(e, "target", None) or (e.get("target") if isinstance(e, dict) else None)
        label = getattr(e, "label", None) or (e.get("label") if isinstance(e, dict) else "") or ""
        if not src or not tgt or is_canvas_decorator_id(str(src)) or is_canvas_decorator_id(str(tgt)):
            continue
        if tgt not in base or base[tgt].get("type") == NODE_TYPE_LEVEL:
            continue
        if src not in base and src != "input":
            continue
        if src in base and base[src].get("type") == NODE_TYPE_LEVEL:
            continue
        animated = getattr(e, "animated", None)
        if animated is None and isinstance(e, dict):
            animated = e.get("animated")
        is_data = bool(animated) or (":" in str(label) and str(label) != "depends")
        if is_data and str(label) not in ("", "depends"):
            port, _, key = str(label).partition(":")
            port = port.strip() or "data"
            key = key.strip()
            base[tgt].setdefault("inputs_map", {})
            base[tgt]["inputs_map"][port] = {"from": src, "key": key if key != "*" else ""}
            if src != "input" and src not in base[tgt]["depends_on"]:
                base[tgt]["depends_on"].append(src)
        else:
            if src not in base[tgt]["depends_on"]:
                base[tgt]["depends_on"].append(src)

    ordered = []
    seen = set()
    for n in base_graph:
        nid = n.get("id") if isinstance(n, dict) else None
        if nid and nid in base and nid not in seen:
            ordered.append(base[nid])
            seen.add(nid)
    for nid, n in base.items():
        if nid not in seen:
            ordered.append(n)

    g = normalize_graph(ordered, materialize_export=False)
    if restack:
        g = auto_stack_all_levels(g, task_id_map=task_id_map)
    return g


def assisted_chain_edges(graph: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """For each level L>1 with empty depends_on, link content to previous level content."""
    g = normalize_graph(graph, materialize_export=False)
    by_level: Dict[int, List[str]] = {}
    for n in g:
        if n.get("type") in (NODE_TYPE_LEVEL,):
            continue
        if n.get("type") == NODE_TYPE_LOOP:
            by_level.setdefault(int(n.get("execution_level") or 1), []).append(n["id"])
            continue
        if n.get("type") in CONTENT_TYPES:
            by_level.setdefault(int(n.get("execution_level") or 1), []).append(n["id"])
    levels = sorted(by_level.keys())
    out = []
    for n in g:
        nn = dict(n)
        if nn.get("type") == NODE_TYPE_LEVEL:
            out.append(nn)
            continue
        lvl = int(nn.get("execution_level") or 1)
        if not nn.get("depends_on") and lvl > 1:
            prev = [l for l in levels if l < lvl]
            if prev:
                nn["depends_on"] = list(by_level[max(prev)])
        out.append(nn)
    return out


def ensure_level_columns(graph: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Ensure a Level column node exists for every execution_level used by content/loops.
    Legacy workflows saved without level nodes get columns inferred from execution_level.
    """
    g = [dict(n) for n in normalize_graph(graph, materialize_export=False)]
    existing = {
        int(n.get("level") or 0)
        for n in g
        if n.get("type") == NODE_TYPE_LEVEL
    }
    needed: set = set()
    for n in g:
        if n.get("type") == NODE_TYPE_LEVEL:
            continue
        if n.get("type") in CONTENT_TYPES or n.get("type") == NODE_TYPE_LOOP:
            needed.add(max(1, int(n.get("execution_level") or 1)))
    for lvl in sorted(needed):
        if lvl not in existing:
            g.append(make_level_node(lvl))
            existing.add(lvl)
    return g


def hydrate_graph_for_canvas(
    graph: Sequence[Dict[str, Any]],
    *,
    task_id_map: Optional[Dict[int, Any]] = None,
    force_layout: bool = True,
) -> List[Dict[str, Any]]:
    """
    Prepare a loaded/saved workflow for the canvas editor:
    - materialize missing Level columns from execution_level
    - auto-stack content into columns (and fit loop frames)
    """
    g = ensure_level_columns(graph)
    if force_layout:
        g = auto_stack_all_levels(g, task_id_map=task_id_map or {})
    return g


def canvas_geometry_fingerprint(graph: Sequence[Dict[str, Any]]) -> Tuple:
    """Compare layout before/after snap to decide if the React Flow state must rebuild."""
    rows = []
    for n in normalize_graph(graph, materialize_export=False):
        ui = get_canvas_ui(n)
        rows.append(
            (
                n.get("id"),
                n.get("type"),
                int(n.get("execution_level") or 0),
                round(float(ui.get("x") or 0), 1),
                round(float(ui.get("y") or 0), 1),
                int(ui.get("width") or 0),
                int(ui.get("height") or 0),
            )
        )
    return tuple(rows)


def strip_decorator_nodes_from_state(state: Any) -> Any:
    """Drop leftover __agent_label_* RF nodes from an existing StreamlitFlowState."""
    if not HAS_STREAMLIT_FLOW or state is None:
        return state
    nodes = getattr(state, "nodes", None) or []
    cleaned = []
    removed = False
    for sn in nodes:
        raw = _state_node_dict(sn)
        nid = str(raw.get("id") or "")
        if is_canvas_decorator_id(nid):
            removed = True
            continue
        cleaned.append(sn)
    if not removed:
        return state
    from streamlit_flow.state import StreamlitFlowState

    return StreamlitFlowState(
        cleaned,
        list(getattr(state, "edges", []) or []),
        selected_id=getattr(state, "selected_id", None),
        timestamp=getattr(state, "timestamp", 0),
    )


def run_streamlit_flow_preserving_size(
    key: str,
    state: Any,
    **kwargs: Any,
) -> Any:
    """
    Call streamlit_flow but keep measured width/height on nodes.

    streamlit-flow's from_dict drops top-level width/height; we merge them into style
    so resize (CSS or measured) survives round-trips.
    Also strips obsolete agent-label decorator nodes.
    """
    from streamlit_flow import streamlit_flow
    from streamlit_flow.elements import StreamlitFlowNode
    from streamlit_flow.state import StreamlitFlowState

    state = strip_decorator_nodes_from_state(state)
    new_state = streamlit_flow(key, state, **kwargs)
    nodes_out = []
    for sn in getattr(new_state, "nodes", []) or []:
        raw = _state_node_dict(sn)
        nid = str(raw.get("id") or "")
        if is_canvas_decorator_id(nid):
            continue
        style = dict(raw.get("style") or {})
        w = raw.get("width")
        h = raw.get("height")
        if w is not None and not style.get("width"):
            style["width"] = f"{int(_parse_dim(w, DEFAULT_NODE_WIDTH))}px"
        if h is not None and not style.get("height"):
            style["height"] = f"{int(_parse_dim(h, DEFAULT_NODE_HEIGHT))}px"
        pos = raw.get("position") or {"x": 0, "y": 0}
        data = raw.get("data") or {}
        # Levels must never accept wires even if the component resets connectable
        alfredo_type = (data or {}).get("alfredo_type") if isinstance(data, dict) else None
        connectable = bool(getattr(sn, "connectable", True))
        if alfredo_type == NODE_TYPE_LEVEL:
            connectable = False
        nodes_out.append(
            StreamlitFlowNode(
                id=nid,
                pos=(float(pos.get("x") or 0), float(pos.get("y") or 0)),
                data=data,
                node_type=getattr(sn, "type", None) or "default",
                source_position=getattr(sn, "source_position", None) or "right",
                target_position=getattr(sn, "target_position", None) or "left",
                hidden=bool(getattr(sn, "hidden", False)),
                selected=bool(getattr(sn, "selected", False)),
                dragging=bool(getattr(sn, "dragging", False)),
                draggable=bool(getattr(sn, "draggable", True)),
                selectable=bool(getattr(sn, "selectable", True)),
                connectable=connectable,
                resizing=bool(getattr(sn, "resizing", True)),
                deletable=bool(getattr(sn, "deletable", True)),
                z_index=float(getattr(sn, "z_index", 0) or 0),
                focusable=bool(getattr(sn, "focusable", True)),
                style=style,
            )
        )
    return StreamlitFlowState(
        nodes_out,
        list(getattr(new_state, "edges", []) or []),
        selected_id=getattr(new_state, "selected_id", None),
        timestamp=getattr(new_state, "timestamp", 0),
    )
