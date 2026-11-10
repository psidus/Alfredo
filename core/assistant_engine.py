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

ASSISTANT_PERSONA = """You are Alfredo, an autonomous AI personal assistant running on the user's machine.
Your underlying model is {model_name}.
You can chat, give advice, inspect and manage files, and directly trigger and monitor workflows.

CAPABILITIES & TOOLS:
You have direct access to tools. When you need to perform an action (execute a workflow, check runs, read/write files), output a tool call block using this exact format:

```tool_call
{{"tool": "<tool_name>", "args": {{<arguments>}}}}
```

Available tools:
1. `list_workflows()`
   Lists all saved workflows with their IDs, names, descriptions, and required inputs.
   Example:
   ```tool_call
   {{"tool": "list_workflows", "args": {{}}}}
   ```

2. `start_workflow(workflow, inputs)`
   Starts a workflow execution immediately in the background.
   - `workflow`: Workflow ID (integer, e.g. 1) or workflow name (string).
   - `inputs`: Dictionary mapping required input keys to values (e.g. {{"idea": "portable solar coffee machine"}}).
   Example:
   ```tool_call
   {{"tool": "start_workflow", "args": {{"workflow": 1, "inputs": {{"idea": "portable solar coffee machine"}}}}}}
   ```

3. `get_run_status(run_id)`
   Checks the status, progress, outputs, and any error message of a workflow run.
   - `run_id`: Integer ID of the run.
   Example:
   ```tool_call
   {{"tool": "get_run_status", "args": {{"run_id": 1}}}}
   ```

4. `list_recent_runs(limit=5)`
   Lists recent workflow runs with their status and workflow name.
   Example:
   ```tool_call
   {{"tool": "list_recent_runs", "args": {{"limit": 5}}}}
   ```

5. `list_files(directory="")`
   Lists files and directories inside the workspace.
   Example:
   ```tool_call
   {{"tool": "list_files", "args": {{"directory": ""}}}}
   ```

6. `read_file(path)`
   Reads the content/preview of a file inside the workspace (reports, markdown, code, etc.).
   Example:
   ```tool_call
   {{"tool": "read_file", "args": {{"path": "report.md"}}}}
   ```

7. `write_file(path, content)`
   Creates or updates a file inside the workspace.
   Example:
   ```tool_call
   {{"tool": "write_file", "args": {{"path": "notes.md", "content": "# Notes\\n..."}}}}
   ```

Rules & Behavior:
- Answer naturally, helpfully, and concisely in the same language as the user (Italian by default if addressed in Italian).
- If the user asks to run or start a workflow (e.g. "avvia startup evaluation per..."):
  1. Extract the inputs from the user request.
  2. Call `start_workflow` directly.
  3. If you need to see available workflows or input keys first, call `list_workflows`.
  4. Once started, inform the user that the workflow has been started and provide the Run ID.
- If the user asks about files or past runs:
  Call `list_files`, `read_file`, `list_recent_runs`, or `get_run_status`.
- Never invent facts about files, data, or run outputs without checking with tools.
- If no tool is needed (general conversation, reasoning, explanations), reply directly without using ```tool_call```.
"""


class AssistantChatResult(str):
    """String result of a chat turn that also carries execution metadata."""

    text: str
    started_run_id: Optional[int]
    workflow_suggestion: Optional[Any]

    def __new__(
        cls,
        text: str = "",
        started_run_id: Optional[int] = None,
        workflow_suggestion: Optional[Any] = None,
    ):
        instance = super().__new__(cls, text)
        instance.text = text
        instance.started_run_id = started_run_id
        instance.workflow_suggestion = workflow_suggestion
        return instance


