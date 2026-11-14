"""Tests for binary Yes/No decisions, structured handoffs, and AI Team & Workflow Architect."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from core.node_executor import (
    RunState,
    build_jev_request_payload,
    execute_router_node,
    execute_typed_node,
    is_binary_decision_node,
    is_jev_decision_model,
    node_is_enabled,
    normalize_yes_no,
    parse_jev_decision_response,
)
from core.sqlite_manager import SQLiteManager
from core.team_architect import (
    generate_team_and_workflow_blueprint,
    materialize_blueprint_to_db,
    recommend_models_for_tiers,
)
from core.workflow_contracts import (
    build_structured_handoff_envelope,
    enforce_node_handoff,
    format_upstream_handoff_block,
)
from core.workflow_graph import (
    NODE_TYPE_ROUTER,
    extract_output_key,
    normalize_node,
    validate_graph,
)


class DummyMemoryManager:
    def __init__(self):
        self.records = {}

    def read_record(self, key):
        return self.records.get(key)

    def write_record(self, key, content_summary, structured_data, agent_role=""):
        self.records[key] = {
            "key": key,
            "summary": content_summary,
            "data": structured_data,
            "agent_role": agent_role,
        }


class BinaryDecisionTests(unittest.TestCase):
    def test_normalize_yes_no_multilingual(self):
        for yes_val in ("yes", "YES", "si", "sì", "Sì", "true", True, 1, "approvato", "OK"):
            self.assertEqual(normalize_yes_no(yes_val), "yes", f"Failed on {yes_val!r}")
        for no_val in ("no", "NO", "false", False, 0, "rifiutato", "ko"):
            self.assertEqual(normalize_yes_no(no_val), "no", f"Failed on {no_val!r}")
        self.assertIsNone(normalize_yes_no("forse"))
        self.assertIsNone(normalize_yes_no(None))

    def test_decision_node_normalizes_italian_and_stores_structured_json(self):
        calls = []

        def fake_complete(messages, temperature=0.0, force_json=True):
            calls.append({"messages": messages, "temperature": temperature, "force_json": force_json})
            return json.dumps({
                "decision": "sì",
                "reason": "Tutte le fonti richieste sono presenti.",
                "confidence": 0.94,
            })

        node = normalize_node(
            {
                "id": "gate_1",
                "type": "decision",
                "question": "Le fonti sono complete?",
                "model_id": 7,
                "depends_on": ["step_1"],
            }
        )
        self.assertEqual(node["type"], NODE_TYPE_ROUTER)
        self.assertTrue(node["decision_mode"])
        self.assertEqual(node["routes"], ["yes", "no"])
        self.assertEqual(node["model_id"], 7)
        self.assertTrue(is_binary_decision_node(node))

        st = RunState(run_id=1, node_outputs={"step_1": "Analisi completa con 5 fonti."})
        out_str = execute_typed_node(NODE_TYPE_ROUTER, node, st, complete=fake_complete)

        self.assertEqual(st.route_of("gate_1"), "yes")
        parsed = json.loads(out_str)
        self.assertEqual(parsed["decision"], "yes")
        self.assertEqual(parsed["route"], "yes")
        self.assertEqual(parsed["reason"], "Tutte le fonti richieste sono presenti.")
        self.assertAlmostEqual(parsed["confidence"], 0.94, places=2)
        self.assertEqual(extract_output_key(st.output_of("gate_1"), "reason"), "Tutte le fonti richieste sono presenti.")
        self.assertEqual(calls[0]["temperature"], 0.0)
        self.assertTrue(calls[0]["force_json"])

    def test_decision_node_handles_bare_text_no_from_tiny_model(self):
        node = {
            "id": "gate_2",
            "type": "router",
            "decision_mode": True,
            "routes": ["yes", "no"],
            "question": "Il budget è sufficiente?",
        }
        st = RunState(run_id=1, node_outputs={"p": "Budget zero"})
        node["depends_on"] = ["p"]
        chosen = execute_router_node(node, st, complete=lambda *a, **kw: "No, budget mancante.")
        # Plain text starting with "No," won't match single token if comma breaks, wait: strip handles punctuation
        self.assertEqual(chosen, "no")

    def test_when_route_gating_and_transitive_skip_propagation(self):
        st = RunState(run_id=2, routes={"gate_1": "yes"})
        task_yes = {"id": "t_yes", "when_route": {"from": "gate_1", "equals": "sì"}, "depends_on": ["gate_1"]}
        task_no = {"id": "t_no", "when_route": {"from": "gate_1", "equals": "no"}, "depends_on": ["gate_1"]}
        self.assertTrue(node_is_enabled(task_yes, st))
        self.assertFalse(node_is_enabled(task_no, st))

        # Mark t_no skipped -> downstream child relying exclusively on t_no must skip automatically
        st.mark_skipped("t_no")
        sub_no = {"id": "t_sub_no", "depends_on": ["t_no"]}
        self.assertFalse(node_is_enabled(sub_no, st))

    def test_is_jev_decision_model_and_payload_builder(self):
        for name in ("jev", "jev:latest", "tev1", "tev1:0.8b", "nimble:latest", "typesafe/jev-1.14"):
            self.assertTrue(is_jev_decision_model(name), f"Expected Jev model: {name}")
        for name in ("qwen2.5:14b", "llama3.2:3b", "gpt-4o", "gemini-2.5-flash"):
            self.assertFalse(is_jev_decision_model(name), f"Unexpected Jev match: {name}")

        node = normalize_node(
            {
                "id": "jev_1",
                "type": "decision",
                "question": "Il report contiene rischi bloccanti?",
                "jev_model": "tev1",
                "decision_primitive": "noul",
                "threshold": 0.65,
                "depends_on": ["step_a"],
            }
        )
        self.assertEqual(node["decision_engine"], "jev")
        self.assertEqual(node["decision_primitive"], "noul")
        self.assertAlmostEqual(node["threshold"], 0.65)
        self.assertEqual(node["jev_model"], "tev1")

        st = RunState(run_id=5, node_outputs={"step_a": "Trovate 2 vulnerabilità SQL injection."})
        payload = build_jev_request_payload(node, st)
        self.assertEqual(payload["model"], "tev1")
        self.assertIn("vulnerabilità SQL injection", payload["state"])
        self.assertEqual(payload["questions"]["decision"]["type"], "noul")
        self.assertEqual(payload["questions"]["decision"]["instructions"], "Il report contiene rischi bloccanti?")

    def test_jev_noul_threshold_gating_and_ollama_systemone_response(self):
        # 1) TypeSafe / OpenRouter `noul` scalar response gated by threshold
        base_node = normalize_node(
            {
                "id": "gate_jev",
                "type": "decision",
                "question": "Approvare l'operazione?",
                "jev_model": "typesafe/jev-1.14",
                "depends_on": ["ctx"],
                "threshold": 0.50,
            }
        )
        st = RunState(run_id=10, node_outputs={"ctx": "Proposta standard con rischio moderato."})

        def fake_decide_typesafe(payload):
            return {
                "model": payload["model"],
                "answers": {"decision": {"type": "noul", "noul": 0.62}},
                "usage": {"input_tokens": 42, "output_tokens": 0},
            }

        out_yes = execute_typed_node(NODE_TYPE_ROUTER, base_node, st, decide=fake_decide_typesafe)
        self.assertEqual(st.route_of("gate_jev"), "yes")
        parsed_yes = json.loads(out_yes)
        self.assertEqual(parsed_yes["engine"], "jev_systemone")
        self.assertEqual(parsed_yes["primitive"], "noul")
        self.assertAlmostEqual(parsed_yes["noul"], 0.62, places=3)
        self.assertEqual(extract_output_key(out_yes, "noul"), "0.62")

        # Raising threshold above 0.62 flips the same probability to 'no'
        strict_node = {**base_node, "threshold": 0.70}
        st_strict = RunState(run_id=11, node_outputs={"ctx": "Proposta standard con rischio moderato."})
        out_no = execute_typed_node(NODE_TYPE_ROUTER, strict_node, st_strict, decide=fake_decide_typesafe)
        self.assertEqual(st_strict.route_of("gate_jev"), "no")
        parsed_no = json.loads(out_no)
        self.assertAlmostEqual(parsed_no["confidence"], 0.38, places=3)

        # 2) Ollama /v1/systemone `probabilities` dict response format
        ollama_resp = {
            "answers": {
                "decision": {
                    "type": "noul",
                    "choice": "yes",
                    "probabilities": {"yes": 0.79, "no": 0.21},
                }
            }
        }
        details = parse_jev_decision_response(ollama_resp, base_node)
        self.assertEqual(details["route"], "yes")
        self.assertAlmostEqual(details["noul"], 0.79, places=3)

        # 3) Jev offline endpoint transparently falls back to generative `complete`
        fallback_route = execute_router_node(
            base_node,
            st,
            decide=lambda _payload: (_ for _ in ()).throw(ConnectionError("Ollama down")),
            complete=lambda *a, **kw: '{"decision": "no", "reason": "Fallback LLM", "confidence": 0.91}',
        )
        self.assertEqual(fallback_route, "no")


class StructuredHandoffTests(unittest.TestCase):
    def test_build_structured_handoff_envelope_from_markdown_and_extract_key(self):
        md = (
            "## Summary\n"
            "Il mercato italiano mostra una crescita del 18% anno su anno nel segmento SaaS.\n\n"
            "## Key Points\n"
            "- Crescita annuale: +18% YoY\n"
            "- Costo acquisizione medio: 240 EUR\n"
            "- Churn mensile inferiore al 3%\n"
        )
        env = build_structured_handoff_envelope(
            md,
            task_id=10,
            task_name="Analisi Mercato",
            agent_role="Market Researcher",
            node_id="step_1",
        )
        self.assertIn("18%", env["summary"])
        self.assertEqual(len(env["key_facts"]), 3)
        self.assertEqual(extract_output_key(md, "summary"), env["summary"])
        self.assertIn("Crescita annuale: +18% YoY", extract_output_key(md, "key_facts"))

        block = format_upstream_handoff_block([env], wired_values={"richiesta": "Focus PMI"})
        self.assertIn("STRUCTURED HANDOFF FROM UPSTREAM", block)
        self.assertIn("Analisi Mercato", block)
        self.assertIn("Crescita annuale: +18% YoY", block)
        self.assertIn("Focus PMI", block)

    def test_enforce_node_handoff_populates_memory_with_envelope(self):
        mem = DummyMemoryManager()
        handoff = enforce_node_handoff(
            mem,
            run_id=9,
            task_id=42,
            task_out='{"summary": "Analisi completata", "key_facts": ["Fatto A", "Fatto B"]}',
            agent_role="Analyst",
            task_tools=[],
            node_id="n_42",
            task_name="Analisi",
        )
        self.assertIn("envelope", handoff)
        self.assertEqual(handoff["envelope"]["summary"], "Analisi completata")
        saved = mem.read_record("task_42")
        self.assertIsNotNone(saved)
        self.assertEqual(saved["data"]["key_facts"], ["Fatto A", "Fatto B"])


class TeamArchitectTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "test_alfredo.sqlite")
        self.db = SQLiteManager(db_path=self.db_path)

        # Register 2 models (1 lightweight local decision model + 1 advanced model)
        self.m_basic_id = self.db.create_model(
            provider="ollama",
            model_name="llama3.2:3b",
            is_local=True,
            supports_tools=True,
            capability_tier="basic",
        )
        self.m_adv_id = self.db.create_model(
            provider="ollama",
            model_name="qwen2.5:14b",
            is_local=True,
            supports_tools=True,
            capability_tier="advanced",
        )
        # Create 1 pre-existing agent to test automatic reuse
        self.existing_researcher_id = self.db.create_agent(
            name="Ricercatore Fonti",
            role="Senior Information Researcher",
            backstory="Ricercatore esperto",
            model_id=self.m_adv_id,
            tools=["web_search"],
        )

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def test_recommend_models_for_tiers(self):
        rec = recommend_models_for_tiers(self.db, prefer_local=True)
        self.assertEqual(rec["decision"], self.m_basic_id)
        self.assertEqual(rec["advanced"], self.m_adv_id)

    def test_recommend_models_separates_jev_from_generative_workers(self):
        m_jev_id = self.db.create_model(
            provider="ollama",
            model_name="tev1:latest",
            is_local=True,
            supports_tools=False,
            capability_tier="basic",
        )
        rec = recommend_models_for_tiers(self.db, prefer_local=True)
        self.assertEqual(rec["decision"], m_jev_id)
        self.assertEqual(rec["decision_model_name"], "tev1:latest")
        # Non-autoregressive Jev model must NEVER be selected for generative agent tiers
        self.assertEqual(rec["basic"], self.m_basic_id)
        self.assertEqual(rec["standard"], self.m_basic_id)
        self.assertEqual(rec["advanced"], self.m_adv_id)

        bp = generate_team_and_workflow_blueprint(
            "Analizza un documento e decidi se è conforme.",
            self.db,
            include_decision_gates=True,
            prefer_local_models=True,
        )
        decs = [n for n in bp["nodes"] if n["type"] == "decision"]
        self.assertEqual(decs[0]["decision_engine"], "jev")
        self.assertEqual(decs[0]["decision_primitive"], "noul")
        self.assertEqual(decs[0]["jev_model"], "tev1:latest")
        mat = materialize_blueprint_to_db(bp, self.db, save_workflow=False)
        self.assertEqual(mat["errors"], [])
        routers = [n for n in mat["graph"] if n.get("type") == NODE_TYPE_ROUTER]
        self.assertEqual(routers[0]["decision_engine"], "jev")
        self.assertEqual(routers[0]["jev_model"], "tev1:latest")

    def test_generate_and_materialize_heuristic_blueprint_with_decision_gate(self):
        bp = generate_team_and_workflow_blueprint(
            "Analizza i competitor SaaS nel settore HR e prepara un report esecutivo.",
            self.db,
            include_decision_gates=True,
            prefer_local_models=True,
            reuse_existing_agents=True,
            expected_exports=["markdown"],
        )
        self.assertEqual(bp["source"], "heuristic")
        self.assertGreaterEqual(len(bp["agents"]), 2)
        # Pre-existing researcher should be matched for reuse
        reused = [a for a in bp["agents"] if a.get("reuse_agent_id") == self.existing_researcher_id]
        self.assertEqual(len(reused), 1)

        decisions = [n for n in bp["nodes"] if n["type"] == "decision"]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["model_id"], self.m_basic_id)

        mat = materialize_blueprint_to_db(bp, self.db, save_workflow=True)
        self.assertEqual(mat["errors"], [])
        self.assertIn(self.existing_researcher_id, mat["reused_agent_ids"])
        self.assertGreaterEqual(len(mat["created_task_ids"]), 3)
        self.assertIsNotNone(mat["workflow_id"])

        # Validate saved graph contains Level columns, Task snapshots, Binary Router & Export
        wf = self.db.read_workflow(mat["workflow_id"])
        self.assertIsNotNone(wf)
        task_map = {t["id"]: t for t in self.db.read_all_tasks()}
        errs, _ = validate_graph(mat["graph"], task_id_map=task_map)
        self.assertEqual(errs, [])

        router_nodes = [n for n in mat["graph"] if n.get("type") == NODE_TYPE_ROUTER]
        self.assertEqual(len(router_nodes), 1)
        self.assertTrue(router_nodes[0]["decision_mode"])
        self.assertEqual(router_nodes[0]["routes"], ["yes", "no"])

        gated_tasks = [n for n in mat["graph"] if isinstance(n.get("when_route"), dict)]
        self.assertGreaterEqual(len(gated_tasks), 2)

    def test_generate_blueprint_with_mock_llm_and_repair(self):
        payload = {
            "workflow_name": "Audit Tecnico Sì/No",
            "description": "Valuta un repository e approva o richiedi fix.",
            "expected_exports": [],
            "agents": [
                {
                    "agent_key": "auditor",
                    "name": "Code Auditor",
                    "role": "Senior Security Auditor",
                    "goal": "Trova vulnerabilità.",
                    "backstory": "Esperto AppSec.",
                    "recommended_tier": "advanced",
                }
            ],
            "nodes": [
                {
                    "id": "audit_1",
                    "type": "task",
                    "name": "Analisi Sicurezza",
                    "agent_key": "auditor",
                    "execution_level": 1,
                    "depends_on": [],
                    "description": "Analizza rischi su {user_input}.",
                    "expected_output": "Lista rischi.",
                },
                {
                    "id": "dec_ok",
                    "type": "decision",
                    "name": "Passa Audit?",
                    "execution_level": 2,
                    "depends_on": ["audit_1"],
                    "question": "Il codice è privo di rischi critici?",
                    "routes": ["yes", "no"],
                },
                {
                    "id": "release_note",
                    "type": "task",
                    "name": "Approva Rilascio",
                    "agent_key": "auditor",
                    "execution_level": 3,
                    "depends_on": ["dec_ok", "audit_1"],
                    "when_route": {"from": "dec_ok", "equals": "yes"},
                    "description": "Emetti ok basato su {audit_sum}.",
                    "expected_output": "Nulla osta.",
                    "inputs_map": {"audit_sum": {"from": "audit_1", "key": "summary"}},
                },
            ],
        }

        bp = generate_team_and_workflow_blueprint(
            "Controllo sicurezza codice prima del rilascio",
            self.db,
            llm_callable=lambda **kwargs: json.dumps(payload),
        )
        self.assertEqual(bp["source"], "llm")
        self.assertEqual(bp["workflow_name"], "Audit Tecnico Sì/No")
        mat = materialize_blueprint_to_db(bp, self.db, save_workflow=False)
        self.assertEqual(mat["errors"], [])


if __name__ == "__main__":
    unittest.main()
