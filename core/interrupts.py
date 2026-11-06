"""
Persistent run interrupts (human approvals) that survive restarts.

The old flow blocked the node thread on ``human_in_the_loop.request_human_input``
for up to an hour, tied to a Telegram ``chat_id``. A run that needed approval
could not be resumed after a restart, and only one approval at a time was
possible per chat.

An *interrupt* is a row in ``run_interrupts``:

* the graph engine saves the question, the options and the state needed to
  resume, sets the run to ``waiting_human`` and releases the thread;
* Telegram, the dashboard or the API answer it;
* ``core.run_service.resume_interrupt`` applies the answer and resumes the run.

This keeps the engine transport-agnostic.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Answers that mean "no edits, continue": they must not trigger an LLM rewrite.
APPROVAL_WORDS = (
    "approve", "approved", "approva", "approvato", "ok", "okay", "yes", "y",
    "si", "sì", "procedi", "continua", "lgtm", "confermo", "corretto",
    "perfect", "perfetto", "✅",
)
APPROVAL_PHRASES = ("va bene", "looks good", "go ahead", "all good", "do it")
NEGATIONS = {"no", "non", "nope", "nemmeno", "invece"}


def is_approval_answer(answer: str) -> bool:
    text = " ".join((answer or "").strip().lower().split())
    if not text:
        return True
    if text in APPROVAL_WORDS:
        return True
    words = [w.strip(".,!?:;()[]\"'") for w in text.split()]
    # A long message is feedback, not a plain approval.
    if len(words) > 6:
        return False
    if any(w in NEGATIONS for w in words):
        return False
    if any(w in APPROVAL_WORDS for w in words):
        return True
    return any(phrase in text for phrase in APPROVAL_PHRASES)


def open_interrupt(
    *,
    run_id: int,
    node_id: str = "",
    task_id: Optional[int] = None,
    chat_id: Optional[str] = None,
    kind: str = "hitl",
    question: str = "",
    options: Optional[List[str]] = None,
    payload: Optional[Dict[str, Any]] = None,
    db: Any = None,
) -> int:
    """Persist a pending approval and return its id."""
    owns_db = db is None
    if db is None:
        from core.db_manager import DBManager

        db = DBManager()
    try:
        interrupt_id = int(
            db.create_run_interrupt(
                run_id=int(run_id),
                node_id=node_id or None,
                task_id=task_id,
                chat_id=str(chat_id) if chat_id is not None else None,
                kind=kind,
                question=question,
                options=list(options or []),
                payload=payload or {},
            )
        )
        from core import events

        events.emit(
            int(run_id),
            events.INTERRUPT_OPENED,
            {
                "interrupt_id": interrupt_id,
                "node_id": node_id or "",
                "task_id": task_id,
                "kind": kind,
                "question": (question or "")[:400],
                "options": list(options or [])[:10],
            },
            db=db,
        )
    finally:
        if owns_db:
            db.close()

    # Best effort, after the DB handle is released: never block the engine on push.
    try:
        from core.push import send_approval_notification_async

        workflow_name = str((payload or {}).get("workflow_name") or "")
        send_approval_notification_async(
            run_id=int(run_id),
            interrupt_id=interrupt_id,
            question=question or "",
            options=list(options or []),
            workflow_name=workflow_name,
        )
    except Exception as exc:  # noqa: BLE001 - notifications are optional
        logger.warning("Could not queue push notification: %s", exc)

    return interrupt_id


def get_interrupt(interrupt_id: int, db: Any = None) -> Optional[Dict[str, Any]]:
    owns_db = db is None
    if db is None:
        from core.db_manager import DBManager

        db = DBManager()
    try:
        return db.get_run_interrupt(int(interrupt_id))
    finally:
        if owns_db:
            db.close()


def pending_interrupts(
    *,
    run_id: Optional[int] = None,
    chat_id: Optional[str] = None,
    db: Any = None,
) -> List[Dict[str, Any]]:
    owns_db = db is None
    if db is None:
        from core.db_manager import DBManager

        db = DBManager()
    try:
        return db.get_pending_interrupts(run_id=run_id, chat_id=chat_id) or []
    finally:
        if owns_db:
            db.close()


def cancel_for_run(run_id: int, db: Any = None) -> int:
    owns_db = db is None
    if db is None:
        from core.db_manager import DBManager

        db = DBManager()
    try:
        return int(db.cancel_run_interrupts(int(run_id)) or 0)
    finally:
        if owns_db:
            db.close()


def resolve_option(interrupt: Dict[str, Any], answer: str) -> str:
    """Map a numeric reply (``1``..``N``) to the stored option text."""
    options = (interrupt or {}).get("options") or []
    text = (answer or "").strip()
    if text.isdigit() and options:
        idx = int(text) - 1
        if 0 <= idx < len(options):
            return str(options[idx])
    return text


def notify_interrupt(interrupt: Dict[str, Any], chat_id: Optional[str] = None) -> bool:
    """Best-effort Telegram notification for a pending interrupt.

    Uses ``rint_<id>_<idx>`` callbacks so a button click resolves the right
    interrupt even when several runs are waiting for the same chat.
    """
    if not interrupt:
        return False
    try:
        from core.notification_manager import NotificationManager

        run_id = interrupt.get("run_id")
        node = interrupt.get("node_id") or ""
        message = (
            "⚠️ <b>Human validation required</b>\n\n"
            f"{interrupt.get('question') or ''}\n\n"
            f"<i>Run #{run_id}{f' · node {node}' if node else ''}</i>"
        )
        return NotificationManager().send_telegram_notification(
            message,
            chat_id=chat_id or interrupt.get("chat_id"),
            options=interrupt.get("options") or [],
            callback_prefix=f"rint_{interrupt.get('id')}",
        )
    except Exception as exc:  # noqa: BLE001 - notification is best effort
        logger.warning("Could not notify interrupt %s: %s", interrupt.get("id"), exc)
        return False


__all__ = [
    "is_approval_answer",
    "open_interrupt",
    "get_interrupt",
    "pending_interrupts",
    "cancel_for_run",
    "resolve_option",
    "notify_interrupt",
]
