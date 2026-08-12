"""
Function-block workflow graph model for Alfredo.

Node types (industrial schema):
  input | task | batch_loop | hitl | export | loop | level

Structured loops wrap a body (nodes / levels / whole workflow) without allowing
arbitrary cyclic edges between unrelated workflow tasks.

Backward compatible with legacy task_ids_json (plain ints and untyped dicts).
Editing lifecycle: normalize (load) → mutate → validate → canonicalize (save).
Runtime: desugar hitl/export/input → executable DAG + metadata overlays;
         expand structured loops into executable loop steps.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

# --- Block types -------------------------------------------------------------

NODE_TYPE_INPUT = "input"
NODE_TYPE_TASK = "task"
NODE_TYPE_BATCH = "batch_loop"
NODE_TYPE_HITL = "hitl"
NODE_TYPE_EXPORT = "export"
NODE_TYPE_LOOP = "loop"
NODE_TYPE_LEVEL = "level"

NODE_TYPES = (
    NODE_TYPE_INPUT,
    NODE_TYPE_TASK,
    NODE_TYPE_BATCH,
    NODE_TYPE_HITL,
    NODE_TYPE_EXPORT,
    NODE_TYPE_LOOP,
    NODE_TYPE_LEVEL,
)

EXECUTABLE_TYPES = {NODE_TYPE_TASK, NODE_TYPE_BATCH, NODE_TYPE_LOOP}
CONTROL_TYPES = {NODE_TYPE_HITL, NODE_TYPE_EXPORT, NODE_TYPE_INPUT, NODE_TYPE_LEVEL}
CONTENT_TYPES = {NODE_TYPE_TASK, NODE_TYPE_BATCH, NODE_TYPE_HITL, NODE_TYPE_EXPORT, NODE_TYPE_INPUT}

LOOP_MODE_FOR_EACH = "for_each"
LOOP_MODE_WHILE = "while"
LOOP_MODES = (LOOP_MODE_FOR_EACH, LOOP_MODE_WHILE)

LOOP_SCOPE_NODES = "nodes"
LOOP_SCOPE_LEVELS = "levels"
LOOP_SCOPE_WORKFLOW = "workflow"
LOOP_SCOPES = (LOOP_SCOPE_NODES, LOOP_SCOPE_LEVELS, LOOP_SCOPE_WORKFLOW)

INPUT_REF = "input"  # special inputs_map.from value for run inputs


def new_node_id(prefix: str = "node") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def infer_node_type(step: Any) -> str:
    if isinstance(step, int):
        return NODE_TYPE_TASK
    if not isinstance(step, dict):
        return NODE_TYPE_TASK
    explicit = (step.get("type") or "").strip().lower()
    if explicit in NODE_TYPES:
        return explicit
    if "task_ids" in step and "task_id" not in step:
        return NODE_TYPE_BATCH
    if step.get("task_id") is not None:
        return NODE_TYPE_TASK
    return NODE_TYPE_TASK


def normalize_node(step: Any, index: int = 0, prev_id: Optional[str] = None) -> Dict[str, Any]:
    """Normalize a single graph step to a typed node dict."""
    if isinstance(step, int):
        node = {
            "id": new_node_id(f"node_{index}"),
            "type": NODE_TYPE_TASK,
            "task_id": step,
            "depends_on": [prev_id] if prev_id else [],
            "execution_level": 1,
            "model_tier": "default",
            "inputs_map": {},
        }
        return node

    if not isinstance(step, dict):
        raise TypeError(f"Unsupported graph step type: {type(step)!r}")

    node = copy.deepcopy(step)
    node.pop("role", None)  # legacy per-node supervisor

    if "id" not in node or not node["id"]:
        node["id"] = new_node_id(f"node_{index}")
    node.setdefault("depends_on", [])
    if not isinstance(node["depends_on"], list):
        node["depends_on"] = list(node["depends_on"] or [])
    node.setdefault("execution_level", 1)
    try:
        node["execution_level"] = max(1, int(node["execution_level"]))
    except (TypeError, ValueError):
        node["execution_level"] = 1
    node.setdefault("model_tier", "default")
    node.setdefault("inputs_map", {})
    if not isinstance(node["inputs_map"], dict):
        node["inputs_map"] = {}

    ntype = infer_node_type(node)
    node["type"] = ntype

    if ntype == NODE_TYPE_BATCH:
        node.setdefault("task_ids", [])
        node.setdefault("batch_size", 5)
        node.setdefault("source_variable", "{previous_result}")
    elif ntype == NODE_TYPE_HITL:
        node.setdefault("message", "Please review and approve before continuing.")
        node.setdefault("gate_mode", "after_parents")  # validate upstream outputs
    elif ntype == NODE_TYPE_EXPORT:
        node.setdefault("exports", [])
        node.setdefault("export_instructions", "")
    elif ntype == NODE_TYPE_INPUT:
        node.setdefault("keys", [])  # optional declared run-input keys
    elif ntype == NODE_TYPE_LEVEL:
        try:
            node["level"] = max(1, int(node.get("level") or node.get("execution_level") or 1))
        except (TypeError, ValueError):
            node["level"] = 1
        node["execution_level"] = node["level"]
        node["depends_on"] = []
        node["inputs_map"] = {}
        ui = node.get("canvas_ui") if isinstance(node.get("canvas_ui"), dict) else {}
        ui.setdefault("width", 280)
        ui.setdefault("height", 520)
        node["canvas_ui"] = ui
    elif ntype == NODE_TYPE_LOOP:
        mode = (node.get("loop_mode") or LOOP_MODE_FOR_EACH).strip().lower()
        node["loop_mode"] = mode if mode in LOOP_MODES else LOOP_MODE_FOR_EACH
        scope = (node.get("scope") or LOOP_SCOPE_NODES).strip().lower()
        node["scope"] = scope if scope in LOOP_SCOPES else LOOP_SCOPE_NODES
        node.setdefault("body_node_ids", [])
        node.setdefault("body_levels", [])
        node.setdefault("batch_size", 5)
        node.setdefault("source_variable", "{previous_result}")
        node.setdefault("max_iterations", 10)
        node.setdefault("exit_on_hitl", True)
        exit_cond = node.get("exit_condition")
        if not isinstance(exit_cond, dict):
            exit_cond = {}
        om = exit_cond.get("output_match")
        if not isinstance(om, dict):
            om = {}
        if node.get("exit_condition_node") and not om.get("source_node"):
            om["source_node"] = str(node.pop("exit_condition_node")).strip()
        if node.get("exit_condition_pattern") and not om.get("pattern"):
            om["pattern"] = str(node.pop("exit_condition_pattern")).strip()
        exit_cond["output_match"] = {
            "source_node": str(om.get("source_node") or "").strip(),
            "pattern": str(om.get("pattern") or "").strip(),
        }
        node["exit_condition"] = exit_cond
        if not isinstance(node["body_node_ids"], list):
            node["body_node_ids"] = list(node["body_node_ids"] or [])
        if not isinstance(node["body_levels"], list):
            node["body_levels"] = list(node["body_levels"] or [])
    # task: task_id may be missing if orphaned

    return node


def resolve_loop_body_ids(graph: Sequence[Dict[str, Any]], loop_node: Dict[str, Any]) -> List[str]:
    """Resolve which node ids are inside a structured loop (order preserved)."""
    by_id = {n["id"]: n for n in graph if n.get("id")}
    scope = loop_node.get("scope") or LOOP_SCOPE_NODES
    loop_id = loop_node.get("id")

    if scope == LOOP_SCOPE_WORKFLOW:
        return [
            n["id"]
            for n in graph
            if n.get("id") != loop_id
            and n.get("type") in (NODE_TYPE_TASK, NODE_TYPE_BATCH, NODE_TYPE_HITL)
        ]

    if scope == LOOP_SCOPE_LEVELS:
        levels = {int(x) for x in (loop_node.get("body_levels") or [])}
        return [
            n["id"]
            for n in graph
            if n.get("id") != loop_id
            and int(n.get("execution_level", 1)) in levels
            and n.get("type") in (NODE_TYPE_TASK, NODE_TYPE_BATCH, NODE_TYPE_HITL)
        ]

    out: List[str] = []
    for nid in loop_node.get("body_node_ids") or []:
        if nid and nid in by_id and nid != loop_id and nid not in out:
            out.append(nid)
    return out


def materialize_export_from_header(
    graph: List[Dict[str, Any]],
    expected_exports: Optional[Sequence[str]] = None,
    export_instructions: str = "",
) -> List[Dict[str, Any]]:
    """If workflow has flat expected_exports and no export node, add a draft export block."""
    exports = list(expected_exports or [])
    if not exports:
        return graph
    if any(n.get("type") == NODE_TYPE_EXPORT for n in graph):
        return graph

    # Depend on all sink executable nodes (no other node depends on them… or max level)
    executable_ids = [n["id"] for n in graph if n.get("type") in EXECUTABLE_TYPES]
    referenced: Set[str] = set()
    for n in graph:
        referenced.update(n.get("depends_on") or [])
    sinks = [i for i in executable_ids if i not in referenced] or executable_ids

    max_level = max((n.get("execution_level", 1) for n in graph), default=1)
    export_node = {
        "id": new_node_id("export"),
        "type": NODE_TYPE_EXPORT,
        "depends_on": list(sinks),
        "execution_level": int(max_level) + 1,
        "model_tier": "default",
        "inputs_map": {},
        "exports": exports,
        "export_instructions": export_instructions or "",
    }
    return list(graph) + [export_node]


def normalize_graph(
    raw: Any,
    *,
    expected_exports: Optional[Sequence[str]] = None,
    export_instructions: str = "",
    materialize_export: bool = True,
) -> List[Dict[str, Any]]:
    """Load-path: legacy ints/dicts → typed node list."""
    if raw is None:
        steps: List[Any] = []
    elif isinstance(raw, str):
        try:
            steps = json.loads(raw) or []
        except json.JSONDecodeError:
            steps = []
    else:
        steps = list(raw or [])

    graph: List[Dict[str, Any]] = []
    prev_id: Optional[str] = None
    for i, step in enumerate(steps):
        node = normalize_node(step, index=i, prev_id=prev_id if isinstance(step, int) else None)
        graph.append(node)
        prev_id = node["id"]

    if materialize_export:
        graph = materialize_export_from_header(graph, expected_exports, export_instructions)
    return graph


def node_by_id(graph: Sequence[Dict[str, Any]], node_id: str) -> Optional[Dict[str, Any]]:
    for n in graph:
        if n.get("id") == node_id:
            return n
    return None


# --- Mutate ------------------------------------------------------------------

def remove_node(graph: List[Dict[str, Any]], node_id: str) -> List[Dict[str, Any]]:
    """Remove a node and repair depends_on + inputs_map wires + loop bodies."""
    out: List[Dict[str, Any]] = []
    for n in graph:
        if n.get("id") == node_id:
            continue
        node = copy.deepcopy(n)
        deps = [d for d in (node.get("depends_on") or []) if d != node_id]
        node["depends_on"] = deps
        imap = node.get("inputs_map") or {}
        if isinstance(imap, dict):
            cleaned = {}
            for port, binding in imap.items():
                if isinstance(binding, dict) and binding.get("from") == node_id:
                    continue
                cleaned[port] = binding
            node["inputs_map"] = cleaned
        if node.get("type") == NODE_TYPE_LOOP:
            node["body_node_ids"] = [x for x in (node.get("body_node_ids") or []) if x != node_id]
        out.append(node)
    return out


def add_node(graph: List[Dict[str, Any]], node: Dict[str, Any]) -> List[Dict[str, Any]]:
    normalized = normalize_node(node, index=len(graph))
    return list(graph) + [normalized]


def set_data_wire(
    graph: List[Dict[str, Any]],
    node_id: str,
    port: str,
    source_from: str,
    key: str = "",
    *,
    sync_depends: bool = True,
) -> List[Dict[str, Any]]:
    """Set inputs_map[port]; optionally ensure control depends_on includes source."""
    out = copy.deepcopy(list(graph))
    target = node_by_id(out, node_id)
    if not target:
        return out
    imap = dict(target.get("inputs_map") or {})
    imap[port] = {"from": source_from, "key": key or ""}
    target["inputs_map"] = imap
    if sync_depends and source_from and source_from != INPUT_REF:
        deps = list(target.get("depends_on") or [])
        if source_from not in deps:
            deps.append(source_from)
        target["depends_on"] = deps
    return out


def clear_data_wire(
    graph: List[Dict[str, Any]],
    node_id: str,
    port: str,
    *,
    prune_depends: bool = False,
) -> List[Dict[str, Any]]:
    out = copy.deepcopy(list(graph))
    target = node_by_id(out, node_id)
    if not target:
        return out
    imap = dict(target.get("inputs_map") or {})
    binding = imap.pop(port, None)
    target["inputs_map"] = imap
    if prune_depends and isinstance(binding, dict):
        src = binding.get("from")
        if src and src != INPUT_REF:
            still = any(
                isinstance(b, dict) and b.get("from") == src
                for b in (target.get("inputs_map") or {}).values()
            )
            if not still:
                target["depends_on"] = [d for d in (target.get("depends_on") or []) if d != src]
    return out


# --- Validate ----------------------------------------------------------------

def _has_cycle(graph: Sequence[Dict[str, Any]]) -> bool:
    ids = {n["id"] for n in graph if n.get("id")}
    adj: Dict[str, List[str]] = {i: [] for i in ids}
    for n in graph:
        nid = n.get("id")
        if not nid:
            continue
        for d in n.get("depends_on") or []:
            if d in ids:
                adj[d].append(nid)

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {i: WHITE for i in ids}

    def dfs(u: str) -> bool:
        color[u] = GRAY
        for v in adj[u]:
            if color[v] == GRAY:
                return True
            if color[v] == WHITE and dfs(v):
                return True
        color[u] = BLACK
        return False

    return any(dfs(i) for i in ids if color[i] == WHITE)


def _validate_structured_loops(
    graph: Sequence[Dict[str, Any]],
) -> Tuple[List[str], List[str]]:
    """Ensure loops are scoped containers — no impossible cross-task cycles."""
    errors: List[str] = []
    warnings: List[str] = []
    by_id = {n["id"]: n for n in graph if n.get("id")}
    loops = [n for n in graph if n.get("type") == NODE_TYPE_LOOP]
    claimed: Dict[str, str] = {}

    for loop in loops:
        lid = loop["id"]
        body = resolve_loop_body_ids(graph, loop)
        if not body and loop.get("scope") != LOOP_SCOPE_WORKFLOW:
            errors.append(f"Loop {lid}: empty body (pick nodes or levels).")
        for nid in body:
            if nid in claimed:
                errors.append(
                    f"Node {nid} is in two loops ({claimed[nid]} and {lid}); "
                    "impossible overlapping loops."
                )
            else:
                claimed[nid] = lid
            member = by_id.get(nid)
            if member and member.get("type") == NODE_TYPE_LOOP:
                errors.append(f"Loop {lid}: nested loop nodes are not allowed ({nid}).")
            if member and member.get("type") == NODE_TYPE_EXPORT:
                errors.append(f"Loop {lid}: export nodes cannot be inside a loop body.")

        body_set = set(body)
        body_graph = []
        for nid in body:
            m = copy.deepcopy(by_id[nid])
            m["depends_on"] = [d for d in (m.get("depends_on") or []) if d in body_set]
            body_graph.append(m)
        if body_graph and _has_cycle(body_graph):
            errors.append(f"Loop {lid}: body has an internal cycle (impossible loop).")

        for nid in body:
            m = by_id[nid]
            for d in m.get("depends_on") or []:
                if d not in body_set and d != lid and d in by_id:
                    if d not in (loop.get("depends_on") or []):
                        warnings.append(
                            f"Loop {lid}: body node {nid} depends on external {d}; "
                            f"prefer wiring {d} → loop, then loop entry → body."
                        )
        for n in graph:
            if n["id"] in body_set or n["id"] == lid:
                continue
            for d in n.get("depends_on") or []:
                if d in body_set:
                    errors.append(
                        f"Impossible cross-edge: {n['id']} depends_on body node {d}. "
                        f"Depend on loop {lid} instead (structured exit)."
                    )

        if loop.get("loop_mode") == LOOP_MODE_FOR_EACH and not (loop.get("source_variable") or "").strip():
            warnings.append(f"Loop {lid}: for_each without source_variable.")
        if loop.get("loop_mode") == LOOP_MODE_WHILE:
            try:
                if int(loop.get("max_iterations") or 0) < 1:
                    errors.append(f"Loop {lid}: while requires max_iterations >= 1.")
            except (TypeError, ValueError):
                errors.append(f"Loop {lid}: invalid max_iterations.")
            ec = loop.get("exit_condition") or {}
            om = ec.get("output_match") if isinstance(ec, dict) else {}
            if isinstance(om, dict) and om.get("pattern") and not om.get("source_node"):
                errors.append(f"Loop {lid}: output_match requires source_node.")
            if not loop.get("exit_on_hitl") and not (isinstance(om, dict) and om.get("pattern")):
                warnings.append(
                    f"Loop {lid}: while without exit_on_hitl or output_match runs until max_iterations."
                )

    if loops:
        outer: List[Dict[str, Any]] = []
        hidden = set(claimed.keys())
        for n in graph:
            if n["id"] in hidden:
                continue
            nn = copy.deepcopy(n)
            new_deps = []
            for d in nn.get("depends_on") or []:
                if d in claimed:
                    new_deps.append(claimed[d])
                else:
                    new_deps.append(d)
            seen = []
            for d in new_deps:
                if d not in seen and d != nn["id"]:
                    seen.append(d)
            nn["depends_on"] = seen
            outer.append(nn)
        if _has_cycle(outer):
            errors.append("Structured loops still leave a cycle in the outer workflow graph.")

    return errors, warnings


def validate_graph(
    graph: Sequence[Dict[str, Any]],
    *,
    task_id_map: Optional[Dict[int, Any]] = None,
) -> Tuple[List[str], List[str]]:
    """
    Returns (errors, warnings).
    errors block save; warnings are soft.
    """
    errors: List[str] = []
    warnings: List[str] = []
    if not graph:
        errors.append("Graph is empty.")
        return errors, warnings

    ids = [n.get("id") for n in graph]
    if any(not i for i in ids):
        errors.append("Every node must have an id.")
    if len(ids) != len(set(ids)):
        errors.append("Duplicate node ids.")

    id_set = {i for i in ids if i}
    executable = [n for n in graph if n.get("type") in EXECUTABLE_TYPES]
    if not executable:
        errors.append("At least one executable block (task, batch_loop, or loop) is required.")

    body_claimed: Set[str] = set()
    for n in graph:
        if n.get("type") == NODE_TYPE_LOOP:
            body_claimed.update(resolve_loop_body_ids(graph, n))

    for n in graph:
        nid = n.get("id") or "?"
        ntype = n.get("type")
        if ntype not in NODE_TYPES:
            errors.append(f"Node {nid}: unknown type {ntype!r}.")
        for d in n.get("depends_on") or []:
            if d not in id_set:
                errors.append(f"Node {nid}: depends_on unknown id {d!r}.")
        imap = n.get("inputs_map") or {}
        if isinstance(imap, dict):
            for port, binding in imap.items():
                if not isinstance(binding, dict):
                    errors.append(f"Node {nid}: inputs_map[{port!r}] must be an object.")
                    continue
                src = binding.get("from")
                if not src:
                    errors.append(f"Node {nid}: inputs_map[{port!r}] missing 'from'.")
                elif src != INPUT_REF and src not in id_set:
                    errors.append(f"Node {nid}: inputs_map[{port!r}] from unknown {src!r}.")

        if ntype == NODE_TYPE_TASK:
            tid = n.get("task_id")
            if tid is None:
                errors.append(f"Node {nid}: task block missing task_id.")
            elif task_id_map is not None and int(tid) not in task_id_map:
                warnings.append(f"Node {nid}: task_id {tid} not found (deleted?).")
            elif task_id_map is not None:
                task = task_id_map.get(int(tid)) or {}
                req = task.get("required_inputs") or task.get("required_inputs_json") or []
                if isinstance(req, str):
                    try:
                        req = json.loads(req) or []
                    except json.JSONDecodeError:
                        req = []
                if req and not (n.get("inputs_map") or {}):
                    warnings.append(
                        f"Node {nid}: task has required_inputs but no data wires (will use {{previous_result}})."
                    )
        elif ntype == NODE_TYPE_BATCH:
            if not (n.get("task_ids") or []):
                errors.append(f"Node {nid}: batch_loop needs inner task_ids.")
            src_var = (n.get("source_variable") or "").strip()
            if not src_var:
                warnings.append(f"Node {nid}: batch_loop has empty source_variable.")
        elif ntype == NODE_TYPE_EXPORT:
            if not (n.get("exports") or []):
                warnings.append(f"Node {nid}: export block has no export formats.")
        elif ntype == NODE_TYPE_LEVEL:
            try:
                if int(n.get("level") or 0) < 1:
                    errors.append(f"Level node {nid}: level must be >= 1.")
            except (TypeError, ValueError):
                errors.append(f"Level node {nid}: invalid level.")
        elif ntype == NODE_TYPE_LOOP:
            if n.get("loop_mode") not in LOOP_MODES:
                errors.append(f"Node {nid}: invalid loop_mode {n.get('loop_mode')!r}.")
            if n.get("scope") not in LOOP_SCOPES:
                errors.append(f"Node {nid}: invalid loop scope {n.get('scope')!r}.")

    if not any(n.get("type") == NODE_TYPE_LOOP for n in graph):
        cycle_graph = [n for n in graph if n.get("type") != NODE_TYPE_LEVEL]
        if _has_cycle(cycle_graph):
            errors.append("Graph has a cycle in depends_on.")
    else:
        flat_for_cycle = [n for n in graph if n["id"] not in body_claimed and n.get("type") != NODE_TYPE_LEVEL]
        # loop validator handles outer cycle; still catch naive cycles among non-body
        if flat_for_cycle and _has_cycle(flat_for_cycle):
            # may be false positive if deps point into body — structured validator covers that
            pass

    loop_errs, loop_warns = _validate_structured_loops(graph)
    errors.extend(loop_errs)
    warnings.extend(loop_warns)

    return errors, warnings


def sync_header_from_export_nodes(
    graph: Sequence[Dict[str, Any]],
    fallback_exports: Optional[Sequence[str]] = None,
    fallback_instructions: str = "",
) -> Tuple[List[str], str]:
    """Derive expected_exports + instructions from export nodes (for DB header fields)."""
    exports: List[str] = []
    instructions_parts: List[str] = []
    for n in graph:
        if n.get("type") != NODE_TYPE_EXPORT:
            continue
        for e in n.get("exports") or []:
            if e and e not in exports:
                exports.append(e)
        instr = (n.get("export_instructions") or "").strip()
        if instr:
            instructions_parts.append(instr)
    if not exports and fallback_exports:
        exports = list(fallback_exports)
    instructions = "\n\n".join(instructions_parts) if instructions_parts else (fallback_instructions or "")
    return exports, instructions


def canonicalize_graph(graph: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sort by execution_level then stable order; ensure typed nodes."""
    normalized = [normalize_node(n, index=i) for i, n in enumerate(graph)]
    # stable sort: level, then original index
    indexed = list(enumerate(normalized))
    indexed.sort(key=lambda pair: (pair[1].get("execution_level", 1), pair[0]))
    return [n for _, n in indexed]


