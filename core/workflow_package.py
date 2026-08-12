"""
Portable Alfredo workflow packages (.alfredo.json).

Export/import workflows by stable names (not DB IDs). Secrets and local
paths are never embedded — only env-var names and metadata travel.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

from core.db_manager import DBManager
from core.schema_loader import get_available_schemas

logger = logging.getLogger(__name__)

FORMAT_VERSION = "1.0"
PACKAGE_EXT = ".alfredo.json"

# Keys that must never appear in a shared package (values or accidental embeds)
_FORBIDDEN_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "password",
    "passwd",
    "secret_value",
    "access_token",
    "refresh_token",
    "private_key",
    "connection_string",
    "database_url",
    "hub_token",
)


def _is_forbidden_key(key: str) -> bool:
    k = (key or "").strip().lower().replace("-", "_")
    if k in {"token", "secret", "password", "passwd", "credential", "credentials"}:
        return True
    return any(frag in k for frag in _FORBIDDEN_KEY_FRAGMENTS)


def sanitize_package_for_share(package: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deep-clean a package so only workflow *logic* is shared:
    agents, tasks, suggested models (by name), tools, inputs/outputs, graph.
    Never ships credential *values* — only optional env var *names*.
    """
    import copy

    def scrub(obj: Any, parent_key: str = "") -> Any:
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if _is_forbidden_key(str(k)):
                    continue
                # Never keep raw secret-looking strings under sensitive parents
                if isinstance(v, str) and _is_forbidden_key(str(k)):
                    continue
                out[k] = scrub(v, str(k))
            return out
        if isinstance(obj, list):
            return [scrub(x, parent_key) for x in obj]
        return obj

    cleaned = scrub(copy.deepcopy(package))

    # Harden models: only allowlist fields (env_var_name = name of env var, never the value)
    safe_models = []
    for m in cleaned.get("models") or []:
        if not isinstance(m, dict):
            continue
        safe_models.append({
            "provider": m.get("provider"),
            "model_name": m.get("model_name"),
            "env_var_name": m.get("env_var_name") or "",
            "is_local": bool(m.get("is_local")),
            "vram_gb": float(m.get("vram_gb") or 0),
            "supports_tools": bool(m.get("supports_tools", True)),
        })
    cleaned["models"] = safe_models

    # App stub: identity only — no api_key, no connection strings
    wf = cleaned.get("workflow") or {}
    app = wf.get("app")
    if isinstance(app, dict):
        wf["app"] = {
            "name": app.get("name"),
            "display_name": app.get("display_name"),
            "db_type": app.get("db_type"),
            # public base URL is ok as hint; credentials stay local via env key *names*
            "api_base_url": app.get("api_base_url"),
            "db_env_key": app.get("db_env_key"),
            "api_env_key": app.get("api_env_key"),
        }
        cleaned["workflow"] = wf

    # tool_manifest.required_secrets = env var NAMES only
    manifest = cleaned.get("tool_manifest") or {}
    secrets = manifest.get("required_secrets") or []
    manifest["required_secrets"] = [
        s for s in secrets
        if isinstance(s, str) and s.isidentifier() or (isinstance(s, str) and s.replace("_", "").isalnum())
    ]
    cleaned["tool_manifest"] = manifest

    cleaned["checksum"] = package_checksum(cleaned)
    cleaned.setdefault("privacy", {
        "ships_credentials": False,
        "note": "Only workflow logic is shared. Each installer uses their own .env / API keys.",
    })
    return cleaned


