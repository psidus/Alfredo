"""
core/module_registry.py

Modular Tool Gateway for Alfredo's unified Assistant Chat.
Groups sidebar-compatible capabilities into toggleable modules so 14B local
models keep a crisp tool prompt (<= 8-10 active tools by default) while letting
users enable Assets/Skills or MCP/Apps with 1 click.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

MODULES: Dict[str, Dict[str, Any]] = {
    "workflows": {
        "id": "workflows",
        "label": "Workflows & Runs",
        "description": "List and start saved workflows, monitor/cancel runs, and answer human approvals.",
        "default_enabled": True,
        "tools": [
            "list_workflows",
            "start_workflow",
            "list_recent_runs",
            "get_run_status",
            "cancel_run",
            "answer_approval",
        ],
    },
    "files_code": {
        "id": "files_code",
        "label": "Files & Codebase",
        "description": "Read/write workspace files and inspect repositories via Graphify (local) or Repomix (cloud/filtered).",
        "default_enabled": True,
        "tools": [
            "list_files",
            "read_file",
            "write_file",
            "inspect_codebase_graph",
            "pack_codebase_repomix",
        ],
    },
    "memory_kb": {
        "id": "memory_kb",
        "label": "Memory & Knowledge Base",
        "description": "Save/recall long-term preferences and query local vector knowledge bases.",
        "default_enabled": True,
        "tools": [
            "remember_fact",
            "recall_facts",
            "query_knowledge_base",
        ],
    },
    "assets_skills": {
        "id": "assets_skills",
        "label": "Assets, Skills & Architect",
        "description": "Browse/load SKILL.md playbooks, list registered agents, and draft new workflow blueprints.",
        "default_enabled": False,
        "tools": [
            "list_skills",
            "load_skill",
            "list_agents",
            "draft_workflow_blueprint",
        ],
    },
    "mcp_apps": {
        "id": "mcp_apps",
        "label": "MCP, My Apps & GPU Status",
        "description": "Invoke connected MCP server tools, list registered My Apps, and inspect remote/local GPU VRAM.",
        "default_enabled": False,
        "tools": [
            "mcp_list_tools",
            "mcp_call_tool",
            "list_apps",
            "get_system_status",
        ],
    },
}

TOOL_DOCS: Dict[str, str] = {
    "list_workflows": "list_workflows() — List saved workflows (IDs, names, descriptions, required inputs).",
    "start_workflow": 'start_workflow(workflow, inputs) — Launch a workflow immediately (by ID or name). Example: {"workflow": 1, "inputs": {"idea": "..."}}.',
    "list_recent_runs": "list_recent_runs(limit=5) — List recent runs with statuses.",
    "get_run_status": "get_run_status(run_id=0) — Get status, output summary, errors, and exports of a run (0 = latest).",
    "cancel_run": "cancel_run(run_id) — Cancel an active running/paused workflow run.",
    "answer_approval": "answer_approval(answer, interrupt_id=0) — Respond to a pending human approval checkpoint (0 = latest pending).",
    "list_files": 'list_files(directory="") — List files/folders inside workspace/.',
    "read_file": "read_file(path) — Read preview/content of a workspace file.",
    "write_file": "write_file(path, content) — Create or overwrite a file in workspace/.",
    "inspect_codebase_graph": 'inspect_codebase_graph(target="alfredo", query="") — Query AST/dependency graph (0 VRAM, ~1-3K tok; target = alfredo, app name, or workspace folder).',
    "pack_codebase_repomix": 'pack_codebase_repomix(target="alfredo", include="", compress=True) — Pack code/subfolder in Repomix XML with token-budget advisor.',
    "remember_fact": 'remember_fact(fact, category="preference") — Persist a user preference, rule, or project fact in long-term memory.',
    "recall_facts": 'recall_facts(query="", limit=8) — Retrieve saved user preferences and rules.',
    "query_knowledge_base": 'query_knowledge_base(query, db_name="") — Semantic search across local Vector Databases.',
    "list_skills": 'list_skills(category="") — List installed SKILL.md playbooks with token costs.',
    "load_skill": "load_skill(name) — Load the full instructions of an installed skill on demand.",
    "list_agents": "list_agents() — List registered agents, their roles, and default skills.",
    "draft_workflow_blueprint": "draft_workflow_blueprint(objective, save=False) — Use Team Architect to generate (and optionally save) a new multi-agent workflow blueprint.",
    "mcp_list_tools": 'mcp_list_tools(server_name="") — List tools on active MCP servers.',
    "mcp_call_tool": "mcp_call_tool(server_name, tool_name, arguments={}) — Execute an external MCP server tool.",
    "list_apps": "list_apps() — List registered external apps in My Apps (without exposing secrets).",
    "get_system_status": "get_system_status() — Inspect local or WireGuard remote GPU VRAM headroom, queue, and active models.",
}


def default_enabled_modules() -> List[str]:
    return [mid for mid, cfg in MODULES.items() if cfg.get("default_enabled")]


def normalize_enabled_modules(enabled_modules: Optional[Iterable[str]]) -> List[str]:
    if enabled_modules is None:
        return default_enabled_modules()
    cleaned = [str(m).strip() for m in enabled_modules if str(m).strip() in MODULES]
    return list(dict.fromkeys(cleaned))


def list_modules() -> List[Dict[str, Any]]:
    return [
        {
            "id": cfg["id"],
            "label": cfg["label"],
            "description": cfg["description"],
            "default_enabled": bool(cfg["default_enabled"]),
            "tools": list(cfg["tools"]),
        }
        for cfg in MODULES.values()
    ]


def build_tools_prompt_section(
    enabled_modules: Optional[Iterable[str]],
    *,
    context_mode: str = "auto",
) -> str:
    active_ids = normalize_enabled_modules(enabled_modules)
    if not active_ids:
        return "Active tool modules: NONE (pure conversational mode)."

    lines: List[str] = [
        f"Active tool modules: {', '.join(active_ids)} (Code context mode: {context_mode}).",
        "Available tools (invoke ONE at a time via ```tool_call {\"tool\": \"...\", \"args\": {...}} ```):",
    ]
    idx = 1
    seen: set[str] = set()
    for mid in active_ids:
        for tool_id in MODULES[mid]["tools"]:
            if tool_id in seen:
                continue
            seen.add(tool_id)
            doc = TOOL_DOCS.get(tool_id, tool_id)
            lines.append(f"{idx}. {doc}")
            idx += 1

    if "files_code" in active_ids:
        if context_mode == "repomix":
            lines.append("- Code context preference: `pack_codebase_repomix` is preferred (keep `include` narrow for local 14B, or route full packs to Cloud).")
        elif context_mode == "graphify":
            lines.append("- Code context preference: `inspect_codebase_graph` is preferred (0 VRAM AST slices).")
    return "\n".join(lines)


def execute_module_tool(
    tool_name: str,
    args: Optional[Dict[str, Any]],
    *,
    engine: Any,
    context_mode: str = "auto",
) -> Dict[str, Any]:
    """Execute one Assistant Chat tool safely and return a JSON-serializable dict."""
    name = (tool_name or "").strip().lower()
    args = args or {}

    try:
        # --- Workflows & Runs --------------------------------------------
        if name == "list_workflows":
            cards = engine.catalogue()
            return {
                "workflows": [
                    {
                        "id": c.id,
                        "name": c.name,
                        "description": c.description,
                        "inputs": [i.key for i in c.inputs],
                    }
                    for c in cards
                ]
            }

        if name == "start_workflow":
            from core.run_service import start_run

            wf_target = args.get("workflow") or args.get("workflow_id") or args.get("id") or args.get("name")
            raw_inputs = args.get("inputs") or {}
            if not isinstance(raw_inputs, dict):
                raw_inputs = {"user_input": str(raw_inputs)}

            cards = engine.catalogue()
            target_card = None
            try:
                wf_id = int(wf_target)
                target_card = next((c for c in cards if c.id == wf_id), None)
            except (ValueError, TypeError):
                pass

            if target_card is None and wf_target:
                query = str(wf_target).lower().strip()
                for c in cards:
                    c_name = c.name.lower().strip()
                    if query in c_name or c_name in query:
                        target_card = c
                        break

            if target_card is None:
                return {
                    "error": f"Workflow '{wf_target}' non trovato.",
                    "available_workflows": [f"#{c.id}: {c.name}" for c in cards],
                }

            declared = target_card.declared_keys()
            cleaned_inputs = dict(raw_inputs)
            if declared and len(declared) == 1 and declared[0] not in cleaned_inputs:
                for val in raw_inputs.values():
                    cleaned_inputs[declared[0]] = val
                    break

            run_id = start_run(target_card.id, inputs=cleaned_inputs, source="assistant-chat")
            return {
                "status": "started",
                "run_id": run_id,
                "workflow_id": target_card.id,
                "workflow_name": target_card.name,
                "inputs": cleaned_inputs,
                "_action": {
                    "type": "open_run",
                    "run_id": int(run_id),
                    "label": f"Open Run #{run_id}",
                },
            }

        if name == "get_run_status":
            from core.files import describe_run_exports
            from core.run_service import get_run, list_recent_runs

            run_id = int(args.get("run_id") or args.get("id") or 0)
            if not run_id:
                recent = list_recent_runs(limit=1, db=engine.db)
                if recent and recent[0].get("id"):
                    run_id = int(recent[0]["id"])
                else:
                    return {"error": "Nessuna esecuzione trovata nel sistema."}
            run = get_run(run_id, db=engine.db)
            if not run:
                return {"error": f"Run #{run_id} non trovata"}
            exports = describe_run_exports(run)
            result_str = str(run.get("result") or "")
            return {
                "run_id": run_id,
                "status": run.get("status"),
                "workflow_id": run.get("workflow_id"),
                "result": result_str[:3000],
                "current_task_idx": run.get("current_task_idx"),
                "started_at": str(run.get("started_at")),
                "finished_at": str(run.get("finished_at")),
                "exported_files": [e.get("name") for e in exports],
            }

        if name == "list_recent_runs":
            from core.run_service import list_recent_runs

            limit = int(args.get("limit") or 5)
            runs = list_recent_runs(limit=limit, db=engine.db)
            return {
                "recent_runs": [
                    {
                        "run_id": r.get("id"),
                        "workflow_id": r.get("workflow_id"),
                        "status": r.get("status"),
                        "started_at": str(r.get("started_at")),
                    }
                    for r in runs
                ]
            }

        if name == "cancel_run":
            from core.run_service import cancel_run

            run_id = int(args.get("run_id") or args.get("id") or 0)
            if not run_id:
                return {"error": "run_id is required"}
            ok = cancel_run(run_id, db=engine.db)
            return {"run_id": run_id, "cancelled": bool(ok)}

        if name == "answer_approval":
            from core.run_service import pending_interrupts, resume_interrupt

            answer = str(args.get("answer") or args.get("feedback") or "").strip()
            if not answer:
                return {"error": "answer is required"}
            intr_id = int(args.get("interrupt_id") or args.get("id") or 0)
            if not intr_id:
                pending = pending_interrupts(db=engine.db) or []
                if len(pending) == 1:
                    intr_id = int(pending[0]["id"])
                elif not pending:
                    return {"error": "No pending human approvals right now."}
                else:
                    return {
                        "error": "Multiple pending approvals exist; specify interrupt_id.",
                        "pending": [
                            {"interrupt_id": p["id"], "run_id": p.get("run_id"), "question": p.get("question")}
                            for p in pending
                        ],
                    }
            outcome = resume_interrupt(intr_id, answer, execute=False)
            if outcome.get("run_id") and outcome.get("status") not in ("stale", "failed"):
                from core.run_service import start_execution

                start_execution(int(outcome["run_id"]), chat_id=None)
                outcome["execution"] = "started"
            return outcome

        # --- Files & Codebase --------------------------------------------
        if name == "list_files":
            from core.files import list_files

            directory = str(args.get("directory") or args.get("rel_path") or args.get("path") or "").strip()
            result_listing = list_files(directory)
            raw_entries = result_listing.get("entries") if isinstance(result_listing, dict) else result_listing
            return {
                "directory": directory or "workspace",
                "files": [
                    {
                        "name": e["name"],
                        "path": e["path"],
                        "is_dir": e["is_dir"],
                        "size": e["size"],
                        "kind": e["kind"],
                    }
                    for e in (raw_entries or [])
                    if isinstance(e, dict)
                ],
            }

        if name == "read_file":
            from core.files import preview

            path = str(args.get("path") or args.get("filename") or args.get("rel_path") or "").strip()
            if not path:
                return {"error": "path is required"}
            data = preview(path, max_chars=4000)
            content = data.get("text") or data.get("rows") or data.get("message") or ""
            return {
                "path": path,
                "kind": data.get("kind"),
                "content": content,
                "truncated": data.get("truncated", False),
            }

        if name == "write_file":
            from core.files import write_file

            path = str(args.get("path") or args.get("filename") or args.get("rel_path") or "").strip()
            content = str(args.get("content") or "")
            if not path:
                return {"error": "path is required"}
            info = write_file(path, content)
            return {"status": "success", "file": info}

        if name == "inspect_codebase_graph":
            from core.codebase_context import query_graphify

            target = str(args.get("target") or "alfredo").strip()
            query = str(args.get("query") or "").strip()
            res = query_graphify(target=target, query=query, max_chars=9000, db=engine.db)
            return res

        if name == "pack_codebase_repomix":
            from core.codebase_context import pack_repomix

            target = str(args.get("target") or "alfredo").strip()
            include = str(args.get("include") or "").strip()
            compress = bool(args.get("compress", True))
            max_chars = 32000 if context_mode == "local" else 60000
            res = pack_repomix(target=target, include=include, compress=compress, max_chars=max_chars, db=engine.db)
            return res

        # --- Memory & Knowledge Base -------------------------------------
        if name == "remember_fact":
            from core.long_term_memory import LongTermFactMemory

            fact = str(args.get("fact") or "").strip()
            category = str(args.get("category") or "preference").strip()
            if not fact:
                return {"error": "fact is required"}
            fid = LongTermFactMemory().add_fact(fact=fact, category=category, source="assistant-chat")
            return {"status": "saved", "id": fid, "fact": fact, "category": category}

        if name == "recall_facts":
            from core.long_term_memory import LongTermFactMemory

            query = str(args.get("query") or "").strip()
            limit = int(args.get("limit") or 8)
            facts = LongTermFactMemory().search_facts(query=query, limit=limit)
            return {"facts": [{"id": f.get("id"), "category": f.get("category"), "fact": f.get("fact")} for f in facts]}

        if name == "query_knowledge_base":
            from core.vector_manager import VectorManager

            query = str(args.get("query") or "").strip()
            db_name = str(args.get("db_name") or args.get("name") or "").strip().lower()
            if not query:
                return {"error": "query is required"}
            vdbs = engine.db.read_all_vector_dbs() or [] if hasattr(engine.db, "read_all_vector_dbs") else []
            if not vdbs:
                return {"error": "No Vector Databases configured yet."}
            matches = [v for v in vdbs if db_name and db_name in str(v.get("name") or "").lower()] or vdbs[:2]
            vm = VectorManager()
            results: List[Dict[str, Any]] = []
            for vdb in matches:
                try:
                    out = vm.query_database(
                        db_path=vdb["path"],
                        provider=vdb["provider"],
                        model_name=vdb["model_name"],
                        query=query,
                        k=4,
                    )
                    results.append({"db_name": vdb.get("name"), "result": str(out)[:2500]})
                except Exception as exc:
                    results.append({"db_name": vdb.get("name"), "error": str(exc)})
            return {"results": results}

        # --- Assets, Skills & Architect ----------------------------------
        if name == "list_skills":
            from core.skill_registry import list_skills

            category = str(args.get("category") or "").strip() or None
            skills = list_skills(category=category)
            return {
                "skills": [
                    {
                        "name": s["name"],
                        "category": s["category"],
                        "scope": s["scope"],
                        "description": s["description"],
                        "recommended_tools": s["recommended_tools"],
                        "token_cost": s["token_cost"],
                    }
                    for s in skills
                ]
            }

        if name == "load_skill":
            from core.skill_registry import get_skill

            sname = str(args.get("name") or args.get("skill") or "").strip()
            sk = get_skill(sname)
            if not sk:
                return {"error": f"Skill '{sname}' not found."}
            return {
                "name": sk["name"],
                "category": sk["category"],
                "scope": sk["scope"],
                "recommended_tools": sk["recommended_tools"],
                "token_cost": sk["token_cost"],
                "instructions": sk["body"],
            }

        if name == "list_agents":
            agents = engine.db.read_all_agents() or []
            return {
                "agents": [
                    {
                        "id": a.get("id"),
                        "name": a.get("name"),
                        "role": a.get("role"),
                        "skills": a.get("skills") or [],
                        "tools": a.get("tools") or [],
                    }
                    for a in agents
                ]
            }

        if name == "draft_workflow_blueprint":
            from core.team_architect import generate_team_and_workflow_blueprint, materialize_blueprint_to_db

            objective = str(args.get("objective") or args.get("goal") or "").strip()
            save_wf = bool(args.get("save", False))
            if not objective:
                return {"error": "objective is required"}
            bp = generate_team_and_workflow_blueprint(objective, engine.db)
            res = materialize_blueprint_to_db(bp, engine.db, save_workflow=save_wf)
            wf_id = res.get("workflow_id")
            action = {
                "type": "open_workflow_assembler",
                "workflow_id": wf_id,
                "workflow_name": bp.get("workflow_name"),
                "label": f"Open '{bp.get('workflow_name')}' in Workflow Assembler",
            }
            return {
                "status": "saved" if wf_id else "drafted",
                "workflow_id": wf_id,
                "workflow_name": bp.get("workflow_name"),
                "description": bp.get("description"),
                "agents_created": len(res.get("created_agent_ids") or []),
                "tasks_created": len(res.get("created_task_ids") or []),
                "_action": action,
            }

        # --- MCP, My Apps & GPU Status -----------------------------------
        if name == "mcp_list_tools":
            from core.mcp_client import get_mcp_manager

            server_name = str(args.get("server_name") or "").strip() or None
            tools = get_mcp_manager().list_tools(server_name=server_name)
            return {
                "tools": [
                    {
                        "server": t["server"],
                        "name": t["name"],
                        "description": t["description"],
                        "args": list(((t.get("inputSchema") or {}).get("properties") or {}).keys()),
                    }
                    for t in tools
                ]
            }

        if name == "mcp_call_tool":
            from core.mcp_client import get_mcp_manager

            server_name = str(args.get("server_name") or "").strip()
            tname = str(args.get("tool_name") or "").strip()
            targs = args.get("arguments") or args.get("args") or {}
            if not server_name or not tname:
                return {"error": "server_name and tool_name are required"}
            output = get_mcp_manager().call_tool(server_name, tname, targs if isinstance(targs, dict) else {})
            return {"server": server_name, "tool": tname, "output": output[:4000]}

        if name == "list_apps":
            apps = engine.db.read_all_apps() or [] if hasattr(engine.db, "read_all_apps") else []
            return {
                "apps": [
                    {
                        "id": a.get("id"),
                        "name": a.get("name"),
                        "display_name": a.get("display_name"),
                        "description": a.get("description"),
                        "app_root_path": a.get("app_root_path"),
                        "status": a.get("status"),
                    }
                    for a in apps
                ]
            }

        if name == "get_system_status":
            from core.gpu_gate import system_status

            status = system_status()
            return {
                "vram": status.get("vram"),
                "loaded_model": status.get("loaded_model"),
                "queue": status.get("queue"),
                "profiles": {
                    k: {"model": v.get("model"), "ctx": v.get("context_tokens"), "fits": v.get("fits")}
                    for k, v in (status.get("profiles") or {}).items()
                },
            }

        return {"error": f"Unknown tool: '{name}'"}
    except Exception as exc:
        logger.warning("Tool execution error for %s: %s", name, exc, exc_info=True)
        return {"error": str(exc)}


__all__ = [
    "MODULES",
    "build_tools_prompt_section",
    "default_enabled_modules",
    "execute_module_tool",
    "list_modules",
    "normalize_enabled_modules",
]
