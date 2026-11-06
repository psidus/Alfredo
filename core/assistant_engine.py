"""
Unified conversational engine for Alfredo.

Two capabilities share the same plumbing:

* **free chat**  — natural conversation, no forced JSON, no workflow side effects;
* **workflow decision** — pick a saved workflow and fill its declared inputs.

This module is deliberately free of Streamlit / Telegram / CrewAI imports so the
dashboard, the Telegram bot and the future mobile API can all call the same code.
The heavy LLM stack (``MasterAI``) is imported lazily.

Public helpers that are pure (no DB, no LLM) live at the top-level so they can be
unit-tested without any provider configured.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# How many turns of history we forward to the model (user + assistant messages).
MAX_HISTORY_MESSAGES = 24
# Hard cap per stored message; keeps local 8-16k context windows usable.
MAX_MESSAGE_CHARS = 6000
# Description shown for a workflow inside the catalogue prompt.
MAX_CATALOG_DESCRIPTION_CHARS = 220

DECISION_ACTIONS = ("answer", "run_workflow", "clarify")

ASSISTANT_PERSONA = """You are Alfredo, a personal AI assistant running on the user's machine.
Rules:
- Answer naturally and concisely, in the same language as the user.
- Be direct and practical. Skip filler and disclaimers.
- Use Markdown when it improves readability (lists, code blocks, tables).
- Never invent facts about the user's files, data or previous runs.
- A catalogue of saved workflows exists, but you cannot execute them from this chat
  on your own. If a request matches one, name it and say it can be started.
- If you are unsure, ask one short clarifying question instead of guessing.
"""

DECISION_PROMPT = """You are Alfredo's workflow dispatcher.

Your job: decide whether the user's request should start one of the SAVED WORKFLOWS.

{saved_context}
{catalogue}

Return ONLY a JSON object with this exact shape:
{{
  "action": "answer" | "run_workflow" | "clarify",
  "reply": "short message for the user, in their language",
  "workflow_id": null,
  "inputs": {{}},
  "missing_inputs": []
}}

Rules:
1. Use "run_workflow" ONLY when one saved workflow clearly matches the request.
2. Copy any value the user already provided into "inputs", using the exact input
   keys declared for that workflow. Never invent a value.
