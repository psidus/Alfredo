"""
Model Capability Registry for Alfredo.

Manages a structured catalog of model capabilities, enabling:
- Tier-based classification (basic → expert)
- Task-model compatibility checks
- Intelligent fallback recommendations
- Auto-population from models_map.yaml

Orchestration model:
- Master AI consults the registry before delegating work to LLM calls.
- Crew Builder validates task→model assignments at build time.
- Bot layer surfaces capability warnings to the user in chat.
"""

from __future__ import annotations

import json
import logging
import os
from enum import IntEnum
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Optional: yaml may not be installed in minimal environments
try:
    import yaml
except ImportError:
    yaml = None


# ---------------------------------------------------------------------------
# Capability Tiers
# ---------------------------------------------------------------------------

class CapabilityTier(IntEnum):
    """Model power levels.  Higher = more capable."""
    BASIC = 1       # 1-7B params: simple Q&A, formatting
    STANDARD = 2    # 8-14B params: analysis, summarization
    ADVANCED = 3    # 14B+ / fast cloud: planning, structured JSON
    EXPERT = 4      # Top cloud models: complex reasoning, DAG design


# String → enum mapping (used when reading YAML / DB)
TIER_MAP: Dict[str, CapabilityTier] = {
    "basic": CapabilityTier.BASIC,
    "standard": CapabilityTier.STANDARD,
    "advanced": CapabilityTier.ADVANCED,
    "expert": CapabilityTier.EXPERT,
    # Backward compatibility with legacy tier names
    "simple": CapabilityTier.BASIC,
    "default": CapabilityTier.STANDARD,
}


# ---------------------------------------------------------------------------
# Critical-Function → Minimum-Tier mapping  (Master AI internal calls)
# ---------------------------------------------------------------------------

CRITICAL_FUNCTIONS: Dict[str, CapabilityTier] = {
    # Functions requiring structured JSON output + complex reasoning
    "review_workflow_plan":          CapabilityTier.ADVANCED,
    "decompose_task_if_complex":     CapabilityTier.ADVANCED,
    "optimize_workflow_coherence":   CapabilityTier.ADVANCED,
    "chat_plan":                     CapabilityTier.ADVANCED,
    "generate_export_files":         CapabilityTier.ADVANCED,

    # Functions that work with simpler prompts
    "optimize_agent_fields":         CapabilityTier.STANDARD,
    "optimize_task_fields":          CapabilityTier.STANDARD,
    "refine_output":                 CapabilityTier.STANDARD,
    "format_validation_request":     CapabilityTier.STANDARD,
    "process_validation_feedback":   CapabilityTier.STANDARD,
    "evaluate_intent":               CapabilityTier.STANDARD,

    # Lightweight functions
    "summarize_global_context":      CapabilityTier.BASIC,
}


# ---------------------------------------------------------------------------
# Task-type criticality  (CrewAI agent / worker tasks)
# ---------------------------------------------------------------------------

