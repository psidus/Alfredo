"""
Typed node execution: ``llm``, ``tool`` and ``router`` blocks.

The graph engine historically knew only one way to run work: a CrewAI agent +
task (``task`` block) or a batch/loop over those tasks. That forces a full agent
(role, backstory, tools, memory) even for a single completion or a deterministic
function call.

This module adds three lighter, typed executors that share one contract:

* ``llm``    — one structured completion (optionally forced JSON).
* ``tool``   — one deterministic call to a whitelisted tool.
* ``router`` — pick a route from rules, or from an LLM classification.
* ``agent``  — alias of the existing ``task`` block (CrewAI); handled by the
               graph engine, not here.

Everything is pure Python: the LLM client and the tool resolver are injected, so
the logic is unit-testable without providers, keys or a database. The runner
integrates it in ``core.crew_builder``; ``RunState`` is the typed view of the
run the engine passes around.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from core.workflow_graph import (
    NODE_TYPE_LLM,
    NODE_TYPE_ROUTER,
    NODE_TYPE_TOOL,
    extract_output_key,
    resolve_inputs_map_values,
)

logger = logging.getLogger(__name__)

TYPED_KINDS = (NODE_TYPE_LLM, NODE_TYPE_TOOL, NODE_TYPE_ROUTER)

DEFAULT_SYSTEM_PROMPT = (
    "You are a precise workflow step. Follow the instruction exactly, "
    "do not add commentary, and answer in the language of the input."
)

MAX_PROMPT_CONTEXT_CHARS = 24000


class NodeExecutionError(RuntimeError):
    """A typed node could not produce a valid output."""


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


# ---------------------------------------------------------------------------
# Typed run state
# ---------------------------------------------------------------------------


@dataclass
class RunState:
    """Typed view of a run: inputs, node outputs, routes, skipped nodes."""

    run_id: int
    workflow_id: Optional[int] = None
    inputs: Dict[str, Any] = field(default_factory=dict)
    node_outputs: Dict[str, str] = field(default_factory=dict)
    routes: Dict[str, str] = field(default_factory=dict)
    skipped: Set[str] = field(default_factory=set)
    meta: Dict[str, Any] = field(default_factory=dict)

    def output_of(self, node_id: Optional[str]) -> str:
        if not node_id:
            return ""
        return self.node_outputs.get(str(node_id), "") or ""

    def record(self, node_id: str, text: Any) -> str:
        value = _stringify(text)
        self.node_outputs[str(node_id)] = value
        return value

    def record_route(self, node_id: str, route: str) -> str:
        route = str(route)
        self.routes[str(node_id)] = route
        self.node_outputs[str(node_id)] = route
        return route

    def route_of(self, node_id: Optional[str]) -> Optional[str]:
        if not node_id:
            return None
        return self.routes.get(str(node_id))

    def skip(self, node_id: str) -> None:
        node_id = str(node_id)
        self.skipped.add(node_id)
        self.node_outputs.setdefault(node_id, "")

    def is_skipped(self, node_id: str) -> bool:
        return str(node_id) in self.skipped


def previous_output(node: Dict[str, Any], state: RunState) -> str:
    """Output of the last declared dependency (the usual ``{previous_result}``)."""
    parents = [p for p in (node.get("depends_on") or []) if p]
    if not parents:
        return ""
    return state.output_of(parents[-1])


# ---------------------------------------------------------------------------
# Route gating (used by the scheduler to skip disabled branches)
# ---------------------------------------------------------------------------


def route_gate(node: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    gate = node.get("when_route")
    return gate if isinstance(gate, dict) else None


def node_is_enabled(node: Dict[str, Any], state: RunState) -> bool:
    """False when the node's ``when_route`` gate does not match the chosen route."""
    gate = route_gate(node)
    if not gate:
        return True
    source = str(gate.get("from") or "")
    chosen = state.route_of(source)
    if not chosen:
        # Router has not run (or is unknown): never skip silently.
        return True
    wanted = gate.get("in")
    if wanted is None:
        wanted = [gate.get("equals")]
    if isinstance(wanted, str):
        wanted = [wanted]
    return str(chosen) in {str(w) for w in (wanted or [])}


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}|\{([a-zA-Z_][\w.\-]*)\}")


