import json
import os
import logging
import re

class RateLimitError(Exception):
    """Exception raised when an LLM returns a 503 or rate limit error."""
    def __init__(self, message, current_task_idx, task_outputs):
        super().__init__(message)
        self.current_task_idx = current_task_idx
        self.task_outputs = task_outputs

from crewai import Agent, Task, Crew
from langchain_openai import ChatOpenAI
from langchain_community.llms import Ollama

from core.db_manager import DBManager
from core.data_manager import DataManager
from core.ephemeral_memory import EphemeralMemoryManager
import tools.local_tools as local_tools
import tools.terminal_executor as terminal_executor
import tools.office_tool as office_tool
import tools.email_tool as email_tool
from tools.vector_search_tool import VectorSearchTool
from tools.tabular_query_tool import TabularQueryTool
from tools.ephemeral_memory_tool import ReadAtomicMemoryTool, WriteAtomicMemoryTool

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Initialize DB
db = DBManager()

# --- GLOBAL INTER-AGENT COMMUNICATION DIRECTIVE ---
# This is appended to EVERY task to enforce concise, structured outputs
# optimized for AI-to-AI communication (not human-readable fluff).
# MEMORY-CENTRIC: Agents are now instructed to use the ephemeral memory tools.
AGENT_COMMS_DIRECTIVE = """

--- COMMUNICATION PROTOCOL (VECTOR MEMORY-CENTRIC) ---
You are part of a sequential AI agent pipeline. Your output will be consumed by the NEXT AI agent and stored in a Semantic Vector Database (ChromaDB).
INTER-AGENT MEMORY:
- You have access to 'write_atomic_memory' and 'read_atomic_memory' tools.
- Results from previous steps are stored in an ephemeral in-memory database.
  Check the EPHEMERAL WORKSPACE MEMORY INDEX below for available records and their keys.
- Use 'read_atomic_memory' with the exact 'key' to retrieve a specific record,
  or use a 'query' string for semantic search when the key is unknown.
- ALWAYS use 'write_atomic_memory' to store your final output so that downstream agents can access it.
OUTPUT RULES (OPTIMIZED FOR VECTOR RETRIEVAL):
1. Start with a descriptive header: `# Topic: [Core Subject of your task]`.
2. Provide a comma-separated keywords block: `[KEYWORDS: tag1, tag2, tag3]`.
3. Provide a concise 1-2 sentence Context Summary.
4. Output your key findings as a structured list (max 10 points).
5. CRITICAL: Use explicit, descriptive nouns in every bullet point. NEVER use pronouns like "it", "they", or "this" because context is lost during vector chunking.
6. NO preamble, NO "In conclusion...", NO filler phrases.
"""

# --- Security Helper: Hardcoded Tool Registry ---
# Define a strict, immutable mapping of allowed tools to prevent injection attacks.
# NOTE: Document-creation tools (Word, Excel, file_write, etc.) are intentionally
# EXCLUDED.  They live in core/export_tools.py and are called deterministically
# by the Master AI at the end of a workflow — never by individual agents.
ALLOWED_TOOLS = {
    # Workspace (sandboxed, read-only)
    "read_file": local_tools.read_file,
    # Full PC (read-only)
    "read_file_anywhere": local_tools.read_file_anywhere,
    "search_files": local_tools.search_files,
    # Web & communication
    "search_web": local_tools.search_web,
    "ask_operator": local_tools.ask_operator,
    # Terminal
    "execute_shell_command": terminal_executor.execute_shell_command,
    # Screenshot
    "take_screenshot": office_tool.take_screenshot,
    # Email (READ/SEARCH only — send_email is reserved for Master AI exports)
    "manage_email": None,  # Sentinel — expanded at task build time into read + search only
    "read_emails": email_tool.read_emails,
    "search_emails": email_tool.search_emails,
    # Vector search — expanded at task build time into VectorSearchTool instances
    "vector_search": None,  # Sentinel
    # Tabular query — auto-injected at task build time when structured CSVs exist
    "tabular_query": None,  # Sentinel
    # Calculator
    "calculator": local_tools.calculate,
    # Code tools (read + execute only — writing is handled by Master AI exports)
    "python_repl_tool": local_tools.python_repl,
    # Ephemeral Memory — sentinels, auto-injected at runtime with the correct manager
    "read_atomic_memory": None,   # Sentinel
    "write_atomic_memory": None,  # Sentinel
}

def _instantiate_llm(model_id):
    """
    Securely creates an LLM string/object based on model_id from the database.
    Ensures API keys are injected into os.environ so that LiteLLM/CrewAI can find them.
    """
    DataManager.load_env() # Ensure .env is loaded first

    model_record = db.read_model(model_id)
    if not model_record:
        raise ValueError(f"Model ID {model_id} not found in database.")

    provider = model_record['provider'].lower()
    model_name = model_record['model_name']
    
    # --- PROVIDER & MODEL NORMALIZATION ---
    provider_mapping = {
        'google': 'gemini',
        'google_vertex': 'vertex_ai',
        'mistralai': 'mistral'
    }
    provider = provider_mapping.get(provider, provider)

    # Clean model name (remove prefixes like 'models/' common in Google API)
    if provider == 'gemini' and model_name.startswith('models/'):
        model_name = model_name.replace('models/', '')

    # --- CRITICAL: Inject API key into os.environ for LiteLLM/CrewAI ---
    # LiteLLM reads keys from environment variables, not from Python objects.
    provider_key_env_map = {
        "gemini": "GEMINI_API_KEY",
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "groq": "GROQ_API_KEY",
        "mistral": "MISTRAL_API_KEY",
        "ollama": None,  # Ollama is local, no key needed
    }
    env_var_name = provider_key_env_map.get(provider, f"{provider.upper()}_API_KEY")
    
    if env_var_name:
        api_key = DataManager.load_api_key(env_var_name)
        if not api_key:
            # Generic fallback to GEMINI_API_KEY for Google models
            api_key = DataManager.load_api_key("GEMINI_API_KEY")
        if api_key:
            os.environ[env_var_name] = api_key
            logging.info(f"API key for '{provider}' injected as '{env_var_name}'.")
        else:
            logging.warning(f"No API key found for provider '{provider}' (expected '{env_var_name}').")

    # Build and return the LLM reference
    if provider == 'openai':
        if not os.getenv("OPENAI_API_KEY"):
            raise EnvironmentError("OPENAI_API_KEY is missing from .env.")
        return ChatOpenAI(model_name=model_name, temperature=0.7)
    elif provider == 'ollama':
        return f"ollama/{model_name}"
    else:
        # Standard LiteLLM format: provider/model_name (e.g. gemini/gemini-2.5-flash-lite)
        model_string = f"{provider}/{model_name}"
        logging.info(f"LLM instantiated: {model_string}")
        return model_string

