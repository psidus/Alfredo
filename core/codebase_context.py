"""
core/codebase_context.py

Dual Codebase Context Engine for Alfredo AI OS:
1. ``graphify`` — Deterministic CPU/AST dependency & symbol graph indexer (0 VRAM).
   Produces compact ~1-3K token slices ideal for local 14B models (16 GB VRAM).
2. ``repomix``  — Full/filtered repository packer (uses ``npx repomix`` if installed,
   with a pure-Python fallback inside Docker) + context-router tier advisor
   (local 14B vs long-context Cloud model).
"""

from __future__ import annotations

import ast
import fnmatch
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.context_router import estimate_tokens, recommend_execution_tier
from core.files import PROJECT_ROOT, WORKSPACE_ROOT, resolve as resolve_workspace_path

logger = logging.getLogger(__name__)

_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".venv",
    "venv",
    "env",
    "storage",
    "db",
    ".alfredo_context",
}

_SKIP_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".svg",
    ".pdf", ".docx", ".xlsx", ".xlsm", ".pptx", ".zip", ".tar", ".gz",
    ".7z", ".exe", ".dll", ".so", ".dylib", ".bin", ".pyc", ".pyo",
    ".woff", ".woff2", ".ttf", ".eot", ".mp3", ".mp4", ".wav", ".db", ".sqlite",
}

_SKIP_FILENAMES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
    ".env",
}

_CODE_EXTS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".md", ".yaml", ".yml", ".json", ".sql", ".toml", ".html", ".css",
}

_TS_EXPORT_RE = re.compile(
    r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?(?:function|class|interface|type|const|enum)\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)
_TS_IMPORT_RE = re.compile(r"^\s*import\s+.*?from\s+['\"]([^'\"]+)['\"]", re.MULTILINE)
_MD_HEADING_RE = re.compile(r"^#{1,3}\s+(.+)$", re.MULTILINE)
_SQL_TABLE_RE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


def _slugify(value: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "project").strip()).strip("-_").lower()
    return clean or "project"


def resolve_project_root(target: str = "", db: Any = None) -> Tuple[Path, str]:
    """Resolve ``target`` safely to a directory on disk and return ``(path, slug)``.

    Supported targets:
    - ``"alfredo"`` or ``"self"`` -> Alfredo project root (read-only codebase inspection)
    - Name/display_name of a registered app in My Apps -> ``app_root_path``
    - Workspace-relative directory (default when empty -> ``workspace/``)
    """
    raw = str(target or "").strip()
    low = raw.lower()
    if low in ("alfredo", "self", "root", "alfredo-os"):
        return PROJECT_ROOT.resolve(), "alfredo"

    if raw:
        owns_db = db is None
        if db is None:
            from core.db_manager import DBManager

            db = DBManager()
        try:
            if hasattr(db, "read_all_apps"):
                apps = db.read_all_apps() or []
                for app in apps:
                    name = str(app.get("name") or "").strip().lower()
                    dname = str(app.get("display_name") or "").strip().lower()
                    if low in (name, dname) and app.get("app_root_path"):
                        candidate = Path(str(app["app_root_path"])).resolve()
                        if candidate.exists() and candidate.is_dir():
                            return candidate, _slugify(app.get("name") or raw)
        except Exception as exc:
            logger.debug("App lookup in resolve_project_root skipped: %s", exc)
        finally:
            if owns_db:
                db.close()

    if not raw:
        WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
        return WORKSPACE_ROOT.resolve(), "workspace"

    resolved = resolve_workspace_path(raw, must_exist=True)
    if not resolved.is_dir():
        resolved = resolved.parent
    return resolved.resolve(), _slugify(raw)


def _iter_project_files(
    root: Path,
    include_globs: Optional[List[str]] = None,
    ignore_globs: Optional[List[str]] = None,
    max_files: int = 800,
) -> List[Path]:
    collected: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith(".")
        )
        for fname in sorted(filenames):
            if fname.startswith(".") or fname in _SKIP_FILENAMES:
                continue
            fpath = Path(dirpath) / fname
            if fpath.suffix.lower() in _SKIP_EXTS:
                continue
            try:
                rel = fpath.relative_to(root).as_posix()
            except ValueError:
                continue
            if include_globs and not any(fnmatch.fnmatch(rel, pat) or pat in rel for pat in include_globs):
                continue
            if ignore_globs and any(fnmatch.fnmatch(rel, pat) or pat in rel for pat in ignore_globs):
                continue
            collected.append(fpath)
            if len(collected) >= max_files:
                return collected
    return collected