# --- Runtime desugar ---------------------------------------------------------

def evaluate_loop_exit_condition(
    *,
    exit_on_hitl: bool,
    exit_condition: Optional[Dict[str, Any]],
    node_outputs: Dict[str, str],
    hitl_approved_nodes: Optional[Set[str]] = None,
) -> bool:
    """
    Return True when a structured while-loop should stop.

    Supports:
    - exit_on_hitl: any body node with HITL completed (not aborted)
    - exit_condition.output_match: regex on a body node's output text
    """
    hitl_approved_nodes = hitl_approved_nodes or set()
    if exit_on_hitl and hitl_approved_nodes:
        return True
    ec = exit_condition or {}
    om = ec.get("output_match") if isinstance(ec, dict) else {}
    if isinstance(om, dict):
        src = (om.get("source_node") or "").strip()
        pattern = (om.get("pattern") or "").strip()
        if src and pattern:
            text = node_outputs.get(src) or ""
            try:
                if re.search(pattern, text, re.IGNORECASE | re.DOTALL):
                    return True
            except re.error:
                if pattern in text:
                    return True
    return False


def desugar_graph_for_runtime(
    graph: Sequence[Dict[str, Any]],
    *,
    workflow_expected_exports: Optional[Sequence[str]] = None,
    workflow_export_instructions: str = "",
) -> Dict[str, Any]:
    """
    Translate control blocks into overlays the current scheduler understands.

    - input nodes: dropped (virtual sources for inputs_map only)
    - hitl nodes: mark human_validation on upstream task parents; rewire children to parents
    - export nodes: fold into expected_exports / export_instructions; rewire & drop
    - loop nodes: kept as executable containers with nested body_steps
    """
    full = [normalize_node(n, index=i) for i, n in enumerate(graph)]
    by_id = {n["id"]: n for n in full}

    hitl_ids = {n["id"] for n in full if n.get("type") == NODE_TYPE_HITL}
    export_ids = {n["id"] for n in full if n.get("type") == NODE_TYPE_EXPORT}
    input_ids = {n["id"] for n in full if n.get("type") == NODE_TYPE_INPUT}
    level_ids = {n["id"] for n in full if n.get("type") == NODE_TYPE_LEVEL}

    loop_body_map: Dict[str, List[str]] = {}
    body_to_loop: Dict[str, str] = {}
    for n in full:
        if n.get("type") != NODE_TYPE_LOOP:
            continue
        body = resolve_loop_body_ids(full, n)
        loop_body_map[n["id"]] = body
        for bid in body:
            body_to_loop[bid] = n["id"]

    skip = hitl_ids | export_ids | input_ids | level_ids
    skip_top = set(skip) | set(body_to_loop.keys())

    human_validation_task_ids: Set[int] = set()
    for hid in hitl_ids:
        hn = by_id[hid]
        for parent_id in hn.get("depends_on") or []:
            parent = by_id.get(parent_id)
            if not parent:
                continue
            if parent.get("type") == NODE_TYPE_TASK and parent.get("task_id") is not None:
                human_validation_task_ids.add(int(parent["task_id"]))
            elif parent.get("type") == NODE_TYPE_BATCH:
                for tid in parent.get("task_ids") or []:
                    if tid is not None:
                        human_validation_task_ids.add(int(tid))

    expected_exports, export_instructions = sync_header_from_export_nodes(
        full, workflow_expected_exports, workflow_export_instructions
    )

    def resolve_parents(node_id: str, seen: Optional[Set[str]] = None) -> List[str]:
        seen = seen or set()
        if node_id in seen:
            return []
        seen.add(node_id)
        n = by_id.get(node_id)
        if not n:
            return []
        if n.get("type") in (NODE_TYPE_TASK, NODE_TYPE_BATCH, NODE_TYPE_LOOP):
            return [node_id]
        if n.get("type") == NODE_TYPE_INPUT:
            return []
        if n.get("type") == NODE_TYPE_LEVEL:
            return []
        resolved: List[str] = []
        for p in n.get("depends_on") or []:
            resolved.extend(resolve_parents(p, seen))
        out: List[str] = []
        for x in resolved:
            if x not in out:
                out.append(x)
        return out

    def rewrite_step(n: Dict[str, Any]) -> Dict[str, Any]:
        step = copy.deepcopy(n)
        new_deps: List[str] = []
        for d in step.get("depends_on") or []:
            if d in body_to_loop and body_to_loop[d] != step.get("id"):
                lid = body_to_loop[d]
                if lid not in new_deps:
                    new_deps.append(lid)
            elif d in skip:
                new_deps.extend(resolve_parents(d))
            elif d in by_id and by_id[d].get("type") == NODE_TYPE_INPUT:
                continue
            else:
                new_deps.append(d)
        seen_d: List[str] = []
        for d in new_deps:
            if d not in seen_d and d != step.get("id"):
                seen_d.append(d)
        step["depends_on"] = seen_d

        imap = copy.deepcopy(step.get("inputs_map") or {})
        rewritten: Dict[str, Any] = {}
        for port, binding in imap.items():
            if not isinstance(binding, dict):
                continue
            src = binding.get("from")
            if src == INPUT_REF:
                rewritten[port] = binding
            elif src in body_to_loop and body_to_loop[src] != step.get("id"):
                rewritten[port] = {"from": body_to_loop[src], "key": binding.get("key") or ""}
            elif src in skip:
                parents = resolve_parents(src)
                if parents:
                    rewritten[port] = {"from": parents[-1], "key": binding.get("key") or ""}
                else:
                    rewritten[port] = {"from": INPUT_REF, "key": binding.get("key") or ""}
            else:
                rewritten[port] = binding
        step["inputs_map"] = rewritten
        return step

    executable_steps: List[Dict[str, Any]] = []
    inputs_map_by_node: Dict[str, Dict[str, Any]] = {}

    for n in full:
        if n["id"] in skip_top:
            continue
        step = rewrite_step(n)
        if step.get("type") == NODE_TYPE_LOOP:
            body_steps = []
            for bid in loop_body_map.get(step["id"], []):
                bn = by_id.get(bid)
                if not bn or bn.get("type") in (NODE_TYPE_EXPORT, NODE_TYPE_INPUT, NODE_TYPE_HITL):
                    continue
                body_steps.append(rewrite_step(bn))
            step["body_steps"] = body_steps
            step["is_structured_loop"] = True
        inputs_map_by_node[step["id"]] = step.get("inputs_map") or {}
        executable_steps.append(step)

    return {
        "executable_steps": executable_steps,
        "human_validation_task_ids": sorted(human_validation_task_ids),
        "expected_exports": expected_exports,
        "export_instructions": export_instructions,
        "inputs_map_by_node": inputs_map_by_node,
        "skipped_nodes": sorted(skip_top),
    }