def _map_tools(tool_names):
    """
    Safely converts a list of tool names into a list of callable Python functions
    using a strict whitelist (ALLOWED_TOOLS).
    The 'manage_email' key is a sentinel that expands into all three email tools.
    """
    if not tool_names:
        return []

    instantiated_tools = []
    for tool_name in tool_names:
        # 'manage_email' is a UI label — expand to read/search only
        # (send_email is reserved for Master AI export phase)
        if tool_name == "manage_email":
            instantiated_tools.extend([
                email_tool.read_emails,
                email_tool.search_emails,
            ])
            logging.info("Expanded 'manage_email' sentinel into 2 email tools (read + search).")
        elif tool_name in ALLOWED_TOOLS:
            tool_fn = ALLOWED_TOOLS[tool_name]
            if tool_fn is not None:  # Skip None sentinels
                instantiated_tools.append(tool_fn)
        else:
            logging.warning(f"Attempted to use unknown or disallowed tool: '{tool_name}'. Skipping.")

    return instantiated_tools

def _get_task_tools(tool_names, vector_dbs, is_local_model=False):
    """
    Combines _map_tools with dynamic instantiation of VectorSearchTool and TabularQueryTool
    based on the provided vector_dbs list.
    """
    if is_local_model:
        return None
        
    task_tools = _map_tools(tool_names) or []
    
    if 'vector_search' in tool_names:
        for db_id in vector_dbs:
            try:
                db_id_int = int(db_id)
                vdb_record = db.cursor.execute("SELECT * FROM vector_databases WHERE id = ?", (db_id_int,)).fetchone()
                if vdb_record:
                    tool_instance = VectorSearchTool(
                        name=f"search_db_{vdb_record['name']}",
                        description=f"Search the vector database '{vdb_record['name']}' for context.",
                        db_path=vdb_record['path'],
                        provider=vdb_record['provider'],
                        model_name=vdb_record['model_name']
                    )
                    task_tools.append(tool_instance)

                    structured_dir = os.path.join(vdb_record['path'], "structured")
                    if os.path.isdir(structured_dir):
                        csv_files = [f for f in os.listdir(structured_dir) if f.endswith('.csv')]
                        if csv_files:
                            table_list = ', '.join(csv_files)
                            tabular_tool = TabularQueryTool(
                                name=f"query_tables_{vdb_record['name']}",
                                description=(
                                    f"Query structured tabular data (CSV/Excel) from database '{vdb_record['name']}'. "
                                    f"Available tables: {table_list}. "
                                    "Use action='list' to see all tables, 'info' for schema, "
                                    "'head' for a preview, 'summary' for statistics, "
                                    "or 'query' with a pandas filter expression to find specific rows."
                                ),
                                structured_dir=structured_dir
                            )
                            task_tools.append(tabular_tool)
                            logging.info(
                                f"Auto-injected TabularQueryTool for DB '{vdb_record['name']}' "
                                f"with {len(csv_files)} CSV file(s)."
                            )
            except (ValueError, TypeError):
                pass
                
    return task_tools if task_tools else None