def _extract_python_symbols(text: str) -> Dict[str, Any]:
    symbols: List[Dict[str, Any]] = []
    imports: List[str] = []
    doc = ""
    try:
        tree = ast.parse(text)
        doc = (ast.get_docstring(tree) or "").strip().splitlines()[0][:140] if ast.get_docstring(tree) else ""
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append({"kind": "func", "name": node.name, "line": node.lineno})
            elif isinstance(node, ast.ClassDef):
                methods = [
                    m.name for m in node.body
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and not m.name.startswith("_")
                ][:8]
                symbols.append({"kind": "class", "name": node.name, "line": node.lineno, "methods": methods})
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
    except SyntaxError:
        pass
    return {"doc": doc, "symbols": symbols[:40], "imports": list(dict.fromkeys(imports))[:25]}


def _extract_generic_symbols(path: Path, text: str) -> Dict[str, Any]:
    ext = path.suffix.lower()
    symbols: List[Dict[str, Any]] = []
    imports: List[str] = []
    if ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        for m in _TS_EXPORT_RE.finditer(text):
            symbols.append({"kind": "symbol", "name": m.group(1)})
        for m in _TS_IMPORT_RE.finditer(text):
            imports.append(m.group(1))
    elif ext == ".md":
        for m in _MD_HEADING_RE.finditer(text):
            symbols.append({"kind": "heading", "name": m.group(1).strip()[:80]})
    elif ext == ".sql":
        for m in _SQL_TABLE_RE.finditer(text):
            symbols.append({"kind": "table", "name": m.group(1)})
    elif ext in (".yaml", ".yml"):
        for line in text.splitlines():
            if line and not line[0].isspace() and not line.startswith("#") and ":" in line:
                symbols.append({"kind": "key", "name": line.split(":", 1)[0].strip()})
    return {"doc": "", "symbols": symbols[:35], "imports": list(dict.fromkeys(imports))[:25]}


def build_graphify_index(root: Path, slug: str) -> Dict[str, Any]:
    """Deterministic CPU/AST indexer producing ``graph.json`` + ``overview.md`` (0 VRAM)."""
    out_dir = (WORKSPACE_ROOT / ".alfredo_context" / _slugify(slug)).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    files = _iter_project_files(root)
    nodes: List[Dict[str, Any]] = []
    for fpath in files:
        ext = fpath.suffix.lower()
        if ext not in _CODE_EXTS and ext != "":
            continue
        try:
            stat = fpath.stat()
            if stat.st_size > 512 * 1024:
                continue
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = fpath.relative_to(root).as_posix()
        extracted = _extract_python_symbols(text) if ext == ".py" else _extract_generic_symbols(fpath, text)
        nodes.append(
            {
                "path": rel,
                "lines": text.count("\n") + 1,
                "doc": extracted["doc"],
                "symbols": extracted["symbols"],
                "imports": extracted["imports"],
            }
        )

    graph = {
        "root": str(root),
        "slug": slug,
        "file_count": len(nodes),
        "nodes": nodes,
    }
    (out_dir / "graph.json").write_text(json.dumps(graph, indent=2, ensure_ascii=False), encoding="utf-8")

    # Compact architecture overview Markdown
    ov_lines = [f"# Codebase Graph Overview: `{slug}` ({len(nodes)} indexed files)", ""]
    for n in nodes[:80]:
        syms = ", ".join(s["name"] for s in (n.get("symbols") or [])[:6])
        sym_txt = f" — `{syms}`" if syms else ""
        doc_txt = f" ({n['doc']})" if n.get("doc") else ""
        ov_lines.append(f"- `{n['path']}` ({n['lines']}L){doc_txt}{sym_txt}")
    if len(nodes) > 80:
        ov_lines.append(f"\n_...and {len(nodes) - 80} more indexed files._")
    (out_dir / "overview.md").write_text("\n".join(ov_lines) + "\n", encoding="utf-8")
    return graph


