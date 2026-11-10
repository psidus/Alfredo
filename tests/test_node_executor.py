"""Tests for typed node execution (llm / tool / router)."""

from __future__ import annotations

import json
import unittest

from core.node_executor import (
    NodeExecutionError,
    RunState,
    build_prompt,
    execute_llm_node,
    execute_router_node,
    execute_tool_node,
    execute_typed_node,
    match_rule,
    node_is_enabled,
    previous_output,
)


class RecordingComplete:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, messages, temperature=0.2, force_json=False):
        self.calls.append(
            {"messages": messages, "temperature": temperature, "force_json": force_json}
        )
        return self.replies.pop(0)


def state(**kwargs):
    base = dict(run_id=1, inputs={}, node_outputs={}, routes={})
    base.update(kwargs)
    return RunState(**base)


class RunStateTests(unittest.TestCase):
    def test_record_and_skip(self):
        st = state()
        self.assertEqual(st.record("n1", "hello"), "hello")
        self.assertEqual(st.output_of("n1"), "hello")
        self.assertEqual(st.output_of("missing"), "")
        st.skip("n2")
        self.assertTrue(st.is_skipped("n2"))
        self.assertEqual(st.output_of("n2"), "")

    def test_previous_output_uses_last_dependency(self):
        st = state(node_outputs={"a": "A", "b": "B"})
        node = {"depends_on": ["a", "b"]}
        self.assertEqual(previous_output(node, st), "B")
        self.assertEqual(previous_output({"depends_on": []}, st), "")


class PromptTests(unittest.TestCase):
    def test_interpolates_inputs_ports_and_previous(self):
        st = state(
            inputs={"topic": "termico"},
            node_outputs={"n1": "OUT1"},
        )
        node = {
            "id": "n2",
            "type": "llm",
            "prompt": "Analizza {input.topic} usando {{dati}}. Contesto: {input.missing}",
            "inputs_map": {"dati": {"from": "n1", "key": ""}},
            "depends_on": ["n1"],
        }
        prompt = build_prompt(node, st)
        self.assertIn("termico", prompt)
        self.assertIn("OUT1", prompt)
        self.assertIn("{input.missing}", prompt)  # unknown placeholder is left intact

    def test_appends_previous_when_not_referenced(self):
        st = state(node_outputs={"n1": "PREV"})
        node = {"id": "n2", "type": "llm", "prompt": "Riassumi.", "depends_on": ["n1"]}
        prompt = build_prompt(node, st)
        self.assertIn("PREV", prompt)
        self.assertTrue(prompt.startswith("Riassumi."))

    def test_can_disable_previous_append(self):
        st = state(node_outputs={"n1": "PREV"})
        node = {
            "id": "n2",
            "type": "llm",
            "prompt": "Solo questo.",
            "append_previous": False,
            "depends_on": ["n1"],
        }
        self.assertEqual(build_prompt(node, st), "Solo questo.")


class LlmTests(unittest.TestCase):
    def test_text_completion(self):
        complete = RecordingComplete("risposta")
        st = state()
        out = execute_llm_node(
            {"id": "n1", "type": "llm", "prompt": "Ciao", "temperature": 0.5},
            st,
            complete,
        )
        self.assertEqual(out, "risposta")
        self.assertEqual(complete.calls[0]["temperature"], 0.5)
        self.assertFalse(complete.calls[0]["force_json"])
        self.assertEqual(complete.calls[0]["messages"][0]["role"], "system")

    def test_json_output_is_normalized(self):
        complete = RecordingComplete('```json\n{"answer": 1}\n```')
        out = execute_llm_node(
            {"id": "n1", "type": "llm", "prompt": "dammi json", "output_format": "json"},
            state(),
            complete,
        )
        self.assertEqual(json.loads(out), {"answer": 1})
        self.assertTrue(complete.calls[0]["force_json"])

    def test_invalid_json_raises(self):
        complete = RecordingComplete("non è json")
        with self.assertRaises(NodeExecutionError):
            execute_llm_node(
                {"id": "n1", "type": "llm", "prompt": "x", "output_format": "json"},
                state(),
                complete,
            )

    def test_empty_prompt_raises(self):
        with self.assertRaises(NodeExecutionError):
            execute_llm_node({"id": "n1", "type": "llm", "prompt": ""}, state(), RecordingComplete("x"))

    def test_empty_response_raises(self):
        with self.assertRaises(NodeExecutionError):
            execute_llm_node({"id": "n1", "type": "llm", "prompt": "x"}, state(), RecordingComplete("   "))

    def test_legacy_client_signature(self):
        calls = []

        def complete(messages):
            calls.append(messages)
            return "ok"

        out = execute_llm_node({"id": "n1", "type": "llm", "prompt": "x"}, state(), complete)
        self.assertEqual(out, "ok")
        self.assertEqual(len(calls), 1)