3. Put declared keys that still have no value in "missing_inputs".
4. If the request is ambiguous, use "clarify" and ask a single question.
5. If no workflow matches, use "answer" and say what you can do.
6. "reply" is free text. Do not put JSON inside it.
"""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class WorkflowInput:
    key: str
    prompt: str = ""


@dataclass
class WorkflowCard:
    """Compact, prompt-friendly view of a saved workflow."""

    id: int
    name: str
    description: str = ""
    inputs: List[WorkflowInput] = field(default_factory=list)
    expected_exports: List[str] = field(default_factory=list)

    def declared_keys(self) -> List[str]:
        return [i.key for i in self.inputs]


@dataclass
class AssistantTurn:
    """Result of a decision call."""

    text: str = ""
    action: str = "answer"
    workflow_id: Optional[int] = None
    inputs: Dict[str, Any] = field(default_factory=dict)
    missing_inputs: List[str] = field(default_factory=list)
    raw: str = ""


# ---------------------------------------------------------------------------
# Catalogue (pure helpers + one DB read)
# ---------------------------------------------------------------------------


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return [text]
        return parsed if isinstance(parsed, list) else [parsed]
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def normalize_required_inputs(raw: Any) -> List[WorkflowInput]:
    """Accepts ``[{key, prompt}]`` or ``["key", ...]`` / JSON string."""
    out: List[WorkflowInput] = []
    seen: set[str] = set()
    for item in _as_list(raw):
        if isinstance(item, dict):
            key = str(item.get("key") or "").strip()
            prompt = str(item.get("prompt") or "").strip()
        else:
            key = str(item or "").strip()
            prompt = ""
        if key and key not in seen:
            seen.add(key)
            out.append(WorkflowInput(key=key, prompt=prompt))
    return out


def _workflow_task_ids(raw: Any) -> List[int]:
    """All numeric task ids referenced by a workflow graph, including loop bodies."""
    try:
        from core.workflow_graph import (
            NODE_TYPE_LOOP,
            collect_task_ids,
            normalize_graph,
            resolve_loop_body_ids,
        )
    except Exception:  # pragma: no cover - defensive if module moves
        return [int(x) for x in (raw or []) if isinstance(x, int)]

    try:
        graph = normalize_graph(raw, materialize_export=False)
    except Exception as exc:
        logger.warning("Could not normalize workflow graph for the catalogue: %s", exc)
        try:
            return collect_task_ids(raw)
        except Exception:
            return []

    ids = collect_task_ids(graph)
    by_id = {n.get("id"): n for n in graph if n.get("id")}
    for node in graph:
        if node.get("type") != NODE_TYPE_LOOP:
            continue
        body_ids = resolve_loop_body_ids(graph, node)
        body_nodes = [by_id[b] for b in body_ids if b in by_id]
        for tid in collect_task_ids(body_nodes):
            if tid not in ids:
                ids.append(tid)
    return ids


def build_workflow_catalog(db: Any = None) -> List[WorkflowCard]:
    """Read saved workflows and turn them into prompt-friendly cards."""
    owns_db = db is None
    if db is None:
        from core.db_manager import DBManager

        db = DBManager()
    try:
        workflows = db.read_all_workflows() or []
        tasks = {int(t["id"]): t for t in (db.read_all_tasks() or []) if t.get("id") is not None}
    finally:
        if owns_db:
            db.close()

    cards: List[WorkflowCard] = []
    for wf in workflows:
        try:
            workflow_id = int(wf["id"])
        except (KeyError, TypeError, ValueError):
            continue

        task_ids = _workflow_task_ids(wf.get("task_ids") or wf.get("task_ids_json") or [])
        seen_inputs: set[str] = set()
        inputs: List[WorkflowInput] = []
        for tid in task_ids:
            task = tasks.get(int(tid)) or {}
            for inp in normalize_required_inputs(task.get("required_inputs")):
                if inp.key not in seen_inputs:
                    seen_inputs.add(inp.key)
                    inputs.append(inp)

        description = str(wf.get("description") or "").strip()
        if not description and task_ids:
            first = tasks.get(int(task_ids[0])) or {}
            description = str(first.get("description") or first.get("name") or "").strip()
        description = re.sub(r"\s+", " ", description).strip()[:MAX_CATALOG_DESCRIPTION_CHARS]

        exports = _as_list(wf.get("expected_exports"))
        cards.append(
            WorkflowCard(
                id=workflow_id,
                name=str(wf.get("name") or f"Workflow {workflow_id}").strip(),
                description=description,
                inputs=inputs,
                expected_exports=[str(x) for x in exports if x],
            )
        )
    return cards


def catalog_to_prompt(cards: Iterable[WorkflowCard]) -> str:
    """Render the catalogue as a compact, numbered block for a prompt."""
    cards = list(cards)
    if not cards:
        return "SAVED WORKFLOWS: none available."

    lines = ["SAVED WORKFLOWS:"]
    for card in cards:
        lines.append(f'[{card.id}] "{card.name}"')
        if card.description:
            lines.append(f"    does: {card.description}")
        if card.inputs:
            rendered = []
            for inp in card.inputs:
                rendered.append(f"{inp.key} ({inp.prompt})" if inp.prompt else inp.key)
            lines.append("    inputs: " + ", ".join(rendered))
        else:
            lines.append("    inputs: (none)")
    return "\n".join(lines)


def catalog_by_id(cards: Iterable[WorkflowCard]) -> Dict[int, WorkflowCard]:
    return {card.id: card for card in cards}


# ---------------------------------------------------------------------------
# Decision parsing (pure)
# ---------------------------------------------------------------------------


def _strip_json_fences(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1].strip()
    return text.strip()


def parse_decision(raw: str, cards: Iterable[WorkflowCard]) -> AssistantTurn:
    """Validate a model decision against the real catalogue.

    Anything that does not match a saved workflow degrades to a plain answer so a
    hallucinated workflow id can never start a run.
    """
    catalogue = catalog_by_id(cards)
    try:
        data = json.loads(_strip_json_fences(raw))
    except (json.JSONDecodeError, TypeError, ValueError):
        return AssistantTurn(text=(raw or "").strip(), action="answer", raw=raw or "")

    if not isinstance(data, dict):
        return AssistantTurn(text=str(data).strip(), action="answer", raw=raw or "")

    action = str(data.get("action") or "answer").strip().lower()
    if action not in DECISION_ACTIONS:
        action = "answer"

    text = str(data.get("reply") or data.get("response") or data.get("message") or "").strip()

    try:
        workflow_id = int(data["workflow_id"]) if data.get("workflow_id") not in (None, "") else None
    except (TypeError, ValueError):
        workflow_id = None

    raw_inputs = data.get("inputs") or {}
    inputs: Dict[str, Any] = {}
    if isinstance(raw_inputs, dict):
        inputs = {str(k): v for k, v in raw_inputs.items() if v is not None}

    raw_missing = data.get("missing_inputs") or []
    missing = [str(x) for x in raw_missing] if isinstance(raw_missing, (list, tuple)) else []

    if action == "run_workflow":
        card = catalogue.get(workflow_id) if workflow_id is not None else None
        if card is None:
            return AssistantTurn(
                text=text or "I could not match that request to a saved workflow.",
                action="clarify",
                raw=raw or "",
            )
        declared = card.declared_keys()
        # Keep only declared keys (plus the conventional free-form one).
        inputs = {k: v for k, v in inputs.items() if k in declared or k == "user_input"}
        for key in declared:
            value = inputs.get(key)
            empty = value is None or (isinstance(value, str) and not value.strip())
            if empty and key not in missing:
                missing.append(key)
        missing = [k for k in missing if k in declared]

    return AssistantTurn(
        text=text,
        action=action,
        workflow_id=workflow_id if action == "run_workflow" else None,
        inputs=inputs if action == "run_workflow" else {},
        missing_inputs=missing if action == "run_workflow" else [],
        raw=raw or "",
    )


# Verbs that signal "do something with a workflow" rather than small talk.
_ROUTE_VERBS = (
    "esegui", "eseguire", "lancia", "lanciare", "avvia", "avviare", "usa", "usare",
    "run", "launch", "execute", "start", "trigger",
)


def should_route(message: str, cards: Iterable[WorkflowCard]) -> bool:
    """Cheap pre-check so free chat only pays for the decision call when useful."""
    text = (message or "").lower()
    if not text.strip():
        return False
    for card in cards:
        name = (card.name or "").lower().strip()
        if len(name) >= 4 and name in text:
            return True
    if "workflow" in text:
        return True
    return any(re.search(rf"\b{re.escape(verb)}\b", text) for verb in _ROUTE_VERBS)


# ---------------------------------------------------------------------------
# History handling
# ---------------------------------------------------------------------------


def normalize_history(history: Optional[Iterable[Dict[str, Any]]]) -> List[Dict[str, str]]:
    """Keep only user/assistant text turns, bounded in count and length."""
    turns: List[Dict[str, str]] = []
    for item in history or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        if role not in ("user", "assistant"):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        turns.append({"role": role, "content": content[:MAX_MESSAGE_CHARS]})
    return turns[-MAX_HISTORY_MESSAGES:]


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class AssistantEngine:
    """Free chat + workflow decision on top of the existing ``MasterAI`` plumbing.

    ``MasterAI`` is reused (not duplicated) for model selection, retries,
    fallbacks and local-runtime handling.
    """

    def __init__(
        self,
        model_id: Optional[int] = None,
        complexity: str = "simple",
        master: Any = None,
        db: Any = None,
    ):
        self.model_id = model_id
        self.complexity = complexity
        self._master = master
        self._db = db

    # -- lazily created collaborators -------------------------------------

    @property
    def master(self) -> Any:
        if self._master is None:
            from core.master_ai import MasterAI

            self._master = MasterAI(model_id=self.model_id, complexity=self.complexity)
        return self._master

    @property
    def db(self) -> Any:
        if self._db is None:
            from core.db_manager import DBManager

            self._db = DBManager()
        return self._db

    def close(self) -> None:
        db = self._db
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
            self._db = None

    def catalogue(self) -> List[WorkflowCard]:
        return build_workflow_catalog(self.db)

    # -- calls -------------------------------------------------------------

    def _complete(
        self,
        messages: List[Dict[str, str]],
        *,
        temperature: float,
        force_json_mode: bool,
    ) -> str:
        master = self.master
        return master._call_llm_with_retry(
            model=master._get_model_string(),
            messages=messages,
            temperature=temperature,
            force_json_mode=force_json_mode,
        )

    def chat(
        self,
        user_message: str,
        history: Optional[Iterable[Dict[str, Any]]] = None,
        *,
        system_prompt: Optional[str] = None,
        temperature: float = 0.4,
        include_workflows: bool = True,
    ) -> str:
        """Free-form reply. Never starts a run and never forces JSON."""
        message = (user_message or "").strip()
        if not message:
            return ""

        system = system_prompt or ASSISTANT_PERSONA
        if include_workflows:
            try:
                names = ", ".join(c.name for c in self.catalogue())
            except Exception as exc:
                logger.warning("Could not load workflow catalogue for chat prompt: %s", exc)
                names = ""
            if names:
                system += f"\n\nSaved workflows you can mention: {names}."

        messages = [{"role": "system", "content": system}]
        messages.extend(normalize_history(history))
        messages.append({"role": "user", "content": message[:MAX_MESSAGE_CHARS]})

        logger.info("AssistantEngine.chat → %s", self.master._get_model_string())
        return (self._complete(messages, temperature=temperature, force_json_mode=False) or "").strip()

    def decide(
        self,
        user_message: str,
        history: Optional[Iterable[Dict[str, Any]]] = None,
        *,
        saved_context: str = "",
        temperature: float = 0.0,
    ) -> AssistantTurn:
        """Pick a saved workflow and extract its declared inputs (one JSON call)."""
        cards = self.catalogue()
        if not cards:
            return AssistantTurn(
                text="There are no saved workflows yet. Create one in Workflow Assembler first.",
                action="answer",
            )

        saved_context_block = (
            f"RECENT RUN CONTEXT (may be relevant):\n{str(saved_context)[:4000]}\n"
            if saved_context
            else ""
        )
        system = DECISION_PROMPT.format(
            saved_context=saved_context_block,
            catalogue=catalog_to_prompt(cards),
        )

        messages = [{"role": "system", "content": system}]
        messages.extend(normalize_history(history))
        messages.append({"role": "user", "content": (user_message or "").strip()[:MAX_MESSAGE_CHARS]})

        logger.info(
            "AssistantEngine.decide → %s (%d workflows)",
            self.master._get_model_string(),
            len(cards),
        )
        raw = self._complete(messages, temperature=temperature, force_json_mode=True)
        return parse_decision(raw, cards)


__all__ = [
    "AssistantEngine",
    "AssistantTurn",
    "WorkflowCard",
    "WorkflowInput",
    "build_workflow_catalog",
    "catalog_by_id",
    "catalog_to_prompt",
    "normalize_history",
    "normalize_required_inputs",
    "parse_decision",
    "should_route",
]
