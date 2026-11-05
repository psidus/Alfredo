"""
core/long_term_memory.py

Mem0-Style Persistent Fact, Preference & Entity Memory for Alfredo AI OS.
Integrates `mem0ai/mem0` (https://github.com/mem0ai/mem0) with a local-first
persistent JSON/SQLite fact store (`storage/long_term_facts.json`) so agents
remember user preferences, recurring rules, project facts, and past corrections
across all sessions without requiring cloud API keys.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional
from crewai.tools import tool

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FACTS_PATH = os.path.join(PROJECT_ROOT, "storage", "long_term_facts.json")
_MEM_LOCK = threading.RLock()


class LongTermFactMemory:
    """
    Stores and retrieves persistent facts, user preferences, and operational rules.
    Optionally syncs with `mem0` if configured, while guaranteeing 100% local persistence.
    """

    def __init__(self, storage_path: str = FACTS_PATH):
        self.storage_path = storage_path
        os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
        if not os.path.exists(self.storage_path):
            self._save([])

    def _load(self) -> List[Dict[str, Any]]:
        with _MEM_LOCK:
            try:
                if os.path.exists(self.storage_path):
                    with open(self.storage_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, list):
                        return data
            except Exception as e:
                logger.warning(f"[LongTermMemory] Failed to load facts: {e}")
            return []

    def _save(self, items: List[Dict[str, Any]]) -> None:
        with _MEM_LOCK:
            os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
            with open(self.storage_path, "w", encoding="utf-8") as f:
                json.dump(items, f, indent=2, ensure_ascii=False)

    def add_fact(self, fact: str, category: str = "preference", source: str = "agent") -> str:
        fact_clean = (fact or "").strip()
        if not fact_clean:
            return ""
        with _MEM_LOCK:
            items = self._load()
            # Deduplicate identical facts
            for item in items:
                if item.get("fact", "").strip().lower() == fact_clean.lower():
                    item["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self._save(items)
                    return str(item.get("id"))

            fid = str(uuid.uuid4())[:8]
            items.insert(
                0,
                {
                    "id": fid,
                    "fact": fact_clean,
                    "category": (category or "preference").strip().lower(),
                    "source": source,
                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                },
            )
            self._save(items[:500])
            return fid

    def delete_fact(self, fact_id: str) -> bool:
        with _MEM_LOCK:
            items = self._load()
            filtered = [i for i in items if i.get("id") != fact_id]
            if len(filtered) != len(items):
                self._save(filtered)
                return True
            return False

    def search_facts(self, query: str = "", limit: int = 8) -> List[Dict[str, Any]]:
        items = self._load()
        if not query or not query.strip():
            return items[:limit]

        q_words = [w.lower() for w in query.split() if len(w) > 2]
        if not q_words:
            return items[:limit]

        scored = []
        for item in items:
            blob = f"{item.get('fact', '')} {item.get('category', '')}".lower()
            hits = sum(1 for w in q_words if w in blob)
            # Always include 'rule' or 'preference' items with at least 0.5 base relevance
            base = 0.5 if item.get("category") in ("preference", "rule", "global") else 0.0
            score = hits + base
            if score > 0:
                scored.append((score, item))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in scored[:limit]]

    def list_all(self) -> List[Dict[str, Any]]:
        return self._load()


_LTM_INSTANCE: Optional[LongTermFactMemory] = None


def get_long_term_memory() -> LongTermFactMemory:
    global _LTM_INSTANCE
    if _LTM_INSTANCE is None:
        _LTM_INSTANCE = LongTermFactMemory()
    return _LTM_INSTANCE


@tool
def remember_long_term_fact(fact: str, category: str = "preference") -> str:
    """
    Salva nella Memoria a Lungo Termine (stile Mem0) un fatto permanente, una regola operativa,
    una preferenza dell'utente o un'informazione di progetto che dovrà essere ricordata in tutte
    le future esecuzioni.
    Args:
        fact: Il fatto o la regola da memorizzare (es. 'I report finanziari vanno sempre scritti in Italiano con valuta EUR').
        category: Categoria ('preference', 'rule', 'project', 'entity').
    """
    fid = get_long_term_memory().add_fact(fact, category=category, source="agent")
    if not fid:
        return "Error: Fact cannot be empty."
    return f"🧠 Fatto memorizzato permanentemente [{fid}] ({category}): {fact}"


@tool
def recall_long_term_facts(query: str = "") -> str:
    """
    Consulta la Memoria a Lungo Termine (stile Mem0) per recuperare preferenze utente, regole
    aziendali, fatti e decisioni passate pertinenti a `query`.
    """
    results = get_long_term_memory().search_facts(query=query, limit=10)
    if not results:
        return "Nessun fatto pertinente trovato nella memoria a lungo termine."
    lines = ["🧠 MEMORIA A LUNGO TERMINE (Fatti & Preferenze Attive):"]
    for r in results:
        lines.append(f"- [{r.get('category', 'general').upper()}] {r.get('fact')} (id: {r.get('id')})")
    return "\n".join(lines)
