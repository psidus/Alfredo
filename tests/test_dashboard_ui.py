"""Static and behavioural checks for the Streamlit dashboard UI.

The dashboard cannot be imported without the container dependencies
(streamlit, crewai, langchain), so the pure helpers are extracted from the
source with ``ast`` and executed in a minimal namespace. The remaining checks
are contracts on the source itself: they fail when an element is removed from
the page without removing its wiring (or vice versa).
"""

from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path

from core.workflow_graph import INPUT_REF, NODE_TYPE_INPUT, normalize_graph

REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = REPO_ROOT / "ui" / "dashboard.py"
ASSISTANT_VIEW = REPO_ROOT / "ui" / "views" / "assistant.py"

_HELPERS = {"BUILTIN_PLACEHOLDERS", "workflow_required_inputs", "run_status_badge", "_human_size"}


def load_helpers() -> dict:
    """Execute the dashboard helper functions/chore constants in isolation."""
    source = DASHBOARD.read_text(encoding="utf-8")
    tree = ast.parse(source)
    picked = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if names & _HELPERS:
                picked.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in _HELPERS:
            picked.append(node)
    namespace = {
        "json": json,
        "re": re,
        "normalize_graph": normalize_graph,
        "NODE_TYPE_INPUT": NODE_TYPE_INPUT,
        "INPUT_REF": INPUT_REF,
    }
    exec(compile(ast.Module(body=picked, type_ignores=[]), str(DASHBOARD), "exec"), namespace)
    return namespace


class WorkflowInputsTest(unittest.TestCase):
    """``workflow_required_inputs`` must ask only for real run inputs."""

    @classmethod
    def setUpClass(cls):
        cls.ns = load_helpers()
        cls.required = staticmethod(cls.ns["workflow_required_inputs"])

    def _workflow(self, steps) -> dict:
        return {"task_ids_json": json.dumps(steps)}

    def test_declared_inputs_keep_their_prompt(self):
        workflow = self._workflow([{"id": "t1", "type": "task", "task_id": 1}])
        tasks = [
            {
                "id": 1,
                "description": "Write about {topic}",
                "expected_output": "text",
                "required_inputs": [{"key": "topic", "prompt": "What topic?"}],
            }
        ]
        self.assertEqual(self.required(workflow, tasks), {"topic": "What topic?"})

    def test_input_block_declares_run_keys(self):
        workflow = self._workflow(
            [
                {"id": "in_1", "type": "input", "keys": ["topic", "language"]},
                {"id": "t1", "type": "task", "task_id": 1},
            ]
        )
        self.assertEqual(
            self.required(workflow, [{"id": 1, "description": "x", "expected_output": "y"}]),
            {"topic": "topic", "language": "language"},
        )

    def test_port_wired_from_run_input_is_asked(self):
        workflow = self._workflow(
            [
                {
                    "id": "t1",
                    "type": "task",
                    "task_id": 1,
                    "inputs_map": {"brief": {"from": INPUT_REF, "key": "angle"}},
                }
            ]
        )
        self.assertEqual(
            self.required(workflow, [{"id": 1, "description": "x", "expected_output": "y"}]),
            {"angle": "angle"},
        )

    def test_port_wired_from_parent_is_not_asked(self):
        workflow = self._workflow(
            [
                {"id": "t1", "type": "task", "task_id": 1},
                {
                    "id": "t2",
                    "type": "task",
                    "task_id": 2,
                    "inputs_map": {"data": {"from": "t1", "key": ""}},
                },
            ]
        )
        tasks = [
            {"id": 1, "description": "first", "expected_output": "y"},
            {"id": 2, "description": "summarise {data}", "expected_output": "y"},
        ]
        self.assertEqual(self.required(workflow, tasks), {})

    def test_builtin_placeholders_are_never_asked(self):
        workflow = self._workflow([3])
        tasks = [
            {
                "id": 3,
                "description": "Check {previous_result} with {user_input} and {context}",
                "expected_output": "{specialization}",
            }
        ]
        self.assertEqual(self.required(workflow, tasks), {})

    def test_legacy_task_text_still_surfaces_vars(self):
        workflow = self._workflow([9])
        tasks = [{"id": 9, "description": "Ask about {subject}", "expected_output": "{subject}"}]
        self.assertEqual(self.required(workflow, tasks), {"subject": "subject"})

    def test_batch_children_are_walked(self):
        workflow = self._workflow(
            [{"id": "b1", "type": "batch_loop", "task_ids": [4]}]
        )
        tasks = [
            {
                "id": 4,
                "description": "x",
                "expected_output": "y",
                "required_inputs": [{"key": "region", "prompt": "Which region?"}],
            }
        ]
        self.assertEqual(self.required(workflow, tasks), {"region": "Which region?"})

    def test_empty_and_broken_graphs_do_not_raise(self):
        self.assertEqual(self.required({}, []), {})
        self.assertEqual(self.required(None, None), {})
        self.assertEqual(self.required({"task_ids_json": "not json"}, []), {})

    def test_instances_field_is_accepted(self):
        # Workflows loaded from the DB expose `task_ids_json`; some callers use `task_ids`.
        workflow = {"task_ids": [5]}
        tasks = [{"id": 5, "description": "x", "expected_output": "y", "required_inputs": []}]
        self.assertEqual(self.required(workflow, tasks), {})


