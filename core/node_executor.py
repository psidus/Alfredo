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
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

import requests

from core.workflow_contracts import normalize_binary_decision
from core.workflow_graph import (
    NODE_TYPE_LLM,
    NODE_TYPE_ROUTER,
    NODE_TYPE_TOOL,
    extract_output_key,
    resolve_inputs_map_values,
)

logger = logging.getLogger(__name__)

TYPED_KINDS = (NODE_TYPE_LLM, NODE_TYPE_TOOL, NODE_TYPE_ROUTER)

YES_SYNONYMS = {"yes", "y", "si", "sì", "true", "1", "ok", "approvato", "approved", "pass", "vero"}
NO_SYNONYMS = {"no", "n", "false", "0", "ko", "rifiutato", "rejected", "fail", "falso"}

JEV_MODEL_KEYWORDS = (
    "jev",
    "tev1",
    "nimble",
    "clef",
    "strands-decider",
    "d1-omni",
    "systemone",
    "typesafe/",
)
JEV_PRIMITIVES = ("noul", "choice", "score")


def is_jev_decision_model(model_name: Optional[str]) -> bool:
    """True when `model_name` refers to a non-autoregressive Jev / System One decision model."""
    name = str(model_name or "").strip().lower()
    if not name:
        return False
    return any(kw in name for kw in JEV_MODEL_KEYWORDS)


def normalize_yes_no(value: Any) -> Optional[str]:
    """Normalize English/Italian binary decision answers into 'yes' or 'no'."""
    norm = normalize_binary_decision(value)
    if norm is not None:
        return norm
    if value is None:
        return None
    low = str(value).strip().lower()
    if low in YES_SYNONYMS:
        return "yes"
    if low in NO_SYNONYMS:
        return "no"
    return None


def is_binary_decision_node(node: Dict[str, Any]) -> bool:
    """True when a node is explicitly marked decision_mode or has strictly binary yes/no routes."""
    if not isinstance(node, dict):
        return False
    if node.get("decision_mode"):
        return True
    routes = {str(r).strip().lower() for r in (node.get("routes") or []) if str(r).strip()}
    return len(routes) == 2 and bool(routes & YES_SYNONYMS) and bool(routes & NO_SYNONYMS)

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

    def mark_skipped(self, node_id: str) -> None:
        self.skip(node_id)

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
    """False when the node's ``when_route`` gate or exclusive parent branch was skipped."""
    deps = [str(d) for d in (node.get("depends_on") or []) if d]
    if deps and all(state.is_skipped(d) for d in deps):
        return False

    gate = route_gate(node)
    if not gate:
        return True
    source = str(gate.get("from") or "")
    if source and state.is_skipped(source):
        return False
    chosen = state.route_of(source)
    if not chosen:
        # Router has not run (or is unknown): never skip silently.
        return True
    wanted = gate.get("in")
    if wanted is None:
        wanted = [gate.get("equals")]
    if isinstance(wanted, str):
        wanted = [wanted]
    wanted_list = [str(w) for w in (wanted or []) if w is not None]
    if str(chosen) in wanted_list:
        return True
    norm_chosen = normalize_yes_no(chosen)
    if norm_chosen is not None:
        for w in wanted_list:
            if normalize_yes_no(w) == norm_chosen:
                return True
    return False


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}|\{([a-zA-Z_][\w.\-]*)\}")


def build_prompt(node: Dict[str, Any], state: RunState, previous: Optional[str] = None) -> str:
    """Interpolate ``{{port}}``, ``{input.key}`` and ``{previous_result}``."""
    previous = previous_output(node, state) if previous is None else previous
    template = str(node.get("prompt") or node.get("question") or "")
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


def _resolve_route_candidate(candidate: Any, routes: List[str], binary: bool = False) -> Optional[str]:
    """Match an LLM/rule route candidate against declared routes, handling yes/no synonyms."""
    raw = str(candidate or "").strip()
    if not raw:
        return None
    if raw in routes:
        return raw
    low_map = {r.strip().lower(): r for r in routes}
    if raw.lower() in low_map:
        return low_map[raw.lower()]
    if binary:
        norm = normalize_yes_no(raw)
        if norm:
            for r in routes:
                if normalize_yes_no(r) == norm:
                    return r
    return None


