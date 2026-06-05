"""
core/learning_memory.py

Persistent ChromaDB-backed learning memory for storing and retrieving
user feedback across workflow executions. Feedback is embedded using
the same embedding function as the ephemeral memory, enabling semantic
similarity search: when a new task is about to execute, we query this
database for feedback on *similar* past tasks.

Data is stored on disk at ``storage/learning_db`` and survives restarts.
"""

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import chromadb
from langchain_chroma import Chroma

from core.data_manager import DataManager

logger = logging.getLogger(__name__)

# Singleton instance — created on first import
_instance: Optional["LearningMemoryManager"] = None


def get_learning_memory() -> "LearningMemoryManager":
    """Return the module-level singleton, creating it lazily."""
    global _instance
    if _instance is None:
        _instance = LearningMemoryManager()
    return _instance


class LearningMemoryManager:
    """
    Persistent vector store for user feedback and corrective learning.

    Each record represents a single feedback entry tied to one or more
    task descriptions.  At query time, the caller passes the *new* task
    description and receives the most relevant past feedback (if any).
    """

    STORAGE_DIR = os.path.join("storage", "learning_db")
    COLLECTION_NAME = "learning_feedback"

    def __init__(self, storage_dir: str = None):
        self.storage_dir = storage_dir or self.STORAGE_DIR
        os.makedirs(self.storage_dir, exist_ok=True)

        try:
            self.embedding_function = self._resolve_embedding_function()
            self.chroma_client = chromadb.PersistentClient(path=self.storage_dir)
            self.vector_store = Chroma(
                client=self.chroma_client,
                collection_name=self.COLLECTION_NAME,
                embedding_function=self.embedding_function,
            )
            logger.info(
                f"[LearningMemory] Initialized persistent store at {self.storage_dir}"
            )
        except Exception as e:
            logger.error(f"[LearningMemory] Failed to initialize: {e}")
            self.chroma_client = None
            self.vector_store = None
            self.embedding_function = None

    # ------------------------------------------------------------------
    # Embedding — reuses the same logic as EphemeralMemoryManager
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_embedding_function() -> Any:
        DataManager.load_env()
        from core.vector_manager import VectorManager
        vm = VectorManager()

        gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if gemini_key:
            return vm._get_embedding_function("gemini", "models/gemini-embedding-001")

        openai_key = os.getenv("OPENAI_API_KEY")
        if openai_key:
            return vm._get_embedding_function("openai", "text-embedding-3-small")

        ollama_model = os.getenv("OLLAMA_EMBEDDING_MODEL")
        if ollama_model:
            return vm._get_embedding_function("ollama", ollama_model)

        raise ValueError(
            "No embedding API key found in .env. Cannot initialise learning memory."
        )

    # ------------------------------------------------------------------
    # Write — save user feedback
    # ------------------------------------------------------------------

    def save_feedback(
        self,
        workflow_name: str,
        task_descriptions: List[str],
        agent_roles: List[str],
        feedback_text: str,
    ) -> bool:
        """
        Save user feedback linked to the tasks that were just executed.

        The feedback is stored as a single document whose text is a
        combination of the task descriptions + the feedback itself.
        This ensures that semantic search finds it when a *similar*
        task is about to run in a future workflow.

        Returns True on success, False on failure.
        """
        if not self.vector_store:
            logger.warning("[LearningMemory] Store not available — skipping save.")
            return False

        try:
            # Build a rich text document for embedding
            tasks_summary = "\n".join(
                f"- [{role}] {desc}" for role, desc in zip(agent_roles, task_descriptions)
            )
            document_text = (
                f"WORKFLOW: {workflow_name}\n"
                f"TASKS:\n{tasks_summary}\n\n"
                f"USER FEEDBACK:\n{feedback_text}"
            )

            metadata = {
                "workflow_name": workflow_name,
                "feedback": feedback_text,
                "agent_roles": json.dumps(agent_roles, ensure_ascii=False),
                "timestamp": datetime.now().isoformat(),
            }

            doc_id = f"feedback_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"

            self.vector_store.add_texts(
                texts=[document_text],
                metadatas=[metadata],
                ids=[doc_id],
            )

            logger.info(
                f"[LearningMemory] Saved feedback '{feedback_text[:60]}...' "
                f"for workflow '{workflow_name}' ({len(task_descriptions)} tasks)."
            )
            return True

        except Exception as e:
            logger.error(f"[LearningMemory] Failed to save feedback: {e}")
            return False

    # ------------------------------------------------------------------
    # Read — retrieve relevant feedback for a task
    # ------------------------------------------------------------------

    def get_relevant_feedback(
        self,
        task_description: str,
        top_k: int = 3,
        min_relevance_score: float = 0.4,
    ) -> str:
        """
        Search the learning database for feedback relevant to the given
        task description.

        Args:
            task_description:    The description of the task about to execute.
            top_k:               Maximum number of feedback entries to return.
            min_relevance_score: Minimum cosine similarity (0–1) threshold.
                                 ChromaDB returns distances (lower = better),
                                 so we convert to a score.

        Returns:
            A formatted string with the aggregated feedback, or an empty
            string if nothing relevant was found.
        """
        if not self.vector_store:
            return ""

        try:
            results = self.vector_store.similarity_search_with_relevance_scores(
                query=task_description,
                k=top_k,
            )

            if not results:
                return ""

            relevant = []
            for doc, score in results:
                if score >= min_relevance_score:
                    feedback = doc.metadata.get("feedback", doc.page_content)
                    workflow = doc.metadata.get("workflow_name", "Unknown")
                    relevant.append(f"[From workflow '{workflow}']: {feedback}")

            if not relevant:
                return ""

            feedback_block = "\n".join(f"  - {item}" for item in relevant)
            return (
                "\n\n=== PREVIOUS LEARNINGS & FEEDBACK ===\n"
                "In past executions, the following corrections were noted "
                "for similar tasks. You MUST adhere to these:\n"
                f"{feedback_block}\n"
                "=== END LEARNINGS ===\n"
            )

        except Exception as e:
            logger.error(f"[LearningMemory] Search failed: {e}")
            return ""

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def list_all_feedback(self) -> List[Dict[str, Any]]:
        """Return all stored feedback entries (for dashboard display)."""
        if not self.vector_store:
            return []
        try:
            collection = self.chroma_client.get_collection(self.COLLECTION_NAME)
            data = collection.get(include=["documents", "metadatas"])
            entries = []
            for doc, meta in zip(data["documents"], data["metadatas"]):
                entries.append({
                    "document": doc,
                    "workflow_name": meta.get("workflow_name", ""),
                    "feedback": meta.get("feedback", ""),
                    "timestamp": meta.get("timestamp", ""),
                })
            return entries
        except Exception as e:
            logger.error(f"[LearningMemory] list_all_feedback failed: {e}")
            return []

    def clear_all(self) -> None:
        """Delete all feedback records (factory reset)."""
        if self.chroma_client:
            try:
                self.chroma_client.delete_collection(self.COLLECTION_NAME)
                # Re-create empty collection
                self.vector_store = Chroma(
                    client=self.chroma_client,
                    collection_name=self.COLLECTION_NAME,
                    embedding_function=self.embedding_function,
                )
                logger.info("[LearningMemory] All feedback cleared.")
            except Exception as e:
                logger.error(f"[LearningMemory] clear_all failed: {e}")
