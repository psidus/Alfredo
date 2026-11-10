"""Tests for persistent run interrupts (DB layer + pure helpers)."""

from __future__ import annotations

import os
import tempfile
import unittest

from core.interrupts import is_approval_answer, resolve_option
from core.sqlite_manager import SQLiteManager


class ApprovalHelpersTests(unittest.TestCase):
    def test_approval_words(self):
        for text in ("ok", "Approva", "va bene", "✅", "", "looks good to me"):
            self.assertTrue(is_approval_answer(text), text)

    def test_feedback_is_not_approval(self):
        for text in (
            "rifai la sezione 2",
            "cambia il tono",
            "no, sbagliato",
            "ok, ma cambia il titolo e aggiungi fonti",
            "non va bene",
        ):
            self.assertFalse(is_approval_answer(text), text)

    def test_short_approval_sentences(self):
        for text in ("ok grazie", "va bene procedi", "Ok, procedi."):
            self.assertTrue(is_approval_answer(text), text)

    def test_resolve_option(self):
        intr = {"options": ["Approva", "Rigenera", "Annulla"]}
        self.assertEqual(resolve_option(intr, "2"), "Rigenera")
        self.assertEqual(resolve_option(intr, "testo libero"), "testo libero")
        self.assertEqual(resolve_option(intr, "9"), "9")


class InterruptStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = SQLiteManager(db_path=os.path.join(self.tmp.name, "test.sqlite"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_lifecycle(self):
        iid = self.db.create_run_interrupt(
            run_id=42,
            node_id="node_1",
            task_id=7,
            chat_id="123",
            question="Ok?",
            options=["Sì", "No"],
            payload={"task_out": "bozza"},
        )
        intr = self.db.get_run_interrupt(iid)
        self.assertEqual(intr["run_id"], 42)
        self.assertEqual(intr["options"], ["Sì", "No"])
        self.assertEqual(intr["payload"]["task_out"], "bozza")
        self.assertEqual(intr["status"], "pending")

        pending = self.db.get_pending_interrupts(run_id=42)
        self.assertEqual([p["id"] for p in pending], [iid])
        self.assertEqual([p["id"] for p in self.db.get_pending_interrupts(chat_id="123")], [iid])

        self.assertTrue(self.db.answer_run_interrupt(iid, "Sì"))
        self.assertEqual(self.db.get_pending_interrupts(run_id=42), [])
        self.assertEqual(self.db.get_run_interrupt(iid)["status"], "answered")

        # Answering twice is a no-op.
        self.assertFalse(self.db.answer_run_interrupt(iid, "No"))

    def test_cancel_for_run(self):
        a = self.db.create_run_interrupt(run_id=1, question="a")
        b = self.db.create_run_interrupt(run_id=1, question="b")
        c = self.db.create_run_interrupt(run_id=2, question="c")
        self.assertEqual(self.db.cancel_run_interrupts(1), 2)
        self.assertEqual(self.db.get_pending_interrupts(run_id=1), [])
        self.assertEqual([p["id"] for p in self.db.get_pending_interrupts(run_id=2)], [c])
        self.assertEqual(self.db.get_run_interrupt(a)["status"], "cancelled")
        self.assertEqual(self.db.get_run_interrupt(b)["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
