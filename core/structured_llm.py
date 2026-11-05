"""
core/structured_llm.py

Instructor-Style Pydantic Validation & Self-Correction Loop for Alfredo AI OS.
Integrates `instructor-ai/instructor` (https://github.com/567-labs/instructor)
and provides an automatic Pydantic schema validation & self-correction retry loop
for local LLMs (Ollama / LM Studio / vLLM).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional, Type, TypeVar
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def validate_or_repair_json(
    raw_output: str,
    sanitize_fn: Callable[[str], str],
    llm_call_fn: Callable[..., str],
    model: str,
    original_messages: List[Dict[str, str]],
    pydantic_model: Optional[Type[T]] = None,
    max_repairs: int = 2,
) -> Dict[str, Any]:
    """
    Parses `raw_output` as JSON (and optionally validates against `pydantic_model`).
    If JSON parsing or Pydantic validation fails, automatically feeds the exact
    error message back to the LLM (Instructor pattern) up to `max_repairs` times.
    """
    current_raw = raw_output
    last_error: Optional[Exception] = None

    for attempt in range(max_repairs + 1):
        try:
            cleaned = sanitize_fn(current_raw)
            parsed = json.loads(cleaned)
            if pydantic_model is not None:
                validated = pydantic_model.model_validate(parsed)
                return validated.model_dump()
            if isinstance(parsed, dict):
                return parsed
            return {"data": parsed}
        except (json.JSONDecodeError, ValidationError, ValueError) as err:
            last_error = err
            if attempt >= max_repairs:
                break

            schema_hint = ""
            if pydantic_model is not None:
                try:
                    schema_hint = (
                        "\nExpected JSON Schema:\n"
                        + json.dumps(pydantic_model.model_json_schema(), indent=2)
                    )
                except Exception:
                    pass

            logger.warning(
                f"[StructuredLLM] Validation failed (attempt {attempt + 1}/{max_repairs + 1}): {err}. "
                "Triggering Instructor-style self-correction..."
            )
            repair_messages = list(original_messages) + [
                {"role": "assistant", "content": str(current_raw)[:3000]},
                {
                    "role": "user",
                    "content": (
                        "Your previous JSON output failed validation with the following error:\n"
                        f"{err}\n"
                        f"{schema_hint}\n\n"
                        "Return ONLY the corrected, valid JSON object matching the required structure. "
                        "Do not include markdown fences or explanations."
                    ),
                },
            ]
            current_raw = llm_call_fn(
                model=model,
                messages=repair_messages,
                temperature=0.0,
                force_json_mode=True,
            )

    raise ValueError(f"Structured JSON validation failed after {max_repairs + 1} attempts: {last_error}")
