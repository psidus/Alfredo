"""
Assistant page: one place for free chat and saved workflows.

Two sections, one engine:

* **Chat**      — free conversation (Claude-like). When the message clearly
                  matches a saved workflow, the decision agent proposes it.
* **Workflows** — pick a workflow manually or let the decision agent choose
                  and pre-fill the declared inputs.

Both sections call ``core.assistant_engine`` and ``core.run_service``; nothing
here talks to CrewAI directly.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import streamlit as st

from core.assistant_engine import (
    AssistantEngine,
    AssistantTurn,
    WorkflowCard,
    build_workflow_catalog,
    should_route,
)
from core.run_service import (
    cancel_run,
    list_recent_runs,
    pending_interrupts,
    start_resume_interrupt,
    start_run,
)

logger = logging.getLogger(__name__)

SECTION_CHAT = "💬 Chat"
SECTION_WORKFLOWS = "🧩 Workflows"

STATUS_ICON = {
    "running": "🔄",
    "completed": "✅",
    "failed": "❌",
    "paused": "⏸️",
    "waiting_human": "🙋",
    "cancelled": "⏹️",
    "stopped": "⏹️",
}


# ---------------------------------------------------------------------------
# Engine + data helpers
# ---------------------------------------------------------------------------


@st.cache_resource(show_spinner=False)
def _engine_factory() -> AssistantEngine:
    return AssistantEngine()


def _get_engine() -> AssistantEngine:
    return _engine_factory()


def _catalogue_safe(engine: AssistantEngine) -> List[WorkflowCard]:
    try:
        return engine.catalogue()
    except Exception as exc:  # noqa: BLE001 - UI must never crash on DB issues
        logger.exception("Could not load workflow catalogue")
        st.error(f"Could not load workflows: {exc}")
        return []


def _workflow_names(db: Any = None) -> Dict[int, str]:
    try:
        return {c.id: c.name for c in build_workflow_catalog(db)}
    except Exception:
        return {}


def _reset_chat() -> None:
    st.session_state["assistant_messages"] = []
    st.session_state.pop("assistant_pending", None)


def _history_for_model() -> List[Dict[str, str]]:
    return [
        {"role": m["role"], "content": m["content"]}
        for m in st.session_state.get("assistant_messages", [])
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]


# ---------------------------------------------------------------------------
# Chat section
# ---------------------------------------------------------------------------


def _render_pending_workflow(engine: AssistantEngine, pending: AssistantTurn) -> None:
    """Inline card shown under a chat reply that matched a workflow."""
    cards = {c.id: c for c in _catalogue_safe(engine)}
    card = cards.get(pending.workflow_id or -1)
    if not card:
        return

    st.markdown("---")
    st.markdown(f"**Suggested workflow:** {card.name}")
    if pending.missing_inputs:
        st.caption(
            "Some declared inputs are missing: "
            + ", ".join(f"`{k}`" for k in pending.missing_inputs)
            + ". You can complete them in the **Workflows** section."
        )
    elif pending.inputs:
        st.caption("Recognized inputs: " + ", ".join(f"`{k}`" for k in pending.inputs))

    col_run, col_clear = st.columns([1, 1])
    with col_run:
        if st.button(
            f"▶ Run «{card.name}»",
            type="primary",
            use_container_width=True,
            key=f"chat_run_{card.id}",
        ):
            inputs = dict(pending.inputs or {})
            missing = [
                i.key
                for i in card.inputs
                if not str(inputs.get(i.key) or "").strip()
            ]
            if missing:
                st.warning("Please complete inputs first: " + ", ".join(missing))
            else:
                run_id = start_run(card.id, inputs, source="dashboard")
                st.session_state.pop("assistant_pending", None)
                st.success(f"Run **#{run_id}** started. Check status in the Workflows section.")
    with col_clear:
        if st.button("Dismiss", use_container_width=True, key="chat_clear_pending"):
            st.session_state.pop("assistant_pending", None)
            st.rerun()


def _render_chat_section(engine: AssistantEngine, cards: List[WorkflowCard]) -> None:
    st.caption(
        "Free conversation. If you ask for something matching a saved workflow, "
        "the assistant will suggest it."
    )
    top_left, top_right = st.columns([3, 1])
    with top_left:
        auto_route = st.toggle(
            "Automatically detect workflows",
            value=True,
            key="assistant_auto_route",
            help="Triggers an extra model call only when the message looks like a workflow request.",
        )
    with top_right:
        if st.button("🧹 New chat", use_container_width=True):
            _reset_chat()
            st.rerun()

    messages = st.session_state.setdefault("assistant_messages", [])
    for msg in messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    pending = st.session_state.get("assistant_pending")
    if isinstance(pending, AssistantTurn):
        _render_pending_workflow(engine, pending)

    prompt = st.chat_input("Message the assistant…", key="assistant_chat_input")
    if not prompt:
        return

    history = _history_for_model()
    messages.append({"role": "user", "content": prompt})

    try:
        with st.spinner("Thinking…"):
            reply = engine.chat(prompt, history=history)
    except Exception as exc:  # noqa: BLE001 - show the real error
        logger.exception("Assistant chat failed")
        reply = f"⚠️ Model error: {exc}"

    turn: Optional[AssistantTurn] = None
    if auto_route and cards and should_route(prompt, cards):
        try:
            with st.spinner("Finding a matching workflow…"):
                candidate = engine.decide(prompt, history=history)
            if candidate.action == "run_workflow" and candidate.workflow_id:
                turn = candidate
                if not reply and candidate.text:
                    reply = candidate.text
        except Exception:
            logger.exception("Assistant routing failed (chat continues)")

    messages.append({"role": "assistant", "content": reply or "(no response)"})
    if turn is not None:
        st.session_state["assistant_pending"] = turn
    else:
        st.session_state.pop("assistant_pending", None)

    st.rerun()


# ---------------------------------------------------------------------------
# Workflow section
# ---------------------------------------------------------------------------


def _render_inputs(card: WorkflowCard, prefix: str, prefill: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    values: Dict[str, str] = {}
    prefill = prefill or {}
    for inp in card.inputs:
        current = str(prefill.get(inp.key) or "")
        values[inp.key] = st.text_input(
            inp.prompt or inp.key,
            value=current,
            key=f"{prefix}_{card.id}_{inp.key}",
            placeholder=inp.key,
        )
    return values


def _run_and_report(card: WorkflowCard, inputs: Dict[str, str]) -> None:
    missing = [k for k, v in inputs.items() if not str(v).strip()]
    if missing:
        st.warning("Please fill required inputs: " + ", ".join(f"`{m}`" for m in missing))
        return
    try:
        run_id = start_run(card.id, inputs, source="dashboard")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Could not start run")
        st.error(f"Failed to start run: {exc}")
        return
    st.success(f"Run **#{run_id}** started for «{card.name}».")


def _render_manual_workflows(cards: List[WorkflowCard]) -> None:
    by_id = {c.id: c for c in cards}
    card = st.selectbox(
        "Workflow",
        options=list(by_id.keys()),
        format_func=lambda cid: by_id[cid].name,
        key="assistant_wf_select",
    )
    selected = by_id[card]
    if selected.description:
        st.caption(selected.description)

    prefill = st.session_state.pop("assistant_prefill", None) or {}
    inputs = _render_inputs(selected, "wf_manual", prefill)

    if st.button("▶ Run workflow", type="primary", key=f"wf_run_{selected.id}"):
        _run_and_report(selected, inputs)


def _render_decided_workflows(cards: List[WorkflowCard], engine: AssistantEngine) -> None:
    st.caption(
        "Describe your goal: the decision agent picks a workflow and pre-fills inferred inputs. "
        "You can edit them before running."
    )
    request = st.text_area(
        "What would you like to do?",
        key="assistant_request",
        placeholder="e.g. analyze thermal documents and prepare an Excel report…",
        height=100,
    )
    if st.button("🧠 Select workflow", key="assistant_decide_btn"):
        if not request.strip():
            st.warning("Please enter a request first.")
        else:
            try:
                with st.spinner("Selecting matching workflow…"):
                    st.session_state["assistant_decision"] = engine.decide(request)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Workflow decision failed")
                st.error(f"Decision failed: {exc}")

    turn = st.session_state.get("assistant_decision")
    if not isinstance(turn, AssistantTurn):
        return

    if turn.text:
        st.info(turn.text)

    if turn.action != "run_workflow" or not turn.workflow_id:
        return

    by_id = {c.id: c for c in cards}
    card = by_id.get(turn.workflow_id)
    if not card:
        st.warning("The selected workflow no longer exists.")
        return

    st.markdown(f"**Selected workflow:** {card.name}")
    inputs = _render_inputs(card, "wf_decided", turn.inputs)
    if turn.missing_inputs:
        st.caption(
            "To complete: " + ", ".join(f"`{k}`" for k in turn.missing_inputs)
        )
    if st.button("▶ Run selected workflow", type="primary", key=f"wf_decided_run_{card.id}"):
        _run_and_report(card, inputs)


def _render_recent_runs() -> None:
    st.divider()
    header_left, header_right = st.columns([3, 1])
    with header_left:
        st.subheader("Recent runs")
    with header_right:
        if st.button("🔄 Refresh", use_container_width=True):
            st.rerun()

    try:
        runs = list_recent_runs(limit=8)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Could not load recent runs")
        st.error(f"Could not load runs: {exc}")
        return

    if not runs:
        st.info("No runs recorded.")
        return

    names = _workflow_names()
    for run in runs:
        status = str(run.get("status") or "unknown").lower()
        icon = STATUS_ICON.get(status, "❓")
        name = names.get(run.get("workflow_id"), f"Workflow {run.get('workflow_id')}")
        label = f"{icon} Run #{run.get('id')} · {name} · {status}"
        with st.expander(label):
            st.caption(f"Started: {run.get('started_at') or '—'} · Source: {run.get('source') or '—'}")
            if run.get("inputs"):
                st.markdown("**Inputs**")
                st.json(run["inputs"])
            text = run.get("display_text") or ""
            if text:
                st.markdown("**Result**")
                st.markdown(text)
            elif status in ("running", "paused", "waiting_human"):
                if status == "waiting_human":
                    st.caption("⏸️ Waiting for approval: respond in the **Pending approvals** section above.")
                else:
                    st.caption("Running… click Refresh to check status.")


def _render_workflow_section(engine: AssistantEngine, cards: List[WorkflowCard]) -> None:
    if not cards:
        st.info("No saved workflows. Create one in **Workflow Assembler**.")
        return

    mode = st.radio(
        "Mode",
        ["Manual selection", "Assistant decides"],
        horizontal=True,
        key="assistant_wf_mode",
    )
    if mode == "Manual selection":
        _render_manual_workflows(cards)
    else:
        _render_decided_workflows(cards, engine)

    _render_recent_runs()


# ---------------------------------------------------------------------------
# Pending approvals (persistent interrupts)
# ---------------------------------------------------------------------------


def _render_pending_approvals() -> None:
    """Approval queue shared with Telegram: answers resume the paused run."""
    try:
        pending = pending_interrupts()
    except Exception as exc:  # noqa: BLE001 - UI must never crash on DB issues
        logger.exception("Could not load pending approvals")
        st.error(f"Could not load approvals: {exc}")
        return

    if not pending:
        return

    with st.container():
        st.markdown(f"#### 🙋 Pending approvals ({len(pending)})")
        st.caption(
            "Runs paused waiting for human feedback. The request is persisted in the database: "
            "you can reply here, from Telegram, or from the mobile app, even after restart."
        )
        for intr in pending:
            _render_single_approval(intr)


def _render_single_approval(intr: Dict[str, Any]) -> None:
    iid = intr.get("id")
    run_id = intr.get("run_id")
    node_id = str(intr.get("node_id") or "") or "—"
    st.markdown(f"**Run #{run_id}** · node `{node_id}`")
    question = str(intr.get("question") or "Confirm?")
    st.write(question)

    options = [str(o) for o in (intr.get("options") or [])]
    if options:
        cols = st.columns(min(len(options), 4))
        for i, opt in enumerate(options):
            if cols[i % len(cols)].button(
                opt,
                key=f"approve_opt_{iid}_{i}",
                use_container_width=True,
                type="primary" if i == 0 else "secondary",
            ):
                start_resume_interrupt(iid, opt)
                st.rerun()

    text_key = f"approve_text_{iid}"
    text = st.text_input(
        "Or reply with text (e.g. 'ok' to approve, or provide feedback)",
        key=text_key,
        placeholder="ok",
    )
    col_send, col_cancel = st.columns([1, 1])
    with col_send:
        if st.button("📨 Send", key=f"approve_send_{iid}", use_container_width=True):
            if not text.strip():
                st.warning("Please enter a response.")
            else:
                start_resume_interrupt(iid, text.strip())
                st.rerun()
    with col_cancel:
        if st.button("⏹️ Cancel run", key=f"approve_cancel_{iid}", use_container_width=True):
            cancel_run(run_id)
            st.rerun()
    st.divider()


# ---------------------------------------------------------------------------
# Page entry point
# ---------------------------------------------------------------------------


def render_assistant(compact: bool = False, *args, **kwargs) -> None:
    """Page entry point.

    ``compact`` is used when the assistant is rendered inside the toolbar dialog:
    the copy gets tighter so the chat surface stays usable in a modal.
    """
    if compact:
        st.markdown("#### 🤖 Assistant")
    else:
        st.title("🤖 Assistant")
    st.caption(
        "Free chat and saved workflows, one engine. Runs go through `run_service`, so they are "
        "visible and cancellable everywhere."
    )

    try:
        engine = _get_engine()
    except Exception as exc:  # noqa: BLE001 - e.g. missing API key
        logger.exception("Assistant engine could not start")
        st.error(
            "Assistant is unavailable. Please configure at least one model in **API Vault**.\n\n"
            f"Details: {exc}"
        )
        return

    cards = _catalogue_safe(engine)

    section = st.radio(
        "Section",
        [SECTION_CHAT, SECTION_WORKFLOWS],
        horizontal=True,
        key="assistant_section",
        label_visibility="collapsed",
    )
    st.divider()

    _render_pending_approvals()

    if section == SECTION_CHAT:
        _render_chat_section(engine, cards)
    else:
        _render_workflow_section(engine, cards)


__all__ = ["render_assistant"]