def extract_output_key(output: str, key: str) -> str:
    """Pull a field from JSON output or return full text if key empty / not found."""
    if not key:
        return output or ""
    text = output or ""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        # try embedded JSON object
        try:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                data = json.loads(text[start : end + 1])
            else:
                return text
        except Exception:
            return text
    if isinstance(data, dict):
        if key in data:
            val = data[key]
            return val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
        # case-insensitive
        lower = {str(k).lower(): v for k, v in data.items()}
        if key.lower() in lower:
            val = lower[key.lower()]
            return val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
    return text


def resolve_inputs_map_values(
    inputs_map: Optional[Dict[str, Any]],
    *,
    node_outputs: Dict[str, str],
    run_inputs: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """Resolve wired ports to string values for interpolation."""
    run_inputs = run_inputs or {}
    resolved: Dict[str, str] = {}
    if not inputs_map:
        return resolved
    for port, binding in inputs_map.items():
        if not isinstance(binding, dict):
            continue
        src = binding.get("from")
        key = binding.get("key") or ""
        if src == INPUT_REF:
            raw = run_inputs.get(key) if key else run_inputs.get("user_input", "")
            if raw is None and key:
                raw = run_inputs.get(key, "")
            resolved[port] = "" if raw is None else (raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False))
        else:
            parent_out = node_outputs.get(src, "")
            resolved[port] = extract_output_key(parent_out, key)
    return resolved


