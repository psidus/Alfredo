"""Re-notify a stuck pending HITL request (e.g. after button markup failure)."""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

from core.db_manager import DBManager
from core.notification_manager import NotificationManager


def main():
    chat_id = sys.argv[1] if len(sys.argv) > 1 else None
    db = DBManager()
    print("DB backend:", type(db).__name__)

    if chat_id:
        req = db.get_hitl_request(str(chat_id))
        rows = [req] if req else []
    else:
        db.cursor.execute("SELECT * FROM hitl_requests WHERE status = 'pending'")
        rows = [db._to_dict(r) for r in db.cursor.fetchall()]

    if not rows or rows == [None]:
        print("No pending HITL requests.")
        return 0

    notifier = NotificationManager()
    for req in rows:
        if not req:
            continue
        cid = req["chat_id"]
        question = req.get("question") or "Please validate the agent output."
        options = req.get("options")
        if options is None and req.get("options_json"):
            import json
            try:
                options = json.loads(req["options_json"])
            except Exception:
                options = []
        options = options or []

        # If options empty, offer numeric resume instructions only.
        msg = (
            f"⚠️ <b>Human validation still pending</b>\n\n{question}\n\n"
            f"<i>Reply with your choice / feedback to resume the workflow.</i>"
        )
        ok = notifier.send_telegram_notification(msg, chat_id=cid, options=options)
        print(f"Re-notified chat {cid}: sent={ok} options={len(options)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
