import logging
import time
import threading
import uuid
from core.db_manager import DBManager

logger = logging.getLogger(__name__)

# --- Serialization Lock ---
# When tasks run in parallel and multiple need human validation,
# we must serialize the HITL requests so the user handles them one
# at a time. Without this, parallel threads would race on the same
# chat_id row in the database, overwriting each other's questions.
_hitl_lock = threading.Lock()

def request_human_input(chat_id: str, question: str, options: list = None, task_id: str = None) -> str:
    """
    Called by an agent tool or the crew_builder HITL check.
    
    Thread-safe: Uses a lock to serialize concurrent HITL requests.
    When multiple parallel tasks need human validation, they queue up
    and are presented to the user one at a time.
    
    1. Acquires the HITL lock (blocks if another task is already waiting).
    2. Records the request in the shared database (question + options).
    3. Sends a notification via Telegram (full options in body + short buttons).
    4. Polls the database until an answer is found.
    5. Releases the lock so the next waiting task can proceed.
    """
    from core.notification_manager import NotificationManager
    
    # Generate a unique request ID for logging/debugging
    request_id = task_id or str(uuid.uuid4())[:8]
    options = [str(o) for o in (options or []) if str(o).strip()]
    
    logger.info(f"HITL [{request_id}] Waiting to acquire lock for chat {chat_id}...")
    
    with _hitl_lock:
        logger.info(f"HITL [{request_id}] Lock acquired. Sending question to user.")
        
        # 1. Record the request in DB (including options for callback index resolution)
        with DBManager() as db:
            db.create_hitl_request(chat_id, question, options=options)
        
        # 2. Send notification (message body includes full options; buttons are short labels)
        notifier = NotificationManager()
        message = f"⚠️ <b>Human validation required</b>\n\n{question}\n\n<i>Tap a button or reply with the option number / your feedback.</i>"
        sent = notifier.send_telegram_notification(message, chat_id=chat_id, options=options)
        if not sent:
            logger.error(
                f"HITL [{request_id}] Failed to deliver Telegram notification to {chat_id}. "
                "Will keep polling — user can still reply with free text if they see another prompt."
            )
        
        logger.info(f"HITL [{request_id}] Blocking for human input from {chat_id}...")
        
        # 3. Poll the database for the answer (with timeout)
        HITL_TIMEOUT_SECONDS = 3600  # 1 hour max wait
        start_time = time.time()
        try:
            while True:
                elapsed = time.time() - start_time
                if elapsed >= HITL_TIMEOUT_SECONDS:
                    logger.warning(f"HITL [{request_id}] Timeout reached ({HITL_TIMEOUT_SECONDS}s) for {chat_id}. Resuming with no feedback.")
                    with DBManager() as db:
                        db.delete_hitl_request(chat_id)
                    return "SYSTEM_ABORT"
                
                # Check for abort signal
                from core.crew_builder import ABORT_FLAGS
                if chat_id and ABORT_FLAGS.get(str(chat_id)):
                    logger.info(f"HITL [{request_id}] Abort signal received for {chat_id}.")
                    with DBManager() as db:
                        db.delete_hitl_request(chat_id)
                    return "SYSTEM_ABORT"
                
                # Re-open connection each poll to ensure we see the latest data
                with DBManager() as db:
                    req = db.get_hitl_request(chat_id)
                    if req and req['status'] == 'replied':
                        answer = req['answer']
                        logger.info(f"HITL [{request_id}] Received human answer for {chat_id}: {answer}")
                        db.delete_hitl_request(chat_id)
                        return answer
                
                time.sleep(2)
        except KeyboardInterrupt:
            logger.warning(f"HITL [{request_id}] Polling interrupted.")
            return "SYSTEM_ABORT"

def provide_human_input(chat_id: str, answer: str) -> bool:
    """
    Called by the Telegram bot when a user replies.
    Updates the database record to unblock the waiting agent.
    Maps plain numeric replies (1..N) to stored option text when possible.
    """
    with DBManager() as db:
        req = db.get_hitl_request(chat_id)
        resolved = answer
        if req:
            options = req.get("options") or []
            text = (answer or "").strip()
            if text.isdigit() and options:
                idx = int(text) - 1
                if 0 <= idx < len(options):
                    resolved = options[idx]
        success = db.set_hitl_answer(chat_id, resolved)
        if success:
            logger.info(f"Successfully provided human input to DB for {chat_id}: {resolved}")
        return success

def has_pending_request(chat_id: str) -> bool:
    """Checks the database for any pending requests for this chat."""
    with DBManager() as db:
        req = db.get_hitl_request(chat_id)
        return req is not None and req['status'] == 'pending'