def _build_agent(agent_id, specialization=None, model_id_override=None):
    """
    Constructs a CrewAI Agent object from database records.
    Automatically disables tools for local models (Ollama/phi3, etc.)
    that do not support the function-calling protocol.
    If a 'specialization' string is provided, the agent's role and backstory
    are dynamically narrowed for that specific task without altering the DB record.
    """
    agent_record = db.read_agent(agent_id)
    if not agent_record:
        raise ValueError(f"Agent ID {agent_id} not found in database.")

    # Determine which model to use: override first, then agent record, then default fallback.
    model_id = model_id_override if model_id_override is not None else agent_record.get('model_id')
    
    # If neither is set, check the global default model from .env!
    if not model_id:
        DataManager.load_env()
        env_model_id = os.getenv("DEFAULT_AGENT_MODEL_ID")
        if env_model_id:
            try:
                model_id = int(env_model_id)
            except ValueError:
                pass
                
    # If still not set, let's find the first available model in the database as fallback
    if not model_id:
        models = db.read_all_models()
        if models:
            model_id = models[0]['id']
            logging.info(f"No model set for agent/task. Using first model as fallback: {models[0]['model_name']}")

    model_record = db.read_model(model_id) if model_id else None
    llm_instance = _instantiate_llm(model_id) if model_id else None
    
    # --- LOCAL MODEL CHECK ---
    # Local models (Ollama, phi3, llama, etc.) do NOT support the tools/function-calling
    # protocol. Passing tools to them causes a 400 BadRequestError.
    # We detect this via the is_local flag OR the ollama provider name.
    is_local_model = False
    if model_record:
        is_local_model = bool(model_record.get('is_local')) or \
                         model_record.get('provider', '').lower() == 'ollama'

    if is_local_model:
        agent_tools = []
        logging.info(f"Agent '{agent_record['name']}' uses a local model — tools disabled.")
    else:
        agent_tools = _map_tools(agent_record.get('tools', []))

    # --- DYNAMIC SPECIALIZATION INJECTION ---
    # If the calling task defines a specialization, we narrow the agent's persona
    # at runtime without touching the database record. This lets a generic agent
    # (e.g. "Web Researcher") act as a domain expert (e.g. "Web Researcher
    # specialized in chemical thermodynamics") for one specific task.
    # The agent's role and backstory can also contain the `{specialization}` placeholder
    # to control exactly where the specialization string is injected.
    base_role = agent_record['role']
    base_backstory = agent_record['backstory'] or ""
    base_goal = agent_record.get('goal', '') or ""

    if specialization:
        if "{specialization}" in base_role:
            effective_role = base_role.replace("{specialization}", specialization)
        else:
            effective_role = f"{base_role} specialized in {specialization}"

        if "{specialization}" in base_backstory:
            effective_backstory = base_backstory.replace("{specialization}", specialization)
        else:
            effective_backstory = (
                base_backstory +
                f"\n\nCRITICAL CONTEXT: For this specific task your area of expertise is "
                f"focused on **{specialization}**. Apply all your base skills strictly within "
                f"this specialized domain. Do not stray outside it."
            )
            
        if "{specialization}" in base_goal:
            effective_goal = base_goal.replace("{specialization}", specialization)
        else:
            effective_goal = f"{base_goal} (specialized in {specialization})"

        logging.info(f"Agent '{agent_record['name']}' specialized as: '{effective_role}'")
    else:
        # Clean up any `{specialization}` placeholders from the base role and backstory
        effective_role = base_role.replace(" specialized in {specialization}", "").replace(" specialized in {specialization}", "").replace("{specialization}", "").strip()
        effective_backstory = base_backstory.replace("{specialization}", "").strip()
        effective_goal = base_goal.replace("{specialization}", "").strip()

    # Safeguard against any other unreplaced variables
    effective_role = re.sub(r'\{([a-zA-Z0-9_]+)\}', r'[missing input: \1]', effective_role)
    effective_backstory = re.sub(r'\{([a-zA-Z0-9_]+)\}', r'[missing input: \1]', effective_backstory)
    effective_goal = re.sub(r'\{([a-zA-Z0-9_]+)\}', r'[missing input: \1]', effective_goal)

    # --- BACKSTORY INJECTION: Enforce conciseness at identity level ---
    conciseness_trait = ("\n\nCRITICAL TRAIT: You are extremely concise and data-driven. "
                         "You never ramble. You output structured bullet points, not essays. "
                         "Every sentence must carry unique, actionable information.")
    enhanced_backstory = effective_backstory + conciseness_trait

    agent = Agent(
        role=effective_role,
        backstory=enhanced_backstory,
        goal=effective_goal,
        llm=llm_instance,
        tools=agent_tools,
        verbose=True,
        allow_delegation=False,
        max_iter=15  # Prevent infinite reasoning loops
    )
    logging.info(f"Built CrewAI Agent: {agent_record['name']} (ID: {agent_id}, local={is_local_model}, specialization={specialization!r})")
    return agent

def _build_task(task_id, agents_cache):
    """
    Constructs a CrewAI Task object and ensures agents are reused via memory reference.
    Tool overrides at task level are also stripped for local model agents.
    """
    task_record = db.read_task(task_id)
    if not task_record:
        raise ValueError(f"Task ID {task_id} not found in database.")

    agent_id = task_record['agent_id']
    task_model_id = task_record.get('model_id')  # Custom LLM model for this task

    # --- SPECIALIZATION / MODEL CACHE BYPASS ---
    # If this task defines an agent_specialization OR a specific model_id override,
    # we must build a fresh, task-specific agent clone rather than pulling from the shared cache.
    # This prevents the specialized role/backstory and model override from leaking into other tasks.
    specialization = task_record.get('agent_specialization')
    if specialization or task_model_id is not None:
        agent_instance = _build_agent(agent_id, specialization=specialization, model_id_override=task_model_id)
    else:
        if agent_id not in agents_cache:
            agents_cache[agent_id] = _build_agent(agent_id)
        agent_instance = agents_cache[agent_id]
    
    # Check if the execution model is local — if so, strip task-level tools too
    agent_record = db.read_agent(agent_id) if agent_id else None
    execution_model_id = task_model_id if task_model_id is not None else (agent_record.get('model_id') if agent_record else None)
    model_record = db.read_model(execution_model_id) if execution_model_id else None
    is_local_model = False
    if model_record:
        is_local_model = bool(model_record.get('is_local')) or \
                         model_record.get('provider', '').lower() == 'ollama'

    if is_local_model:
        task_tools = None  # No tools for local models
    else:
        task_tools = _get_task_tools(task_record.get('tools', []), task_record.get('vector_dbs', []), is_local_model)

    # --- INTER-AGENT COMMUNICATION GUARDRAIL ---
    task_description = task_record['description'] + AGENT_COMMS_DIRECTIVE

    # Enhance expected_output to enforce structured format
    # Enhance expected_output for Vector DB structured format
    base_expected = task_record['expected_output']
    vector_format_directive = (
        " FORMAT CRITERIA (For Vector DB): Begin with a clear '# Topic: <Subject>' header, "
        "a 1-line summary, a '[KEYWORDS: ...]' block, and then self-contained, noun-heavy bullet points."
    )
    if "vector" not in base_expected.lower() and "header" not in base_expected.lower():
        base_expected += vector_format_directive

    task = Task(
        description=task_description,
        expected_output=base_expected,
        agent=agent_instance,
        tools=task_tools,
        async_execution=False
    )
    logging.info(f"Built CrewAI Task: {task_record['description'][:50]}... (ID: {task_id}) for Agent ID: {agent_id}")
    return task