def extract_tool_call(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    # 1. Look for ```tool_call ... ``` or ```json ... ```
    match = re.search(r"```(?:tool_call|json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            if isinstance(data, dict) and ("tool" in data or "name" in data):
                return data
        except Exception:
            pass

    # 2. Look for <tool_call> ... </tool_call>
    match2 = re.search(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", text, re.DOTALL)
    if match2:
        try:
            data = json.loads(match2.group(1))
            if isinstance(data, dict) and ("tool" in data or "name" in data):
                return data
        except Exception:
            pass

    # 3. Look for bare JSON with "tool" key
    tool_idx = text.find('"tool"')
    if tool_idx != -1:
        start = text.rfind("{", 0, tool_idx)
        if start != -1:
            depth = 0
            for i in range(start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            data = json.loads(text[start : i + 1])
                            if isinstance(data, dict) and "tool" in data:
                                return data
                        except Exception:
                            pass
                        break
    return None


def strip_tool_call(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"```(?:tool_call|json)?\s*\{.*?\}\s*```", "", text, flags=re.DOTALL)
    cleaned = re.sub(r"<tool_call>\s*\{.*?\}\s*</tool_call>", "", cleaned, flags=re.DOTALL)
    return cleaned.strip()

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

    def _execute_tool(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a local tool safely and return JSON-serializable dictionary."""
        name = (tool_name or "").strip().lower()
        args = args or {}
        try:
            if name == "list_workflows":
                cards = self.catalogue()
                return {
                    "workflows": [
                        {
                            "id": c.id,
                            "name": c.name,
                            "description": c.description,
                            "inputs": [i.key for i in c.inputs],
                        }
                        for c in cards
                    ]
                }

            if name == "start_workflow":
                from core.run_service import start_run

                wf_target = args.get("workflow") or args.get("workflow_id") or args.get("id") or args.get("name")
                raw_inputs = args.get("inputs") or {}
                if not isinstance(raw_inputs, dict):
                    raw_inputs = {"user_input": str(raw_inputs)}

                cards = self.catalogue()
                target_card = None
                try:
                    wf_id = int(wf_target)
                    target_card = next((c for c in cards if c.id == wf_id), None)
                except (ValueError, TypeError):
                    pass

                if target_card is None and wf_target:
                    query = str(wf_target).lower().strip()
                    for c in cards:
                        c_name = c.name.lower().strip()
                        if query in c_name or c_name in query:
                            target_card = c
                            break

                if target_card is None:
                    return {
                        "error": f"Workflow '{wf_target}' non trovato.",
                        "available_workflows": [f"#{c.id}: {c.name}" for c in cards],
                    }

                declared = target_card.declared_keys()
                cleaned_inputs = dict(raw_inputs)
                if declared and len(declared) == 1 and declared[0] not in cleaned_inputs:
                    for val in raw_inputs.values():
                        cleaned_inputs[declared[0]] = val
                        break

                run_id = start_run(target_card.id, inputs=cleaned_inputs, source="assistant-chat")
                return {
                    "status": "started",
                    "run_id": run_id,
                    "workflow_id": target_card.id,
                    "workflow_name": target_card.name,
                    "inputs": cleaned_inputs,
                }

            if name == "get_run_status":
                from core.run_service import get_run
                from core.files import describe_run_exports

                run_id = int(args.get("run_id") or args.get("id") or 0)
                if not run_id:
                    from core.run_service import list_recent_runs
                    recent = list_recent_runs(limit=1, db=self.db)
                    if recent and recent[0].get("id"):
                        run_id = int(recent[0]["id"])
                    else:
                        return {"error": "Nessuna esecuzione trovata nel sistema."}
                run = get_run(run_id, db=self.db)
                if not run:
                    return {"error": f"Run #{run_id} non trovata"}
                exports = describe_run_exports(run)
                return {
                    "run_id": run_id,
                    "status": run.get("status"),
                    "workflow_id": run.get("workflow_id"),
                    "result": run.get("result"),
                    "current_task_idx": run.get("current_task_idx"),
                    "started_at": str(run.get("started_at")),
                    "finished_at": str(run.get("finished_at")),
                    "exported_files": [e.get("name") for e in exports],
                }

            if name == "list_recent_runs":
                from core.run_service import list_recent_runs

                limit = int(args.get("limit") or 5)
                runs = list_recent_runs(limit=limit, db=self.db)
                return {
                    "recent_runs": [
                        {
                            "run_id": r.get("id"),
                            "workflow_id": r.get("workflow_id"),
                            "status": r.get("status"),
                            "started_at": str(r.get("started_at")),
                        }
                        for r in runs
                    ]
                }

            if name == "list_files":
                from core.files import list_files

                directory = str(args.get("directory") or args.get("rel_path") or args.get("path") or "").strip()
                result_listing = list_files(directory)
                raw_entries = result_listing.get("entries") if isinstance(result_listing, dict) else result_listing
                return {
                    "directory": directory or "workspace",
                    "files": [
                        {
                            "name": e["name"],
                            "path": e["path"],
                            "is_dir": e["is_dir"],
                            "size": e["size"],
                            "kind": e["kind"],
                        }
                        for e in (raw_entries or [])
                        if isinstance(e, dict)
                    ],
                }

            if name == "read_file":
                from core.files import preview

                path = str(args.get("path") or args.get("filename") or args.get("rel_path") or "").strip()
                if not path:
                    return {"error": "path is required"}
                data = preview(path, max_chars=4000)
                content = data.get("text") or data.get("rows") or data.get("message") or ""
                return {
                    "path": path,
                    "kind": data.get("kind"),
                    "content": content,
                    "truncated": data.get("truncated", False),
                }

            if name == "write_file":
                from core.files import write_file

                path = str(args.get("path") or args.get("filename") or args.get("rel_path") or "").strip()
                content = str(args.get("content") or "")
                if not path:
                    return {"error": "path is required"}
                info = write_file(path, content)
                return {"status": "success", "file": info}

            return {"error": f"Unknown tool: '{name}'"}
        except Exception as exc:
            logger.warning("Tool execution error for %s: %s", name, exc, exc_info=True)
            return {"error": str(exc)}

    def chat(
        self,
        user_message: str,
        history: Optional[Iterable[Dict[str, Any]]] = None,
        *,
        system_prompt: Optional[str] = None,
        temperature: float = 0.4,
        include_workflows: bool = True,
        max_tool_iterations: int = 4,
    ) -> AssistantChatResult:
        """Complete conversational assistant with tool execution for workflows and files."""
        message = (user_message or "").strip()
        if not message:
            return AssistantChatResult(text="")

        model_name = self.master._get_model_string()
        system = system_prompt or ASSISTANT_PERSONA.format(model_name=model_name)
        if include_workflows:
            try:
                cards = self.catalogue()
                names = ", ".join(f"[{c.id}] {c.name}" for c in cards)
            except Exception as exc:
                logger.warning("Could not load workflow catalogue for chat prompt: %s", exc)
                names = ""
            if names:
                system += f"\n\nCurrently saved workflows: {names}."

        messages = [{"role": "system", "content": system}]
        messages.extend(normalize_history(history))
        messages.append({"role": "user", "content": message[:MAX_MESSAGE_CHARS]})

        logger.info("AssistantEngine.chat → %s", model_name)

        started_run_id: Optional[int] = None
        iteration = 0
        final_text = ""
        raw_reply = ""

        while iteration < max_tool_iterations:
            iteration += 1
            raw_reply = (self._complete(messages, temperature=temperature, force_json_mode=False) or "").strip()
            tool_call = extract_tool_call(raw_reply)

            if not tool_call:
                # No tool called: model produced its final direct response
                final_text = strip_tool_call(raw_reply) or raw_reply
                break

            tool_name = tool_call.get("tool") or tool_call.get("name") or ""
            tool_args = tool_call.get("args") or tool_call.get("arguments") or tool_call.get("parameters") or {}
            logger.info("AssistantEngine executing tool '%s' with args: %s", tool_name, tool_args)

            tool_result = self._execute_tool(tool_name, tool_args)
            if str(tool_name).lower() == "start_workflow" and tool_result.get("run_id"):
                started_run_id = int(tool_result["run_id"])

            messages.append({"role": "assistant", "content": raw_reply})
            messages.append({
                "role": "user",
                "content": (
                    f"[Tool Result for '{tool_name}']:\n"
                    f"{json.dumps(tool_result, ensure_ascii=False, indent=2)}\n\n"
                    f"Based on this result, provide your clear response to the user."
                ),
            })

        if not final_text:
            final_text = strip_tool_call(raw_reply) or raw_reply

        return AssistantChatResult(
            text=final_text.strip(),
            started_run_id=started_run_id,
        )

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
    "AssistantChatResult",
    "AssistantEngine",
    "AssistantTurn",
    "WorkflowCard",
    "WorkflowInput",
    "build_workflow_catalog",
    "catalog_by_id",
    "catalog_to_prompt",
    "extract_tool_call",
    "normalize_history",
    "normalize_required_inputs",
    "parse_decision",
    "should_route",
    "strip_tool_call",
]
