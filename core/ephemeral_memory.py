"""
core/ephemeral_memory.py

Manages an ephemeral (in-memory) ChromaDB vector database for inter-agent
communication within a single workflow run. Records are stored as atomic
JSON/dict payloads with vector embeddings, enabling both deterministic
key-based retrieval and semantic similarity search.

The database lives only in RAM and is destroyed when the EphemeralMemoryManager
instance is garbage-collected — no files are left on disk.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

import chromadb
from langchain_chroma import Chroma

from core.data_manager import DataManager
from core.vector_manager import VectorManager

logger = logging.getLogger(__name__)


class EphemeralMemoryManager:
    """
    Ephemeral in-memory ChromaDB store for a single workflow run.

    Acts as the mediator for atomic, structured communication between agents:
    agents *write* their results as keyed records with metadata/embeddings,
    and downstream agents *read* from the store via exact key or semantic query.
    """

    def __init__(self, run_id: int):
        self.run_id = run_id
        self.chroma_client = chromadb.EphemeralClient()
        self.embedding_function = self._resolve_embedding_function()

        self.vector_store = Chroma(
            client=self.chroma_client,
            collection_name=f"run_memory_{run_id}",
            embedding_function=self.embedding_function,
        )
        # Local index of all keys written during this session
        self._keys_index: List[Dict[str, Any]] = []
        logger.info(
            f"[EphemeralMemory] Initialized in-memory store for Run ID {run_id}"
        )

    # ------------------------------------------------------------------
    # Embedding resolution — delegates to VectorManager
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_embedding_function() -> Any:
        """
        Determines the embedding provider/model from the environment and
        delegates instantiation to VectorManager._get_embedding_function(),
        ensuring the exact same embedding logic used for persistent vector
        databases is reused here.
        """
        DataManager.load_env()

        vm = VectorManager()  # lightweight — only sets up a directory path

        # 1. Gemini / Google (Alfredo's default provider)
        gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if gemini_key:
            return vm._get_embedding_function("gemini", "models/gemini-embedding-001")

        # 2. OpenAI
        openai_key = os.getenv("OPENAI_API_KEY")
        if openai_key:
            return vm._get_embedding_function("openai", "text-embedding-3-small")

        # 3. Ollama (local)
        ollama_model = os.getenv("OLLAMA_EMBEDDING_MODEL")
        if ollama_model:
            return vm._get_embedding_function("ollama", ollama_model)

        raise ValueError(
            "No embedding API key found in .env (GEMINI_API_KEY, OPENAI_API_KEY, "
            "or OLLAMA_EMBEDDING_MODEL). Cannot initialise ephemeral memory."
        )

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def write_record(
        self,
        key: str,
        content_summary: str,
        structured_data: Dict[str, Any],
        agent_role: str,
    ) -> None:
        """
        Writes an atomic record into the in-memory vector store.

        Args:
            key:              Unique identifier (e.g. ``task_3``, ``schema_draft``).
            content_summary:  Human-readable summary; this text is embedded.
            structured_data:  Arbitrary JSON-serialisable dict with the payload.
            agent_role:       The role of the agent that produced this record.
        """
        metadata = {
            "key": key,
            "agent_role": agent_role,
            "run_id": self.run_id,
            "structured_data_json": json.dumps(structured_data, ensure_ascii=False),
        }

        # Remove any previous record with the same key to avoid duplicates
        self.delete_record(key)

        self.vector_store.add_texts(
            texts=[content_summary],
            metadatas=[metadata],
            ids=[key],
        )

        # Update the local key index
        self._keys_index = [item for item in self._keys_index if item["key"] != key]
        self._keys_index.append(
            {"key": key, "agent_role": agent_role, "summary": content_summary}
        )
        logger.info(
            f"[EphemeralMemory] Written record key='{key}' by agent='{agent_role}'"
        )

    # ------------------------------------------------------------------
    # Read (deterministic by key)
    # ------------------------------------------------------------------

    def read_record(self, key: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves a record by its exact key.

        Returns ``None`` if the key does not exist.
        """
        try:
            results = self.vector_store.get(
                ids=[key], include=["metadatas", "documents"]
            )
        except Exception:
            return None

        if results and results.get("metadatas") and results["metadatas"]:
            metadata = results["metadatas"][0]
            try:
                data = json.loads(metadata.get("structured_data_json", "{}"))
            except Exception:
                data = {}
            return {
                "key": key,
                "agent_role": metadata.get("agent_role"),
                "summary": results["documents"][0] if results.get("documents") else "",
                "data": data,
            }
        return None

    # ------------------------------------------------------------------
    # Search (semantic)
    # ------------------------------------------------------------------

    def search_records(
        self, query: str, k: int = 3, filter_agent: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Performs a semantic similarity search across all stored records.

        Args:
            query:        Natural-language search query.
            k:            Maximum number of results.
            filter_agent: If set, only return records written by this agent role.
        """
        search_filter = {"agent_role": filter_agent} if filter_agent else None
        results = self.vector_store.similarity_search(
            query, k=k, filter=search_filter
        )

        retrieved = []
        for doc in results:
            meta = doc.metadata
            try:
                data = json.loads(meta.get("structured_data_json", "{}"))
            except Exception:
                data = {}
            retrieved.append(
                {
                    "key": meta.get("key"),
                    "agent_role": meta.get("agent_role"),
                    "summary": doc.page_content,
                    "data": data,
                }
            )
        return retrieved

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete_record(self, key: str) -> None:
        """Removes a record by key (no-op if it doesn't exist)."""
        try:
            self.vector_store.delete(ids=[key])
            self._keys_index = [
                item for item in self._keys_index if item["key"] != key
            ]
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Index table (injected into task prompts)
    # ------------------------------------------------------------------

    def get_memory_index_table(self) -> str:
        """
        Returns a compact Markdown table summarising every record stored
        so far.  This is injected into the next agent's prompt so it knows
        *which* keys to request via ``read_atomic_memory``.
        """
        if not self._keys_index:
            return "*No records in ephemeral memory yet.*"

        table = "| Key | Written by (Agent) | Summary |\n"
        table += "| :--- | :--- | :--- |\n"
        for item in self._keys_index:
            # Truncate long summaries to keep the index compact
            summary = item["summary"]
            if len(summary) > 120:
                summary = summary[:117] + "..."
            table += f"| `{item['key']}` | **{item['agent_role']}** | {summary} |\n"
        return table