TASK_TYPE_REQUIREMENTS: Dict[str, CapabilityTier] = {
    "dag_planning":          CapabilityTier.EXPERT,
    "complex_reasoning":     CapabilityTier.ADVANCED,
    "code_generation":       CapabilityTier.ADVANCED,
    "multi_step_planning":   CapabilityTier.ADVANCED,
    "structured_json":       CapabilityTier.STANDARD,
    "analysis":              CapabilityTier.STANDARD,
    "summarization":         CapabilityTier.BASIC,
    "simple_qa":             CapabilityTier.BASIC,
    "formatting":            CapabilityTier.BASIC,
    "keyword_extraction":    CapabilityTier.BASIC,
}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class ModelCapabilityRegistry:
    """Central registry for model capabilities and compatibility checks.

    Loads capability metadata from ``config/models_map.yaml`` (key
    ``capability_catalog``) and exposes lookup / compatibility / fallback
    helpers that the Master AI, Crew Builder, and Bot can consume.
    """

    def __init__(self, config_path: str = None):
        self._catalog: Dict[str, Dict[str, Any]] = {}
        self._config_path = config_path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config",
            "models_map.yaml",
        )
        self._load_catalog()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load_catalog(self) -> None:
        """Load capability catalog from *models_map.yaml*."""
        if yaml is None:
            logger.warning(
                "PyYAML not installed — capability catalog will rely on "
                "heuristic tier inference only."
            )
            return
        try:
            with open(self._config_path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            catalog = data.get("capability_catalog") or {}
            for model_name, meta in catalog.items():
                if not isinstance(meta, dict):
                    continue
                self._catalog[self._norm(model_name)] = {
                    "tier": TIER_MAP.get(
                        str(meta.get("tier", "standard")).lower(),
                        CapabilityTier.STANDARD,
                    ),
                    "max_context": int(meta.get("max_context", 0)),
                    "strengths": set(meta.get("strengths") or []),
                    "weaknesses": set(meta.get("weaknesses") or []),
                }
            logger.info(
                f"ModelCapabilityRegistry: loaded {len(self._catalog)} models "
                f"from {self._config_path}"
            )
        except FileNotFoundError:
            logger.warning(
                f"Capability catalog file not found: {self._config_path}"
            )
        except Exception as exc:
            logger.warning(f"Could not load capability catalog: {exc}")

    @staticmethod
    def _norm(name: str) -> str:
        """Normalise a model name for dict lookup."""
        return name.strip().lower()

    # ------------------------------------------------------------------
    # Tier lookup
    # ------------------------------------------------------------------

    def get_tier(self, model_name: str) -> CapabilityTier:
        """Return the capability tier for *model_name*.

        Falls back to a heuristic when the model is not in the catalog.
        """
        key = self._norm(model_name)
        if key in self._catalog:
            return self._catalog[key]["tier"]
        # Try partial match (e.g. "ollama/qwen3:14b" → "qwen3:14b")
        for suffix_len in range(1, len(key)):
            partial = key[-suffix_len:]
            if partial in self._catalog:
                return self._catalog[partial]["tier"]
        return self._infer_tier(key)

    def _infer_tier(self, name: str) -> CapabilityTier:
        """Heuristic tier inference for models not in the catalog."""
        n = name.lower()
        # Expert indicators
        if any(k in n for k in (
            "gpt-4o",  "gpt-4-turbo",
            "claude-3-5", "claude-3-opus",
            "gemini-2.5-pro", "gemini-3.1-pro",
        )):
            return CapabilityTier.EXPERT
        # Advanced indicators
        if any(k in n for k in (
            "70b", "65b",
            "gemini-2.5-flash", "gemini-3.5",
            "mistral-large", "codestral",
            "pixtral-large",
        )):
            return CapabilityTier.ADVANCED
        # Basic indicators
        if any(k in n for k in (
            "3b", "1b", "mini", "tiny",
            "flash-lite", "flash-8b",
        )):
            return CapabilityTier.BASIC
        return CapabilityTier.STANDARD

    # ------------------------------------------------------------------
    # Strengths / weaknesses
    # ------------------------------------------------------------------

    def get_strengths(self, model_name: str) -> Set[str]:
        return self._catalog.get(self._norm(model_name), {}).get(
            "strengths", set()
        )

    def get_weaknesses(self, model_name: str) -> Set[str]:
        return self._catalog.get(self._norm(model_name), {}).get(
            "weaknesses", set()
        )

    def get_max_context(self, model_name: str) -> int:
        return self._catalog.get(self._norm(model_name), {}).get(
            "max_context", 0
        )

    # ------------------------------------------------------------------
    # Compatibility check
    # ------------------------------------------------------------------

    def check_compatibility(
        self,
        model_name: str,
        required_tier: CapabilityTier,
        required_strengths: Set[str] | None = None,
    ) -> Tuple[bool, str]:
        """Check whether *model_name* meets *required_tier* (and optional
        strength requirements).

        Returns ``(is_compatible, reason_if_not)``.
        """
        tier = self.get_tier(model_name)
        if tier < required_tier:
            return False, (
                f"Model '{model_name}' is tier {tier.name} "
                f"but task requires {required_tier.name}"
            )
        if required_strengths:
            weaknesses = self.get_weaknesses(model_name)
            conflicts = required_strengths & weaknesses
            if conflicts:
                return False, (
                    f"Model '{model_name}' has weaknesses in: "
                    f"{', '.join(sorted(conflicts))}"
                )
        return True, ""

    # ------------------------------------------------------------------
    # Fallback recommendation
    # ------------------------------------------------------------------

    def recommend_fallback(
        self,
        current_model: str,
        required_tier: CapabilityTier,
        available_models: List[Dict[str, Any]],
        prefer_local: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Recommend the cheapest model from *available_models* that meets
        *required_tier*.

        When *prefer_local* is ``True``, local models are ranked higher than
        cloud models of the same tier.
        """
        candidates: list[tuple[int, bool, Dict[str, Any]]] = []
        for m in available_models:
            name = m.get("model_name", "")
            tier = self.get_tier(name)
            if tier >= required_tier and self._norm(name) != self._norm(current_model):
                is_local = bool(m.get("is_local")) or str(
                    m.get("provider", "")
                ).lower() in ("ollama", "lmstudio", "lm_studio", "bionic")
                candidates.append((int(tier), not is_local if prefer_local else is_local, m))

        if not candidates:
            return None

        # Sort: lowest sufficient tier first, then preference flag
        candidates.sort(key=lambda x: (x[0], x[1]))
        return candidates[0][2]

    # ------------------------------------------------------------------
    # Task-criticality classifier
    # ------------------------------------------------------------------

    def classify_task_criticality(
        self,
        task_description: str,
        task_tools: List[str] | None = None,
    ) -> CapabilityTier:
        """Analyse a task description (and its tools) to determine the
        minimum model tier required for reliable execution.
        """
        desc = (task_description or "").lower()
        tools = [str(t).lower() for t in (task_tools or [])]
        max_tier = CapabilityTier.BASIC

        # --- Tool-based signals ---
        tool_tier_map = {
            "search_scientific_literature": CapabilityTier.STANDARD,
            "execute_shell_command":        CapabilityTier.ADVANCED,
            "write_python_file":            CapabilityTier.ADVANCED,
            "write_file":                   CapabilityTier.STANDARD,
        }
        for tool_name, tier in tool_tier_map.items():
            if tool_name in tools:
                max_tier = max(max_tier, tier)

        # --- Keyword-based signals (ordered by tier) ---
        keyword_tiers: Dict[CapabilityTier, List[str]] = {
            CapabilityTier.EXPERT: [
                "design and implement",
                "complex algorithm",
                "multi-step reasoning",
                "architect",
                "full system design",
            ],
            CapabilityTier.ADVANCED: [
                "analyze and synthesize",
                "create a plan",
                "structured json",
                "write code",
                "decompose",
                "mathematical model",
                "blueprint",
                "design a pipeline",
                "build a dag",
            ],
            CapabilityTier.STANDARD: [
                "summarize",
                "analyze",
                "compare",
                "research",
                "review",
                "evaluate",
                "extract",
                "classify",
            ],
        }

        for tier, keywords in keyword_tiers.items():
            if any(kw in desc for kw in keywords):
                max_tier = max(max_tier, tier)

        return max_tier

    # ------------------------------------------------------------------
    # Master AI function guard
    # ------------------------------------------------------------------

    def check_function_capability(
        self,
        model_name: str,
        function_name: str,
    ) -> Tuple[bool, str]:
        """Check if *model_name* can reliably execute the Master AI
        function *function_name*.

        Returns ``(can_proceed, warning_message)``.
        """
        required = CRITICAL_FUNCTIONS.get(function_name)
        if required is None:
            return True, ""
        return self.check_compatibility(model_name, required)

    # ------------------------------------------------------------------
    # Utility: tier label
    # ------------------------------------------------------------------

    @staticmethod
    def tier_label(tier: CapabilityTier) -> str:
        """Human-readable label for a tier."""
        labels = {
            CapabilityTier.BASIC:    "🟢 Basic",
            CapabilityTier.STANDARD: "🟡 Standard",
            CapabilityTier.ADVANCED: "🟠 Advanced",
            CapabilityTier.EXPERT:   "🔴 Expert",
        }
        return labels.get(tier, str(tier.name))
