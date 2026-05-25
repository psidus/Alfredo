"""
tools/ephemeral_memory_tool.py

CrewAI tools that allow agents to read from and write to the ephemeral
in-memory vector store during a workflow run.  These are injected at runtime
by the crew builder — agents do NOT need to be pre-configured with them.
"""

import json
import logging
from typing import Any, Optional, Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Input schemas
# ---------------------------------------------------------------------------

class ReadAtomicMemoryInput(BaseModel):
    """Input schema for reading from the ephemeral memory."""

    query: Optional[str] = Field(
        None,
        description=(
            "A natural-language question or concept to search for semantically "
            "across all stored records.  Use this when you don't know the exact key."
        ),
    )
    key: Optional[str] = Field(
        None,
        description=(
            "The exact key of the record to retrieve (e.g. 'task_1', 'database_schema'). "
            "Highly recommended when the Memory Index Table shows the key you need."
        ),
    )
    filter_agent: Optional[str] = Field(
        None,
        description="Optional: filter results to records written by a specific agent role.",
    )


class WriteAtomicMemoryInput(BaseModel):
    """Input schema for writing to the ephemeral memory."""

    key: str = Field(
        ...,
        description=(
            "Unique reference key for this record (e.g. 'implementation_plan', "
            "'validated_schema').  Downstream agents will use this key to retrieve it."
        ),
    )
    content_summary: str = Field(
        ...,
        description=(
            "A clear, concise textual summary of the data being saved.  "
            "This text is vectorised for semantic search."
        ),
    )
    structured_data: str = Field(
        ...,
        description=(
            "A valid JSON string containing dictionaries, lists, or structured "
            "parameters to persist.  Example: '{\"columns\": [\"id\", \"name\"], "
            "\"row_count\": 42}'"
        ),
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

class ReadAtomicMemoryTool(BaseTool):
    """
    Reads structured atomic records from the ephemeral in-memory vector store
    of the current workflow session.  Supports both deterministic key-based
    retrieval and semantic similarity search.
    """

    name: str = "read_atomic_memory"
    description: str = (
        "Read information from the ephemeral in-memory database of the current "
        "workflow session.  Use 'key' for deterministic retrieval when the Memory "
        "Index Table shows the key you need, or 'query' for semantic search."
    )
    args_schema: Type[BaseModel] = ReadAtomicMemoryInput

    # Injected at runtime by the crew builder
    memory_manager: Any = None

    def _run(
        self,
        query: Optional[str] = None,
        key: Optional[str] = None,
        filter_agent: Optional[str] = None,
    ) -> str:
        if not self.memory_manager:
            return "Error: EphemeralMemoryManager not initialised."

        # --- Deterministic read by key ---
        if key:
            record = self.memory_manager.read_record(key)
            if record:
                return (
                    f"--- ATOMIC RECORD (key: '{key}') ---\n"
                    f"Author: {record['agent_role']}\n"
                    f"Summary: {record['summary']}\n"
                    f"Structured Data:\n{json.dumps(record['data'], indent=2, ensure_ascii=False)}"
                )
            return f"No record found for key '{key}'."

        # --- Semantic search ---
        if query:
            results = self.memory_manager.search_records(
                query, filter_agent=filter_agent
            )
            if not results:
                return "No matching records found via semantic search."

            output_parts = ["--- SEMANTIC SEARCH RESULTS ---"]
            for idx, r in enumerate(results):
                output_parts.append(
                    f"Result {idx + 1} [key: '{r['key']}', author: {r['agent_role']}]:\n"
                    f"  Summary: {r['summary']}\n"
                    f"  Data: {json.dumps(r['data'], ensure_ascii=False)}\n"
                )
            return "\n".join(output_parts)

        return "Error: Provide at least 'key' or 'query'."


class WriteAtomicMemoryTool(BaseTool):
    """
    Writes an atomic structured record into the ephemeral in-memory database
    of the current workflow session.  Use this to pass your output, technical
    data, or configuration to downstream agents.
    """

    name: str = "write_atomic_memory"
    description: str = (
        "Write a structured atomic record into the ephemeral in-memory database "
        "of the current session.  Use this to store your output so that downstream "
        "agents can retrieve it via 'read_atomic_memory'."
    )
    args_schema: Type[BaseModel] = WriteAtomicMemoryInput

    # Injected at runtime by the crew builder
    memory_manager: Any = None

    def _run(self, key: str, content_summary: str, structured_data: str) -> str:
        if not self.memory_manager:
            return "Error: EphemeralMemoryManager not initialised."

        # Parse the JSON string produced by the LLM
        try:
            data_dict = json.loads(structured_data)
        except Exception as exc:
            # Graceful fallback: wrap raw text in a dict
            data_dict = {"raw_content": structured_data, "parse_note": str(exc)}
            logger.warning(
                f"[WriteAtomicMemory] Could not parse structured_data as JSON "
                f"for key '{key}': {exc}.  Wrapping as raw_content."
            )

        # Resolve the calling agent's role for metadata
        agent_role = "Unknown Agent"
        # CrewAI injects the agent reference on the tool instance
        if hasattr(self, "agent") and self.agent:
            agent_role = getattr(self.agent, "role", agent_role)

        self.memory_manager.write_record(
            key=key,
            content_summary=content_summary,
            structured_data=data_dict,
            agent_role=agent_role,
        )
        return f"Success: Record '{key}' stored in ephemeral memory."