def build_crew(workflow_id):
    """
    The primary, fail-safe entry point to assemble an executable CrewAI Crew.
    Queries the database for workflow tasks, joins with Agents and Models,
    instantiates objects dynamically, and returns the Crew.
    """
    try:
        workflow_record = db.read_workflow(workflow_id)
        if not workflow_record:
            raise ValueError(f"Workflow ID {workflow_id} not found in database.")

        # Debug: check available keys
        logging.info(f"Workflow record keys: {list(workflow_record.keys())}")
        
        task_ids = workflow_record.get('task_ids')
        if task_ids is None:
            # Fallback for older database records or un-processed rows
            if 'task_ids_json' in workflow_record:
                task_ids = json.loads(workflow_record['task_ids_json'])
            else:
                task_ids = []

        if not task_ids:
            raise ValueError(f"Workflow ID {workflow_id} has no tasks defined.")

        crew_tasks = []
        agents_cache = {} # Cache to store unique agent instances
        crew_agents = []

        for task_id in task_ids:
            task_obj = _build_task(task_id, agents_cache)
            crew_tasks.append(task_obj)
            # Collect all unique agent instances (including specialized clones)
            if task_obj.agent and task_obj.agent not in crew_agents:
                crew_agents.append(task_obj.agent)

        if not crew_agents:
            raise ValueError(f"No agents found for workflow ID {workflow_id}.")
        if not crew_tasks:
            raise ValueError(f"No tasks found for workflow ID {workflow_id}.")

        crew = Crew(
            agents=crew_agents,
            tasks=crew_tasks,
            verbose=True, # Crew verbosity for overall process logging
            process='sequential' # Default to sequential processing
        )
        logging.info(f"Successfully built Crew for Workflow ID: {workflow_id} (Name: {workflow_record['name']})")
        return crew

    except Exception as e:
        logging.error(f"Error building crew for workflow ID {workflow_id}: {e}", exc_info=True)
        # Re-raise or return a structured error, depending on how the calling function handles it
        raise RuntimeError(f"Failed to build crew for workflow {workflow_id}: {e}") from e

def _inject_memory_tools(agent_instance, task_obj, read_tool, write_tool):
    """
    Injects the ephemeral memory read/write tools into both the agent and the
    task, removing any stale instances first.  This is idempotent.
    """
    memory_tool_names = ('read_atomic_memory', 'write_atomic_memory')

    if agent_instance:
        if not agent_instance.tools:
            agent_instance.tools = []
        agent_instance.tools = [
            t for t in agent_instance.tools if t.name not in memory_tool_names
        ]
        agent_instance.tools.extend([read_tool, write_tool])

    if task_obj.tools is None:
        task_obj.tools = []
    task_obj.tools = [
        t for t in task_obj.tools if t.name not in memory_tool_names
    ]
    task_obj.tools.extend([read_tool, write_tool])


def _auto_save_to_memory(memory_manager, task_id, last_output, agent_role):
    """
    Automatically saves the raw output of a completed task into the ephemeral
    memory so that downstream agents can retrieve it by key even if the agent
    forgot to call write_atomic_memory explicitly.
    """
    # Try to extract structured JSON from the output
    try:
        json_match = re.search(r'```json\s*(.*?)\s*```', last_output, re.DOTALL)
        json_str = json_match.group(1).strip() if json_match else last_output.strip()
        structured_data = json.loads(json_str)
    except Exception:
        structured_data = {"raw_output": last_output}

    task_rec = db.read_task(task_id)
    task_name = task_rec.get('name') if task_rec else None
    key_name = f"task_{task_id}"

    # Truncate summary for the embedding to keep it focused
    summary = last_output[:500] if len(last_output) > 500 else last_output
    summary_text = (
        f"Output of task '{task_name or task_id}' by agent '{agent_role}': {summary}"
    )

    memory_manager.write_record(
        key=key_name,
        content_summary=summary_text,
        structured_data=structured_data,
        agent_role=agent_role,
    )


