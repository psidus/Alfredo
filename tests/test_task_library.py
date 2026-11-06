"""Tests for task templates vs. workflow instances (snapshot-on-node model)."""

from __future__ import annotations

import unittest

from core.task_library import (
    attach_snapshot,
    detach_snapshot,
    is_instance,
    isolate_workflow_nodes,
    resolve_task_record,
    shared_task_usage,
    snapshot_diff,
    snapshot_for,
    snapshot_from_record,
    sync_from_template,
)


class FakeDB:
    def __init__(self, tasks=None, workflows=None):
        self.tasks = {int(k): dict(v) for k, v in (tasks or {}).items()}
        self.workflows = list(workflows or [])

    def read_task(self, task_id):
        return self.tasks.get(int(task_id))

    def read_all_workflows(self):
        return list(self.workflows)


TEMPLATE = {
    "id": 7,
    "name": "Report termico",
    "description": "Analizza i documenti termici",
    "expected_output": "Report",
    "agent_id": 2,
    "model_id": None,
    "agent_specialization": None,
    "tools": ["search"],
    "required_inputs": [{"key": "month", "prompt": "Mese"}],
    "vector_dbs": [],
    "human_validation": 1,
    "max_input_context": 0,
    "max_output_tokens": 0,
    "output_pydantic": None,
    "tool_profile": "",
}


class SnapshotTests(unittest.TestCase):
    def test_snapshot_coerces_json_strings(self):
        record = {
            "description": "d",
            "expected_output": "e",
            "tools": '["search", "calc"]',
            "required_inputs": '[{"key": "x", "prompt": "X"}]',
            "human_validation": True,
            "agent_id": "3",
        }
        snap = snapshot_from_record(record, task_id=7, captured_at="2026-01-01T00:00:00+00:00")
        self.assertEqual(snap["tools"], ["search", "calc"])
        self.assertEqual(snap["required_inputs"][0]["key"], "x")
        self.assertEqual(snap["human_validation"], 1)
        self.assertEqual(snap["agent_id"], 3)
        self.assertEqual(snap["source_task_id"], 7)
        self.assertEqual(snap["captured_at"], "2026-01-01T00:00:00+00:00")

    def test_attach_and_detach_on_task_node(self):
        node = {"id": "n1", "type": "task", "task_id": 7}
        inst = attach_snapshot(node, TEMPLATE, captured_at="2026-01-01T00:00:00+00:00")
        self.assertTrue(is_instance(inst))
        self.assertEqual(snapshot_for(inst)["description"], TEMPLATE["description"])
        self.assertNotIn("task_snapshot", node)  # original untouched

        linked = detach_snapshot(inst)
        self.assertFalse(is_instance(linked))

    def test_batch_snapshots_are_per_task(self):
        node = {"id": "b1", "type": "batch_loop", "task_ids": [7, 8]}
        inst = attach_snapshot(node, TEMPLATE, task_id=7)
        self.assertTrue(is_instance(inst, task_id=7))
        self.assertFalse(is_instance(inst, task_id=8))
        self.assertIsNone(inst.get("task_snapshot"))


class ResolutionTests(unittest.TestCase):
    def test_snapshot_wins_over_edited_template(self):
        inst = attach_snapshot({"type": "task", "task_id": 7}, TEMPLATE)
        edited = dict(TEMPLATE, description="CAMBIATA NELLA LIBRERIA", tools=["other"])
        record = resolve_task_record(7, inst, FakeDB({7: edited}))
        self.assertEqual(record["description"], TEMPLATE["description"])
        self.assertEqual(record["tools"], ["search"])

    def test_link_follows_template(self):
        node = {"type": "task", "task_id": 7}
        edited = dict(TEMPLATE, description="NUOVA")
        record = resolve_task_record(7, node, FakeDB({7: edited}))
        self.assertEqual(record["description"], "NUOVA")

    def test_snapshot_survives_deleted_template(self):
        inst = attach_snapshot({"type": "task", "task_id": 7}, TEMPLATE)
        record = resolve_task_record(7, inst, FakeDB({}))
        self.assertEqual(record["description"], TEMPLATE["description"])
        self.assertEqual(record["id"], 7)
        self.assertEqual(record["required_inputs"][0]["key"], "month")

    def test_missing_everything_returns_empty(self):
        self.assertEqual(resolve_task_record(None, {}, FakeDB({})), {})
        self.assertEqual(resolve_task_record(99, {}, FakeDB({})), {})


