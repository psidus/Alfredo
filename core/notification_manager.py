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

    def send_telegram_notification(self, message, chat_id=None, options=None):
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
        
        if options:
            inline_keyboard = []
            for opt in options:
                # Telegram callback_data limit is 64 bytes. "hitl_" is 5 bytes. 59 bytes remaining.
                callback_data = f"hitl_{opt}"[:64]
                inline_keyboard.append([{"text": opt, "callback_data": callback_data}])
            payload["reply_markup"] = {"inline_keyboard": inline_keyboard}
        
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

    def send_file_notification(self, file_path: str, caption: str = "", chat_id=None) -> bool:
        """
        Sends a file (document or image) to the Telegram operator.
        Automatically selects sendPhoto for images and sendDocument for other files.
        """
        if not self.bot_token:
            logger.error("TELEGRAM_BOT_TOKEN not found in environment.")
            return False

        target_chat_id = chat_id or self.default_chat_id
        if not target_chat_id:
            logger.error("No chat_id available for file notification.")
            return False

        if not os.path.isfile(file_path):
            logger.error(f"File not found for Telegram send: {file_path}")
            return False

        # Choose endpoint based on file type
        image_extensions = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
        ext = os.path.splitext(file_path)[1].lower()
        endpoint = "sendPhoto" if ext in image_extensions else "sendDocument"
        field_name = "photo" if ext in image_extensions else "document"

        url = f"https://api.telegram.org/bot{self.bot_token}/{endpoint}"

        try:
            import urllib.parse
            import http.client
            import mimetypes

            boundary = "----TelegramBoundary7Ma4YWxkTrZu0gW"
            filename = os.path.basename(file_path)
            mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"

            with open(file_path, "rb") as f:
                file_data = f.read()

            body_parts = []
            # Add caption field
            if caption:
                body_parts.append(
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="caption"\r\n\r\n'
                    f"{caption}\r\n"
                )
                body_parts.append(
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="parse_mode"\r\n\r\n'
                    f"HTML\r\n"
                )
            # Add chat_id field
            body_parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
                f"{target_chat_id}\r\n"
            )

            body_str = "".join(body_parts).encode("utf-8")
            file_part = (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
                f"Content-Type: {mime_type}\r\n\r\n"
            ).encode("utf-8")
            closing = f"\r\n--{boundary}--\r\n".encode("utf-8")

            body = body_str + file_part + file_data + closing
            content_type = f"multipart/form-data; boundary={boundary}"

            parsed = urllib.parse.urlparse(url)
            conn = http.client.HTTPSConnection(parsed.netloc, timeout=30)
            conn.request("POST", parsed.path, body=body, headers={"Content-Type": content_type})
            response = conn.getresponse()

            if response.status == 200:
                logger.info(f"File '{filename}' sent to Telegram ({target_chat_id})")
                return True
            else:
                resp_body = response.read().decode("utf-8", errors="replace")
                logger.error(f"Telegram file send failed: HTTP {response.status} — {resp_body}")
                return False

        except Exception as e:
            logger.error(f"Error sending file to Telegram: {e}", exc_info=True)
            return False