def _load_known_tool_names() -> Set[str]:
    names: Set[str] = set()
    tools_map = os.path.join(os.getcwd(), "config", "tools_map.yaml")
    if os.path.isfile(tools_map):
        try:
            with open(tools_map, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            names.update((data.get("tools_registry") or {}).keys())
        except Exception as e:
            logger.warning(f"Could not read tools_map.yaml: {e}")
    try:
        from core.crew_builder import ALLOWED_TOOLS
        names.update(ALLOWED_TOOLS.keys())
    except Exception:
        pass
    # Always include memory sentinels
    names.update({"read_atomic_memory", "write_atomic_memory", "vector_search", "tabular_query"})
    return names


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return s or f"workflow-{uuid.uuid4().hex[:8]}"


def _task_ids_from_graph(task_ids: Any) -> List[int]:
    """Collect numeric task IDs from legacy / DAG / batch_loop / typed block structures."""
    try:
        from core.workflow_graph import collect_task_ids
        return collect_task_ids(task_ids or [])
    except Exception:
        out: List[int] = []
        if not task_ids:
            return out
        for step in task_ids:
            if isinstance(step, int):
                out.append(step)
            elif isinstance(step, dict):
                if step.get("type") in ("input", "hitl", "export"):
                    continue
                if step.get("type") == "batch_loop":
                    for tid in step.get("task_ids") or []:
                        if tid is not None:
                            out.append(int(tid))
                elif step.get("task_id") is not None:
                    out.append(int(step["task_id"]))
        seen = set()
        ordered = []
        for tid in out:
            if tid not in seen:
                seen.add(tid)
                ordered.append(tid)
        return ordered


def package_checksum(package: Dict[str, Any]) -> str:
    """SHA-256 of canonical JSON (excluding checksum field itself)."""
    payload = {k: v for k, v in package.items() if k != "checksum"}
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_package(package: Dict[str, Any]) -> List[str]:
    """Return list of hard errors (empty = ok)."""
    errors = []
    if not isinstance(package, dict):
        return ["Package must be a JSON object"]
    if package.get("format_version") != FORMAT_VERSION:
        errors.append(f"Unsupported format_version (expected {FORMAT_VERSION})")
    if not package.get("workflow") or not isinstance(package["workflow"], dict):
        errors.append("Missing workflow object")
    else:
        if not package["workflow"].get("name"):
            errors.append("workflow.name is required")
        if not package["workflow"].get("graph"):
            errors.append("workflow.graph is required")
    if not isinstance(package.get("tasks"), list) or not package["tasks"]:
        errors.append("tasks[] is required and must be non-empty")
    if not isinstance(package.get("agents"), list) or not package["agents"]:
        errors.append("agents[] is required and must be non-empty")
    return errors


def analyze_package_compatibility(package: Dict[str, Any]) -> Dict[str, Any]:
    """Soft warnings for missing tools, schemas, env vars on this host."""
    known_tools = _load_known_tool_names()
    available_schemas = set(get_available_schemas().keys())
    tools_needed: Set[str] = set()
    secrets_needed: Set[str] = set()
    schemas_needed: Set[str] = set()

    for m in package.get("models") or []:
        env = (m.get("env_var_name") or "").strip()
        if env:
            secrets_needed.add(env)
    for a in package.get("agents") or []:
        tools_needed.update(a.get("tools") or [])
    for t in package.get("tasks") or []:
        tools_needed.update(t.get("tools") or [])
        pydantic = t.get("output_pydantic") or ""
        for name in [s.strip() for s in pydantic.split(",") if s.strip()]:
            schemas_needed.add(name)
    manifest = package.get("tool_manifest") or {}
    tools_needed.update(manifest.get("builtins") or [])
    tools_needed.update(manifest.get("custom") or [])
    secrets_needed.update(manifest.get("required_secrets") or [])

    missing_tools = sorted(t for t in tools_needed if t and t not in known_tools)
    custom_tools = sorted(manifest.get("custom") or [])
    missing_schemas = sorted(s for s in schemas_needed if s not in available_schemas)
    missing_secrets = sorted(s for s in secrets_needed if s and not os.getenv(s))

    return {
        "missing_tools": missing_tools,
        "custom_tools": custom_tools,
        "missing_schemas": missing_schemas,
        "missing_secrets": missing_secrets,
        "required_secrets": sorted(secrets_needed),
    }


def export_workflow(
    workflow_id: int,
    *,
    description: str = "",
    tags: Optional[List[str]] = None,
    author: str = "",
    license_name: str = "proprietary",
    db=None,
) -> Dict[str, Any]:
    """Build a portable package dict for the given workflow ID."""
    db = db or DBManager()
    wf = db.read_workflow(workflow_id)
    if not wf:
        raise ValueError(f"Workflow ID {workflow_id} not found")

    task_ids = _task_ids_from_graph(wf.get("task_ids") or [])
    tasks_raw = []
    agents_by_id: Dict[int, Dict] = {}
    models_by_id: Dict[int, Dict] = {}
    vector_db_ids: Set[int] = set()
    all_tool_names: Set[str] = set()

    for tid in task_ids:
        t = db.read_task(tid)
        if not t:
            continue
        tasks_raw.append(t)
        for tool in t.get("tools") or []:
            all_tool_names.add(tool)
        for v in t.get("vector_dbs") or []:
            try:
                vector_db_ids.add(int(v))
            except (TypeError, ValueError):
                pass
        aid = t.get("agent_id")
        if aid and aid not in agents_by_id:
            agent = db.read_agent(aid)
            if agent:
                agents_by_id[aid] = agent
                for tool in agent.get("tools") or []:
                    all_tool_names.add(tool)
                mid = agent.get("model_id")
                if mid and mid not in models_by_id:
                    m = db.read_model(mid)
                    if m:
                        models_by_id[mid] = m
        tmid = t.get("model_id")
        if tmid and tmid not in models_by_id:
            m = db.read_model(tmid)
            if m:
                models_by_id[tmid] = m

    # Stable refs for tasks
    id_to_ref: Dict[int, str] = {}
    for t in tasks_raw:
        tid = t["id"]
        base = t.get("name") or f"task_{tid}"
        ref = _slugify(str(base))
        # uniquify
        candidate = ref
        n = 2
        while candidate in id_to_ref.values():
            candidate = f"{ref}-{n}"
            n += 1
        id_to_ref[tid] = candidate

    models_out = []
    for m in models_by_id.values():
        models_out.append({
            "provider": m.get("provider"),
            "model_name": m.get("model_name"),
            "env_var_name": m.get("env_var_name") or "",
            "is_local": bool(m.get("is_local")),
            "vram_gb": float(m.get("vram_gb") or 0),
            "supports_tools": bool(m.get("supports_tools", 1)),
        })

    agents_out = []
    for a in agents_by_id.values():
        model_name = None
        if a.get("model_id"):
            m = models_by_id.get(a["model_id"]) or db.read_model(a["model_id"])
            model_name = m.get("model_name") if m else None
        agents_out.append({
            "name": a.get("name"),
            "role": a.get("role"),
            "backstory": a.get("backstory") or "",
            "model": model_name,
            "tools": list(a.get("tools") or []),
        })

    # Vector DBs by id → name
    all_vdbs = {v["id"]: v for v in (db.read_all_vector_dbs() or [])}
    vector_out = []
    vdb_id_to_name: Dict[int, str] = {}
    for vid in vector_db_ids:
        v = all_vdbs.get(vid)
        if not v:
            continue
        vdb_id_to_name[vid] = v["name"]
        vector_out.append({
            "name": v["name"],
            "embedding_provider": v.get("provider"),
            "embedding_model": v.get("model_name"),
            "content_included": False,
            "content": None,
        })

    tasks_out = []
    for t in tasks_raw:
        model_name = None
        if t.get("model_id"):
            m = models_by_id.get(t["model_id"]) or db.read_model(t["model_id"])
            model_name = m.get("model_name") if m else None
        agent_name = None
        if t.get("agent_id") and t["agent_id"] in agents_by_id:
            agent_name = agents_by_id[t["agent_id"]].get("name")
        vdb_names = []
        for vid in t.get("vector_dbs") or []:
            try:
                name = vdb_id_to_name.get(int(vid))
                if name:
                    vdb_names.append(name)
            except (TypeError, ValueError):
                pass
        tasks_out.append({
            "ref": id_to_ref[t["id"]],
            "name": t.get("name"),
            "description": t.get("description"),
            "expected_output": t.get("expected_output"),
            "agent": agent_name,
            "model": model_name,
            "tools": list(t.get("tools") or []),
            "required_inputs": list(t.get("required_inputs") or []),
            "vector_dbs": vdb_names,
            "output_pydantic": t.get("output_pydantic") or "",
            "tool_profile": t.get("tool_profile") or "",
            "agent_specialization": t.get("agent_specialization"),
            "human_validation": bool(t.get("human_validation")),
            "max_input_context": int(t.get("max_input_context") or 0),
            "max_output_tokens": int(t.get("max_output_tokens") or 0),
        })

    # Remap graph
    graph_out = []
    raw_steps = wf.get("task_ids") or []
    for i, step in enumerate(raw_steps):
        if isinstance(step, int):
            graph_out.append({
                "id": f"node_{i}",
                "task": id_to_ref.get(step),
                "depends_on": [f"node_{i-1}"] if i > 0 else [],
                "execution_level": 1,
                "model_tier": "default",
            })
        elif isinstance(step, dict) and step.get("type") == "batch_loop":
            inner_refs = [id_to_ref[int(tid)] for tid in (step.get("task_ids") or []) if int(tid) in id_to_ref]
            node = {
                "id": step.get("id") or f"node_{i}",
                "type": "batch_loop",
                "tasks": inner_refs,
                "batch_size": step.get("batch_size", 5),
                "source_variable": step.get("source_variable", ""),
                "depends_on": list(step.get("depends_on") or []),
                "execution_level": step.get("execution_level", 1),
                "model_tier": step.get("model_tier", "default"),
            }
            graph_out.append(node)
        elif isinstance(step, dict):
            tid = step.get("task_id")
            node = {
                "id": step.get("id") or f"node_{i}",
                "task": id_to_ref.get(int(tid)) if tid is not None else None,
                "depends_on": list(step.get("depends_on") or []),
                "execution_level": step.get("execution_level", 1),
                "model_tier": step.get("model_tier", "default"),
            }
            graph_out.append(node)

    app_stub = None
    if wf.get("app_id"):
        app = db.get_app(wf["app_id"])
        if app:
            app_stub = {
                "name": app.get("name"),
                "display_name": app.get("display_name"),
                "db_type": app.get("db_type"),
                "api_base_url": app.get("api_base_url"),
                "db_env_key": app.get("db_env_key"),
                "api_env_key": app.get("api_env_key"),
                # never ship api_key
            }

    known = _load_known_tool_names()
    builtins = sorted(t for t in all_tool_names if t in known)
    custom = sorted(t for t in all_tool_names if t not in known)
    required_secrets = sorted({
        m.get("env_var_name") for m in models_out if m.get("env_var_name")
    })

    package = {
        "format_version": FORMAT_VERSION,
        "package": {
            "name": wf.get("name"),
            "slug": _slugify(wf.get("name") or ""),
            "description": description or "",
            "tags": list(tags or []),
            "author": author or "",
            "license": license_name,
            "alfredo_min_version": "1.0",
            "exported_at": datetime.now(timezone.utc).isoformat(),
        },
        "models": models_out,
        "agents": agents_out,
        "vector_databases": vector_out,
        "tasks": tasks_out,
        "workflow": {
            "name": wf.get("name"),
            "requires_human_check": bool(wf.get("requires_human_check")),
            "expected_exports": list(wf.get("expected_exports") or []),
            "export_instructions": wf.get("export_instructions") or "",
            "app": app_stub,
            "graph": graph_out,
        },
        "tool_manifest": {
            "builtins": builtins,
            "custom": custom,
            "required_secrets": required_secrets,
        },
        "schemas": [],
    }
    return sanitize_package_for_share(package)


def _unique_workflow_name(db, base: str) -> str:
    existing = {w["name"] for w in (db.read_all_workflows() or [])}
    if base not in existing:
        return base
    n = 2
    while f"{base} ({n})" in existing:
        n += 1
    return f"{base} ({n})"


def import_workflow(
    package: Dict[str, Any],
    *,
    conflict_policy: str = "rename",
    db=None,
) -> Dict[str, Any]:
    """
    Import a package into the local runtime DB.

    conflict_policy:
      - rename: rename workflow if name clashes (default)
      - skip: raise if workflow name exists
      - overwrite: delete existing workflow with same name then create (tasks/agents upsert)

    Returns dict with workflow_id, warnings, created entities summary.
    """
    db = db or DBManager()
    errors = validate_package(package)
    if errors:
        raise ValueError("Invalid package: " + "; ".join(errors))

    # Optional checksum verify
    if package.get("checksum"):
        expected = package["checksum"]
        actual = package_checksum(package)
        if expected != actual:
            logger.warning(f"Package checksum mismatch (expected {expected[:12]}… got {actual[:12]}…)")

    compat = analyze_package_compatibility(package)
    warnings = []
    if compat["missing_tools"]:
        warnings.append(f"Missing tools on this host: {', '.join(compat['missing_tools'])}")
    if compat["custom_tools"]:
        warnings.append(
            f"Custom tools declared (must already exist in custom_tools.py): {', '.join(compat['custom_tools'])}"
        )
    if compat["missing_schemas"]:
        warnings.append(
            f"Pydantic schemas not found locally (output_pydantic will be cleared): "
            f"{', '.join(compat['missing_schemas'])}"
        )
    if compat["missing_secrets"]:
        warnings.append(f"Env vars not set (configure .env): {', '.join(compat['missing_secrets'])}")

    available_schemas = set(get_available_schemas().keys())
    known_tools = _load_known_tool_names()

    # --- models upsert ---
    model_name_to_id: Dict[str, int] = {}
    for m in package.get("models") or []:
        name = m.get("model_name")
        if not name:
            continue
        existing = db.read_model_by_name(name)
        if existing:
            db.update_model(
                existing["id"],
                m.get("provider") or existing.get("provider"),
                name,
                m.get("env_var_name") or existing.get("env_var_name") or "",
                bool(m.get("is_local", existing.get("is_local"))),
                float(m.get("vram_gb") or existing.get("vram_gb") or 0),
                bool(m.get("supports_tools", existing.get("supports_tools", True))),
            )
            model_name_to_id[name] = existing["id"]
        else:
            mid = db.create_model(
                m.get("provider") or "openai",
                name,
                m.get("env_var_name") or "",
                bool(m.get("is_local")),
                float(m.get("vram_gb") or 0),
                bool(m.get("supports_tools", True)),
            )
            model_name_to_id[name] = mid

    # --- agents upsert by name ---
    agent_name_to_id: Dict[str, int] = {}
    for a in package.get("agents") or []:
        aname = a.get("name")
        if not aname:
            continue
        mid = model_name_to_id.get(a.get("model")) if a.get("model") else None
        if a.get("model") and mid is None:
            mrec = db.read_model_by_name(a["model"])
            mid = mrec["id"] if mrec else None
        tools = list(a.get("tools") or [])
        dropped = [t for t in tools if t not in known_tools]
        if dropped:
            warnings.append(
                f"Agent '{aname}': tools not registered on this host (kept in package assignment): {sorted(dropped)}"
            )

        existing = None
        if hasattr(db, "read_agent_by_name"):
            existing = db.read_agent_by_name(aname)
        else:
            for ag in db.read_all_agents() or []:
                if ag.get("name") == aname:
                    existing = ag
                    break
        if existing:
            db.update_agent(
                existing["id"],
                aname,
                a.get("role") or existing.get("role"),
                a.get("backstory") if a.get("backstory") is not None else existing.get("backstory"),
                mid if mid is not None else existing.get("model_id"),
                tools if tools is not None else (existing.get("tools") or []),
            )
            agent_name_to_id[aname] = existing["id"]
        else:
            aid = db.create_agent(
                aname,
                a.get("role") or "Worker",
                a.get("backstory") or "",
                mid,
                tools,
            )
            agent_name_to_id[aname] = aid

    # --- vector DBs upsert (metadata only, local empty path) ---
    vdb_name_to_id: Dict[str, int] = {}
    existing_vdbs = {v["name"]: v for v in (db.read_all_vector_dbs() or [])}
    for v in package.get("vector_databases") or []:
        vname = v.get("name")
        if not vname:
            continue
        if vname in existing_vdbs:
            vdb_name_to_id[vname] = existing_vdbs[vname]["id"]
        else:
            local_path = os.path.join("storage", "vector_dbs", _slugify(vname)).replace("\\", "/")
            os.makedirs(local_path, exist_ok=True)
            vid = db.create_vector_db(
                vname,
                local_path,
                v.get("embedding_provider") or "ollama",
                v.get("embedding_model") or "nomic-embed-text",
            )
            vdb_name_to_id[vname] = vid

    # --- tasks create ---
    ref_to_task_id: Dict[str, int] = {}
    for t in package.get("tasks") or []:
        ref = t.get("ref") or _slugify(t.get("name") or t.get("description") or "task")
        agent_id = agent_name_to_id.get(t.get("agent"))
        if not agent_id:
            raise ValueError(f"Task '{ref}' references unknown agent '{t.get('agent')}'")
        mid = None
        if t.get("model"):
            mid = model_name_to_id.get(t["model"])
            if mid is None:
                mrec = db.read_model_by_name(t["model"])
                mid = mrec["id"] if mrec else None
        tools = list(t.get("tools") or [])
        unknown = [x for x in tools if x not in known_tools]
        if unknown:
            warnings.append(
                f"Task '{ref}': tools not registered on this host (kept on task): {unknown}"
            )
        vdb_ids = []
        for vn in t.get("vector_dbs") or []:
            if vn in vdb_name_to_id:
                vdb_ids.append(str(vdb_name_to_id[vn]))
        pydantic = t.get("output_pydantic") or ""
        if pydantic:
            keep = [s.strip() for s in pydantic.split(",") if s.strip() in available_schemas]
            pydantic = ",".join(keep)

        tid = db.create_task(
            description=t.get("description") or "",
            expected_output=t.get("expected_output") or "",
            agent_id=agent_id,
            tools=tools,
            required_inputs=t.get("required_inputs") or [],
            vector_dbs=vdb_ids,
            agent_specialization=t.get("agent_specialization"),
            name=t.get("name"),
            model_id=mid,
            human_validation=bool(t.get("human_validation")),
            max_input_context=int(t.get("max_input_context") or 0),
            max_output_tokens=int(t.get("max_output_tokens") or 0),
            output_pydantic=pydantic or None,
            tool_profile=t.get("tool_profile") or "",
        )
        ref_to_task_id[ref] = tid

    # --- workflow name / conflict ---
    wf_meta = package["workflow"]
    wf_name = wf_meta.get("name") or package.get("package", {}).get("name") or "Imported Workflow"
    existing_wfs = {w["name"]: w for w in (db.read_all_workflows() or [])}
    if wf_name in existing_wfs:
        if conflict_policy == "skip":
            raise ValueError(f"Workflow '{wf_name}' already exists")
        if conflict_policy == "overwrite":
            db.delete_workflow(existing_wfs[wf_name]["id"])
        else:
            wf_name = _unique_workflow_name(db, wf_name)
            warnings.append(f"Workflow renamed to '{wf_name}' to avoid name clash")

    # --- remap graph ---
    graph_in = wf_meta.get("graph") or []
    graph_out = []
    for i, node in enumerate(graph_in):
        if node.get("type") == "batch_loop":
            inner_ids = []
            for ref in node.get("tasks") or []:
                if ref not in ref_to_task_id:
                    raise ValueError(f"batch_loop references unknown task ref '{ref}'")
                inner_ids.append(ref_to_task_id[ref])
            graph_out.append({
                "id": node.get("id") or f"node_{i}",
                "type": "batch_loop",
                "task_ids": inner_ids,
                "batch_size": node.get("batch_size", 5),
                "source_variable": node.get("source_variable", ""),
                "depends_on": list(node.get("depends_on") or []),
                "execution_level": node.get("execution_level", 1),
                "model_tier": node.get("model_tier", "default"),
            })
        else:
            ref = node.get("task")
            if ref not in ref_to_task_id:
                raise ValueError(f"Graph node references unknown task ref '{ref}'")
            graph_out.append({
                "id": node.get("id") or f"node_{i}",
                "task_id": ref_to_task_id[ref],
                "depends_on": list(node.get("depends_on") or []),
                "execution_level": node.get("execution_level", 1),
                "model_tier": node.get("model_tier", "default"),
            })

    app_id = None
    app_stub = wf_meta.get("app")
    if app_stub and app_stub.get("name"):
        existing_app = db.get_app_by_name(app_stub["name"])
        if existing_app:
            app_id = existing_app["id"]
            warnings.append(f"Linked to existing app '{app_stub['name']}' (credentials stay local)")
        else:
            warnings.append(
                f"App stub '{app_stub['name']}' not registered locally — workflow imported without app_id"
            )

    wf_id = db.create_workflow(
        wf_name,
        graph_out,
        bool(wf_meta.get("requires_human_check")),
        list(wf_meta.get("expected_exports") or []),
        wf_meta.get("export_instructions") or "",
        app_id=app_id,
    )

    return {
        "workflow_id": wf_id,
        "workflow_name": wf_name,
        "warnings": warnings,
        "compatibility": compat,
        "models_imported": len(model_name_to_id),
        "agents_imported": len(agent_name_to_id),
        "tasks_imported": len(ref_to_task_id),
    }


def dumps_package(package: Dict[str, Any]) -> str:
    return json.dumps(package, indent=2, ensure_ascii=False, default=str)


def loads_package(raw: str) -> Dict[str, Any]:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Package root must be an object")
    return data
