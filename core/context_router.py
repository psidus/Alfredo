"""
core/context_router.py

Pure helpers for hybrid Local (16 GB VRAM) vs Cloud (long-context) routing.
Estimates prompt/artifact token budgets and selects an available cloud fallback
model from the Database / API Vault when context exceeds the safe local limit.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

LOCAL_SAFE_CONTEXT_TOKENS = int(os.getenv("ALFREDO_LOCAL_SAFE_CTX_TOKENS", "12000") or "12000")

# Ordered preference for affordable, long-context cloud models when routing large contexts.
_CLOUD_PRIORITY = (
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.5-pro",
    "deepseek-chat",
    "claude-3-5-sonnet",
    "gpt-4o-mini",
    "gpt-4o",
)


def estimate_tokens(text: Optional[str]) -> int:
    """Fast ~4 chars/token heuristic for sizing prompts and codebase packs."""
    raw = text or ""
    if not raw.strip():
        return 0
    return max(1, len(raw) // 4)


def recommend_execution_tier(
    estimated_tokens: int,
    preferred_mode: str = "auto",
) -> Dict[str, Any]:
    """Recommend whether a workload should run on the local 14B GPU or Cloud API.

    Args:
        estimated_tokens: Estimated input/context size in tokens.
        preferred_mode: ``"auto"``, ``"local"``, or ``"cloud"``.
    """
    tokens = max(0, int(estimated_tokens or 0))
    mode = (preferred_mode or "auto").strip().lower()
    limit = LOCAL_SAFE_CONTEXT_TOKENS

    if mode == "cloud":
        return {
            "tier": "cloud",
            "estimated_tokens": tokens,
            "local_limit": limit,
            "reason": "Cloud mode explicitly selected.",
        }
    if mode == "local":
        reason = (
            f"Local 14B forced despite large context ({tokens} > {limit} tok)."
            if tokens > limit
            else f"Fits comfortably inside local safe budget ({tokens} <= {limit} tok)."
        )
        return {
            "tier": "local",
            "estimated_tokens": tokens,
            "local_limit": limit,
            "reason": reason,
        }

    if tokens > limit:
        return {
            "tier": "cloud",
            "estimated_tokens": tokens,
            "local_limit": limit,
            "reason": f"Context ({tokens} tok) exceeds local 16 GB VRAM sweet spot ({limit} tok).",
        }
    return {
        "tier": "local",
        "estimated_tokens": tokens,
        "local_limit": limit,
        "reason": f"Context ({tokens} tok) fits within local 16 GB VRAM budget ({limit} tok).",
    }


def _has_env_secret(env_var_name: Optional[str]) -> bool:
    key = (env_var_name or "").strip()
    if not key:
        return False
    return bool((os.getenv(key) or "").strip())


def find_cloud_long_context_model(db: Any = None) -> Optional[Dict[str, Any]]:
    """Return the best configured non-local model whose API key is set in env."""
    owns_db = db is None
    if db is None:
        from core.db_manager import DBManager

        db = DBManager()
    try:
        try:
            models: List[Dict[str, Any]] = db.read_all_models() or []
        except Exception as exc:
            logger.debug("Could not query models table for cloud router: %s", exc)
            return None

        candidates: List[Dict[str, Any]] = []
        for m in models:
            provider = str(m.get("provider") or "").strip().lower()
            is_local = bool(m.get("is_local")) or provider in ("ollama", "lmstudio", "lm_studio", "local")
            if is_local:
                continue
            env_var = m.get("env_var_name")
            # Accept default env names if env_var_name was omitted
            if not env_var:
                if provider in ("google", "gemini"):
                    env_var = "GEMINI_API_KEY"
                elif provider == "openai":
                    env_var = "OPENAI_API_KEY"
                elif provider == "anthropic":
                    env_var = "ANTHROPIC_API_KEY"
                elif provider == "deepseek":
                    env_var = "DEEPSEEK_API_KEY"
            if not _has_env_secret(env_var):
                continue
            candidates.append(m)

        if not candidates:
            return None

        def _rank(rec: Dict[str, Any]) -> tuple[int, int]:
            name = str(rec.get("model_name") or "").lower()
            for idx, pattern in enumerate(_CLOUD_PRIORITY):
                if pattern in name:
                    return (0, idx)
            return (1, int(rec.get("id") or 999))

        candidates.sort(key=_rank)
        return candidates[0]
    finally:
        if owns_db:
            db.close()


__all__ = [
    "LOCAL_SAFE_CONTEXT_TOKENS",
    "estimate_tokens",
    "find_cloud_long_context_model",
    "recommend_execution_tier",
]
