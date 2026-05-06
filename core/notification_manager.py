import os
import urllib.request
import json
import logging
from core.data_manager import load_env

logger = logging.getLogger(__name__)

class NotificationManager:
    def __init__(self):
        load_env()
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        
        # Handle potentially empty allowed_ids robustly
        allowed_ids_str = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")
        if allowed_ids_str and allowed_ids_str.strip():
            self.allowed_ids = allowed_ids_str.split(",")
            self.default_chat_id = self.allowed_ids[0].strip() if self.allowed_ids else None
        else:
            self.allowed_ids = []
            self.default_chat_id = None

    def send_telegram_notification(self, message, chat_id=None):
        """Sends a notification message via Telegram Bot API using built-in urllib."""
        if not self.bot_token:
            logger.error("TELEGRAM_BOT_TOKEN not found in environment.")
            return False
        
        target_chat_id = chat_id or self.default_chat_id
        if not target_chat_id:
            logger.error("No chat_id available for notification.")
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": target_chat_id,
            "text": message,
            "parse_mode": "HTML"
        }
        
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'}, method='POST')

        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status == 200:
                    logger.info(f"Notification sent to {target_chat_id}")
                    return True
                else:
                    logger.error(f"Failed to send Telegram notification: HTTP {response.status}")
                    return False
        except Exception as e:
            logger.error(f"Failed to send Telegram notification: {e}")
            return False

    def notify_workflow_completion(self, workflow_name, result, chat_id=None):
        """Specific helper for workflow completion."""
        # Truncate result for notification if too long
        display_result = (str(result)[:500] + '...') if len(str(result)) > 500 else str(result)
        
        message = (
            f"✅ <b>Workflow Completed!</b>\n\n"
            f"🎯 <b>Workflow:</b> {workflow_name}\n"
            f"📝 <b>Result:</b>\n<pre>{display_result}</pre>"
        )
        return self.send_telegram_notification(message, chat_id)
