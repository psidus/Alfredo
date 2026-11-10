"""Tests for Web Push helpers (VAPID keys, broadcast, subscription storage)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import push
from core.push import PushUnavailable


class FakeDB:
    def __init__(self, rows):
        self.rows = list(rows)
        self.deleted = []

    def list_push_subscriptions(self):
        return list(self.rows)

    def delete_push_subscription(self, endpoint=None, subscription_id=None):
        self.deleted.append(endpoint)
        self.rows = [r for r in self.rows if r.get("endpoint") != endpoint]
        return True


class GoneError(Exception):
    def __init__(self):
        self.response = type("R", (), {"status_code": 410})()


def subscription(endpoint="https://push.example/1"):
    return {"id": 1, "endpoint": endpoint, "p256dh": "pk", "auth": "auth"}


class KeyTests(unittest.TestCase):
    def setUp(self):
        push.reset_cache()
        self._tmp = tempfile.TemporaryDirectory()
        self.keys_file = Path(self._tmp.name) / "push_vapid.json"
        self._patches = [
            patch.object(push, "KEYS_FILE", self.keys_file),
            patch.object(push, "_keys_from_environment", return_value=None),
            patch.dict(os.environ, {push.PUBLIC_KEY_ENV: "", push.PRIVATE_KEY_ENV: ""}),
        ]
        for patcher in self._patches:
            patcher.start()

    def tearDown(self):
        for patcher in self._patches:
            patcher.stop()
        push.reset_cache()
        self._tmp.cleanup()

    def test_generates_persists_and_reloads_keys(self):
        keys = push.load_keys()
        raw_public = push.b64url_decode(keys.public_key)
        self.assertEqual(len(raw_public), 65)
        self.assertEqual(raw_public[0], 0x04)
        self.assertIn("BEGIN PRIVATE KEY", keys.private_key)
        self.assertTrue(self.keys_file.exists())
        self.assertEqual(push.public_key_from_private(keys.private_key), keys.public_key)

        self.assertIs(push.load_keys(), keys)  # cached

        push.reset_cache()
        reloaded = push.load_keys()
        self.assertEqual(reloaded.public_key, keys.public_key)
        self.assertEqual(reloaded.private_key, keys.private_key)

    def test_env_private_key_without_public_is_derived(self):
        keys = push.load_keys()
        env = {
            push.PRIVATE_KEY_ENV: keys.private_key.replace("\n", "\\n"),
            push.PUBLIC_KEY_ENV: "",
        }
        with patch.dict(os.environ, env):
            with patch.object(push, "_keys_from_environment", wraps=push._keys_from_environment):
                push.reset_cache()
                loaded = push.load_keys()
        self.assertEqual(loaded.public_key, keys.public_key)

    def test_subject_defaults(self):
        with patch.dict(os.environ, {push.SUBJECT_ENV: ""}):
            self.assertEqual(push.subject(), push.DEFAULT_SUBJECT)
        with patch.dict(os.environ, {push.SUBJECT_ENV: "mailto:x@y.z"}):
            self.assertEqual(push.subject(), "mailto:x@y.z")


class BroadcastTests(unittest.TestCase):
    def test_sends_to_every_subscription(self):
        sent = []
        db = FakeDB([subscription("https://a"), subscription("https://b")])
        delivered = push.broadcast({"type": "test"}, db=db, send=lambda sub, payload: sent.append(sub["endpoint"]))
        self.assertEqual(delivered, 2)
        self.assertEqual(sent, ["https://a", "https://b"])

    def test_dead_endpoint_is_removed(self):
        db = FakeDB([subscription("https://dead"), subscription("https://alive")])
        delivered_out = []

        def sender(sub, payload):
            if sub["endpoint"] == "https://dead":
                raise GoneError()
            delivered_out.append(sub["endpoint"])

        delivered = push.broadcast({"type": "test"}, db=db, send=sender)
        self.assertEqual(delivered, 1)
        self.assertEqual(delivered_out, ["https://alive"])
        self.assertEqual(db.deleted, ["https://dead"])

    def test_other_errors_do_not_raise(self):
        db = FakeDB([subscription()])

        def sender(sub, payload):
            raise RuntimeError("network down")

        self.assertEqual(push.broadcast({"type": "test"}, db=db, send=sender), 0)
        self.assertEqual(db.deleted, [])

    def test_missing_sender_stops_quietly(self):
        db = FakeDB([subscription()])

        def sender(sub, payload):
            raise PushUnavailable("pywebpush missing")

        push._warned_unavailable = False
        self.assertEqual(push.broadcast({"type": "test"}, db=db, send=sender), 0)
        self.assertEqual(db.deleted, [])

    def test_approval_payload_includes_context(self):
        payload = push.approval_payload(
            run_id=7,
            interrupt_id=12,
            question="Procedo?",
            options=["Sì", "No"],
            workflow_name="Report",
        )
        self.assertEqual(payload["type"], "approval")
        self.assertEqual(payload["run_id"], 7)
        self.assertEqual(payload["interrupt_id"], 12)
        self.assertIn("Report", payload["body"])
        self.assertIn("Procedo?", payload["body"])
        self.assertIn("Sì", payload["body"])
        self.assertLessEqual(len(payload["body"]), 400)

    def test_approval_without_question_has_fallback(self):
        payload = push.approval_payload(run_id=3, interrupt_id=4)
        self.assertIn("#3", payload["body"])

    def test_test_notification_payload(self):
        sent = []
        push.send_test_notification(db=FakeDB([subscription()]), send=lambda s, p: sent.append(p))
        self.assertEqual(sent[0]["type"], "test")
        self.assertIn("Alfredo", sent[0]["title"])


class SubscriptionStorageTests(unittest.TestCase):
    def setUp(self):
        from core.sqlite_manager import SQLiteManager

        self.db = SQLiteManager(":memory:")

    def tearDown(self):
        self.db.close()

    def test_upsert_and_delete(self):
        sub_id = self.db.save_push_subscription("https://push/1", "pk1", "auth1", "ua")
        self.assertIsInstance(sub_id, int)
        rows = self.db.list_push_subscriptions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["endpoint"], "https://push/1")

        self.db.save_push_subscription("https://push/1", "pk2", "auth2", "ua2")
        rows = self.db.list_push_subscriptions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["p256dh"], "pk2")
        self.assertEqual(rows[0]["user_agent"], "ua2")

        self.assertTrue(self.db.delete_push_subscription(endpoint="https://push/1"))
        self.assertEqual(self.db.list_push_subscriptions(), [])
        self.assertFalse(self.db.delete_push_subscription())


if __name__ == "__main__":
    unittest.main()
