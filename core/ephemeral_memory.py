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

try:
    import chromadb  # type: ignore
    from langchain_chroma import Chroma  # type: ignore
except Exception:  # pragma: no cover
    chromadb = None
    Chroma = None

from core.data_manager import DataManager

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
        # If ChromaDB isn't available (common on minimal Windows/Python setups),
        # we fall back to a pure in-memory dict store. This preserves deterministic
        # key-based reads and a simple text search, keeping workflow execution alive.
        self._fallback_records: Dict[str, Dict[str, Any]] = {}
        self.vector_store = None

        if chromadb is not None and Chroma is not None:
            self.chroma_client = chromadb.EphemeralClient()
            self.embedding_function = self._resolve_embedding_function()
            self.vector_store = Chroma(
                client=self.chroma_client,
                collection_name=f"run_memory_{run_id}",
                embedding_function=self.embedding_function,
            )
        else:
            self.chroma_client = None
            self.embedding_function = None

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

        # Lazy import: VectorManager pulls optional LangChain dependencies.
        from core.vector_manager import VectorManager
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
        # Remove any previous record with the same key to avoid duplicates
        self.delete_record(key)

        if self.vector_store is not None:
            metadata = {
                "key": key,
                "agent_role": agent_role,
                "run_id": self.run_id,
                "structured_data_json": json.dumps(structured_data, ensure_ascii=False),
            }
            self.vector_store.add_texts(
                texts=[content_summary],
                metadatas=[metadata],
                ids=[key],
            )
        else:
            self._fallback_records[key] = {
                "key": key,
                "agent_role": agent_role,
                "summary": content_summary,
                "data": structured_data,
            }

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
        if self.vector_store is None:
            return self._fallback_records.get(key)

        try:
            results = self.vector_store.get(ids=[key], include=["metadatas", "documents"])
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
        if self.vector_store is None:
            q = (query or "").lower()
            matches = []
            for rec in self._fallback_records.values():
                if filter_agent and rec.get("agent_role") != filter_agent:
                    continue
                hay = f"{rec.get('key','')} {rec.get('summary','')}".lower()
                if (not q) or (q in hay):
                    matches.append(rec)
            return matches[:k]

        search_filter = {"agent_role": filter_agent} if filter_agent else None
        results = self.vector_store.similarity_search(query, k=k, filter=search_filter)

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
        if self.vector_store is None:
            self._fallback_records.pop(key, None)
            self._keys_index = [item for item in self._keys_index if item["key"] != key]
            return

        try:
            self.vector_store.delete(ids=[key])
            self._keys_index = [item for item in self._keys_index if item["key"] != key]
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

    # ------------------------------------------------------------------
    # Full dump (for Master AI post-processing)
    # ------------------------------------------------------------------

    def dump_all_records(self) -> list:
        """
        Returns a list of ALL records stored in the ephemeral memory.

        Each entry is a dict with keys: ``key``, ``agent_role``, ``summary``,
        ``data``.  This is used at the end of a workflow run to pass the
        complete execution context to the Master AI for export generation.
        """
        records = []
        for item in self._keys_index:
            full_record = self.read_record(item["key"])
            if full_record:
                records.append(full_record)
            else:
                # Fallback: use the index entry itself
                records.append({
                    "key": item["key"],
                    "agent_role": item["agent_role"],
                    "summary": item["summary"],
                    "data": {},
                })
        return records

    def load_from_dump(self, dump_json: str) -> None:
        """
        Re-populates the ephemeral memory from a JSON dump of a previous session.
        This allows new workflows to immediately query and access past context.
        """
        if not dump_json:
            return
        try:
            records = json.loads(dump_json)
            for record in records:
                self.write_record(
                    key=record.get("key", "unknown_key"),
                    agent_role=record.get("agent_role", "Unknown"),
                    content_summary=record.get("summary", ""),
                    structured_data=record.get("data", {})
                )
            logger.info(f"[EphemeralMemory] Re-populated {len(records)} records from JSON dump.")
        except Exception as e:
            logger.error(f"[EphemeralMemory] Failed to load from dump: {e}")