# --- Graphviz ----------------------------------------------------------------

def graph_to_dot(
    graph: Sequence[Dict[str, Any]],
    *,
    task_labels: Optional[Dict[int, str]] = None,
) -> str:
    """Build Graphviz DOT for mini-map preview."""
    task_labels = task_labels or {}
    lines = [
        "digraph AlfredoWorkflow {",
        "  rankdir=LR;",
        "  node [shape=box, style=rounded, fontname=Helvetica];",
        "  edge [fontname=Helvetica, fontsize=9];",
    ]
    shape_map = {
        NODE_TYPE_INPUT: "parallelogram",
        NODE_TYPE_TASK: "box",
        NODE_TYPE_BATCH: "box3d",
        NODE_TYPE_HITL: "diamond",
        NODE_TYPE_EXPORT: "folder",
        NODE_TYPE_LOOP: "component",
        NODE_TYPE_LEVEL: "box",
    }
    for n in graph:
        nid = n.get("id") or "unknown"
        ntype = n.get("type") or NODE_TYPE_TASK
        label = ntype
        if ntype == NODE_TYPE_TASK:
            tid = n.get("task_id")
            tname = task_labels.get(int(tid), f"task_{tid}") if tid is not None else "task?"
            label = f"{tname}\\n({nid})"
        elif ntype == NODE_TYPE_BATCH:
            label = f"batch×{n.get('batch_size', '?')}\\n({nid})"
        elif ntype == NODE_TYPE_HITL:
            label = f"HITL\\n({nid})"
        elif ntype == NODE_TYPE_EXPORT:
            ex = ",".join(n.get("exports") or []) or "export"
            label = f"{ex}\\n({nid})"
        elif ntype == NODE_TYPE_INPUT:
            label = f"INPUT\\n({nid})"
        elif ntype == NODE_TYPE_LOOP:
            label = f"LOOP {n.get('loop_mode')}\\n({nid})"
        elif ntype == NODE_TYPE_LEVEL:
            label = f"LEVEL {n.get('level', '?')}\\n({nid})"
        shape = shape_map.get(ntype, "box")
        safe_id = nid.replace("-", "_").replace(".", "_")
        lines.append(f'  "{safe_id}" [label="{label}", shape={shape}];')

    # Control edges
    for n in graph:
        nid = n.get("id") or ""
        safe_to = nid.replace("-", "_").replace(".", "_")
        for d in n.get("depends_on") or []:
            safe_from = d.replace("-", "_").replace(".", "_")
            lines.append(f'  "{safe_from}" -> "{safe_to}";')

        # Data wires as dashed labeled edges
        for port, binding in (n.get("inputs_map") or {}).items():
            if not isinstance(binding, dict):
                continue
            src = binding.get("from")
            if not src or src == INPUT_REF:
                if src == INPUT_REF:
                    lines.append(
                        f'  "run_input" [label="run inputs", shape=parallelogram, style=dashed];'
                    )
                    lines.append(
                        f'  "run_input" -> "{safe_to}" [style=dashed, label="{port}"];'
                    )
                continue
            safe_from = src.replace("-", "_").replace(".", "_")
            key = binding.get("key") or ""
            elabel = f"{port}" + (f":{key}" if key else "")
            lines.append(
                f'  "{safe_from}" -> "{safe_to}" [style=dashed, label="{elabel}"];'
            )

    lines.append("}")
    return "\n".join(lines)


