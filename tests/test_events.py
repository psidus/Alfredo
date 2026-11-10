"""Tests for the append-only run event log."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from core import events
from core.sqlite_manager import SQLiteManager


class EventLogTests(unittest.TestCase):
    def setUp(self):
        self.db = SQLiteManager(":memory:")
        self.db.cursor.execute(
            "INSERT INTO workflow_runs (workflow_id, status, source) VALUES (NULL, 'running', 'test')"
        )
        self.run_id = int(self.db.cursor.lastrowid)
        self.db.cursor.execute(
            "INSERT INTO workflow_runs (workflow_id, status, source) VALUES (NULL, 'running', 'test')"
        )
        self.other_run = int(self.db.cursor.lastrowid)
        self.db.conn.commit()

    def tearDown(self):
        self.db.close()

    def emit(self, event_type, payload=None, run_id=None):
        return events.emit(run_id or self.run_id, event_type, payload or {}, db=self.db)

    def test_sequences_are_dense_and_per_run(self):
        self.assertEqual(self.emit(events.RUN_STARTED, {"source": "test"}), 1)
        self.assertEqual(self.emit(events.NODE_STARTED, {"node_id": "a"}), 2)
        self.assertEqual(self.emit(events.RUN_FINISHED, {"ok": True}), 3)
        self.assertEqual(self.emit(events.RUN_STARTED, {}, run_id=self.other_run), 1)
        self.assertEqual(self.db.latest_run_event_seq(self.run_id), 3)
        self.assertEqual(self.db.latest_run_event_seq(self.other_run), 1)

    def test_tail_returns_only_new_events(self):
        for i in range(5):
            self.emit(events.NODE_COMPLETED, {"node_id": f"n{i}"})
        all_events = events.tail(run_id=self.run_id, db=self.db)
        self.assertEqual([e["seq"] for e in all_events], [1, 2, 3, 4, 5])
        self.assertEqual(all_events[0]["payload"], {"node_id": "n0"})
        self.assertEqual(all_events[0]["type"], events.NODE_COMPLETED)
        self.assertTrue(all_events[0]["created_at"])

        missing = events.tail(after_seq=3, run_id=self.run_id, db=self.db)
        self.assertEqual([e["seq"] for e in missing], [4, 5])

        # No duplicates when nothing new happened.
        self.assertEqual(events.tail(after_seq=5, run_id=self.run_id, db=self.db), [])

    def test_tail_is_ordered_across_runs(self):
        self.emit("a", run_id=self.run_id)
        self.emit("b", run_id=self.other_run)
        self.emit("c", run_id=self.run_id)
        rows = events.tail(after_seq=0, db=self.db)
        self.assertEqual(
            [(r["run_id"], r["seq"]) for r in rows],
            [(self.run_id, 1), (self.run_id, 2), (self.other_run, 1)],
        )

    def test_event_content_is_isolated_per_run(self):
        self.emit("mine")
        self.emit("theirs", run_id=self.other_run)
        mine = events.tail(run_id=self.run_id, db=self.db)
        theirs = events.tail(run_id=self.other_run, db=self.db)
        self.assertEqual([e["type"] for e in mine], ["mine"])
        self.assertEqual([e["type"] for e in theirs], ["theirs"])

    def test_replay_folds_state(self):
        self.emit(events.RUN_STARTED, {"source": "automation"})
        self.emit(events.NODE_STARTED, {"node_id": "n1"})
        self.emit(events.NODE_COMPLETED, {"node_id": "n1"})
        self.emit(events.INTERRUPT_OPENED, {"interrupt_id": 4, "node_id": "n2"})
        state = events.replay(events.tail(run_id=self.run_id, db=self.db))
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["nodes"], {"n1": "completed"})
        self.assertEqual(state["pending_interrupts"], [4])
        self.assertEqual(state["last_seq"], 4)

        self.emit(events.INTERRUPT_ANSWERED, {"interrupt_id": 4})
        self.emit(events.RUN_FINISHED, {})
        state = events.replay(events.tail(run_id=self.run_id, db=self.db))
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["pending_interrupts"], [])

    def test_prune_keeps_the_newest_events(self):
        for _ in range(130):
            self.emit(events.NODE_COMPLETED, {"node_id": "x"})
        self.db.prune_run_events(self.run_id, keep=100)
        rows = events.tail(run_id=self.run_id, db=self.db)
        self.assertEqual(len(rows), 100)
        self.assertEqual(rows[0]["seq"], 31)
        self.assertEqual(rows[-1]["seq"], 130)

    def test_long_payload_is_truncated(self):
        self.emit("big", {"text": "x" * (events.MAX_PAYLOAD_CHARS * 2)})
        row = events.tail(run_id=self.run_id, db=self.db)[0]
        self.assertTrue(row["payload"].get("_truncated"))
        self.assertLessEqual(len(row["payload"].get("text", "")), events.MAX_PAYLOAD_CHARS)

    def test_unserializable_payload_does_not_raise(self):
        self.emit("weird", {"obj": object()})
        row = events.tail(run_id=self.run_id, db=self.db)[0]
        self.assertIn("obj", row["payload"])

    def test_emit_never_raises_on_storage_failure(self):
        class BrokenDB:
            def append_run_event(self, *args, **kwargs):
                raise RuntimeError("disk full")

        self.assertIsNone(events.emit(1, "x", {}, db=BrokenDB()))

    def test_emit_without_run_id_is_ignored(self):
        self.assertIsNone(events.emit(None, "x", {}, db=self.db))


class RunServiceEventTests(unittest.TestCase):
    def test_cancel_emits_cancelled_event(self):
        from core import run_service

        class FakeDB:
            def read_run(self, run_id):
                return {"id": run_id, "status": "running", "result": ""}

            def cancel_run_interrupts(self, run_id):
                return 0

            def update_run(self, *args, **kwargs):
                return None

            def close(self):
                return None

        emitted = []
        with patch("core.events.emit", side_effect=lambda *a, **k: emitted.append(a)):
            with patch("core.db_manager.DBManager", return_value=FakeDB()):
                with patch("core.interrupts.cancel_for_run", return_value=0):
                    self.assertTrue(run_service.cancel_run(12))
        event_types = [call[1] for call in emitted]
        self.assertIn(events.RUN_CANCELLED, event_types)


if __name__ == "__main__":
    unittest.main()
