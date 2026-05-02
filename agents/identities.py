import os
import requests
import time
import asyncio
from crewai import Agent, LLM
from dotenv import load_dotenv
from typing import Any, Optional, List, Union

# --- CARICAMENTO .ENV ---
def load_project_env():
    """
    Carica le variabili d'ambiente da file .env in diverse posizioni possibili.
    """
    local_path = os.path.join(os.path.dirname(__file__), '.env')
    if os.path.exists(local_path):
        load_dotenv(local_path)
        return local_path
    
    root_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '.env'))
    if os.path.exists(root_path):
        load_dotenv(root_path)
        return root_path
    
    load_dotenv()
    return "Standard Search"

percorso_env = load_project_env()

# --- CONTROLLO API KEY ---
api_key = os.getenv("GEMINI_API_KEY")
if api_key:
    print(f"[INFO] GEMINI_API_KEY trovata (inizia con: {api_key[:5]}...)")
else:
    print(f"[WARNING] GEMINI_API_KEY NON trovata. Il sistema potrebbe fallire.")

# --- CLASSE LLM ROBUSTA ---
class RobustLLM(LLM):
    """
    Classe LLM personalizzata con Retry (per errori 503/429) e Fallback automatico.
    Supporta chiamate sia sincrone che asincrone (richieste da CrewAI).
    """
    fallback_model: Optional[str] = None

    def _should_retry(self, e, attempt, max_retries):
        err_msg = str(e)
        # Errori 503 (Unavailable) o 429 (Rate Limit) sono spesso temporanei
        if ("503" in err_msg or "429" in err_msg or "high demand" in err_msg.lower()) and attempt < max_retries - 1:
            wait = (attempt + 1) * 3
            print(f"\n[RETRY] {err_msg[:60]}... (Tentativo {attempt+1}/{max_retries}) - Attesa {wait}s...")
            return wait
        return None

    def call(self, *args, **kwargs):
        max_retries = 3
        for i in range(max_retries):
            try:
                return super().call(*args, **kwargs)
            except Exception as e:
                wait = self._should_retry(e, i, max_retries)
                if wait:
                    time.sleep(wait)
                    continue
                
                # Se i retry falliscono o l'errore è diverso, proviamo il fallback
                if self.fallback_model:
                    print(f"\n[FALLBACK] Errore critico: {e}. Passaggio a {self.fallback_model}...")
                    try:
                        # Creiamo un'istanza temporanea per il fallback per evitare conflitti di stato
                        fb_llm = LLM(model=self.fallback_model, api_key=self.api_key, temperature=self.temperature)
                        return fb_llm.call(*args, **kwargs)
                    except Exception as fe:
                        print(f"[ERROR] Anche il fallback è fallito: {fe}")
                raise e

    async def acall(self, *args, **kwargs):
        max_retries = 3
        for i in range(max_retries):
            try:
                return await super().acall(*args, **kwargs)
            except Exception as e:
                wait = self._should_retry(e, i, max_retries)
                if wait:
                    await asyncio.sleep(wait)
                    continue
                
                if self.fallback_model:
                    print(f"\n[FALLBACK ASYNC] Errore critico: {e}. Passaggio a {self.fallback_model}...")
                    try:
                        fb_llm = LLM(model=self.fallback_model, api_key=self.api_key, temperature=self.temperature)
                        return await fb_llm.acall(*args, **kwargs)
                    except Exception as fe:
                        print(f"[ERROR ASYNC] Anche il fallback è fallito: {fe}")
                raise e

def get_robust_llm(complexity="simple"):
    """
    Restituisce un'istanza di RobustLLM configurata.
    """
    if complexity == "complex":
        primary = "gemini/gemini-3.1-pro-preview"
        secondary = "gemini/gemini-3-flash-preview"
    else:
        primary = "gemini/gemini-2.5-flash"
        secondary = "gemini/gemini-2.0-flash"

    return RobustLLM(
        model=primary,
        fallback_model=secondary,
        api_key=api_key,
        temperature=0.7
    )

# --- DEFINIZIONE AGENTI ---
# NOTA: Creiamo istanze LLM separate per ogni agente per evitare conflitti durante i fallback.

# 1. THE MANAGER (PFC)
pfc_manager = Agent(
    role="GSD Project Manager",
    goal="Read the project status from the PRD, identify the next executable atomic task, and coordinate developers to complete it.",
    backstory=(
        "You are the conductor of a software project. Your task is to apply the GSD (Getting Things Done) method. "
        "You do not write code. At each cycle, you read the status files, find the next task without blocking dependencies "
        "and provide precise instructions to the team. You are methodical, ruthless with inefficiencies, and "
        "ensure that no one deviates from the Product Requirements Document (PRD)."
    ),
    allow_delegation=True,
    verbose=True,
    llm=get_robust_llm("complex"),
)

# 2. THE SYSTEMS ARCHITECT
systems_architect = Agent(
    role="Lead Systems & Security Architect",
    goal="Validate local file operations, YAML database structures, and Telegram security logic.",
    backstory=(
        "You are a strict software architect specialized in local operating systems, data persistence via YAML, "
        "and API security. Your job is to review the Manager's plans and ensure that the code won't corrupt local files, "
        "that `.env` secrets are handled properly, and that the Telegram bot is securely locked to a specific user ID. "
        "You prevent bugs in process communication between Streamlit and the Telegram bot."
    ),
    allow_delegation=False,
    verbose=True,
    llm=get_robust_llm("complex")
)

# 3. THE FRONTEND DEVELOPER
frontend_dev = Agent(
    role="Senior Streamlit Developer",
    goal="Build clean, dynamic user interfaces for configuring AI agents and workflows using Streamlit.",
    backstory=(
        "You are a frontend specialist focused entirely on Python and Streamlit. "
        "You build intuitive dashboards to read and write YAML configuration files securely. "
        "You use native Streamlit components (st.tabs, st.columns, st.data_editor) flawlessly. "
        "You never write backend business logic, focusing purely on UX and configuration forms."
    ),
    allow_delegation=False,
    verbose=True,
    llm=get_robust_llm("simple")
)

# 4. THE BACKEND DEVELOPER
backend_dev = Agent(
    role="Senior Backend & Automation Engineer",
    goal="Create robust scripts for Telegram bot interactions, YAML CRUD operations, and CrewAI orchestration.",
    backstory=(
        "You are a backend architect specialized in `python-telegram-bot`, `CrewAI`, and local OS automation. "
        "You write clean Python code to manage stateful Telegram conversations, dynamic inline keyboards, "
        "and file system interactions. You ensure that the bot seamlessly reads workflows from YAML files "
        "and executes AI agent tasks cleanly. You write atomic, testable code."
    ),
    allow_delegation=False,
    verbose=True,
    llm=get_robust_llm("simple")

)