def build_prompt(node: Dict[str, Any], state: RunState, previous: Optional[str] = None) -> str:
    """Interpolate ``{{port}}``, ``{input.key}`` and ``{previous_result}``."""
    previous = previous_output(node, state) if previous is None else previous
    template = str(node.get("prompt") or "")
    wired = resolve_inputs_map_values(
        node.get("inputs_map") or {},
        node_outputs=state.node_outputs,
        run_inputs=state.inputs,
    )

    def repl(match: "re.Match[str]") -> str:
        expr = (match.group(1) or match.group(2) or "").strip()
        if not expr:
            return match.group(0)
        lowered = expr.lower()
        if lowered in ("previous_result", "previous", "parent"):
            return previous
        if expr.startswith("input."):
            key = expr.split(".", 1)[1]
            if key not in state.inputs:
                return match.group(0)
            return _stringify(state.inputs[key])
        if expr in wired:
            return wired[expr]
        if expr in state.inputs:
            return _stringify(state.inputs[expr])
        if expr in state.node_outputs:
            return state.output_of(expr)
        return match.group(0)

    text = _PLACEHOLDER_RE.sub(repl, template)
    if node.get("append_previous", True) and previous and previous not in text:
        text = f"{text}\n\n---\n[Output of the previous step]\n{previous}"
    text = text.strip()
    if len(text) > MAX_PROMPT_CONTEXT_CHARS:
        text = text[:MAX_PROMPT_CONTEXT_CHARS] + "\n...[truncated]"
    return text


# ---------------------------------------------------------------------------
# llm
# ---------------------------------------------------------------------------


def _strip_fences(text: str) -> str:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()


def coerce_json_output(text: str) -> Any:
    raw = _strip_fences(text)
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        start, end = raw.find("{"), raw.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                pass
        raise NodeExecutionError(f"Invalid JSON output: {raw[:200]!r}") from exc


def execute_llm_node(
    node: Dict[str, Any],
    state: RunState,
    complete: Callable[..., str],
    *,
    previous: Optional[str] = None,
) -> str:
    """One completion. ``complete(messages, temperature=..., force_json=...)``."""
    prompt = build_prompt(node, state, previous)
    if not prompt:
        raise NodeExecutionError(f"LLM node {node.get('id')} has an empty prompt.")
    system = str(node.get("system_prompt") or DEFAULT_SYSTEM_PROMPT)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]
    wants_json = str(node.get("output_format") or "text").lower() == "json"
    try:
        temperature = float(node.get("temperature", 0.2))
    except (TypeError, ValueError):
        temperature = 0.2

    try:
        text = complete(messages, temperature=temperature, force_json=wants_json)
    except TypeError:
        # Older/simpler clients: fall back to the positional form.
        text = complete(messages)

    text = (text or "").strip()
    if not text:
        raise NodeExecutionError(f"LLM node {node.get('id')} returned an empty response.")
    if wants_json:
        return json.dumps(coerce_json_output(text), ensure_ascii=False)
    return text


# ---------------------------------------------------------------------------
# tool
# ---------------------------------------------------------------------------


def build_tool_args(
    node: Dict[str, Any], state: RunState, previous: Optional[str] = None
) -> Dict[str, Any]:
    """Static ``tool_args`` + wired ports; falls back to ``input`` = previous."""
    previous = previous_output(node, state) if previous is None else previous
    args: Dict[str, Any] = {}
    for key, value in (node.get("tool_args") or {}).items():
        # Keep scalar literals as-is so tools receive ints/bools, not strings.
        args[str(key)] = value if isinstance(value, (int, float, bool)) else _stringify(value)
    wired = resolve_inputs_map_values(
        node.get("input_map") or node.get("inputs_map") or {},
        node_outputs=state.node_outputs,
        run_inputs=state.inputs,
    )
    args.update({str(k): _stringify(v) for k, v in wired.items()})
    if not args:
        args["input"] = previous
    return args


def execute_tool_node(
    node: Dict[str, Any],
    state: RunState,
    resolve_tool: Callable[[str], Any],
    *,
    previous: Optional[str] = None,
) -> str:
    """Call a whitelisted tool by name with deterministic arguments."""
    name = str(node.get("tool_name") or "").strip()
    if not name:
        raise NodeExecutionError(f"Tool node {node.get('id')} has no tool_name.")
    tool = resolve_tool(name)
    if tool is None:
        raise NodeExecutionError(f"Tool {name!r} is not available.")

    args = build_tool_args(node, state, previous)
    try:
        if hasattr(tool, "run") and callable(getattr(tool, "run")):
            try:
                result = tool.run(**args)
            except TypeError:
                result = tool.run(args.get("input") or previous or "")
        else:
            try:
                result = tool(**args)
            except TypeError:
                result = tool(args.get("input") or previous or "")
    except NodeExecutionError:
        raise
    except Exception as exc:  # noqa: BLE001 - boundary of the tool
        raise NodeExecutionError(f"Tool {name!r} failed: {exc}") from exc
    return _stringify(result)


# ---------------------------------------------------------------------------
# router
# ---------------------------------------------------------------------------


def _route_source_text(rule: Dict[str, Any], state: RunState, previous: str) -> str:
    source = rule.get("source")
    if source:
        if source == "input":
            key = str(rule.get("key") or "")
            if key:
                return _stringify(state.inputs.get(key, ""))
            return _stringify(state.inputs)
        key = str(rule.get("key") or "")
        return extract_output_key(state.output_of(str(source)), key)
    return previous