class DiffSyncTests(unittest.TestCase):
    def test_diff_only_changed_fields(self):
        inst = attach_snapshot({"type": "task", "task_id": 7}, TEMPLATE)
        edited = dict(TEMPLATE, description="NUOVA", expected_output="Altro")
        diff = snapshot_diff(inst, edited)
        self.assertEqual(set(diff.keys()), {"description", "expected_output"})
        self.assertEqual(diff["description"]["template"], "NUOVA")
        self.assertEqual(diff["description"]["instance"], TEMPLATE["description"])

    def test_sync_all_fields(self):
        inst = attach_snapshot({"type": "task", "task_id": 7}, TEMPLATE)
        edited = dict(TEMPLATE, description="NUOVA")
        synced = sync_from_template(inst, edited, captured_at="2026-02-02T00:00:00+00:00")
        self.assertEqual(snapshot_for(synced)["description"], "NUOVA")
        self.assertEqual(snapshot_for(synced)["synced_at"], "2026-02-02T00:00:00+00:00")
        self.assertEqual(snapshot_diff(synced, edited), {})

    def test_sync_selected_fields_keeps_the_rest(self):
        inst = attach_snapshot({"type": "task", "task_id": 7}, TEMPLATE)
        edited = dict(TEMPLATE, description="NUOVA", expected_output="Altro")
        synced = sync_from_template(inst, edited, fields=["description"])
        snap = snapshot_for(synced)
        self.assertEqual(snap["description"], "NUOVA")
        self.assertEqual(snap["expected_output"], TEMPLATE["expected_output"])


class UsageTests(unittest.TestCase):
    def test_shared_usage_counts_batches_and_plain_ids(self):
        workflows = [
            {"id": 1, "task_ids": [7, 8, {"type": "batch_loop", "task_ids": [9, 7]}]},
            {"id": 2, "task_ids": [{"type": "task", "task_id": 9}]},
            {"id": 3, "task_ids": []},
        ]
        usage = shared_task_usage(FakeDB({}, workflows))
        self.assertEqual(usage[7], [1])
        self.assertEqual(usage[8], [1])
        self.assertEqual(usage[9], [1, 2])

    def test_isolate_only_shared_tasks(self):
        tasks = {
            7: TEMPLATE,
            8: dict(TEMPLATE, id=8, name="Solo qui"),
            9: dict(TEMPLATE, id=9, name="Condiviso"),
        }
        db = FakeDB(
            tasks,
            workflows=[
                {"id": 1, "task_ids": [7, 9]},
                {"id": 2, "task_ids": [9]},
            ],
        )
        steps = [7, {"type": "batch_loop", "task_ids": [8, 9]}]
        # usage: 7 -> [1], 8 -> [], 9 -> [1, 2]
        result = isolate_workflow_nodes(steps, db, only_shared=True)
        self.assertFalse(is_instance(result[0]))  # 7 used once -> stays linked
        self.assertFalse(is_instance(result[1], task_id=8))
        self.assertTrue(is_instance(result[1], task_id=9))

    def test_usage_accepts_json_string(self):
        workflows = [
            {"id": 1, "task_ids_json": '[7, {"type": "batch_loop", "task_ids": [8]}]'},
        ]
        usage = shared_task_usage(FakeDB({}, workflows))
        self.assertEqual(usage[7], [1])
        self.assertEqual(usage[8], [1])

    def test_isolate_all(self):
        db = FakeDB({7: TEMPLATE}, workflows=[{"id": 1, "task_ids": [7]}])
        result = isolate_workflow_nodes([7], db, only_shared=False)
        self.assertTrue(is_instance(result[0]))


if __name__ == "__main__":
    unittest.main()