def execute_run_with_resume(run_id: int, status_callback=None, accumulated_context: str = None, chat_id: str = None) -> str:
    """
    Executes a workflow run task-by-task with MEMORY-CENTRIC communication.

    Instead of stuffing all previous outputs into each task's prompt (which
    wastes tokens and dilutes context), we use an ephemeral in-memory ChromaDB
    instance.  Each agent's output is stored as a keyed vector record, and
    downstream agents receive only a compact Memory Index Table telling them
    which keys are available.  They retrieve details on demand via the
    'read_atomic_memory' tool.

    The database is destroyed when this function returns.
    """
    # 1. Read run record
    run = db.read_run(run_id)
    if not run:
        raise ValueError(f"Run ID {run_id} not found.")

    workflow_id = run['workflow_id']
    workflow_record = db.read_workflow(workflow_id)
    if not workflow_record:
        raise ValueError(f"Workflow ID {workflow_id} not found.")

    task_ids = workflow_record.get('task_ids')
    if task_ids is None:
        if 'task_ids_json' in workflow_record:
            task_ids = json.loads(workflow_record['task_ids_json'])
        else:
            task_ids = []

    if not task_ids:
        raise ValueError(f"Workflow ID {workflow_id} has no tasks defined.")

    # Parse inputs and task_outputs
    inputs = {}
    if run.get('inputs'):
        try:
            inputs = json.loads(run['inputs'])
        except Exception:
            inputs = {}

    task_outputs = {}
    if run.get('task_outputs'):
        try:
            task_outputs = json.loads(run['task_outputs'])
        except Exception:
            task_outputs = {}

    start_idx = run.get('current_task_idx', 0)
    if start_idx >= len(task_ids):
        return run.get('result', '')

    # Mark run as running
    db.update_run(run_id, status='running', current_task_idx=start_idx, task_outputs=task_outputs)

    # --- MEMORY-CENTRIC: Initialise ephemeral vector store for this run ---
    memory_manager = EphemeralMemoryManager(run_id=run_id)
    if accumulated_context:
        memory_manager.load_from_dump(accumulated_context)
    read_memory_tool = ReadAtomicMemoryTool(memory_manager=memory_manager)
    write_memory_tool = WriteAtomicMemoryTool(memory_manager=memory_manager)

    # If resuming, seed the ephemeral memory with outputs from already-completed tasks
    if start_idx > 0:
        for prev_i in range(start_idx):
            prev_tid = task_ids[prev_i]
            prev_output = task_outputs.get(str(prev_tid), "")
            if prev_output:
                prev_task_rec = db.read_task(prev_tid)
                prev_agent_role = "Unknown"
                if prev_task_rec and prev_task_rec.get('agent_id'):
                    agent_rec = db.read_agent(prev_task_rec['agent_id'])
                    if agent_rec:
                        prev_agent_role = agent_rec.get('name', 'Unknown')
                _auto_save_to_memory(
                    memory_manager, prev_tid, prev_output, prev_agent_role
                )

    agents_cache = {}
    last_output = ""
    if start_idx > 0:
        prev_task_id = task_ids[start_idx - 1]
        last_output = task_outputs.get(str(prev_task_id), "")

    for i in range(start_idx, len(task_ids)):
        task_id = task_ids[i]

        if status_callback:
            try:
                status_callback(f"Building and executing Task {i+1}/{len(task_ids)} (ID: {task_id})...")
            except Exception:
                pass

        # Build task object (and agent)
        task_obj = _build_task(task_id, agents_cache)

        # --- MEMORY-CENTRIC: Inject memory tools into agent & task ---
        _inject_memory_tools(task_obj.agent, task_obj, read_memory_tool, write_memory_tool)

        # --- Placeholder resolution (task names, IDs, aliases) ---
        # --- Placeholder resolution (task names, IDs, aliases) ---
        def normalize_name(s: str) -> str:
            if not s:
                return ""
            s_norm = s.lower().replace('_', ' ').replace('-', ' ')
            return " ".join(s_norm.split())

        workflow_tasks = []
        for tid in task_ids:
            try:
                t_rec = db.read_task(tid)
                if t_rec:
                    workflow_tasks.append(t_rec)
                else:
                    workflow_tasks.append({'id': tid, 'name': None, 'description': '', 'agent_id': None})
            except Exception:
                workflow_tasks.append({'id': tid, 'name': None, 'description': '', 'agent_id': None})

        lookup = {}
        for idx in range(i):
            t_rec = workflow_tasks[idx]
            t_id = t_rec['id']
            t_name = t_rec.get('name')
            t_output = task_outputs.get(str(t_id), "")

            if t_name:
                norm = normalize_name(t_name)
                if norm:
                    lookup[norm] = t_output

            lookup[f"task {t_id}"] = t_output
            lookup[f"task_{t_id}"] = t_output
            lookup[str(t_id)] = t_output

        if i > 0:
            prev_t_id = task_ids[i - 1]
            prev_output = task_outputs.get(str(prev_t_id), "")
            lookup["previous task"] = prev_output
            lookup["previous_task"] = prev_output
            lookup["previous"] = prev_output
            lookup["task precedente"] = prev_output
            lookup["task_precedente"] = prev_output

        pattern = re.compile(r'\{task:([^\}]+)\}|\{([^\}]+)\}')

        def repl(match):
            g1 = match.group(1)
            g2 = match.group(2)
            key = g1 if g1 is not None else g2
            if not key:
                return match.group(0)
            norm_key = normalize_name(key)
            if norm_key in lookup:
                return lookup[norm_key]
            lower_key = key.strip().lower()
            if lower_key in lookup:
                return lookup[lower_key]
            return match.group(0)

        def apply_interpolation(text: str) -> str:
            if not text:
                return text
            for k, v in inputs.items():
                text = text.replace(f"{{{k}}}", str(v))
            text = text.replace("{user_input}", inputs.get('user_input', ''))
            text = text.replace("{previous_result}", last_output)
            text = text.replace("{context}", last_output)
            text = text.replace("{flexible_input}", inputs.get('user_input', last_output))
            text = pattern.sub(repl, text)
            # Safeguard: Prevent CrewAI from crashing on unreplaced template variables
            # Convert {variable} to <variable> so the agent sees it conceptually and can search for it
            text = re.sub(r'\{([a-zA-Z0-9_]+)\}', r'<\1>', text)
            return text

        # Apply interpolations to both description and expected_output
        original_desc = apply_interpolation(task_obj.description)
        original_expected = apply_interpolation(task_obj.expected_output)

        # --- MEMORY-CENTRIC: Replace prompt-stuffing with Memory Index Table ---
        # Instead of appending the full text of every previous output (which
        # consumes O(n²) tokens across n tasks), we append only a compact
        # index table.  The agent uses read_atomic_memory to fetch details.
        if i > 0:
            index_table = memory_manager.get_memory_index_table()
            context_str = (
                "\n\n--- [EPHEMERAL WORKSPACE MEMORY INDEX] ---\n"
                "Results from previous steps are stored in the ephemeral in-memory database.\n"
                "Use the 'read_atomic_memory' tool with the exact 'key' column value to retrieve data.\n"
                "Use 'write_atomic_memory' to store YOUR output for downstream agents.\n\n"
                f"{index_table}\n"
                "--- [END MEMORY INDEX] ---\n"
            )
            original_desc += context_str

        task_obj.description = original_desc
        task_obj.expected_output = original_expected

        # Create a single-task Crew and execute
        single_task_crew = Crew(
            agents=[task_obj.agent],
            tasks=[task_obj],
            verbose=True,
            process='sequential'
        )

        try:
            result = single_task_crew.kickoff()
        except Exception as e:
            err_str = str(e).lower()
            if "503" in err_str or "unavailable" in err_str or "rate" in err_str:
                logging.warning(f"Rate limit / 503 encountered at task {i}: {e}")
                # Save state to DB before raising so it's safely stored
                db.update_run(run_id, status='paused', result=str(e), current_task_idx=i, task_outputs=task_outputs)
                raise RateLimitError(f"Model high demand error (503): {e}", i, task_outputs)
            elif "none or empty" in err_str:
                logging.warning(f"Agent reached max iterations or hallucinatory loop: {e}")
                raise RuntimeError(f"Agent failed to generate a valid output (likely hit max iterations due to loop or missing tools). Please check the tools available. Original error: {e}")
            else:
                raise e
        last_output = str(result)

        # --- MEMORY-CENTRIC: Auto-save task output to ephemeral memory ---
        agent_role = task_obj.agent.role if task_obj.agent else "Unknown"
        _auto_save_to_memory(memory_manager, task_id, last_output, agent_role)

        # Handle Human Validation
        t_rec = workflow_tasks[i]
        if chat_id and t_rec.get('human_validation'):
            from core.master_ai import MasterAI
            from core.human_in_the_loop import request_human_input
            master_ai = MasterAI()
            
            logging.info(f"Task {task_id} requires human validation. Pausing execution.")
            question = master_ai.format_validation_request(last_output)
            user_feedback = request_human_input(chat_id, question)
            
            if user_feedback and user_feedback != "SYSTEM_ABORT":
                logging.info(f"Processing human feedback for task {task_id}...")
                last_output = master_ai.process_validation_feedback(last_output, user_feedback)
                # Overwrite memory with the human-edited output
                _auto_save_to_memory(memory_manager, task_id, last_output, f"{agent_role} (Human Edited)")

        # Persist to DB for resume support (fallback)
        task_outputs[str(task_id)] = last_output
        db.update_run(run_id, status='running', result=last_output, current_task_idx=i + 1, task_outputs=task_outputs)

    # --- Build global context dump for Master AI ---
    import json as _json
    all_records = memory_manager.dump_all_records()
    global_context = _json.dumps(all_records, indent=2, ensure_ascii=False)

    # Return both the last output (for the chat message) and the full context (for exports)
    return last_output, global_context