class StatusBadgeTest(unittest.TestCase):
    def test_badges_cover_every_run_status(self):
        badge = load_helpers()["run_status_badge"]
        for status in ("running", "completed", "failed", "paused", "waiting_human", "cancelled", "stopped"):
            self.assertTrue(badge(status).strip(), status)
        self.assertIn("Waiting", badge("waiting_human"))
        self.assertEqual(badge("weird"), "weird")
        self.assertEqual(badge(None), "unknown")

    def test_human_size(self):
        human = load_helpers()["_human_size"]
        self.assertEqual(human(0), "0 B")
        self.assertEqual(human(1536), "1.5 KB")
        self.assertEqual(human(None), "0 B")


class DashboardSourceContractTest(unittest.TestCase):
    """The toolbar/page contract: an element exists **and** something uses it."""

    @classmethod
    def setUpClass(cls):
        cls.source = DASHBOARD.read_text(encoding="utf-8")

    def test_assistant_is_a_toolbar_dialog_not_a_page(self):
        self.assertTrue('key="assistant_open_btn"' in self.source, "toolbar button")
        self.assertTrue('@st.dialog("🤖 Alfredo Assistant"' in self.source, "dialog")
        self.assertTrue("show_assistant()" in self.source, "dialog invocation")
        self.assertTrue("render_assistant(compact=True)" in self.source, "compact render")
        self.assertFalse("st.Page(page_assistant" in self.source, "old page removed")
        self.assertFalse("def page_assistant" in self.source, "old page renderer removed")

    def test_every_page_has_a_renderer(self):
        for page in re.findall(r"st\.Page\((page_\w+)", self.source):
            self.assertIn(f"def {page}():", self.source, page)

    def test_pages_do_not_reference_removed_renderers(self):
        defined = set(re.findall(r"^def (render_\w+)", self.source, flags=re.M))
        for name in re.findall(r"^\s+(render_\w+)\(\)", self.source, flags=re.M):
            self.assertIn(name, defined, name)

    def test_run_panel_uses_run_service(self):
        self.assertTrue("def _render_run_panel(" in self.source, "run panel")
        for call in ("start_run(", "cancel_run(", "start_execution(", "pending_interrupts("):
            self.assertTrue(call in self.source, call)
        # The old direct-execution path must be gone from the run panel.
        self.assertFalse("execute_run_with_resume(run['id'])" in self.source, "legacy resume removed")

    def test_history_exposes_timeline_and_approvals(self):
        self.assertTrue("def _render_event_timeline(" in self.source, "timeline")
        self.assertTrue('render_interrupt_queue(pending, prefix="hist")' in self.source, "approvals")
        self.assertTrue("ev_run_" in self.source, "timeline button")

    def test_run_artifacts_are_reachable_from_the_timeline(self):
        self.assertTrue("def _render_run_artifacts(" in self.source, "artifacts block")
        self.assertTrue("describe_run_exports(" in self.source, "backend call")
        self.assertIn("_render_run_artifacts(run_id)", self.source, "called by the timeline")
        # A link must be a signed token, never a raw server path.
        self.assertTrue("_share_link(" in self.source, "signed link")

    def test_new_services_have_a_control_surface(self):
        self.assertTrue("def render_workspace_system(" in self.source, "page")
        self.assertTrue("from core import files as ws_files" in self.source, "files")
        self.assertTrue("from core import uploads" in self.source, "uploads")
        self.assertTrue("from core.gpu_gate import GPU_GATE" in self.source, "gpu")
        self.assertTrue("from core import push" in self.source, "push")
        self.assertTrue("events.tail(" in self.source, "events")

    def test_guide_documents_the_new_surfaces(self):
        for anchor in (
            "Workspace & System",
            "GPU & routing",
            "Files, artifacts & share links",
            "Automations",
            "Event timeline",
            "Pending approvals",
            "Where you control it",
        ):
            self.assertTrue(anchor in self.source, anchor)

    def test_assistant_view_supports_dialog_mode(self):
        view = ASSISTANT_VIEW.read_text(encoding="utf-8")
        self.assertIn("def render_assistant(compact: bool = False)", view)
        self.assertIn("if compact:", view)


if __name__ == "__main__":
    unittest.main()
