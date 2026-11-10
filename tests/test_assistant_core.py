"""Unit tests for the provider-free parts of the assistant core.

Run with:  py -3.13 -m unittest discover -s tests -v
They only need the standard library (no Streamlit, no LiteLLM, no DB).
"""

from __future__ import annotations

import json
import unittest

from core.assistant_engine import (
    AssistantTurn,
    WorkflowCard,
    WorkflowInput,
    build_workflow_catalog,
    catalog_to_prompt,
    normalize_history,
    normalize_required_inputs,
    parse_decision,
    should_route,
)
from core.run_service import parse_run_record


class FakeDB:
    def __init__(self, workflows, tasks):
        self._workflows = workflows
        self._tasks = tasks
        self.closed = False

    def read_all_workflows(self):
        return list(self._workflows)

    def read_all_tasks(self):
        return list(self._tasks)

    def close(self):
        self.closed = True


def _db():
    workflows = [
        {
            "id": 7,
            "name": "Report termico",
            "task_ids": [1, {"id": "node_b", "type": "task", "task_id": 2, "depends_on": ["node_a"]}],
            "expected_exports": '["xlsx", "docx"]',
        },
        {
            "id": 8,
            "name": "Workflow vuoto",
            "task_ids": [],
            "expected_exports": [],
        },
    ]
    tasks = [
        {
            "id": 1,
            "name": "Estrai",
            "description": "Estrae i parametri termici dal documento.",
            "required_inputs": [{"key": "documento", "prompt": "Documento da analizzare"}],
        },
        {
            "id": 2,
            "name": "Report",
            "description": "Scrive il report finale.",
            "required_inputs": [
                {"key": "documento", "prompt": "Documento da analizzare"},
                {"key": "lingua", "prompt": ""},
            ],
        },
    ]
    return FakeDB(workflows, tasks)


class CatalogueTests(unittest.TestCase):
    def test_normalize_required_inputs_accepts_shapes(self):
        self.assertEqual(
            [i.key for i in normalize_required_inputs([{"key": "a"}, "b", {"key": "a"}])],
            ["a", "b"],
        )
        self.assertEqual([i.key for i in normalize_required_inputs('["x", "y"]')], ["x", "y"])
        self.assertEqual(normalize_required_inputs(None), [])

    def test_catalog_dedups_inputs_and_fills_description(self):
        cards = {c.id: c for c in build_workflow_catalog(_db())}
        card = cards[7]
        self.assertEqual(card.name, "Report termico")
        self.assertEqual([i.key for i in card.inputs], ["documento", "lingua"])
        self.assertEqual(card.expected_exports, ["xlsx", "docx"])
        # No workflow description -> first task description is used.
        self.assertIn("parametri termici", card.description)
        self.assertEqual(cards[8].inputs, [])

    def test_catalog_prompt_is_stable(self):
        prompt = catalog_to_prompt(build_workflow_catalog(_db()))
        self.assertIn('[7] "Report termico"', prompt)
        self.assertIn("documento (Documento da analizzare)", prompt)
        self.assertIn("lingua", prompt)
        self.assertIn("(none)", prompt)


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.cards = build_workflow_catalog(_db())

    def test_valid_run_workflow_computes_missing(self):
        raw = json.dumps(
            {
                "action": "run_workflow",
                "reply": "Lancio il report.",
                "workflow_id": 7,
                "inputs": {"documento": "dati.pdf", "junk": "x"},
                "missing_inputs": [],
            }
        )
        turn = parse_decision(raw, self.cards)
        self.assertEqual(turn.action, "run_workflow")
        self.assertEqual(turn.workflow_id, 7)
        self.assertEqual(turn.inputs, {"documento": "dati.pdf"})
        self.assertEqual(turn.missing_inputs, ["lingua"])

    def test_unknown_workflow_degrades_to_clarify(self):
        raw = json.dumps({"action": "run_workflow", "reply": "", "workflow_id": 999, "inputs": {}})
        turn = parse_decision(raw, self.cards)
        self.assertEqual(turn.action, "clarify")
        self.assertIsNone(turn.workflow_id)

    def test_fenced_json_is_accepted(self):
        raw = '```json\n{"action": "answer", "reply": "Ciao!"}\n```'
        turn = parse_decision(raw, self.cards)
        self.assertEqual(turn.action, "answer")
        self.assertEqual(turn.text, "Ciao!")

    def test_invalid_json_becomes_answer(self):
        turn = parse_decision("non json", self.cards)
        self.assertEqual(turn.action, "answer")
        self.assertEqual(turn.text, "non json")

    def test_should_route(self):
        self.assertTrue(should_route("Esegui il Report termico", self.cards))
        self.assertTrue(should_route("run something", self.cards))
        self.assertTrue(should_route("che workflow hai?", self.cards))
        self.assertFalse(should_route("ciao, come stai?", self.cards))


class HistoryTests(unittest.TestCase):
    def test_history_filters_and_bounds(self):
        history = [{"role": "system", "content": "x"}] + [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
            for i in range(40)
        ]
        out = normalize_history(history)
        self.assertLessEqual(len(out), 24)
        self.assertTrue(all(m["role"] in ("user", "assistant") for m in out))


class RunRecordTests(unittest.TestCase):
    def test_parse_run_record_extracts_payload(self):
        payload = json.dumps({"final_result": "ok", "procedure_summary": "s", "exports": []})
        record = {
            "id": 3,
            "status": "completed",
            "result": payload,
            "inputs": '{"topic": "x"}',
            "task_outputs": "{}",
        }
        parsed = parse_run_record(record)
        self.assertEqual(parsed["inputs"], {"topic": "x"})
        self.assertEqual(parsed["display_text"], "ok")
        self.assertEqual(parsed["payload"]["procedure_summary"], "s")

    def test_parse_run_record_handles_empty(self):
        self.assertEqual(parse_run_record(None), {})
        parsed = parse_run_record({"id": 1, "status": "running", "result": None})
        self.assertEqual(parsed["display_text"], "")
        self.assertIsNone(parsed["payload"])


if __name__ == "__main__":
    unittest.main()