def collect_task_ids(graph: Sequence[Any]) -> List[int]:
    """All numeric task IDs referenced by the graph (incl. batch inners)."""
    out: List[int] = []
    seen: Set[int] = set()
    for step in graph or []:
        if isinstance(step, int):
            if step not in seen:
                seen.add(step)
                out.append(step)
            continue
        if not isinstance(step, dict):
            continue
        ntype = infer_node_type(step)
        if ntype in (NODE_TYPE_INPUT, NODE_TYPE_HITL, NODE_TYPE_EXPORT, NODE_TYPE_LOOP, NODE_TYPE_LEVEL):
            continue
        if ntype == NODE_TYPE_BATCH:
            for tid in step.get("task_ids") or []:
                if tid is not None and int(tid) not in seen:
                    seen.add(int(tid))
                    out.append(int(tid))
        elif step.get("task_id") is not None:
            tid = int(step["task_id"])
            if tid not in seen:
                seen.add(tid)
                out.append(tid)
    return out


def next_level_number(graph: Sequence[Dict[str, Any]]) -> int:
    """Smallest positive level index not already used by a level container."""
    used = set()
    for n in graph or []:
        if not isinstance(n, dict):
            continue
        if n.get("type") == NODE_TYPE_LEVEL:
            try:
                used.add(int(n.get("level") or 0))
            except (TypeError, ValueError):
                pass
    n = 1
    while n in used:
        n += 1
    return n