def _clamp_threshold(raw: Any, default: float = 0.5) -> float:
    try:
        val = float(default if raw in (None, "") else raw)
    except (TypeError, ValueError):
        val = default
    return max(0.0, min(1.0, val))


def _record_decision_details(
    state: RunState,
    node_id: str,
    details: Dict[str, Any],
) -> None:
    if not node_id:
        return
    details_map = state.meta.setdefault("decision_details", {})
    if isinstance(details_map, dict):
        details_map[node_id] = details


def _interpolate_text_for_state(template: str, node: Dict[str, Any], state: RunState, previous: str) -> str:
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
            return _stringify(state.inputs[key]) if key in state.inputs else match.group(0)
        if expr in wired:
            return wired[expr]
        if expr in state.inputs:
            return _stringify(state.inputs[expr])
        if expr in state.node_outputs:
            return state.output_of(expr)
        return match.group(0)

    return _PLACEHOLDER_RE.sub(repl, template or "").strip()


def build_jev_request_payload(
    node: Dict[str, Any],
    state: RunState,
    previous: Optional[str] = None,
    *,
    model_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build a Jev / Ollama `/v1/systemone` request payload:
    `{"model": ..., "state": ..., "questions": {"decision": {...}}}`.
    """
    previous = previous_output(node, state) if previous is None else previous
    routes = [str(r) for r in (node.get("routes") or ["yes", "no"]) if str(r).strip()]
    binary = is_binary_decision_node(node)
    primitive = str(node.get("decision_primitive") or ("noul" if binary else "choice")).strip().lower()
    if primitive not in JEV_PRIMITIVES:
        primitive = "noul" if binary else "choice"

    raw_q = str(node.get("question") or node.get("prompt") or "").strip()
    instructions = _interpolate_text_for_state(raw_q, node, state, previous)
    if not instructions:
        instructions = (
            "Evaluate strictly whether the proposition holds (yes/no) based on the state."
            if primitive == "noul"
            else "Select the best matching criterion for the provided state."
        )

    wired = resolve_inputs_map_values(
        node.get("inputs_map") or {},
        node_outputs=state.node_outputs,
        run_inputs=state.inputs,
    )
    state_parts: List[str] = []
    if str(previous or "").strip():
        state_parts.append(str(previous).strip())
    if wired:
        wired_lines = [f"{k}: {v}" for k, v in wired.items() if str(v or "").strip()]
        if wired_lines:
            wired_block = "\n".join(wired_lines)
            if wired_block not in (state_parts[0] if state_parts else ""):
                state_parts.append(wired_block)
    if not state_parts and state.inputs:
        state_parts.append(_stringify(state.inputs))

    state_text = "\n\n".join(state_parts).strip() or instructions
    if len(state_text) > MAX_PROMPT_CONTEXT_CHARS:
        state_text = state_text[:MAX_PROMPT_CONTEXT_CHARS] + "\n...[truncated]"

    q_spec: Dict[str, Any] = {
        "type": primitive,
        "instructions": instructions,
    }
    if primitive == "choice":
        q_spec["choices"] = routes
        q_spec["criteria"] = routes
    elif primitive == "score":
        q_spec["criteria"] = routes

    resolved_model = str(
        model_name
        or node.get("jev_model")
        or node.get("model_name")
        or "tev1"
    ).strip()

    return {
        "model": resolved_model,
        "state": state_text,
        "questions": {
            "decision": q_spec,
        },
    }


def _find_binary_routes(routes: List[str]) -> tuple[str, str]:
    yes_route = next((r for r in routes if normalize_yes_no(r) == "yes"), None) or (routes[0] if routes else "yes")
    no_route = next((r for r in routes if normalize_yes_no(r) == "no"), None) or (routes[-1] if routes else "no")
    return yes_route, no_route


def parse_jev_decision_response(resp_data: Any, node: Dict[str, Any]) -> Dict[str, Any]:
    """
    Parse TypeSafe Jev / Ollama `/v1/systemone` response (`noul`, `choice`, `score`),
    apply `node['threshold']` calibration, and return normalized decision details.
    """
    if isinstance(resp_data, str):
        resp_data = coerce_json_output(resp_data)
    if not isinstance(resp_data, dict):
        raise NodeExecutionError(f"Expected dict response from Jev decision model, got {type(resp_data).__name__}")

    routes = [str(r) for r in (node.get("routes") or ["yes", "no"]) if str(r).strip()]
    binary = is_binary_decision_node(node)
    threshold = _clamp_threshold(node.get("threshold"), default=0.5)
    yes_route, no_route = _find_binary_routes(routes)

    answers = resp_data.get("answers")
    ans: Dict[str, Any]
    if isinstance(answers, dict) and answers:
        candidate = answers.get("decision")
        if not isinstance(candidate, dict):
            candidate = next((v for v in answers.values() if isinstance(v, dict)), {})
        ans = dict(candidate or {})
    else:
        ans = dict(resp_data)

    primitive = str(
        ans.get("type")
        or ("noul" if "noul" in ans else "")
        or node.get("decision_primitive")
        or ("noul" if binary else "choice")
    ).strip().lower()
    if primitive not in JEV_PRIMITIVES:
        primitive = "noul" if binary else "choice"

    probs = ans.get("probabilities") if isinstance(ans.get("probabilities"), dict) else {}
    chosen: str
    confidence: float = 1.0
    noul_prob: Optional[float] = None
    score_val: Optional[float] = None
    reason: str = str(ans.get("reason") or ans.get("rationale") or "").strip()

    if primitive == "noul":
        p_yes: Optional[float] = None
        if ans.get("noul") not in (None, ""):
            try:
                p_yes = float(ans["noul"])
            except (TypeError, ValueError):
                p_yes = None
        if p_yes is None and probs:
            for k, v in probs.items():
                norm_k = normalize_yes_no(k)
                if norm_k == "yes":
                    try:
                        p_yes = float(v)
                        break
                    except (TypeError, ValueError):
                        pass
            if p_yes is None:
                for k, v in probs.items():
                    if normalize_yes_no(k) == "no":
                        try:
                            p_yes = 1.0 - float(v)
                            break
                        except (TypeError, ValueError):
                            pass
        if p_yes is None:
            norm_ch = normalize_yes_no(ans.get("choice") or ans.get("decision") or ans.get("route"))
            if norm_ch == "yes":
                p_yes = 1.0
            elif norm_ch == "no":
                p_yes = 0.0
            else:
                def_is_yes = normalize_yes_no(_default_route(node)) == "yes"
                p_yes = 1.0 if def_is_yes else 0.0

        p_yes = round(max(0.0, min(1.0, p_yes)), 6)
        p_no = round(1.0 - p_yes, 6)
        noul_prob = p_yes
        is_yes = p_yes >= threshold
        chosen = yes_route if is_yes else no_route
        confidence = round(p_yes if is_yes else p_no, 6)
        if not probs:
            probs = {"yes": p_yes, "no": p_no}
        if not reason:
            op = ">=" if is_yes else "<"
            reason = f"Jev noul p(yes)={p_yes:.3f} {op} threshold {threshold:.2f}"

    elif primitive == "choice":
        raw_choice = ans.get("choice") or ans.get("route") or ans.get("decision")
        if not raw_choice and probs:
            try:
                raw_choice = max(probs.items(), key=lambda kv: float(kv[1]))[0]
            except (TypeError, ValueError):
                raw_choice = None
        resolved_choice = _resolve_route_candidate(raw_choice, routes, binary=binary)
        try:
            confidence = float(ans.get("confidence")) if ans.get("confidence") is not None else None
        except (TypeError, ValueError):
            confidence = None
        p_choice: Optional[float] = None
        if probs:
            for pk, pv in probs.items():
                if str(pk).strip().lower() == str(raw_choice or "").strip().lower() or (
                    resolved_choice and _resolve_route_candidate(pk, [resolved_choice], binary=binary) == resolved_choice
                ):
                    try:
                        p_choice = float(pv)
                        break
                    except (TypeError, ValueError):
                        pass
        if confidence is None:
            confidence = p_choice if p_choice is not None else 1.0
        confidence = round(max(0.0, min(1.0, confidence)), 6)
        eff_prob = p_choice if p_choice is not None else confidence

        if resolved_choice is None or eff_prob < threshold:
            chosen = _default_route(node)
            if not reason:
                reason = (
                    f"Jev choice {raw_choice!r} below threshold "
                    f"({eff_prob:.3f} < {threshold:.2f}) -> default {chosen!r}"
                )
        else:
            chosen = resolved_choice
            if not reason:
                reason = f"Jev choice={chosen!r} (p={eff_prob:.3f}, conf={confidence:.3f})"

        if binary:
            norm_ch = normalize_yes_no(chosen)
            if norm_ch == "yes":
                noul_prob = round(eff_prob, 6)
            elif norm_ch == "no":
                noul_prob = round(1.0 - eff_prob, 6)

    else:  # primitive == "score"
        try:
            score_val = float(ans.get("score")) if ans.get("score") is not None else 0.0
        except (TypeError, ValueError):
            score_val = 0.0
        denom = max(len(routes) - 1, 1)
        norm_score = score_val / denom if score_val > 1.0 else max(0.0, min(1.0, score_val))
        try:
            confidence = float(ans.get("confidence", norm_score))
        except (TypeError, ValueError):
            confidence = norm_score
        confidence = round(max(0.0, min(1.0, confidence)), 6)
        if binary:
            is_yes = norm_score >= threshold
            chosen = yes_route if is_yes else no_route
            noul_prob = round(norm_score, 6)
        else:
            idx = min(len(routes) - 1, max(0, int(round(score_val)))) if score_val > 1.0 else min(
                len(routes) - 1, max(0, int(round(norm_score * denom)))
            )
            chosen = routes[idx] if routes else ""
        if not reason:
            reason = f"Jev score={score_val:.3f} (norm={norm_score:.3f}, threshold={threshold:.2f})"

    return {
        "route": chosen,
        "decision": normalize_yes_no(chosen) or chosen,
        "primitive": primitive,
        "engine": "jev_systemone",
        "model": str(resp_data.get("model") or node.get("jev_model") or ""),
        "noul": noul_prob,
        "score": score_val,
        "probabilities": probs,
        "confidence": confidence,
        "threshold": threshold,
        "reason": reason,
    }


def call_jev_decision_model(
    payload: Dict[str, Any],
    *,
    provider: str = "ollama",
    api_base: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """
    Call a System One / Jev decision endpoint:
    - Ollama local (`/v1/systemone`)
    - OpenRouter Decisions API (`https://openrouter.ai/api/alpha/decisions`)
    - TypeSafe Jev API (`/v1/decisions`)
    """
    model_str = str(payload.get("model") or "tev1").strip()
    prov = str(provider or "").strip().lower()
    if "/" in model_str and model_str.lower().startswith("typesafe/") and prov in ("", "ollama"):
        prov = "openrouter"

    headers = {"Content-Type": "application/json"}
    if prov == "openrouter":
        url = (api_base or "https://openrouter.ai/api/alpha/decisions").rstrip("/")
        key = api_key or os.getenv("OPENROUTER_API_KEY", "")
        if key:
            headers["Authorization"] = f"Bearer {key}"
    elif prov in ("typesafe", "jev"):
        base = (api_base or os.getenv("JEV_API_BASE", "https://api.thejevai.com")).rstrip("/")
        url = base if base.endswith("/decisions") else f"{base}/v1/decisions"
        key = api_key or os.getenv("TYPESAFE_API_KEY") or os.getenv("JEV_API_KEY") or ""
        if key:
            headers["Authorization"] = f"Bearer {key}"
    else:
        base = (api_base or os.getenv("OLLAMA_API_BASE", "http://localhost:11434")).rstrip("/")
        url = base if base.endswith("/v1/systemone") else f"{base}/v1/systemone"
        key = api_key or os.getenv("OLLAMA_API_KEY", "")
        if key and key.lower() != "ollama":
            headers["Authorization"] = f"Bearer {key}"

    response = requests.post(url, headers=headers, json=payload, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _llm_route(node: Dict[str, Any], state: RunState, complete: Callable[..., str], previous: str) -> str:
    routes = [str(r) for r in (node.get("routes") or [])]
    binary = is_binary_decision_node(node)
    threshold = _clamp_threshold(node.get("threshold"), default=0.5)
    prompt = build_prompt(node, state, previous)
    if not prompt:
        prompt = "Decide strictly YES or NO based on the criterion." if binary else "Classify the previous step output."

    if binary:
        sys_msg = (
            "You are a fast, strict binary decision model (YES/NO). "
            "Evaluate the criterion against the provided context and decide strictly YES or NO. "
            'Answer ONLY with JSON: {"decision": "yes"|"no", "route": "yes"|"no", '
            '"reason": "<1 concise sentence>", "confidence": 0.0}.'
        )
    else:
        sys_msg = (
            "You are a router. Choose exactly one route for the request. "
            'Answer ONLY with JSON: {"route": "<route>"}.'
        )

    messages = [
        {"role": "system", "content": sys_msg},
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

    data: Optional[Dict[str, Any]] = None
    try:
        parsed = coerce_json_output(raw)
        if isinstance(parsed, dict):
            data = parsed
    except NodeExecutionError:
        data = None

    # Allow Jev-shaped responses even when returned via generic `complete`
    if isinstance(data, dict) and ("answers" in data or "noul" in data):
        jev_details = parse_jev_decision_response(data, node)
        _record_decision_details(state, str(node.get("id") or ""), jev_details)
        return str(jev_details["route"])

    chosen: Optional[str] = None
    reason = ""
    confidence = 1.0
    if isinstance(data, dict):
        for key in ("route", "decision", "answer", "result"):
            if key in data:
                chosen = _resolve_route_candidate(data.get(key), routes, binary=binary)
                if chosen is not None:
                    break
        reason = str(data.get("reason") or data.get("rationale") or "").strip()
        try:
            confidence = float(data.get("confidence", 1.0))
        except (TypeError, ValueError):
            confidence = 1.0
        confidence = max(0.0, min(1.0, confidence))
        if binary and chosen is not None and normalize_yes_no(chosen) == "yes" and confidence < threshold:
            chosen = _default_route(node)
            reason = (
                f"{reason} (confidence {confidence:.2f} < threshold {threshold:.2f} -> {chosen})".strip()
            )
    elif binary and raw:
        # Handle plain text 'yes' / 'sì' / 'no' (or "No, reason...") replies from tiny models
        tokens = re.split(r"[\s,.;:!?()\"']+", str(raw).strip())
        first_token = next((t for t in tokens if t), "")
        chosen = _resolve_route_candidate(first_token, routes, binary=True)
        reason = str(raw).strip()[:200]

    if chosen is None:
        logger.warning("Router %s: unparsable/unknown LLM route from %r, using default.", node.get("id"), raw)
        chosen = _default_route(node)
        if not reason:
            reason = "Fallback to default route"

    if binary:
        _record_decision_details(
            state,
            str(node.get("id") or ""),
            {
                "route": chosen,
                "decision": normalize_yes_no(chosen) or chosen,
                "engine": "llm",
                "threshold": threshold,
                "reason": reason,
                "confidence": confidence,
            },
        )
    return chosen


def execute_router_node(
    node: Dict[str, Any],
    state: RunState,
    complete: Optional[Callable[..., str]] = None,
    *,
    decide: Optional[Callable[[Dict[str, Any]], Any]] = None,
    previous: Optional[str] = None,
) -> str:
    """Return the selected route name (rules first, then Jev/SystemOne `decide`, then LLM fallback)."""
    binary = is_binary_decision_node(node)
    routes = [str(r) for r in (node.get("routes") or []) if str(r).strip()]
    if not routes and node.get("decision_mode"):
        routes = ["yes", "no"]
        node = {**node, "routes": routes}
    if not routes:
        raise NodeExecutionError(f"Router {node.get('id')} has no routes.")
    previous = previous_output(node, state) if previous is None else previous
    mode = str(node.get("router_mode") or "auto").lower()
    engine = str(node.get("decision_engine") or ("jev" if binary else "auto")).lower()

    if mode in ("auto", "rules"):
        for rule in node.get("rules") or []:
            if not isinstance(rule, dict):
                continue
            if match_rule(rule, _route_source_text(rule, state, previous)):
                chosen = _resolve_route_candidate(rule.get("route"), routes, binary=binary)
                if chosen is not None:
                    if binary:
                        _record_decision_details(
                            state,
                            str(node.get("id") or ""),
                            {
                                "route": chosen,
                                "decision": normalize_yes_no(chosen) or chosen,
                                "engine": "rules",
                                "reason": f"Matched rule: {rule}",
                                "confidence": 1.0,
                            },
                        )
                    return chosen
        if mode == "rules":
            return _default_route(node)
        has_context = bool(previous.strip() or (binary and str(node.get("question") or node.get("prompt") or "").strip()))
        wants_jev = decide is not None and engine in ("jev", "auto")
        if not (node.get("rules") or []):
            if (complete is None and not wants_jev) or not has_context:
                return _default_route(node)
        elif complete is None and not wants_jev:
            return _default_route(node)

    # Prefer Jev / System One non-autoregressive decision model when `decide` is supplied and allowed
    if decide is not None and mode in ("auto", "llm", "jev") and engine != "llm":
        try:
            payload = build_jev_request_payload(node, state, previous)
            resp = decide(payload)
            details = parse_jev_decision_response(resp, node)
            _record_decision_details(state, str(node.get("id") or ""), details)
            return str(details["route"])
        except Exception as exc:  # noqa: BLE001 - network/endpoint offline fallback
            logger.warning("Router %s Jev/SystemOne call failed (%s); falling back.", node.get("id"), exc)
            if complete is not None and mode in ("auto", "llm", "jev"):
                return _llm_route(node, state, complete, previous)
            if complete is None and not node.get("fallback_to_default_on_error", True):
                raise NodeExecutionError(f"Jev decision failed: {exc}") from exc
            return _default_route(node)

    if mode in ("auto", "llm") and complete is not None:
        return _llm_route(node, state, complete, previous)
    return _default_route(node)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def node_kind(node: Dict[str, Any]) -> Optional[str]:
    ntype = str((node or {}).get("type") or "").strip().lower()
    if ntype == "decision":
        return NODE_TYPE_ROUTER
    return ntype if ntype in TYPED_KINDS else None


def execute_typed_node(
    kind: str,
    node: Dict[str, Any],
    state: RunState,
    *,
    complete: Optional[Callable[..., str]] = None,
    decide: Optional[Callable[[Dict[str, Any]], Any]] = None,
    resolve_tool: Optional[Callable[[str], Any]] = None,
    previous: Optional[str] = None,
) -> str:
    """Run one typed node and record its output in ``state``."""
    nid = str(node.get("id"))
    if kind == NODE_TYPE_LLM:
        if complete is None:
            raise NodeExecutionError("LLM client not configured.")
        output = execute_llm_node(node, state, complete, previous=previous)
    elif kind == NODE_TYPE_TOOL:
        if resolve_tool is None:
            raise NodeExecutionError("Tool resolver not configured.")
        output = execute_tool_node(node, state, resolve_tool, previous=previous)
    elif kind == NODE_TYPE_ROUTER:
        output = execute_router_node(node, state, complete, decide=decide, previous=previous)
        state.record_route(nid, output)
        details = (state.meta.get("decision_details") or {}).get(nid)
        if node.get("decision_mode") or (isinstance(details, dict) and details.get("engine") == "jev_systemone"):
            payload_out = details or {
                "route": output,
                "decision": normalize_yes_no(output) or output,
                "reason": "",
                "confidence": 1.0,
            }
            structured_str = json.dumps(payload_out, ensure_ascii=False)
            state.node_outputs[nid] = structured_str
            return structured_str
        return output
    else:
        raise NodeExecutionError(f"Unknown typed node kind {kind!r}.")
    state.record(nid, output)
    return output


def typed_nodes(graph: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [n for n in graph if node_kind(n)]


__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "JEV_MODEL_KEYWORDS",
    "JEV_PRIMITIVES",
    "NO_SYNONYMS",
    "NodeExecutionError",
    "RunState",
    "TYPED_KINDS",
    "YES_SYNONYMS",
    "build_jev_request_payload",
    "build_prompt",
    "build_tool_args",
    "call_jev_decision_model",
    "coerce_json_output",
    "execute_llm_node",
    "execute_router_node",
    "execute_tool_node",
    "execute_typed_node",
    "is_binary_decision_node",
    "is_jev_decision_model",
    "match_rule",
    "node_is_enabled",
    "node_kind",
    "normalize_yes_no",
    "parse_jev_decision_response",
    "previous_output",
    "route_gate",
    "typed_nodes",
]
