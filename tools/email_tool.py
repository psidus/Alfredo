"""
tools/email_tool.py

Agent tools for reading and sending email via Outlook/Office365 using SMTP and IMAP.

SECURITY MODEL:
  - SEND operations always require human confirmation via Telegram before execution.
  - READ operations (read_emails, search_emails) do not require confirmation.
  - Credentials are loaded from environment variables (never hardcoded):
      OUTLOOK_EMAIL         — your Outlook email address
      OUTLOOK_APP_PASSWORD  — app password (not your main account password)
      OUTLOOK_IMAP_SERVER   — defaults to 'outlook.office365.com'
      OUTLOOK_SMTP_SERVER   — defaults to 'smtp.office365.com'
      OUTLOOK_SMTP_PORT     — defaults to 587
      OUTLOOK_IMAP_PORT     — defaults to 993
"""

import os
import logging
import imaplib
import smtplib
import email as email_lib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from crewai.tools import tool

logger = logging.getLogger(__name__)


def _get_outlook_credentials() -> dict | None:
    """
    Loads Outlook credentials from environment variables.
    Returns None if credentials are not configured.
    """
    from core.data_manager import DataManager
    DataManager.load_env()

    email_addr = os.getenv("OUTLOOK_EMAIL", "").strip()
    app_password = os.getenv("OUTLOOK_APP_PASSWORD", "").strip()

    if not email_addr or not app_password:
        return None

    return {
        "email": email_addr,
        "password": app_password,
        "imap_server": os.getenv("OUTLOOK_IMAP_SERVER", "outlook.office365.com"),
        "imap_port": int(os.getenv("OUTLOOK_IMAP_PORT", "993")),
        "smtp_server": os.getenv("OUTLOOK_SMTP_SERVER", "smtp.office365.com"),
        "smtp_port": int(os.getenv("OUTLOOK_SMTP_PORT", "587")),
    }


def _decode_header_value(value: str) -> str:
    """Decodes an email header value (handles UTF-8, base64, etc.)."""
    parts = decode_header(value)
    decoded_parts = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded_parts.append(part.decode(charset or 'utf-8', errors='replace'))
        else:
            decoded_parts.append(part)
    return " ".join(decoded_parts)


def _request_confirmation(preview_message: str) -> bool:
    """Sends a confirmation request to the Telegram operator and blocks until a response."""
    from core.human_in_the_loop import request_human_input
    from core.notification_manager import NotificationManager

    chat_id = os.environ.get("CURRENT_CHAT_ID")
    if not chat_id:
        logger.warning("No CURRENT_CHAT_ID set — auto-denying email send.")
        return False

    full_message = (
        "⚠️ <b>CONFIRMATION REQUIRED — SEND EMAIL</b>\n\n"
        f"{preview_message}\n\n"
        "Reply:\n"
        "✅ <code>CONFIRM</code> — to proceed\n"
        "❌ <code>CANCEL</code> — to abort"
    )

    notifier = NotificationManager()
    notifier.send_telegram_notification(full_message, chat_id=chat_id)

    answer = request_human_input(chat_id, full_message)
    answer_clean = answer.strip().upper() if answer else ""

    if answer_clean in ("CONFIRM", "CONFERMA"):
        logger.info("Operator confirmed email send.")
        return True
    else:
        logger.info(f"Operator denied email send (replied: '{answer}').")
        return False


# ===========================================================================
# READ TOOLS (no confirmation required)
# ===========================================================================

@tool
def read_emails(folder: str = "INBOX", max_emails: int = 10, unread_only: bool = False) -> str:
    """
    Reads emails from an Outlook mailbox using IMAP.
    Returns the most recent emails with their sender, subject, date, and body preview.

    Args:
        folder: The mailbox folder to read from (default: 'INBOX').
        max_emails: Maximum number of emails to return (default: 10, max: 50).
        unread_only: If True, returns only unread emails.

    Requires OUTLOOK_EMAIL and OUTLOOK_APP_PASSWORD in the .env file.
    """
    creds = _get_outlook_credentials()
    if not creds:
        return (
            "Email credentials not configured. "
            "Go to Task Builder -> Assign Tools -> manage_email to set them up."
        )

    max_emails = min(max_emails, 50)

    try:
        mail = imaplib.IMAP4_SSL(creds["imap_server"], creds["imap_port"])
        mail.login(creds["email"], creds["password"])
        mail.select(folder)

        search_criteria = "UNSEEN" if unread_only else "ALL"
        status, data = mail.search(None, search_criteria)
        if status != "OK":
            return f"Error: Could not search mailbox '{folder}'."

        mail_ids = data[0].split()
        if not mail_ids:
            return f"No emails found in '{folder}'{' (unread only)' if unread_only else ''}."

        # Get the most recent emails
        recent_ids = mail_ids[-max_emails:][::-1]
        results = []

        for mail_id in recent_ids:
            status, msg_data = mail.fetch(mail_id, "(RFC822)")
            if status != "OK":
                continue

            msg = email_lib.message_from_bytes(msg_data[0][1])

            subject = _decode_header_value(msg.get("Subject", "(No Subject)"))
            sender = _decode_header_value(msg.get("From", "Unknown"))
            date = msg.get("Date", "Unknown date")

            # Extract plain text body
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition")):
                        try:
                            body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                            break
                        except Exception:
                            pass
            else:
                try:
                    body = msg.get_payload(decode=True).decode("utf-8", errors="replace")
                except Exception:
                    body = "(Unable to decode body)"

            body_preview = body.strip()[:300] + ("..." if len(body.strip()) > 300 else "")

            results.append(
                f"--- Email ---\n"
                f"From: {sender}\n"
                f"Subject: {subject}\n"
                f"Date: {date}\n"
                f"Message:\n{body_preview}\n"
            )

        mail.logout()
        return f"📬 Recent {len(results)} email(s) from '{folder}':\n\n" + "\n".join(results)

    except imaplib.IMAP4.error as e:
        return f"IMAP Error: {e}. Check your credentials and verify that IMAP is enabled on the account."
    except Exception as e:
        logger.error(f"Error reading emails: {e}", exc_info=True)
        return f"Error reading emails: {e}"


