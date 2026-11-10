"""Web Push (VAPID) notifications for pending human approvals.

The PWA subscribes through ``POST /api/push/subscribe``; every time an interrupt
is opened (``core.interrupts.open_interrupt``) a push is broadcast in a daemon
thread so the engine never waits for the network.

Design notes
------------
* Sending is best effort: a dead endpoint (404/410) is removed, any other error
  is logged and the run keeps waiting for its approval.
* ``pywebpush`` is imported lazily, so the app (and the test suite) works even
  when the optional dependency is missing. In that case pushes are skipped and
  the PWA still shows approvals in its badge.
* VAPID keys come from ``ALFREDO_VAPID_PUBLIC_KEY`` / ``ALFREDO_VAPID_PRIVATE_KEY``
  when set; otherwise they are generated once and stored in
  ``config/push_vapid.json`` (git-ignored config folder, never in the repo).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

PUBLIC_KEY_ENV = "ALFREDO_VAPID_PUBLIC_KEY"
PRIVATE_KEY_ENV = "ALFREDO_VAPID_PRIVATE_KEY"
SUBJECT_ENV = "ALFREDO_VAPID_SUBJECT"
DEFAULT_SUBJECT = "mailto:alfredo@localhost"

KEYS_FILE = Path(__file__).resolve().parents[1] / "config" / "push_vapid.json"

GONE_STATUSES = {404, 410}

_lock = threading.Lock()
_cached_keys: Optional["VapidKeys"] = None
_send_lock = threading.Lock()
_warned_unavailable = False


class PushUnavailable(RuntimeError):
    """``pywebpush`` (or ``cryptography``) is not installed."""


@dataclass(frozen=True)
class VapidKeys:
    public_key: str  # base64url, uncompressed point (65 bytes)
    private_key: str  # PKCS8 PEM

    def as_dict(self) -> Dict[str, str]:
        return {"public_key": self.public_key, "private_key": self.private_key}


# ---------------------------------------------------------------------------
# base64url helpers
# ---------------------------------------------------------------------------

def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    text = (text or "").strip()
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


# ---------------------------------------------------------------------------
# VAPID keys
# ---------------------------------------------------------------------------

def _generate_keys() -> VapidKeys:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise PushUnavailable("cryptography is required for Web Push") from exc

    private = ec.generate_private_key(ec.SECP256R1())
    numbers = private.public_key().public_numbers()
    raw_public = (
        b"\x04"
        + numbers.x.to_bytes(32, "big")
        + numbers.y.to_bytes(32, "big")
    )
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii").strip()
    return VapidKeys(public_key=b64url_encode(raw_public), private_key=pem)


def public_key_from_private(private_key_pem: str) -> str:
    """Derive the base64url public key from a PKCS8/EC PEM private key."""
    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError as exc:  # pragma: no cover
        raise PushUnavailable("cryptography is required for Web Push") from exc
    private = serialization.load_pem_private_key(
        (private_key_pem or "").replace("\\n", "\n").encode("utf-8"), password=None
    )
    numbers = private.public_key().public_numbers()
    raw_public = b"\x04" + numbers.x.to_bytes(32, "big") + numbers.y.to_bytes(32, "big")
    return b64url_encode(raw_public)


def _keys_from_environment() -> Optional[VapidKeys]:
    public = (os.getenv(PUBLIC_KEY_ENV) or "").strip()
    private = (os.getenv(PRIVATE_KEY_ENV) or "").strip()
    if not private:
        return None
    private = private.replace("\\n", "\n")
    if not public:
        public = public_key_from_private(private)
    return VapidKeys(public_key=public, private_key=private)


def _keys_from_file() -> Optional[VapidKeys]:
    try:
        data = json.loads(KEYS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    public = str(data.get("public_key") or "").strip()
    private = str(data.get("private_key") or "").strip()
    if not private:
        return None
    if not public:
        public = public_key_from_private(private)
    return VapidKeys(public_key=public, private_key=private)


def _persist_keys(keys: VapidKeys) -> None:
    try:
        KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
        KEYS_FILE.write_text(json.dumps(keys.as_dict(), indent=2), encoding="utf-8")
        try:
            os.chmod(KEYS_FILE, 0o600)
        except OSError:  # pragma: no cover - Windows / read-only volumes
            pass
    except OSError as exc:  # pragma: no cover - read-only container
        logger.warning("Could not persist VAPID keys to %s: %s", KEYS_FILE, exc)


def load_keys() -> VapidKeys:
    """Return the VAPID key pair, generating and persisting one when needed."""
    global _cached_keys
    with _lock:
        if _cached_keys is not None:
            return _cached_keys
        keys = _keys_from_environment() or _keys_from_file()
        if keys is None:
            keys = _generate_keys()
            _persist_keys(keys)
            logger.info("Generated new VAPID keys (stored in %s).", KEYS_FILE)
        _cached_keys = keys
        return keys


def reset_cache() -> None:
    """Test helper: forget cached keys so env/file changes are re-read."""
    global _cached_keys, _warned_unavailable
    with _lock:
        _cached_keys = None
        _warned_unavailable = False


def public_key() -> str:
    return load_keys().public_key


def subject() -> str:
    return (os.getenv(SUBJECT_ENV) or DEFAULT_SUBJECT).strip() or DEFAULT_SUBJECT


def is_available() -> bool:
    """True when the optional sender dependency is installed."""
    try:
        import pywebpush  # noqa: F401
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------------------
# Subscription storage
# ---------------------------------------------------------------------------

def _db(db: Any = None) -> Any:
    if db is not None:
        return db
    from core.db_manager import DBManager

    return DBManager()


def save_subscription(
    endpoint: str,
    p256dh: str,
    auth: str,
    *,
    user_agent: str = "",
    db: Any = None,
) -> int:
    """Store (or refresh) a browser push subscription."""
    owns_db = db is None
    handle = _db(db)
    try:
        return int(handle.save_push_subscription(endpoint, p256dh, auth, user_agent) or 0)
    finally:
        if owns_db:
            handle.close()


def remove_subscription(endpoint: Optional[str] = None, *, subscription_id: Optional[int] = None, db: Any = None) -> bool:
    owns_db = db is None
    handle = _db(db)
    try:
        return bool(handle.delete_push_subscription(endpoint=endpoint, subscription_id=subscription_id))
    finally:
        if owns_db:
            handle.close()


def list_subscriptions(db: Any = None) -> List[Dict[str, Any]]:
    owns_db = db is None
    handle = _db(db)
    try:
        return list(handle.list_push_subscriptions() or [])
    finally:
        if owns_db:
            handle.close()


def subscription_count(db: Any = None) -> int:
    return len(list_subscriptions(db))


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def _default_send(subscription: Dict[str, Any], payload: Dict[str, Any]) -> None:
    """Deliver one push through ``pywebpush`` (raises on failure)."""
    try:
        from pywebpush import webpush
    except ImportError as exc:
        raise PushUnavailable("pywebpush is not installed") from exc

    keys = load_keys()
    subscription_info = {
        "endpoint": subscription.get("endpoint"),
        "keys": {
            "p256dh": subscription.get("p256dh"),
            "auth": subscription.get("auth"),
        },
    }
    if not subscription_info["endpoint"] or not subscription_info["keys"]["p256dh"]:
        raise ValueError("incomplete push subscription")
    try:
        webpush(
            subscription_info=subscription_info,
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=keys.private_key,
            vapid_claims={"sub": subject()},
            timeout=10,
        )
    except TypeError:
        # Older pywebpush without the timeout kwarg.
        webpush(
            subscription_info=subscription_info,
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=keys.private_key,
            vapid_claims={"sub": subject()},
        )


def _is_gone(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status in GONE_STATUSES


def broadcast(
    payload: Dict[str, Any],
    *,
    db: Any = None,
    send: Optional[Callable[[Dict[str, Any], Dict[str, Any]], None]] = None,
) -> int:
    """Send ``payload`` to every subscription; returns the delivered count."""
    global _warned_unavailable
    sender = send or _default_send
    owns_db = db is None
    handle = _db(db)
    delivered = 0
    try:
        subscriptions = list(handle.list_push_subscriptions() or [])
    except Exception as exc:  # noqa: BLE001 - push must never break the run
        logger.warning("Could not read push subscriptions: %s", exc)
        subscriptions = []
    finally:
        if owns_db:
            handle.close()

    for subscription in subscriptions:
        try:
            sender(subscription, payload)
            delivered += 1
        except PushUnavailable as exc:
            if not _warned_unavailable:
                _warned_unavailable = True
                logger.warning(
                    "Web Push disabled: %s (pip install pywebpush)", exc
                )
            return delivered
        except Exception as exc:  # noqa: BLE001 - one bad endpoint must not stop the rest
            endpoint = str(subscription.get("endpoint") or "")
            if _is_gone(exc):
                try:
                    with _send_lock:
                        handle = _db(db)
                        try:
                            handle.delete_push_subscription(endpoint=endpoint)
                        finally:
                            if owns_db:
                                handle.close()
                except Exception as cleanup_exc:  # noqa: BLE001
                    logger.debug("Could not delete dead subscription: %s", cleanup_exc)
                continue
            logger.warning("Push failed for %s…: %s", endpoint[:60], exc)
    return delivered


def approval_payload(
    *,
    run_id: int,
    interrupt_id: int,
    question: str = "",
    options: Optional[List[str]] = None,
    workflow_name: str = "",
    url: str = "./",
) -> Dict[str, Any]:
    title = "Alfredo · approvazione richiesta"
    body = (question or "").strip() or f"Il run #{run_id} attende una conferma."
    if workflow_name:
        body = f"{workflow_name}: {body}"
    if options:
        body = f"{body}\nOpzioni: {', '.join(str(o) for o in options)}"
    return {
        "type": "approval",
        "title": title,
        "body": body[:400],
        "run_id": int(run_id),
        "interrupt_id": int(interrupt_id),
        "options": [str(o) for o in (options or [])],
        "url": url,
    }


def send_approval_notification(
    *,
    run_id: int,
    interrupt_id: int,
    question: str = "",
    options: Optional[List[str]] = None,
    workflow_name: str = "",
    db: Any = None,
    send: Optional[Callable[[Dict[str, Any], Dict[str, Any]], None]] = None,
) -> int:
    payload = approval_payload(
        run_id=run_id,
        interrupt_id=interrupt_id,
        question=question,
        options=options,
        workflow_name=workflow_name,
    )
    return broadcast(payload, db=db, send=send)


def send_approval_notification_async(**kwargs: Any) -> threading.Thread:
    """Fire and forget: used by the engine when an interrupt is opened."""
    thread = threading.Thread(
        target=_notify_safely,
        kwargs=kwargs,
        name="alfredo-push-approval",
        daemon=True,
    )
    thread.start()
    return thread


def _notify_safely(**kwargs: Any) -> None:
    try:
        send_approval_notification(**kwargs)
    except Exception as exc:  # noqa: BLE001 - notifications never break runs
        logger.warning("Approval push notification failed: %s", exc)


def send_test_notification(
    *,
    db: Any = None,
    send: Optional[Callable[[Dict[str, Any], Dict[str, Any]], None]] = None,
) -> int:
    payload = {
        "type": "test",
        "title": "Alfredo · notifiche attive",
        "body": "Riceverai qui le richieste di approvazione dei workflow.",
        "url": "./",
    }
    return broadcast(payload, db=db, send=send)


__all__ = [
    "DEFAULT_SUBJECT",
    "PRIVATE_KEY_ENV",
    "PUBLIC_KEY_ENV",
    "PushUnavailable",
    "SUBJECT_ENV",
    "VapidKeys",
    "approval_payload",
    "b64url_decode",
    "b64url_encode",
    "broadcast",
    "is_available",
    "list_subscriptions",
    "load_keys",
    "public_key",
    "public_key_from_private",
    "remove_subscription",
    "reset_cache",
    "save_subscription",
    "send_approval_notification",
    "send_approval_notification_async",
    "send_test_notification",
    "subject",
    "subscription_count",
]
