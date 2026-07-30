import os
import urllib.request
import json
import logging
from core.data_manager import load_env

logger = logging.getLogger(__name__)

# Telegram hard limits
TG_MESSAGE_MAX = 3900
TG_BUTTON_TEXT_MAX = 60


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

    def _post_send_message(self, payload: dict) -> bool:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                if response.status == 200:
                    return True
                body = response.read().decode("utf-8", errors="replace")
                logger.error(f"Failed to send Telegram notification: HTTP {response.status} — {body}")
                return False
        except Exception as e:
            logger.error(f"Failed to send Telegram notification: {e}")
            return False

    @staticmethod
    def _chunk_text(text: str, max_len: int = TG_MESSAGE_MAX) -> list:
        if len(text) <= max_len:
            return [text]
        chunks = []
        current = ""
        for line in text.split("\n"):
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) > max_len:
                if current:
                    chunks.append(current)
                while len(line) > max_len:
                    chunks.append(line[:max_len])
                    line = line[max_len:]
                current = line
            else:
                current = candidate
        if current:
            chunks.append(current)
        return chunks or [text[:max_len]]

    @staticmethod
    def _short_button_label(idx: int, opt: str) -> str:
        prefix = f"{idx + 1}. "
        budget = TG_BUTTON_TEXT_MAX - len(prefix)
        label = (opt or "").strip().replace("\n", " ")
        if len(label) > budget:
            label = label[: max(0, budget - 1)] + "…"
        return prefix + label

    def send_telegram_notification(self, message, chat_id=None, options=None):
        """Sends a notification message via Telegram Bot API using built-in urllib."""
        if not self.bot_token:
            logger.error("TELEGRAM_BOT_TOKEN not found in environment.")
            return False
        
        target_chat_id = chat_id or self.default_chat_id
        if not target_chat_id:
            logger.error("No chat_id available for notification.")
            return False

        # Ensure choices are visible in the message body (buttons are optional UX).
        options = [str(o) for o in (options or []) if str(o).strip()]
        full_message = message or ""
        if options:
            listed = "\n".join(f"{i + 1}. {opt}" for i, opt in enumerate(options))
            if listed not in full_message:
                full_message = f"{full_message.rstrip()}\n\n<b>Options:</b>\n{listed}"

        chunks = self._chunk_text(full_message)
        ok = True
        for i, chunk in enumerate(chunks):
            payload = {
                "chat_id": target_chat_id,
                "text": chunk,
                "parse_mode": "HTML",
            }
            # Attach buttons only on the last chunk
            if options and i == len(chunks) - 1:
                inline_keyboard = []
                for idx, opt in enumerate(options):
                    # Index-only callback_data stays well under Telegram's 64-byte limit.
                    callback_data = f"hitl_{idx}"
                    inline_keyboard.append([{
                        "text": self._short_button_label(idx, opt),
                        "callback_data": callback_data,
                    }])
                payload["reply_markup"] = {"inline_keyboard": inline_keyboard}

            sent = self._post_send_message(payload)
            if not sent and payload.get("reply_markup"):
                # Retry last chunk without buttons if markup caused rejection.
                logger.warning("Retrying HITL notification without inline buttons.")
                payload.pop("reply_markup", None)
                sent = self._post_send_message(payload)
            if not sent and payload.get("parse_mode"):
                payload.pop("parse_mode", None)
                sent = self._post_send_message(payload)
            ok = ok and sent

        if ok:
            logger.info(f"Notification sent to {target_chat_id} ({len(chunks)} chunk(s))")
        return ok

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
