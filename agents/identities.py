import os
import requests
from crewai import Agent
from dotenv import load_dotenv


# --- IMPROVED .ENV LOADING ---
def load_project_env():
    # 1. Try local dir (agents/)
    local_path = os.path.join(os.path.dirname(__file__), '.env')
    if os.path.exists(local_path):
        load_dotenv(local_path)
        return local_path
    
    # 2. Try root dir (one level up from agents/)
    root_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '.env'))
    if os.path.exists(root_path):
        load_dotenv(root_path)
        return root_path
    
    # Fallback to standard search if specific paths don't exist
    load_dotenv()
    return "Standard Search"

percorso_env = load_project_env()

# --- GEMINI API CHECK ---
chiave = os.getenv("GEMINI_API_KEY")
if chiave:
    print(f"[INFO] GEMINI_API_KEY for agents found (starts with: {chiave[:5]})")
else:
    print(f"[WARNING] GEMINI_API_KEY NOT found. Search path info: {percorso_env}")

def is_ollama_alive():
    """Checks if Ollama instance is running locally."""
    try:
        # Check standard Ollama API endpoint
        res = requests.get("http://localhost:11434/api/tags", timeout=1)
        return res.status_code == 200
    except:
        return False

def get_llm(complexity="simple"):
    """
    Returns the appropriate LLM string based on required task complexity.
    - simple: local model (e.g., ollama) with fallback to Gemini if Ollama is unavailable
    - complex: cloud model (e.g., gemini/gpt4)
    """
    simple_default = os.getenv("SIMPLE_LLM", "ollama/llama3")
    complex_default = os.getenv("COMPLEX_LLM", "gemini/gemini-2.5-pro")
    
    if complexity == "simple":
        if is_ollama_alive():
            return simple_default
        else:
            # Fallback to Gemini if Ollama is not present
            return complex_default
    else:
        return os.getenv("COMPLEX_LLM", complex_default)

model_id = get_llm("complex") # Default for existing agents

# 1. THE MANAGER (PFC) - Invariato
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
    llm=model_id,
)

# 2. THE SYSTEMS ARCHITECT (Sostituisce il Process Engineer)
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
    llm=model_id
)

# 3. THE FRONTEND DEVELOPER (Aggiornato per YAML/Dashboard)
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
    llm=model_id
)

# 4. THE BACKEND DEVELOPER (Aggiornato per Telegram e CrewAI)
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
    llm=model_id
)