@tool
def search_emails(query: str, folder: str = "INBOX", max_results: int = 10) -> str:
    """
    Searches emails in an Outlook mailbox by subject or sender.

    Args:
        query: Text to search for (will search in subject and sender fields).
        folder: The mailbox folder to search in (default: 'INBOX').
        max_results: Maximum number of results to return (default: 10).

    Requires OUTLOOK_EMAIL and OUTLOOK_APP_PASSWORD in the .env file.
    """
    creds = _get_outlook_credentials()
    if not creds:
        return (
            "Email credentials not configured. "
            "Go to Task Builder -> Assign Tools -> manage_email to set them up."
        )

    max_results = min(max_results, 30)

    try:
        mail = imaplib.IMAP4_SSL(creds["imap_server"], creds["imap_port"])
        mail.login(creds["email"], creds["password"])
        mail.select(folder)

        # Search by subject
        status, subject_data = mail.search(None, f'SUBJECT "{query}"')
        # Search by sender
        status2, from_data = mail.search(None, f'FROM "{query}"')

        all_ids = set()
        if status == "OK" and subject_data[0]:
            all_ids.update(subject_data[0].split())
        if status2 == "OK" and from_data[0]:
            all_ids.update(from_data[0].split())

        if not all_ids:
            return f"No emails found matching '{query}' in '{folder}'."

        recent_ids = sorted(all_ids)[-max_results:][::-1]
        results = []

        for mail_id in recent_ids:
            status, msg_data = mail.fetch(mail_id, "(RFC822.HEADER)")
            if status != "OK":
                continue
            msg = email_lib.message_from_bytes(msg_data[0][1])
            subject = _decode_header_value(msg.get("Subject", "(No Subject)"))
            sender = _decode_header_value(msg.get("From", "Unknown"))
            date = msg.get("Date", "Unknown date")
            results.append(f"• From: {sender} | Subject: {subject} | Date: {date}")

        mail.logout()
        return (
            f"🔍 Found {len(results)} email(s) for '{query}' in '{folder}':\n\n"
            + "\n".join(results)
        )

    except imaplib.IMAP4.error as e:
        return f"IMAP Error: {e}. Check your credentials and verify that IMAP is enabled on the account."
    except Exception as e:
        logger.error(f"Error searching emails: {e}", exc_info=True)
        return f"Error searching emails: {e}"


# ===========================================================================
# SEND TOOL (confirmation required)
# ===========================================================================

@tool
def send_email(to: str, subject: str, body: str, cc: str = "") -> str:
    """
    Sends an email via Outlook SMTP.

    IMPORTANT: Before sending, the operator will receive a Telegram preview
    of the email and must reply CONFERMA to approve the send.

    Args:
        to: Recipient email address (or comma-separated list for multiple recipients).
        subject: Email subject line.
        body: Email body text (plain text).
        cc: Optional CC recipients (comma-separated).

    Requires OUTLOOK_EMAIL and OUTLOOK_APP_PASSWORD in the .env file.
    """
    creds = _get_outlook_credentials()
    if not creds:
        return (
            "Email credentials not configured. "
            "Go to Task Builder -> Assign Tools -> manage_email to set them up."
        )

    body_preview = body.strip()[:400] + ("..." if len(body.strip()) > 400 else "")

    preview = (
        f"📧 <b>Send Email via Outlook</b>\n"
        f"From: <code>{creds['email']}</code>\n"
        f"To: <code>{to}</code>\n"
        + (f"CC: <code>{cc}</code>\n" if cc else "")
        + f"Subject: <b>{subject}</b>\n"
        f"Body:\n<pre>{body_preview}</pre>"
    )

    confirmed = _request_confirmation(preview)
    if not confirmed:
        return "Email send cancelled by the operator."

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = creds["email"]
        msg["To"] = to
        msg["Subject"] = subject
        if cc:
            msg["CC"] = cc

        msg.attach(MIMEText(body, "plain", "utf-8"))

        with smtplib.SMTP(creds["smtp_server"], creds["smtp_port"]) as server:
            server.ehlo()
            server.starttls()
            server.login(creds["email"], creds["password"])

            all_recipients = [r.strip() for r in to.split(",")]
            if cc:
                all_recipients += [r.strip() for r in cc.split(",")]

            server.sendmail(creds["email"], all_recipients, msg.as_string())

        return f"✅ Email sent successfully to '{to}' with subject '{subject}'."

    except smtplib.SMTPAuthenticationError:
        return (
            "❌ SMTP Authentication Error: Check your email and App Password. "
            "Make sure you have created an App Password on your Microsoft account and that SMTP is enabled."
        )
    except Exception as e:
        logger.error(f"Error sending email: {e}", exc_info=True)
        return f"Error sending email: {e}"