def make_level_node(level: int, *, x: Optional[float] = None) -> Dict[str, Any]:
    """Create a level column container node."""
    lvl = max(1, int(level))
    col_gap = 300.0
    return {
        "id": new_node_id("level"),
        "type": NODE_TYPE_LEVEL,
        "level": lvl,
        "execution_level": lvl,
        "depends_on": [],
        "inputs_map": {},
        "canvas_ui": {
            "width": 280,
            "height": 520,
            "x": float(x if x is not None else (lvl - 1) * col_gap),
            "y": 0.0,
        },
    }


def compute_loop_frame_ui(
    body_nodes: Sequence[Dict[str, Any]],
    *,
    padding: float = 28.0,
) -> Dict[str, float]:
    """Bounding box for a loop overlay from body node canvas_ui positions/sizes."""
    if not body_nodes:
        return {"x": 40.0, "y": 40.0, "width": 320.0, "height": 220.0}
    xs, ys, x2s, y2s = [], [], [], []
    for n in body_nodes:
        ui = n.get("canvas_ui") if isinstance(n.get("canvas_ui"), dict) else {}
        x = float(ui.get("x") or 0)
        y = float(ui.get("y") or 0)
        w = float(ui.get("width") or 220)
        h = float(ui.get("height") or 120)
        xs.append(x)
        ys.append(y)
        x2s.append(x + w)
        y2s.append(y + h)
    left = min(xs) - padding
    top = min(ys) - padding - 36
    return {
        "x": left,
        "y": top,
        "width": max(200.0, max(x2s) - left + padding),
        "height": max(160.0, max(y2s) - top + padding),
    }