def match_rule(rule: Dict[str, Any], text: str) -> bool:
    text = text or ""
    if "exists" in rule:
        return bool(text.strip()) == bool(rule.get("exists"))
    if "equals" in rule:
        return text.strip() == str(rule.get("equals")).strip()
    if "contains" in rule:
        return str(rule.get("contains")).lower() in text.lower()
    pattern = str(rule.get("pattern") or "")
    if not pattern:
        return False
    try:
        return re.search(pattern, text, re.IGNORECASE | re.DOTALL) is not None
    except re.error:
        return pattern.lower() in text.lower()


def _default_route(node: Dict[str, Any]) -> str:
    routes = [str(r) for r in (node.get("routes") or [])]
    default = str(node.get("default_route") or "").strip()
    if default and default in routes:
        return default
    return routes[0] if routes else ""


def _llm_route(node: Dict[str, Any], state: RunState, complete: Callable[..., str], previous: str) -> str:
    routes = [str(r) for r in (node.get("routes") or [])]
    prompt = build_prompt(node, state, previous) or "Classify the previous step output."
    messages = [
        {
            "role": "system",
            "content": (
                "You are a router. Choose exactly one route for the request. "
                'Answer ONLY with JSON: {"route": "<route>"}.'
            ),
        },
        {
            "role": "user",
            "content": (
                f"Routes: {json.dumps(routes, ensure_ascii=False)}\n\n"
                f"{prompt}\n\n---\n[Output to classify]\n{previous}"
            ),
        },
    ]
    try:
        raw = complete(messages, temperature=0.0, force_json=True)
    except TypeError:
        raw = complete(messages)
    try:
        data = coerce_json_output(raw)
    except NodeExecutionError:
        logger.warning("Router %s: unparsable LLM answer, using default route.", node.get("id"))
        return _default_route(node)
    route = str((data or {}).get("route") or "").strip() if isinstance(data, dict) else ""
    if route in routes:
        return route
    logger.warning("Router %s: LLM picked unknown route %r, using default.", node.get("id"), route)
    return _default_route(node)


def execute_router_node(
    node: Dict[str, Any],
    state: RunState,
    complete: Optional[Callable[..., str]] = None,
    *,
    previous: Optional[str] = None,
) -> str:
    """Return the selected route name (rules first, LLM only if allowed)."""
    routes = [str(r) for r in (node.get("routes") or [])]
    if not routes:
        raise NodeExecutionError(f"Router {node.get('id')} has no routes.")
    previous = previous_output(node, state) if previous is None else previous
    mode = str(node.get("router_mode") or "auto").lower()

    if mode in ("auto", "rules"):
        for rule in node.get("rules") or []:
            if not isinstance(rule, dict):
                continue
            if match_rule(rule, _route_source_text(rule, state, previous)):
                chosen = str(rule.get("route") or "")
                if chosen in routes:
                    return chosen
        if mode == "rules":
            return _default_route(node)
        if not (node.get("rules") or []):
            # Nothing deterministic to try: only call the LLM when there is text.
            if complete is None or not previous.strip():
                return _default_route(node)
        elif complete is None:
            return _default_route(node)

    if mode in ("auto", "llm") and complete is not None:
        return _llm_route(node, state, complete, previous)
    return _default_route(node)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def node_kind(node: Dict[str, Any]) -> Optional[str]:
    ntype = str((node or {}).get("type") or "").strip().lower()
    return ntype if ntype in TYPED_KINDS else None


def execute_typed_node(
    kind: str,
    node: Dict[str, Any],
    state: RunState,
    *,
    complete: Optional[Callable[..., str]] = None,
    resolve_tool: Optional[Callable[[str], Any]] = None,
    previous: Optional[str] = None,
) -> str:
    """Run one typed node and record its output in ``state``."""
    if kind == NODE_TYPE_LLM:
        if complete is None:
            raise NodeExecutionError("LLM client not configured.")
        output = execute_llm_node(node, state, complete, previous=previous)
    elif kind == NODE_TYPE_TOOL:
        if resolve_tool is None:
            raise NodeExecutionError("Tool resolver not configured.")
        output = execute_tool_node(node, state, resolve_tool, previous=previous)
    elif kind == NODE_TYPE_ROUTER:
        output = execute_router_node(node, state, complete, previous=previous)
        state.record_route(str(node.get("id")), output)
        return output
    else:
        raise NodeExecutionError(f"Unknown typed node kind {kind!r}.")
    state.record(str(node.get("id")), output)
    return output


def typed_nodes(graph: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [n for n in graph if node_kind(n)]


__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "NodeExecutionError",
    "RunState",
    "TYPED_KINDS",
    "build_prompt",
    "build_tool_args",
    "coerce_json_output",
    "execute_llm_node",
    "execute_router_node",
    "execute_tool_node",
    "execute_typed_node",
    "match_rule",
    "node_is_enabled",
    "node_kind",
    "previous_output",
    "route_gate",
    "typed_nodes",
]
