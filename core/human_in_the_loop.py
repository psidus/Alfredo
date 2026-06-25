import logging
import time
from core.db_manager import DBManager

logger = logging.getLogger(__name__)

def request_human_input(chat_id: str, question: str, options: list = None) -> str:
    """
    Called by an agent tool. 
    1. Records the request in the shared database.
    2. Sends a notification via Telegram.
    3. Polls the database until an answer is found.
    """
    from core.notification_manager import NotificationManager
    
    # 1. Record the request in DB
    with DBManager() as db:
        db.create_hitl_request(chat_id, question)
    
    # 2. Send notification
    notifier = NotificationManager()
    message = f"⚠️ <b>Agent Question:</b>\n{question}\n\n<i>Reply to this message to resume execution.</i>"
    notifier.send_telegram_notification(message, chat_id=chat_id, options=options)
    
    logger.info(f"Agent blocking for human input (DB-backed) from {chat_id}...")
    
    # 3. Poll the database for the answer (with timeout to prevent infinite blocking)
    HITL_TIMEOUT_SECONDS = 3600  # 1 hour max wait
    start_time = time.time()
    try:
        while True:
            elapsed = time.time() - start_time
            if elapsed >= HITL_TIMEOUT_SECONDS:
                logger.warning(f"HITL timeout reached ({HITL_TIMEOUT_SECONDS}s) for {chat_id}. Resuming with no feedback.")
                # Clean up the pending request
                with DBManager() as db:
                    db.delete_hitl_request(chat_id)
                return "SYSTEM_ABORT"
            
            # Re-open connection each poll to ensure we see the latest data on disk (SQLite)
            with DBManager() as db:
                req = db.get_hitl_request(chat_id)
                if req and req['status'] == 'replied':
                    answer = req['answer']
                    logger.info(f"Received human answer from DB for {chat_id}: {answer}")
                    db.delete_hitl_request(chat_id)
                    return answer
            
            time.sleep(2) 
    except KeyboardInterrupt:
        logger.warning("HITL polling interrupted.")
        return "SYSTEM_ABORT"

def provide_human_input(chat_id: str, answer: str) -> bool:
    """
    Called by the Telegram bot when a user replies.
    Updates the database record to unblock the waiting agent.
    """
    with DBManager() as db:
        success = db.set_hitl_answer(chat_id, answer)
        if success:
            logger.info(f"Successfully provided human input to DB for {chat_id}.")
        return success

def has_pending_request(chat_id: str) -> bool:
    """Checks the database for any pending requests for this chat."""
    with DBManager() as db:
        req = db.get_hitl_request(chat_id)
        return req is not None and req['status'] == 'pending'
