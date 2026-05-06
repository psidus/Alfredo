import threading
import logging

logger = logging.getLogger(__name__)

# Dictionary to hold pending requests: {chat_id: {"event": Event, "answer": str}}
_pending_requests = {}

def request_human_input(chat_id: str, question: str) -> str:
    """Called by the agent tool. Blocks until an answer is provided."""
    from core.notification_manager import NotificationManager
    
    event = threading.Event()
    _pending_requests[chat_id] = {
        "event": event,
        "answer": None
    }
    
    notifier = NotificationManager()
    message = f"⚠️ <b>Agent Question:</b>\n{question}\n\n<i>Reply to this message to resume execution.</i>"
    notifier.send_telegram_notification(message, chat_id=chat_id)
    
    logger.info(f"Agent blocking for human input from {chat_id}...")
    event.wait() # Block the agent thread until event is set
    
    answer = _pending_requests[chat_id]["answer"]
    del _pending_requests[chat_id]
    return answer

def provide_human_input(chat_id: str, answer: str) -> bool:
    """Called by the Telegram bot when a user replies."""
    if chat_id in _pending_requests:
        _pending_requests[chat_id]["answer"] = answer
        _pending_requests[chat_id]["event"].set()
        return True
    return False

def has_pending_request(chat_id: str) -> bool:
    return chat_id in _pending_requests
