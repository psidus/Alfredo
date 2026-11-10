"""Tests for typed blocks (llm/tool/router) inside the workflow graph."""

from __future__ import annotations

import unittest

from core.workflow_graph import (
    NODE_TYPE_TASK,
    collect_task_ids,
    desugar_graph_for_runtime,
    infer_node_type,
    normalize_graph,
    validate_graph,
)


def typed_graph():
    return [
        {"id": "in1", "type": "input", "keys": ["topic"]},
        {
            "id": "llm1",
            "type": "llm",
            "prompt": "Analizza {input.topic}",
            "depends_on": ["in1"],
            "execution_level": 1,
        },
        {
            "id": "r1",
            "type": "router",
            "routes": ["ok", "ko"],
            "rules": [{"route": "ok", "pattern": "approv"}],
            "default_route": "ko",
            "depends_on": ["llm1"],
            "execution_level": 2,
        },
        {
            "id": "tool1",
            "type": "tool",
            "tool_name": "search_web",
            "depends_on": ["r1"],
            "when_route": {"from": "r1", "equals": "ok"},
            "execution_level": 3,
        },
    ]


class NormalizeTests(unittest.TestCase):
    def test_typed_defaults(self):
        graph = normalize_graph(
            [
                {"id": "a", "type": "llm", "prompt": "x"},
                {"id": "b", "type": "tool", "tool_name": "t"},
                {"id": "c", "type": "router", "routes": ["one", "two"]},
            ],
            materialize_export=False,
        )
        llm, tool, router = graph
        self.assertEqual(llm["type"], "llm")
        self.assertEqual(llm["output_format"], "text")
        self.assertAlmostEqual(llm["temperature"], 0.2)
        self.assertEqual(tool["tool_args"], {})
        self.assertEqual(router["router_mode"], "auto")
        self.assertEqual(router["default_route"], "one")

    def test_agent_is_task_alias(self):
        self.assertEqual(infer_node_type({"type": "agent", "task_id": 3}), NODE_TYPE_TASK)

    def test_inference_from_shape(self):
        self.assertEqual(infer_node_type({"tool_name": "x"}), "tool")
        self.assertEqual(infer_node_type({"routes": ["a", "b"]}), "router")
        self.assertEqual(infer_node_type({"prompt": "hi"}), "llm")

    def test_snapshot_keys_survive_normalization(self):
        snapshot = {"name": "X", "description": "d"}
        graph = normalize_graph(
            [{"id": "n1", "type": "task", "task_id": 1, "task_snapshot": snapshot}],
            materialize_export=False,
        )
        self.assertEqual(graph[0]["task_snapshot"], snapshot)


class ValidationTests(unittest.TestCase):
    def test_valid_typed_graph(self):
        errors, _ = validate_graph(normalize_graph(typed_graph(), materialize_export=False))
        self.assertEqual(errors, [])

    def test_llm_requires_prompt(self):
        errors, _ = validate_graph(
            normalize_graph([{"id": "l", "type": "llm", "prompt": "  "}], materialize_export=False)
        )
        self.assertTrue(any("needs a prompt" in e for e in errors))

    def test_tool_requires_name(self):
        errors, _ = validate_graph(
            normalize_graph([{"id": "t", "type": "tool"}], materialize_export=False)
        )
        self.assertTrue(any("needs tool_name" in e for e in errors))

    def test_router_needs_two_routes(self):
        errors, _ = validate_graph(
            normalize_graph(
                [{"id": "r", "type": "router", "routes": ["only"]}], materialize_export=False
            )
        )
        self.assertTrue(any("two distinct routes" in e for e in errors))

    def test_router_rule_must_target_known_route(self):
        errors, _ = validate_graph(
            normalize_graph(
                [
                    {
                        "id": "r",
                        "type": "router",
                        "routes": ["a", "b"],
                        "rules": [{"route": "zzz", "pattern": "x"}],
                    }
                ],
                materialize_export=False,
            )
        )
        self.assertTrue(any("unknown route" in e for e in errors))

    def test_router_rule_needs_matcher(self):
        errors, _ = validate_graph(
            normalize_graph(
                [{"id": "r", "type": "router", "routes": ["a", "b"], "rules": [{"route": "a"}]}],
                materialize_export=False,
            )
        )
        self.assertTrue(any("pattern/equals/contains/exists" in e for e in errors))

    def test_gate_requires_router(self):
        errors, _ = validate_graph(
            normalize_graph(
                [
                    {"id": "l", "type": "llm", "prompt": "x"},
                    {"id": "t", "type": "tool", "tool_name": "s", "depends_on": ["l"], "when_route": {"from": "l", "equals": "ok"}},
                ],
                materialize_export=False,
            )
        )
        self.assertTrue(any("is not a router node" in e for e in errors))

    def test_gate_value_must_be_a_route(self):
        errors, _ = validate_graph(
            normalize_graph(
                [
                    {"id": "r", "type": "router", "routes": ["a", "b"]},
                    {
                        "id": "t",
                        "type": "tool",
                        "tool_name": "s",
                        "depends_on": ["r"],
                        "when_route": {"from": "r", "in": ["zzz"]},
                    },
                ],
                materialize_export=False,
            )
        )
        self.assertTrue(any("is not a route" in e for e in errors))

    def test_gate_requires_dependency(self):
        errors, _ = validate_graph(
            normalize_graph(
                [
                    {"id": "r", "type": "router", "routes": ["a", "b"]},
                    {"id": "t", "type": "tool", "tool_name": "s", "when_route": {"from": "r", "equals": "a"}},
                ],
                materialize_export=False,
            )
        )
        self.assertTrue(any("must also be in depends_on" in e for e in errors))

    def test_hitl_on_typed_node_is_rejected(self):
        errors, _ = validate_graph(
            normalize_graph(
                [
                    {"id": "l", "type": "llm", "prompt": "x"},
                    {"id": "h", "type": "hitl", "depends_on": ["l"]},
                ],
                materialize_export=False,
            )
        )
        self.assertTrue(any("not supported yet" in e for e in errors))


class DesugarTests(unittest.TestCase):
    def test_typed_nodes_are_executable(self):
        prepared = desugar_graph_for_runtime(normalize_graph(typed_graph(), materialize_export=False))
        kinds = [step["type"] for step in prepared["executable_steps"]]
        self.assertIn("llm", kinds)
        self.assertIn("router", kinds)
        self.assertIn("tool", kinds)
        self.assertNotIn("in1", [s["id"] for s in prepared["executable_steps"]])

    def test_collect_task_ids_ignores_typed_nodes(self):
        graph = normalize_graph(typed_graph(), materialize_export=False)
        graph.append({"id": "task1", "type": "task", "task_id": 5, "depends_on": ["tool1"]})
        self.assertEqual(collect_task_ids(graph), [5])

    def test_export_materializes_after_typed_sink(self):
        graph = normalize_graph(typed_graph(), expected_exports=["pdf"])
        export = [n for n in graph if n.get("type") == "export"]
        self.assertEqual(len(export), 1)
        self.assertIn("tool1", export[0]["depends_on"])


if __name__ == "__main__":
    unittest.main()