def query_graphify(
    target: str = "alfredo",
    query: str = "",
    *,
    max_chars: int = 10000,
    rebuild: bool = False,
    db: Any = None,
) -> Dict[str, Any]:
    """Build (if needed) and query the AST dependency graph in compact token slices."""
    root, slug = resolve_project_root(target, db=db)
    cache_file = WORKSPACE_ROOT / ".alfredo_context" / _slugify(slug) / "graph.json"
    graph: Optional[Dict[str, Any]] = None
    if cache_file.is_file() and not rebuild:
        try:
            graph = json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            graph = None
    if not graph:
        graph = build_graphify_index(root, slug)

    nodes = graph.get("nodes") or []
    q = (query or "").strip().lower()
    if not q:
        ov_path = WORKSPACE_ROOT / ".alfredo_context" / _slugify(slug) / "overview.md"
        content = ov_path.read_text(encoding="utf-8", errors="replace") if ov_path.is_file() else ""
        content = content[:max_chars]
        tokens = estimate_tokens(content)
        return {
            "engine": "graphify",
            "target": slug,
            "file_count": len(nodes),
            "tokens": tokens,
            "recommended_tier": recommend_execution_tier(tokens, "auto")["tier"],
            "content": content,
        }

    terms = [w for w in re.split(r"[\s,./:]+", q) if len(w) >= 2] or [q]
    scored: List[Tuple[int, Dict[str, Any]]] = []
    for n in nodes:
        sym_names = " ".join(
            f"{s.get('name', '')} {' '.join(s.get('methods') or [])}"
            for s in (n.get("symbols") or [])
        )
        blob = f"{n.get('path', '')} {n.get('doc', '')} {sym_names} {' '.join(n.get('imports') or [])}".lower()
        score = sum(3 if t in n.get("path", "").lower() else (2 if t in sym_names.lower() else (1 if t in blob else 0)) for t in terms)
        if score > 0:
            scored.append((score, n))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    top_nodes = [n for _, n in scored[:12]]
    if not top_nodes:
        return {
            "engine": "graphify",
            "target": slug,
            "query": query,
            "matches": 0,
            "tokens": 20,
            "recommended_tier": "local",
            "content": f"No symbols or files matching '{query}' in '{slug}' ({len(nodes)} files indexed).",
        }

    chunks: List[str] = [f"# Graphify Query: `{query}` in `{slug}` ({len(top_nodes)} matches)\n"]
    used = len(chunks[0])
    for n in top_nodes:
        syms = ", ".join(
            f"{s['name']}@L{s['line']}" if "line" in s else s["name"]
            for s in (n.get("symbols") or [])[:12]
        )
        imps = ", ".join((n.get("imports") or [])[:10])
        block = [f"## `{n['path']}` ({n['lines']}L)"]
        if n.get("doc"):
            block.append(f"- Doc: {n['doc']}")
        if syms:
            block.append(f"- Symbols: {syms}")
        if imps:
            block.append(f"- Imports: {imps}")
        # Include short excerpt for the top 3 matches
        if len(chunks) <= 3:
            fpath = root / n["path"]
            if fpath.is_file():
                try:
                    snippet = "\n".join(fpath.read_text(encoding="utf-8", errors="replace").splitlines()[:45])
                    block.append(f"```\n{snippet}\n```")
                except OSError:
                    pass
        rendered = "\n".join(block) + "\n"
        if used + len(rendered) > max_chars:
            break
        chunks.append(rendered)
        used += len(rendered)

    content = "\n".join(chunks)
    tokens = estimate_tokens(content)
    return {
        "engine": "graphify",
        "target": slug,
        "query": query,
        "matches": len(top_nodes),
        "tokens": tokens,
        "recommended_tier": recommend_execution_tier(tokens, "auto")["tier"],
        "content": content,
    }


