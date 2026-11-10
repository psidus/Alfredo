"""Tests for the automation scheduler: persistence and run_service routing."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from core.automation_scheduler import AutomationEngine


class SchedulerTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.config_path = os.path.join(self.root, "automations.json")
        self.state_path = os.path.join(self.root, "automations_state.json")
        self.engine = self.make_engine()

    def tearDown(self):
        self._tmp.cleanup()

    def make_engine(self) -> AutomationEngine:
        return AutomationEngine(config_path=self.config_path, state_path=self.state_path)

    @staticmethod
    def ts(hour, minute=0, day=2):
        """Epoch seconds for a fixed local date (2026-03-<day>)."""
        return datetime(2026, 3, day, hour, minute).timestamp()

    def rule(self, **overrides):
        base = {
            "id": "r1",
            "name": "Report orario",
            "workflow_id": 5,
            "trigger_type": "interval",
            "interval_minutes": 60,
            "default_inputs": {"topic": "x"},
            "enabled": True,
            "last_triggered": None,
        }
        base.update(overrides)
        return base


class IntervalPersistenceTests(SchedulerTestBase):
    def test_first_tick_starts_the_clock_without_firing(self):
        self.engine.save_rules([self.rule()])
        with patch("core.run_service.start_run") as start:
            self.engine._tick(now_ts=1000.0)
        start.assert_not_called()
        self.assertEqual(self.engine._state["r1"]["last_run_ts"], 1000.0)
        self.assertTrue(os.path.exists(self.state_path))

    def test_fires_after_the_interval_and_survives_restart(self):
        self.engine.save_rules([self.rule()])
        with patch("core.run_service.start_run", return_value=77) as start:
            with patch("core.events.emit") as emit:
                self.engine._tick(now_ts=1000.0)
                self.engine._tick(now_ts=1000.0 + 3599)
                start.assert_not_called()
                self.engine._tick(now_ts=1000.0 + 3600)
        start.assert_called_once()
        self.assertEqual(start.call_args.args[0], 5)
        self.assertEqual(start.call_args.kwargs["source"], "automation")
        self.assertEqual(start.call_args.kwargs["inputs"]["topic"], "x")
        emit.assert_called_once()
        self.assertEqual(emit.call_args.args[0], 77)
        self.assertEqual(emit.call_args.args[1], "automation.fired")

        restarted = self.make_engine()
        with patch("core.run_service.start_run") as start_again:
            restarted._tick(now_ts=4600.0)
        start_again.assert_not_called()

        with patch("core.run_service.start_run", return_value=78) as start_again:
            restarted._tick(now_ts=4600.0 + 3600)
        start_again.assert_called_once()

    def test_disabled_rule_never_fires(self):
        self.engine.save_rules([self.rule(enabled=False)])
        with patch("core.run_service.start_run") as start:
            self.engine._tick(now_ts=1000.0)
            self.engine._tick(now_ts=999999.0)
        start.assert_not_called()

    def test_failing_start_does_not_break_the_loop(self):
        self.engine.save_rules([self.rule()])
        with patch("core.run_service.start_run", side_effect=RuntimeError("no workflow")):
            self.engine._tick(now_ts=0.0)
            self.engine._tick(now_ts=3600.0)  # must not raise

    def test_delete_rule_drops_its_state(self):
        self.engine.save_rules([self.rule()])
        self.engine._tick(now_ts=1000.0)
        self.assertIn("r1", self.engine._state)
        self.assertTrue(self.engine.delete_rule("r1"))
        self.assertNotIn("r1", self.engine._state)


class FolderWatchPersistenceTests(SchedulerTestBase):
    def setUp(self):
        super().setUp()
        self.watch = os.path.join(self.root, "inbox")
        os.makedirs(self.watch, exist_ok=True)

    def add_watch_rule(self):
        self.engine.save_rules(
            [
                self.rule(
                    trigger_type="folder_watch",
                    watch_folder=self.watch,
                    file_extensions=".txt",
                )
            ]
        )

    def touch(self, name):
        path = os.path.join(self.watch, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("x")
        return path

    def test_snapshot_then_only_new_files_fire(self):
        existing = self.touch("old.txt")
        self.add_watch_rule()

        with patch("core.run_service.start_run") as start:
            self.engine._tick(now_ts=1000.0)
            start.assert_not_called()
            self.assertEqual(self.engine._state["r1"]["seen_files"], [existing])

            new_file = self.touch("new.txt")
            self.engine._tick(now_ts=1010.0)
            start.assert_called_once()
            inputs = start.call_args.kwargs["inputs"]
            self.assertEqual(inputs["file_path"], new_file)
            self.assertEqual(inputs["file_name"], "new.txt")

            # Same file is not re-processed.
            self.engine._tick(now_ts=1020.0)
            self.assertEqual(start.call_count, 1)

    def test_files_arriving_while_stopped_fire_after_restart(self):
        self.touch("old.txt")
        self.add_watch_rule()
        with patch("core.run_service.start_run"):
            self.engine._tick(now_ts=1000.0)

        offline_file = self.touch("landed_offline.txt")

        restarted = self.make_engine()
        with patch("core.run_service.start_run") as start:
            restarted._tick(now_ts=2000.0)
        start.assert_called_once()
        self.assertEqual(start.call_args.kwargs["inputs"]["file_name"], "landed_offline.txt")
        self.assertEqual(start.call_args.kwargs["inputs"]["file_path"], offline_file)

    def test_state_stays_bounded_to_existing_files(self):
        self.touch("a.txt")
        self.add_watch_rule()
        with patch("core.run_service.start_run"):
            self.engine._tick(now_ts=1000.0)
            os.remove(os.path.join(self.watch, "a.txt"))
            self.engine._tick(now_ts=1010.0)
        self.assertEqual(self.engine._state["r1"]["seen_files"], [])

    def test_corrupt_state_file_is_ignored(self):
        with open(self.state_path, "w", encoding="utf-8") as f:
            f.write("{not json")
        engine = self.make_engine()
        self.add_watch_rule()
        with patch("core.run_service.start_run") as start:
            engine.save_rules(self.engine.load_rules())
            engine._tick(now_ts=1000.0)
        start.assert_not_called()


class CatchUpTests(SchedulerTestBase):
    def test_interval_within_window_fires_once(self):
        self.engine.save_rules([self.rule(catchup_minutes=60)])
        with patch("core.run_service.start_run", return_value=1) as start:
            self.engine._tick(now_ts=0.0)
            # due at 3600; 30 minutes late is fine
            self.engine._tick(now_ts=3600.0 + 30 * 60)
        start.assert_called_once()

    def test_interval_beyond_window_is_skipped_and_clock_reset(self):
        self.engine.save_rules([self.rule(catchup_minutes=60)])
        with patch("core.run_service.start_run") as start:
            self.engine._tick(now_ts=0.0)
            late = 3600.0 + 61 * 60
            self.engine._tick(now_ts=late)
            start.assert_not_called()
            self.assertEqual(self.engine._state["r1"]["last_run_ts"], late)
            self.assertIn("missed_since", self.engine._state["r1"]["last_skip"])
            # The clock now starts from "late": no immediate catch-up burst.
            self.engine._tick(now_ts=late + 60)
            start.assert_not_called()

    def _daily_rule(self, **overrides):
        return self.rule(trigger_type="daily", daily_time="08:00", catchup_minutes=120, **overrides)

    def test_daily_catches_up_late_within_window(self):
        self.engine.save_rules([self._daily_rule()])
        with patch("core.run_service.start_run", return_value=2) as start:
            self.engine._tick(now_ts=self.ts(9, 30))
        start.assert_called_once()
        self.assertEqual(self.engine._state["r1"]["daily_handled_date"], "2026-03-02")

        with patch("core.run_service.start_run") as start:
            self.engine._tick(now_ts=self.ts(9, 45))
        start.assert_not_called()

    def test_daily_before_target_does_not_fire(self):
        self.engine.save_rules([self._daily_rule()])
        with patch("core.run_service.start_run") as start:
            self.engine._tick(now_ts=self.ts(7, 59))
        start.assert_not_called()

    def test_daily_beyond_window_is_skipped_for_the_day(self):
        self.engine.save_rules([self._daily_rule()])
        with patch("core.run_service.start_run") as start:
            self.engine._tick(now_ts=self.ts(10, 1))
            start.assert_not_called()
            # Late by 121 minutes: skipped, marked handled, no retry later today.
            self.engine._tick(now_ts=self.ts(23, 0))
            start.assert_not_called()
            self.assertEqual(self.engine._state["r1"]["daily_handled_date"], "2026-03-02")
            self.assertEqual(
                self.engine._state["r1"]["last_skip"]["missed_since"], "2026-03-02 08:00"
            )

        with patch("core.run_service.start_run", return_value=3) as start:
            self.engine._tick(now_ts=self.ts(8, 0, day=3))
        start.assert_called_once()

    def test_daily_with_legacy_last_triggered_is_not_duplicated(self):
        rule = self._daily_rule(last_triggered="2026-03-02 08:00:00")
        self.engine.save_rules([rule])
        with patch("core.run_service.start_run") as start:
            self.engine._tick(now_ts=self.ts(9, 0))
        start.assert_not_called()


class UpdateRuleTests(SchedulerTestBase):
    def test_update_fields_and_reset_clock(self):
        self.engine.save_rules([self.rule()])
        with patch("core.run_service.start_run"):
            self.engine._tick(now_ts=1000.0)
        self.assertEqual(self.engine._state["r1"]["last_run_ts"], 1000.0)

        self.assertTrue(
            self.engine.update_rule(
                "r1", name="Nuovo nome", interval_minutes=15, catchup_minutes=30, enabled=False
            )
        )
        rule = self.engine.load_rules()[0]
        self.assertEqual(rule["name"], "Nuovo nome")
        self.assertEqual(rule["interval_minutes"], 15)
        self.assertEqual(rule["catchup_minutes"], 30)
        self.assertFalse(rule["enabled"])
        self.assertGreater(self.engine._state["r1"]["last_run_ts"], 1000.0)

    def test_name_only_update_keeps_clock(self):
        self.engine.save_rules([self.rule()])
        with patch("core.run_service.start_run"):
            self.engine._tick(now_ts=1000.0)
        self.assertTrue(self.engine.update_rule("r1", name="Solo rinominata"))
        self.assertEqual(self.engine._state["r1"]["last_run_ts"], 1000.0)

    def test_folder_change_resnapshots_existing_files(self):
        watch = os.path.join(self.root, "watched")
        os.makedirs(watch)
        with open(os.path.join(watch, "existing.txt"), "w", encoding="utf-8") as f:
            f.write("x")
        self.engine.save_rules([self.rule(trigger_type="folder_watch", watch_folder=self.root)])
        with patch("core.run_service.start_run"):
            self.engine._tick(now_ts=1000.0)

        self.assertTrue(self.engine.update_rule("r1", watch_folder=watch))
        seen = self.engine._state["r1"]["seen_files"]
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].endswith("existing.txt"))

    def test_unknown_rule_returns_false_and_ignores_junk(self):
        self.engine.save_rules([self.rule()])
        self.assertFalse(self.engine.update_rule("nope", name="x"))
        self.assertTrue(self.engine.update_rule("r1", not_a_field="x"))
        self.assertNotIn("not_a_field", self.engine.load_rules()[0])


class NextRunsTests(SchedulerTestBase):
    def test_interval_next_run(self):
        self.engine.save_rules([self.rule(interval_minutes=60)])
        with patch("core.run_service.start_run"):
            self.engine._tick(now_ts=1000.0)
        entries = self.engine.next_runs(window_hours=3, now_ts=1000.0)
        self.assertEqual(entries[0]["when"], datetime.fromtimestamp(4600.0).strftime("%Y-%m-%d %H:%M"))
        self.assertEqual(len(entries), 3)  # 4600, 8200, 11800 (<= 11800 horizon)
        self.assertEqual(entries[0]["rule_id"], "r1")

    def test_daily_next_runs_sorted_and_limited(self):
        later = self.rule(id="r2", interval_minutes=30)
        daily = self.rule(trigger_type="daily", daily_time="08:00")
        self.engine.save_rules([later, daily])
        with patch("core.run_service.start_run"):
            self.engine._tick(now_ts=self.ts(6))
        entries = self.engine.next_runs(window_hours=48, now_ts=self.ts(6))
        self.assertEqual(entries[0]["rule_id"], "r2")  # 06:30 first
        daily_entry = next(e for e in entries if e["rule_id"] == "r1")
        self.assertEqual(daily_entry["when"], datetime(2026, 3, 2, 8, 0).strftime("%Y-%m-%d %H:%M"))
        self.assertTrue(all(entries[i]["ts"] <= entries[i + 1]["ts"] for i in range(len(entries) - 1)))
        self.assertLessEqual(len(entries), 6)

    def test_event_rules_are_not_in_calendar(self):
        watch = os.path.join(self.root, "inbox2")
        os.makedirs(watch)
        self.engine.save_rules(
            [self.rule(trigger_type="folder_watch", watch_folder=watch)]
        )
        self.assertEqual(self.engine.next_runs(now_ts=self.ts(6)), [])

    def test_disabled_rules_are_hidden(self):
        self.engine.save_rules([self.rule(enabled=False)])
        self.assertEqual(self.engine.next_runs(now_ts=self.ts(6)), [])


if __name__ == "__main__":
    unittest.main()