def build_dynamic_crew(plan: dict, default_model_id=None):
    """
    Builds a CrewAI Crew dynamically from a JSON plan generated by Master AI.
    Does not require database records for Agents or Tasks.
    """
    if not plan or 'agents' not in plan or 'tasks' not in plan:
        raise ValueError("Invalid plan format. Must contain 'agents' and 'tasks'.")
        
    # Pick a default model if not provided
    if not default_model_id:
        DataManager.load_env()
        env_model_id = os.getenv("DEFAULT_AGENT_MODEL_ID")
        if env_model_id:
            try:
                default_model_id = int(env_model_id)
            except ValueError:
                pass

    if not default_model_id:
        models = db.read_all_models()
        if models:
            default_model_id = models[0]['id']
        else:
            raise ValueError("No models found in the database. Please configure a model first.")
            
    llm_instance = _instantiate_llm(default_model_id)
    
    # Check if the default model is local
    model_record = db.read_model(default_model_id)
    is_local_model = False
    if model_record:
        is_local_model = bool(model_record.get('is_local')) or \
                         model_record.get('provider', '').lower() == 'ollama'

    agents_cache = {}
    actual_crew_agents = []
    agents_data_by_role = {a['role']: a for a in plan.get('agents', [])}
    
    conciseness_trait = ("\n\nCRITICAL TRAIT: You are extremely concise and data-driven. "
                         "You never ramble. You output structured bullet points, not essays. "
                         "Every sentence must carry unique, actionable information.")
        
    crew_tasks = []
    for task_data in plan['tasks']:
        agent_role = task_data.get('agent_role')
        specialization = task_data.get('agent_specialization')
        
        if agent_role not in agents_data_by_role:
            logging.warning(f"Task specifies unknown agent role '{agent_role}'.")
            agent_instance = None
        else:
            agent_info = agents_data_by_role[agent_role]
            
            # 1. Resolve agent's persona based on task specialization
            if specialization:
                base_role = agent_info['role']
                base_backstory = agent_info.get('backstory', '') or ""
                
                if "{specialization}" in base_role:
                    effective_role = base_role.replace("{specialization}", specialization)
                else:
                    effective_role = f"{base_role} specialized in {specialization}"
                
                if "{specialization}" in base_backstory:
                    effective_backstory = base_backstory.replace("{specialization}", specialization)
                else:
                    effective_backstory = (
                        base_backstory +
                        f"\n\nCRITICAL CONTEXT: For this specific task your area of expertise is "
                        f"focused on **{specialization}**. Apply all your base skills strictly within "
                        f"this specialized domain. Do not stray outside it."
                    )
                logging.info(f"Dynamic Agent '{agent_role}' specialized as: '{effective_role}'")
            else:
                # Clean up any leftover placeholders
                base_role = agent_info['role']
                base_backstory = agent_info.get('backstory', '') or ""
                effective_role = base_role.replace(" specialized in {specialization}", "").replace("{specialization}", "").strip()
                effective_backstory = base_backstory.replace("{specialization}", "").strip()

            # 2. Get or create agent instance
            if not specialization and agent_role in agents_cache:
                agent_instance = agents_cache[agent_role]
            else:
                # Strip tools if model is local
                if is_local_model:
                    agent_tools = []
                    logging.info(f"Dynamic Agent '{agent_role}' tools stripped (local model).")
                else:
                    agent_tools = _map_tools(agent_info.get('tools', []))
                    
                enhanced_backstory = effective_backstory + conciseness_trait
                
                agent_instance = Agent(
                    role=effective_role,
                    backstory=enhanced_backstory,
                    goal=agent_info.get('goal', ''),
                    llm=llm_instance,
                    tools=agent_tools,
                    verbose=True,
                    allow_delegation=False,
                    max_iter=5
                )
                
                if not specialization:
                    agents_cache[agent_role] = agent_instance
            
            if agent_instance not in actual_crew_agents:
                actual_crew_agents.append(agent_instance)
            
        task_tools = _get_task_tools(agent_info.get('tools', []), task_data.get('vector_dbs', []), is_local_model)

        task_description = task_data['description'] + AGENT_COMMS_DIRECTIVE

        # Enhance expected_output for structured format
        # Enhance expected_output for Vector DB structured format
        base_expected = task_data.get('expected_output', 'Task Output')
        vector_format_directive = (
            " FORMAT CRITERIA (For Vector DB): Begin with a clear '# Topic: <Subject>' header, "
            "a 1-line summary, a '[KEYWORDS: ...]' block, and then self-contained, noun-heavy bullet points."
        )
        if "vector" not in base_expected.lower() and "header" not in base_expected.lower():
            base_expected += vector_format_directive

        task = Task(
            description=task_description,
            expected_output=base_expected,
            agent=agent_instance,
            tools=task_tools,
            async_execution=False
        )
        crew_tasks.append(task)
        logging.info(f"Built Dynamic Task for Agent: {agent_role} (specialization={specialization})")
        
    if not actual_crew_agents:
        raise ValueError("No agents could be built from the dynamic plan.")
    if not crew_tasks:
        raise ValueError("No tasks could be built from the dynamic plan.")
        
    crew = Crew(
        agents=actual_crew_agents,
        tasks=crew_tasks,
        verbose=True,
        process='sequential'
    )
    
    logging.info("Successfully built Dynamic Crew!")
    return crew