def _compress_python_outline(text: str) -> str:
    """Extract module docstring, imports, class/function signatures & docstrings via AST."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return "\n".join(text.splitlines()[:40])
    lines = text.splitlines()
    out: List[str] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            seg = lines[node.lineno - 1]
            out.append(seg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(lines[node.lineno - 1])
            doc = ast.get_docstring(node)
            if doc:
                out.append(f'    """{doc.splitlines()[0][:120]}"""')
        elif isinstance(node, ast.ClassDef):
            out.append(lines[node.lineno - 1])
            for m in node.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append("    " + lines[m.lineno - 1].strip())
    return "\n".join(out) or "\n".join(lines[:30])


def pack_repomix(
    target: str = "alfredo",
    include: str = "",
    ignore: str = "",
    *,
    compress: bool = False,
    max_chars: int = 80000,
    db: Any = None,
) -> Dict[str, Any]:
    """Pack a codebase (or filtered subset) in Repomix XML/Markdown format."""
    root, slug = resolve_project_root(target, db=db)
    include_globs = [p.strip() for p in (include or "").split(",") if p.strip()]
    ignore_globs = [p.strip() for p in (ignore or "").split(",") if p.strip()]

    # Use native npx repomix only when explicitly enabled AND npx is installed.
    if os.getenv("ALFREDO_USE_NPX_REPOMIX") == "1" and shutil.which("npx"):
        out_path = WORKSPACE_ROOT / ".alfredo_context" / f"{slug}_repomix.xml"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["npx", "-y", "repomix", str(root), "--output", str(out_path), "--style", "xml"]
        if include:
            cmd.extend(["--include", include])
        if ignore:
            cmd.extend(["--ignore", ignore])
        if compress:
            cmd.append("--compress")
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=45)
            content = out_path.read_text(encoding="utf-8", errors="replace")[:max_chars]
            tokens = estimate_tokens(content)
            tier_info = recommend_execution_tier(tokens, "auto")
            return {
                "engine": "repomix-npx",
                "target": slug,
                "compressed": compress,
                "tokens": tokens,
                "recommended_tier": tier_info["tier"],
                "tier_reason": tier_info["reason"],
                "content": content,
            }
        except Exception as exc:
            logger.warning("npx repomix failed, falling back to Python packer: %s", exc)

    files = _iter_project_files(root, include_globs=include_globs, ignore_globs=ignore_globs, max_files=250)
    parts: List[str] = ["<directory_structure>"]
    for f in files:
        parts.append(f"  {f.relative_to(root).as_posix()}")
    parts.append("</directory_structure>\n<files>")

    used = sum(len(p) + 1 for p in parts)
    packed_count = 0
    truncated = False
    for f in files:
        rel = f.relative_to(root).as_posix()
        try:
            if f.stat().st_size > 256 * 1024:
                continue
            body = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if compress and f.suffix.lower() == ".py":
            body = _compress_python_outline(body)
        elif compress:
            body_lines = body.splitlines()
            body = "\n".join(body_lines[:80]) + ("\n... (truncated)" if len(body_lines) > 80 else "")
        block = f'<file path="{rel}">\n{body}\n</file>'
        if used + len(block) > max_chars:
            truncated = True
            break
        parts.append(block)
        used += len(block) + 1
        packed_count += 1
    parts.append("</files>")

    content = "\n".join(parts)
    tokens = estimate_tokens(content)
    tier_info = recommend_execution_tier(tokens, "auto")
    return {
        "engine": "repomix-python",
        "target": slug,
        "files_packed": packed_count,
        "truncated": truncated,
        "compressed": compress,
        "tokens": tokens,
        "recommended_tier": tier_info["tier"],
        "tier_reason": tier_info["reason"],
        "warning": (
            f"Packed context (~{tokens} tok) exceeds 14B local limit ({tier_info['local_limit']} tok); use Cloud model or narrow `include`."
            if tier_info["tier"] == "cloud"
            else ""
        ),
        "content": content,
    }


def get_codebase_context(
    target: str = "alfredo",
    *,
    mode: str = "auto",
    query: str = "",
    include: str = "",
    compress: bool = False,
    db: Any = None,
) -> Dict[str, Any]:
    """Unified entry point selecting Graphify vs Repomix safely for 16 GB VRAM."""
    chosen = (mode or "auto").strip().lower()
    if chosen == "graphify":
        return query_graphify(target=target, query=query, db=db)
    if chosen == "repomix":
        return pack_repomix(target=target, include=include, compress=compress, db=db)

    # Auto mode: default to Graphify (safe 1-3K tokens for local 14B), unless a narrow
    # `include` filter fits comfortably inside 10K tokens as Repomix.
    if include and not query:
        packed = pack_repomix(target=target, include=include, compress=compress, max_chars=40000, db=db)
        if packed.get("tokens", 99999) <= 10000:
            return packed
    return query_graphify(target=target, query=query, db=db)


__all__ = [
    "build_graphify_index",
    "get_codebase_context",
    "pack_repomix",
    "query_graphify",
    "resolve_project_root",
]