class ToolTests(unittest.TestCase):
    def test_plain_function_with_static_args(self):
        received = {}

        def my_tool(query, limit=3):
            received.update({"query": query, "limit": limit})
            return "done"

        st = state()
        out = execute_tool_node(
            {"id": "t1", "type": "tool", "tool_name": "search", "tool_args": {"query": "x", "limit": 5}},
            st,
            lambda name: my_tool if name == "search" else None,
        )
        self.assertEqual(out, "done")
        self.assertEqual(received, {"query": "x", "limit": 5})

    def test_wired_port_overrides_static_arg(self):
        def my_tool(query):
            return f"q={query}"

        st = state(node_outputs={"n1": "VAL"})
        out = execute_tool_node(
            {
                "id": "t1",
                "type": "tool",
                "tool_name": "search",
                "tool_args": {"query": "static"},
                "inputs_map": {"query": {"from": "n1", "key": ""}},
                "depends_on": ["n1"],
            },
            st,
            lambda name: my_tool,
        )
        self.assertEqual(out, "q=VAL")

    def test_object_with_run_method(self):
        class Tool:
            def run(self, **kwargs):
                return f"run:{kwargs.get('input')}"

        st = state(node_outputs={"n1": "PREV"})
        out = execute_tool_node(
            {"id": "t1", "type": "tool", "tool_name": "x", "depends_on": ["n1"]},
            st,
            lambda name: Tool(),
        )
        self.assertEqual(out, "run:PREV")

    def test_unknown_tool_raises(self):
        with self.assertRaises(NodeExecutionError):
            execute_tool_node({"id": "t1", "type": "tool", "tool_name": "nope"}, state(), lambda n: None)

    def test_tool_exception_is_wrapped(self):
        def boom(**kwargs):
            raise ValueError("kaput")

        with self.assertRaises(NodeExecutionError) as ctx:
            execute_tool_node({"id": "t1", "type": "tool", "tool_name": "x"}, state(), lambda n: boom)
        self.assertIn("kaput", str(ctx.exception))


class RuleMatchTests(unittest.TestCase):
    def test_matchers(self):
        self.assertTrue(match_rule({"pattern": "approv"}, "APPROVATO"))
        self.assertTrue(match_rule({"equals": "ok"}, " ok "))
        self.assertTrue(match_rule({"contains": "urgente"}, "richiesta Urgente"))
        self.assertTrue(match_rule({"exists": True}, "testo"))
        self.assertFalse(match_rule({"exists": True}, "   "))
        self.assertFalse(match_rule({"pattern": "zzz"}, "abc"))
        self.assertTrue(match_rule({"pattern": "([bad"}, "([bad"))  # invalid regex → substring

    def test_route_source(self):
        st = state(node_outputs={"n1": "APPROVATO"})
        node = {"id": "r1", "type": "router", "routes": ["yes", "no"], "rules": [{"route": "yes", "pattern": "approv", "source": "n1"}]}
        self.assertEqual(execute_router_node(node, st), "yes")