def execute_dynamic_crew_with_memory(plan: dict, execution_context: dict = None, default_model_id=None, run_id: int = None, start_idx: int = 0, initial_task_outputs: dict = None, accumulated_context: str = None, chat_id: str = None) -> str:
    """
    Builds AND executes a dynamic crew from a JSON plan using memory-centric
    communication.  This is the counterpart of execute_run_with_resume for
    plans generated by the Chat Planner (not stored in the DB).

    Instead of running all tasks in a single Crew.kickoff() (which chains
    outputs via raw text), we run each task individually and mediate
    communication through the ephemeral ChromaDB store.
    """
    if not plan or 'agents' not in plan or 'tasks' not in plan:
        raise ValueError("Invalid plan format. Must contain 'agents' and 'tasks'.")

    execution_context = execution_context or {}

    # Resolve model
    if not default_model_id:
        DataManager.load_env()
        env_model_id = os.getenv("DEFAULT_AGENT_MODEL_ID")
        if env_model_id:
            try:
                default_model_id = int(env_model_id)
            except ValueError:
                pass

    if not default_model_id:
        models = db.read_all_models()
        if models:
            default_model_id = models[0]['id']
        else:
            raise ValueError("No models found in the database. Please configure a model first.")

    llm_instance = _instantiate_llm(default_model_id)

    model_record = db.read_model(default_model_id)
    is_local_model = False
    if model_record:
        is_local_model = bool(model_record.get('is_local')) or \
                         model_record.get('provider', '').lower() == 'ollama'

    agents_data_by_role = {a['role']: a for a in plan.get('agents', [])}
    conciseness_trait = ("\n\nCRITICAL TRAIT: You are extremely concise and data-driven. "
                         "You never ramble. You output structured bullet points, not essays. "
                         "Every sentence must carry unique, actionable information.")

    # --- MEMORY-CENTRIC: Initialise ephemeral memory (run_id=0 for dynamic runs) ---
    import time
    dynamic_run_id = int(time.time()) % 1_000_000  # Pseudo-unique ID
    memory_manager = EphemeralMemoryManager(run_id=dynamic_run_id)
    if accumulated_context:
        memory_manager.load_from_dump(accumulated_context)
    read_memory_tool = ReadAtomicMemoryTool(memory_manager=memory_manager)
    write_memory_tool = WriteAtomicMemoryTool(memory_manager=memory_manager)

    agents_cache = {}
    task_outputs = initial_task_outputs or {}
    last_output = ""
    if start_idx > 0 and str(start_idx - 1) in task_outputs:
        last_output = task_outputs[str(start_idx - 1)]

    for task_idx in range(start_idx, len(plan['tasks'])):
        task_data = plan['tasks'][task_idx]
        if run_id:
            db.update_run(run_id, status='running', current_task_idx=task_idx, task_outputs=task_outputs)

        agent_role = task_data.get('agent_role')
        specialization = task_data.get('agent_specialization')

        if agent_role not in agents_data_by_role:
            logging.warning(f"Task specifies unknown agent role '{agent_role}'. Skipping.")
            continue

        agent_info = agents_data_by_role[agent_role]

        # Resolve persona
        base_role = agent_info['role']
        base_backstory = agent_info.get('backstory', '') or ""
        base_goal = agent_info.get('goal', '') or ""

        if specialization:
            if "{specialization}" in base_role:
                effective_role = base_role.replace("{specialization}", specialization)
            else:
                effective_role = f"{base_role} specialized in {specialization}"
            
            if "{specialization}" in base_backstory:
                effective_backstory = base_backstory.replace("{specialization}", specialization)
            else:
                effective_backstory = (
                    base_backstory +
                    f"\n\nCRITICAL CONTEXT: For this specific task your area of expertise is "
                    f"focused on **{specialization}**."
                )

            if "{specialization}" in base_goal:
                effective_goal = base_goal.replace("{specialization}", specialization)
            else:
                effective_goal = f"{base_goal} (specialized in {specialization})"
        else:
            effective_role = base_role.replace(" specialized in {specialization}", "").replace("{specialization}", "").strip()
            effective_backstory = base_backstory.replace("{specialization}", "").strip()
            effective_goal = base_goal.replace("{specialization}", "").strip()

        # Safeguard against any other unreplaced variables
        effective_role = re.sub(r'\{([a-zA-Z0-9_]+)\}', r'<\1>', effective_role)
        effective_backstory = re.sub(r'\{([a-zA-Z0-9_]+)\}', r'<\1>', effective_backstory)
        effective_goal = re.sub(r'\{([a-zA-Z0-9_]+)\}', r'<\1>', effective_goal)

        # Build or reuse agent
        if not specialization and agent_role in agents_cache:
            agent_instance = agents_cache[agent_role]
        else:
            if is_local_model:
                agent_tools = []
            else:
                agent_tools = _map_tools(agent_info.get('tools', []))

            enhanced_backstory = effective_backstory + conciseness_trait
            agent_instance = Agent(
                role=effective_role,
                backstory=enhanced_backstory,
                goal=effective_goal,
                llm=llm_instance,
                tools=agent_tools,
                verbose=True,
                allow_delegation=False,
                max_iter=5
            )
            if not specialization:
                agents_cache[agent_role] = agent_instance

        # Build task
        # Combine tools from agent definition and task definition (Optimizer adds tools directly to the task)
        agent_t = agent_info.get('tools') or []
        task_t = task_data.get('tools') or []
        combined_tools = list(set(agent_t + task_t))
        task_tools = _get_task_tools(combined_tools, task_data.get('vector_dbs') or [], is_local_model)
        task_description = task_data['description'] + AGENT_COMMS_DIRECTIVE

        # Inject user inputs
        for k, v in execution_context.items():
            task_description = task_description.replace(f"{{{k}}}", str(v))
        task_description = task_description.replace("{user_input}", execution_context.get('user_input', ''))
        task_description = task_description.replace("{previous_result}", last_output)
        task_description = task_description.replace("{context}", last_output)

        # Append memory index for steps after the first
        if task_idx > 0:
            index_table = memory_manager.get_memory_index_table()
            task_description += (
                "\n\n--- [EPHEMERAL WORKSPACE MEMORY INDEX] ---\n"
                "Results from previous steps are stored in the ephemeral in-memory database.\n"
                "Use the 'read_atomic_memory' tool with the exact 'key' to retrieve data.\n"
                "Use 'write_atomic_memory' to store YOUR output for downstream agents.\n\n"
                f"{index_table}\n"
                "--- [END MEMORY INDEX] ---\n"
            )

        # Enhance expected_output for Vector DB structured format
        base_expected = task_data.get('expected_output', 'Task Output')
        vector_format_directive = (
            " FORMAT CRITERIA (For Vector DB): Begin with a clear '# Topic: <Subject>' header, "
            "a 1-line summary, a '[KEYWORDS: ...]' block, and then self-contained, noun-heavy bullet points."
        )
        if "vector" not in base_expected.lower() and "header" not in base_expected.lower():
            base_expected += vector_format_directive

        # Apply interpolations to expected_output
        for k, v in execution_context.items():
            base_expected = base_expected.replace(f"{{{k}}}", str(v))
        base_expected = base_expected.replace("{user_input}", execution_context.get('user_input', ''))
        base_expected = base_expected.replace("{previous_result}", last_output)
        base_expected = base_expected.replace("{context}", last_output)

        # Safeguard: Prevent CrewAI from crashing on unreplaced template variables
        task_description = re.sub(r'\{([a-zA-Z0-9_]+)\}', r'<\1>', task_description)
        base_expected = re.sub(r'\{([a-zA-Z0-9_]+)\}', r'<\1>', base_expected)

        task_obj = Task(
            description=task_description,
            expected_output=base_expected,
            agent=agent_instance,
            tools=task_tools,
            async_execution=False
        )

        # Inject memory tools
        _inject_memory_tools(agent_instance, task_obj, read_memory_tool, write_memory_tool)

        # Execute single-task crew
        single_crew = Crew(
            agents=[agent_instance],
            tasks=[task_obj],
            verbose=True,
            process='sequential'
        )

        try:
            result = single_crew.kickoff(inputs=execution_context)
        except Exception as e:
            err_str = str(e).lower()
            if "503" in err_str or "unavailable" in err_str or "rate" in err_str:
                logging.warning(f"Rate limit / 503 encountered at dynamic task {task_idx}: {e}")
                if run_id:
                    db.update_run(run_id, status='paused', result=str(e), current_task_idx=task_idx, task_outputs=task_outputs)
                raise RateLimitError(f"Model high demand error (503): {e}", task_idx, task_outputs)
            else:
                raise e
                
        last_output = str(result)

        # Auto-save to ephemeral memory
        key_name = f"dynamic_task_{task_idx}"
        summary_text = f"Output of dynamic task {task_idx + 1} by agent '{effective_role}': {last_output[:500]}"
        memory_manager.write_record(
            key=key_name,
            content_summary=summary_text,
            structured_data={"raw_output": last_output},
            agent_role=effective_role,
        )
        logging.info(f"[Memory-Centric] Dynamic task {task_idx + 1} completed by '{effective_role}'.")
        
        # Handle Human Validation for Dynamic Plan
        if chat_id and task_data.get('human_validation'):
            from core.master_ai import MasterAI
            from core.human_in_the_loop import request_human_input
            master_ai = MasterAI()
            
            logging.info(f"Dynamic task {task_idx + 1} requires human validation. Pausing execution.")
            question = master_ai.format_validation_request(last_output)
            user_feedback = request_human_input(chat_id, question)
            
            if user_feedback and user_feedback != "SYSTEM_ABORT":
                logging.info(f"Processing human feedback for dynamic task {task_idx + 1}...")
                last_output = master_ai.process_validation_feedback(last_output, user_feedback)
                # Overwrite memory with the human-edited output
                memory_manager.write_record(
                    key=key_name,
                    content_summary=f"Output of dynamic task {task_idx + 1} by agent '{effective_role} (Human Edited)': {last_output[:500]}",
                    structured_data={"raw_output": last_output},
                    agent_role=f"{effective_role} (Human Edited)",
                )
        
        task_outputs[str(task_idx)] = last_output
        if run_id:
            db.update_run(run_id, status='running', result=last_output, current_task_idx=task_idx + 1, task_outputs=task_outputs)

    # --- Build global context dump for Master AI ---
    import json as _json
    all_records = memory_manager.dump_all_records()
    global_context = _json.dumps(all_records, indent=2, ensure_ascii=False)

    return last_output, global_context

