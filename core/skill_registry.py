"""
core/skill_registry.py

Modular SKILL.md registry with Progressive Disclosure, CrewAI zero-duplication
merge, and optional GitHub import for Alfredo AI OS.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import yaml

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILLS_ROOT = Path(os.getenv("ALFREDO_SKILLS_ROOT") or (PROJECT_ROOT / "skills")).resolve()

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_CONCISENESS_SKILL_KEYWORDS = ("karpathy", "concise", "simplicity", "surgical")

MAX_INJECTED_SKILL_CHARS = int(os.getenv("ALFREDO_MAX_SKILL_CHARS", "6000") or "6000")


def _estimate_tokens(text: str) -> int:
    raw = (text or "").strip()
    if not raw:
        return 0
    return max(1, len(raw) // 4)


def _normalize_slug(raw: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", str(raw or "").strip().lower()).strip("-_")
    if not slug or not _SLUG_RE.match(slug):
        raise ValueError(f"Invalid skill slug: {raw!r}")
    return slug


def _split_frontmatter(raw: str) -> Tuple[Dict[str, Any], str]:
    text = (raw or "").lstrip("\ufeff")
    match = re.match(r"^---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|$)(.*)$", text, re.DOTALL)
    if not match:
        return {}, text.strip()
    fm_raw, body = match.group(1), match.group(2).strip()
    meta: Dict[str, Any] = {}
    try:
        loaded = yaml.safe_load(fm_raw)
        if isinstance(loaded, dict):
            meta = loaded
    except Exception:
        for line in fm_raw.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
    return meta, body


def _as_str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("[") and stripped.endswith("]"):
            stripped = stripped[1:-1]
        return [item.strip().strip("'\"") for item in stripped.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(x).strip() for x in value if str(x).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _parse_skill_file(path: Path, slug: str) -> Optional[Dict[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("Could not read skill %s: %s", path, exc)
        return None
    meta, body = _split_frontmatter(raw)
    name = str(meta.get("name") or slug).strip()
    category = str(meta.get("category") or "general").strip().lower()
    description = str(meta.get("description") or "").strip()
    if not description and body:
        for line in body.splitlines():
            clean_line = line.strip().lstrip("#").strip()
            if clean_line:
                description = clean_line[:160]
                break
    scope = str(meta.get("scope") or "task").strip().lower()
    if scope not in ("agent", "task", "both"):
        scope = "task"
    recommended_tools = _as_str_list(meta.get("recommended_tools"))
    min_model_tier = str(meta.get("min_model_tier") or "basic").strip().lower()
    return {
        "slug": slug,
        "name": name,
        "category": category,
        "description": description,
        "scope": scope,
        "recommended_tools": recommended_tools,
        "min_model_tier": min_model_tier,
        "body": body,
        "raw": raw,
        "token_cost": _estimate_tokens(body),
        "path": str(path),
    }


def list_skills(
    *,
    category: Optional[str] = None,
    scope: Optional[str] = None,
    root: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """List installed skills discovered under ``skills/*/SKILL.md``."""
    base = (root or SKILLS_ROOT).resolve()
    if not base.exists() or not base.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for child in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.is_file():
            continue
        parsed = _parse_skill_file(skill_md, child.name)
        if not parsed:
            continue
        if category and parsed["category"] != category.strip().lower():
            continue
        if scope and parsed["scope"] not in (scope.strip().lower(), "both"):
            continue
        out.append(parsed)
    return out


def get_skill(name: str, *, root: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Lookup a single skill by ``name`` or folder slug (case-insensitive)."""
    needle = str(name or "").strip().lower()
    if not needle:
        return None
    for skill in list_skills(root=root):
        if skill["slug"].lower() == needle or skill["name"].lower() == needle:
            return skill
    return None


def skills_index_prompt(max_skills: int = 30, *, root: Optional[Path] = None) -> str:
    """Compact 1-line-per-skill catalogue for Progressive Disclosure (~25 tokens/skill)."""
    skills = list_skills(root=root)[: max(1, int(max_skills))]
    if not skills:
        return "AVAILABLE SKILLS: none installed."
    lines = ["AVAILABLE SKILLS (use `load_skill` or assign by exact name):"]
    for sk in skills:
        tools_txt = f" | tools: {', '.join(sk['recommended_tools'])}" if sk["recommended_tools"] else ""
        lines.append(
            f"- {sk['name']} [{sk['category']}, scope={sk['scope']}, ~{sk['token_cost']} tok]: "
            f"{sk['description']}{tools_txt}"
        )
    return "\n".join(lines)


def merge_skills_and_tools(
    agent_skills: Optional[Iterable[str]],
    task_skills: Optional[Iterable[str]],
    task_tools: Optional[Iterable[str]],
    *,
    tool_profile: str = "",
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Deduplicate inherited agent + task skills, collect recommended tools safely, and render prompt block.

    Guarantees:
    - Agent + task skills are merged with order-preserving deduplication.
    - Recommended tools from active skills are unioned with explicit task tools and
      filtered through ``filter_tool_names_by_profile`` so intermediate nodes never
      receive durable write tools.
    - Signals ``covers_conciseness`` when an active skill already enforces concise/surgical behavior.
    """
    from core.workflow_contracts import filter_tool_names_by_profile

    seen_skill_keys: set[str] = set()
    effective_skills: List[Dict[str, Any]] = []
    missing_skills: List[str] = []

    for raw_name in list(agent_skills or []) + list(task_skills or []):
        key = str(raw_name or "").strip().lower()
        if not key or key in seen_skill_keys:
            continue
        skill = get_skill(key, root=root)
        if not skill:
            missing_skills.append(str(raw_name).strip())
            continue
        canon = skill["name"].lower()
        if canon in seen_skill_keys:
            continue
        seen_skill_keys.add(canon)
        seen_skill_keys.add(skill["slug"].lower())
        effective_skills.append(skill)

    merged_tools: List[str] = []
    seen_tools: set[str] = set()
    for t in list(task_tools or []):
        t_clean = str(t or "").strip()
        if t_clean and t_clean not in seen_tools:
            seen_tools.add(t_clean)
            merged_tools.append(t_clean)
    for sk in effective_skills:
        for t in sk.get("recommended_tools") or []:
            t_clean = str(t or "").strip()
            if t_clean and t_clean not in seen_tools:
                seen_tools.add(t_clean)
                merged_tools.append(t_clean)

    effective_tools = filter_tool_names_by_profile(merged_tools, tool_profile)

    blocks: List[str] = []
    total_chars = 0
    covers_conciseness = False
    for sk in effective_skills:
        slug_and_name = f"{sk['slug']} {sk['name']} {sk['description']}".lower()
        if any(kw in slug_and_name for kw in _CONCISENESS_SKILL_KEYWORDS):
            covers_conciseness = True
        body = sk.get("body") or ""
        if not body:
            continue
        chunk = f"### Skill: {sk['name']} ({sk['category']})\n{body}"
        if total_chars + len(chunk) > MAX_INJECTED_SKILL_CHARS and blocks:
            blocks.append(f"### Skill: {sk['name']}\n{sk['description']}")
            continue
        blocks.append(chunk)
        total_chars += len(chunk)

    rendered_block = ""
    if blocks:
        rendered_block = "\n\n--- ACTIVE SKILLS (FOLLOW STRICTLY) ---\n" + "\n\n".join(blocks)

    return {
        "effective_skills": [sk["name"] for sk in effective_skills],
        "missing_skills": missing_skills,
        "effective_tools": effective_tools,
        "rendered_block": rendered_block,
        "estimated_tokens": _estimate_tokens(rendered_block),
        "covers_conciseness": covers_conciseness,
    }


def save_skill(slug: str, content: str, *, root: Optional[Path] = None) -> Dict[str, Any]:
    """Save or overwrite ``skills/<slug>/SKILL.md`` safely inside ``SKILLS_ROOT``."""
    base = (root or SKILLS_ROOT).resolve()
    clean_slug = _normalize_slug(slug)
    text = (content or "").strip()
    if not text:
        raise ValueError("Skill content cannot be empty.")
    meta, body = _split_frontmatter(text)
    if not meta:
        text = (
            f"---\nname: {clean_slug}\ncategory: general\n"
            f"description: Imported skill {clean_slug}\nscope: task\n---\n\n{body}\n"
        )
    target_dir = (base / clean_slug).resolve()
    target_dir.relative_to(base)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / "SKILL.md"
    target_file.write_text(text.rstrip() + "\n", encoding="utf-8")
    parsed = _parse_skill_file(target_file, clean_slug)
    if not parsed:
        raise ValueError("Saved skill could not be parsed.")
    return parsed


def delete_skill(slug: str, *, root: Optional[Path] = None) -> bool:
    """Remove ``skills/<slug>`` safely inside ``SKILLS_ROOT``."""
    base = (root or SKILLS_ROOT).resolve()
    clean_slug = _normalize_slug(slug)
    target_dir = (base / clean_slug).resolve()
    target_dir.relative_to(base)
    if not target_dir.exists():
        return False
    shutil.rmtree(target_dir)
    return True


def _raw_github_skill_urls(url_or_repo: str) -> List[Tuple[str, str]]:
    """Return list of ``(suggested_slug, raw_url)`` candidates from a GitHub repo or raw URL."""
    import requests

    raw = (url_or_repo or "").strip().rstrip("/")
    if not raw:
        return []

    if raw.endswith(".md") and raw.startswith(("http://", "https://")):
        raw_url = raw
        if "github.com/" in raw and "/blob/" in raw:
            raw_url = raw.replace("https://github.com/", "https://raw.githubusercontent.com/").replace("/blob/", "/")
        parts = [p for p in urlparse(raw_url).path.split("/") if p]
        slug_guess = parts[-2] if len(parts) >= 2 and parts[-1].upper() == "SKILL.MD" else Path(parts[-1]).stem
        return [(re.sub(r"[^a-z0-9_-]+", "-", slug_guess.lower()).strip("-_") or "imported-skill", raw_url)]

    # Normalize owner/repo or https://github.com/owner/repo
    if raw.startswith(("http://", "https://")):
        path_parts = [p for p in urlparse(raw).path.split("/") if p]
        if len(path_parts) < 2:
            raise ValueError("Expected https://github.com/owner/repo or raw SKILL.md URL")
        owner, repo = path_parts[0], path_parts[1]
    elif "/" in raw:
        parts = [p for p in raw.split("/") if p]
        if len(parts) != 2:
            raise ValueError("Expected owner/repo format")
        owner, repo = parts[0], parts[1]
    else:
        raise ValueError("Provide a GitHub URL (repo or raw SKILL.md) or owner/repo")

    repo = repo[:-4] if repo.endswith(".git") else repo
    api_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/HEAD?recursive=1"
    resp = requests.get(api_url, timeout=10, headers={"Accept": "application/vnd.github+json"})
    if resp.status_code == 404:
        api_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/main?recursive=1"
        resp = requests.get(api_url, timeout=10, headers={"Accept": "application/vnd.github+json"})
    resp.raise_for_status()
    tree = (resp.json() or {}).get("tree") or []

    results: List[Tuple[str, str]] = []
    for node in tree:
        if node.get("type") != "blob":
            continue
        path = str(node.get("path") or "")
        if Path(path).name.upper() == "SKILL.MD":
            parent = Path(path).parent.name or repo
            slug = re.sub(r"[^a-z0-9_-]+", "-", parent.lower()).strip("-_") or "imported-skill"
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{path}"
            results.append((slug, raw_url))
    return results[:15]


def import_skills_from_github(url_or_repo: str, *, root: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Fetch one or more ``SKILL.md`` files from a GitHub URL or ``owner/repo``."""
    import requests

    candidates = _raw_github_skill_urls(url_or_repo)
    if not candidates:
        raise ValueError(f"No SKILL.md files found at {url_or_repo!r}")

    saved: List[Dict[str, Any]] = []
    for slug, raw_url in candidates:
        resp = requests.get(raw_url, timeout=10)
        resp.raise_for_status()
        content = resp.text
        if not content.strip():
            continue
        meta, _ = _split_frontmatter(content)
        preferred = str(meta.get("name") or slug)
        clean_slug = re.sub(r"[^a-z0-9_-]+", "-", preferred.lower()).strip("-_") or slug
        saved.append(save_skill(clean_slug, content, root=root))
    return saved


__all__ = [
    "SKILLS_ROOT",
    "delete_skill",
    "get_skill",
    "import_skills_from_github",
    "list_skills",
    "merge_skills_and_tools",
    "save_skill",
    "skills_index_prompt",
]