class RouterTests(unittest.TestCase):
    def test_first_matching_rule_wins(self):
        node = {
            "id": "r1",
            "type": "router",
            "routes": ["yes", "no"],
            "rules": [
                {"route": "no", "pattern": "rifai"},
                {"route": "yes", "pattern": "ok|approv"},
            ],
            "default_route": "no",
        }
        st = state(node_outputs={"p": "ok, approvato"})
        node["depends_on"] = ["p"]
        self.assertEqual(execute_router_node(node, st), "yes")

    def test_rules_mode_uses_default(self):
        node = {
            "id": "r1",
            "type": "router",
            "router_mode": "rules",
            "routes": ["yes", "no"],
            "rules": [{"route": "yes", "pattern": "ok"}],
            "default_route": "no",
        }
        self.assertEqual(execute_router_node(node, state(node_outputs={"p": "boh"})), "no")

    def test_llm_mode_parses_route(self):
        complete = RecordingComplete('{"route": "no"}')
        node = {
            "id": "r1",
            "type": "router",
            "router_mode": "llm",
            "routes": ["yes", "no"],
            "default_route": "yes",
        }
        self.assertEqual(execute_router_node(node, state(), complete), "no")

    def test_llm_mode_unknown_route_falls_back(self):
        complete = RecordingComplete('{"route": "boh"}')
        node = {
            "id": "r1",
            "type": "router",
            "router_mode": "llm",
            "routes": ["yes", "no"],
            "default_route": "yes",
        }
        self.assertEqual(execute_router_node(node, state(), complete), "yes")

    def test_auto_without_rules_calls_llm(self):
        complete = RecordingComplete('{"route": "no"}')
        node = {"id": "r1", "type": "router", "routes": ["yes", "no"], "default_route": "yes"}
        st = state(node_outputs={"p": "qualcosa"})
        node["depends_on"] = ["p"]
        self.assertEqual(execute_router_node(node, st, complete), "no")

    def test_no_routes_raises(self):
        with self.assertRaises(NodeExecutionError):
            execute_router_node({"id": "r1", "type": "router", "routes": []}, state())


class GateTests(unittest.TestCase):
    def test_gate_allows_matching_route(self):
        st = state(routes={"r1": "yes"})
        node = {"id": "n1", "when_route": {"from": "r1", "equals": "yes"}}
        self.assertTrue(node_is_enabled(node, st))

    def test_gate_blocks_other_route(self):
        st = state(routes={"r1": "no"})
        node = {"id": "n1", "when_route": {"from": "r1", "in": ["yes"]}}
        self.assertFalse(node_is_enabled(node, st))

    def test_unknown_router_does_not_skip(self):
        self.assertTrue(node_is_enabled({"id": "n1", "when_route": {"from": "r9", "equals": "x"}}, state()))

    def test_no_gate_is_enabled(self):
        self.assertTrue(node_is_enabled({"id": "n1"}, state()))


class DispatchTests(unittest.TestCase):
    def test_llm_dispatch_records_output(self):
        st = state()
        out = execute_typed_node(
            "llm",
            {"id": "n1", "type": "llm", "prompt": "x"},
            st,
            complete=RecordingComplete("out"),
        )
        self.assertEqual(out, "out")
        self.assertEqual(st.output_of("n1"), "out")

    def test_router_dispatch_records_route(self):
        st = state()
        node = {"id": "r1", "type": "router", "rules": [], "routes": ["a", "b"], "default_route": "b"}
        out = execute_typed_node("router", node, st)
        self.assertEqual(out, "b")
        self.assertEqual(st.route_of("r1"), "b")

    def test_tool_without_resolver_raises(self):
        with self.assertRaises(NodeExecutionError):
            execute_typed_node("tool", {"id": "t1", "tool_name": "x"}, state())

    def test_unknown_kind_raises(self):
        with self.assertRaises(NodeExecutionError):
            execute_typed_node("weird", {"id": "x"}, state())


if __name__ == "__main__":
    unittest.main()