if __name__ == '__main__':
    # Example usage (for testing purposes)
    # This requires a populated database with models, agents, tasks, and a workflow.
    # Ensure your database.sqlite is set up and has data.
    # Ensure your .env has OPENAI_API_KEY if using OpenAI models.

    print("--- Testing Crew Builder ---")

    # Example: Assuming workflow_id=1 exists in your database
    # and it references valid agents and tasks.
    test_workflow_id = 1 

    try:
        test_crew = build_crew(test_workflow_id)
        print(f"\nCrew built successfully for workflow ID {test_workflow_id}!")
        print(f"Number of Agents: {len(test_crew.agents)}")
        for agent in test_crew.agents:
            print(f"  - Agent Role: {agent.role}, LLM: {agent.llm.__class__.__name__}, Tools: {[t.name for t in agent.tools]}")
        print(f"Number of Tasks: {len(test_crew.tasks)}")
        for task in test_crew.tasks:
            print(f"  - Task Description: {task.description[:50]}..., Agent: {task.agent.role}")
        
        # You can uncomment the following line to actually kick off the crew
        # print("\n--- Kicking off the crew ---")
        # result = test_crew.kickoff()
        # print("\n--- Crew execution finished ---")
        # print(result)

    except Exception as e:
        print(f"\nFailed to build or run crew: {e}")
        import traceback
        traceback.print_exc()

    print("\n--- End Testing Crew Builder ---")