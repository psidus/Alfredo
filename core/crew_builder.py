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

ABORT_FLAGS = {}

class ExecutionCancelledError(Exception):
    pass

def abort_crew_execution(chat_id: str):
    if chat_id:
        ABORT_FLAGS[str(chat_id)] = True

def check_abort(*args, **kwargs):
    chat_id = os.getenv("CURRENT_CHAT_ID")
    if chat_id and ABORT_FLAGS.get(str(chat_id)):
        raise ExecutionCancelledError("Execution aborted by user.")

from core.db_manager import DBManager
from core.data_manager import DataManager
from core.ephemeral_memory import EphemeralMemoryManager
import tools.local_tools as local_tools
import tools.terminal_executor as terminal_executor
import tools.office_tool as office_tool
import tools.email_tool as email_tool
import tools.thermo_excel_writer as thermo_excel_writer
import tools.vector_pagination_tool as vector_pagination_tool
import tools.thermo_excel_reader as thermo_excel_reader
import tools.workflow_trigger_tool as workflow_trigger_tool
from tools.ephemeral_memory_tool import ReadAtomicMemoryTool, WriteAtomicMemoryTool
from core.schema_loader import get_schema_class

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Headroom AI: Context Compression ---
# If HEADROOM_ENABLED=true in .env, compress prompts before CrewAI/LiteLLM calls.
# If HEADROOM_PROXY_URL is set, we route traffic to the proxy instead.
if os.getenv("HEADROOM_ENABLED", "").lower() == "true":
    proxy_url = os.getenv("HEADROOM_PROXY_URL")
    if proxy_url:
        os.environ["LITELLM_API_BASE"] = proxy_url
        os.environ["OPENAI_BASE_URL"] = proxy_url
        logging.info(f"Headroom AI Proxy Mode enabled for CrewAI at {proxy_url}")
    else:
        logging.info("Headroom AI inline compression enabled for CrewAI (note: inline compress is not currently wired up to CrewAI's internal LangChain LLMs, use proxy mode for full coverage).")


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
# Write tools are strictly sandboxed into the workspace/ folder.
ALLOWED_TOOLS = {
    # Workspace (sandboxed read & write)
    "read_file": local_tools.read_file,
    "write_file": local_tools.write_file,
    "write_python_file": local_tools.write_python_file,
    # Full PC (read-only)
    "read_file_anywhere": local_tools.read_file_anywhere,
    "search_files": local_tools.search_files,
    # Web & communication
    "search_web": local_tools.search_web,
    "ask_operator": local_tools.ask_operator,
    # Terminal
    "execute_shell_command": terminal_executor.execute_shell_command,
    # Office & Screenshot (Word/Excel generation requires human confirmation & sandboxing)
    "take_screenshot": office_tool.take_screenshot,
    "create_word_document": office_tool.create_word_document,
    "edit_word_document": office_tool.edit_word_document,
    "create_excel_document": office_tool.create_excel_document,
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
    # Code tools (read + execute)
    "python_repl_tool": local_tools.python_repl,
    # Thermodynamic tools
    "merge_and_save_data": thermo_excel_writer.merge_and_save_data,
    "read_rag_chunks": vector_pagination_tool.read_rag_chunks,
    "check_excel_db": thermo_excel_reader.check_excel_db,
    "trigger_next_batch": workflow_trigger_tool.trigger_next_batch,
    # Ephemeral Memory — sentinels, auto-injected at runtime with the correct manager
    "read_atomic_memory": None,   # Sentinel
    "write_atomic_memory": None,  # Sentinel
    # App-specific tools — sentinels, instantiated at runtime with app credentials
    # App-specific tools — sentinels, instantiated at runtime with app credentials
    "app_database_query": None,  # Sentinel
    "app_api_caller": None,      # Sentinel
}

# --- Load Custom Tools ---
import inspect
try:
    import tools.custom_tools as custom_tools
    for name, obj in inspect.getmembers(custom_tools):
        # CrewAI tools have name, description and run method
        if hasattr(obj, 'name') and hasattr(obj, 'description') and hasattr(obj, 'run') and not name.startswith('_'):
            if name not in ALLOWED_TOOLS:
                ALLOWED_TOOLS[name] = obj
                logging.info(f"Loaded custom tool: {name}")
except Exception as e:
    logging.error(f"Error loading custom tools: {e}")

def _instantiate_llm(model_id, task_record=None):
    """
    Securely creates an LLM string/object based on model_id from the database.
    Ensures API keys are injected into os.environ so that LiteLLM/CrewAI can find them.
    Also injects max_input_context and max_tokens if provided via task_record.
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
        "ollama": "OLLAMA_API_KEY",  # Supports Ollama Cloud (optional for local)
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

    # --- Extract Task Overrides ---
    max_input_context = task_record.get('max_input_context', 0) if task_record else 0
    max_output_tokens = task_record.get('max_output_tokens', 0) if task_record else 0
    
    # Build and return the LLM reference
    if provider == 'openai':
        if not os.getenv("OPENAI_API_KEY"):
            raise EnvironmentError("OPENAI_API_KEY is missing from .env.")
        # Lazy import so workflow execution doesn't hard-require LangChain packages
        # when running in minimal environments (e.g. Python 3.14 + CrewAI shim).
        from langchain_openai import ChatOpenAI  # type: ignore
        kwargs = {"model_name": model_name, "temperature": 0.7}
        if max_output_tokens > 0:
            kwargs["max_tokens"] = max_output_tokens
        return ChatOpenAI(**kwargs)
    elif provider == 'ollama':
        # If we have specific context sizes, we must instantiate via crewai.LLM
        if max_input_context > 0 or max_output_tokens > 0:
            try:
                from crewai import LLM
                kwargs = {
                    "model": f"ollama/{model_name}",
                    "base_url": os.getenv("OLLAMA_API_BASE", "http://localhost:11434")
                }
                if max_input_context > 0:
                    kwargs["num_ctx"] = max_input_context
                if max_output_tokens > 0:
                    kwargs["max_tokens"] = max_output_tokens
                return LLM(**kwargs)
            except ImportError:
                pass # fallback to string if LLM class not available
                
        # Prefer LiteLLM-style string for compatibility with the CrewAI shim.
        return f"ollama/{model_name}"
    else:
        # Standard LiteLLM format: provider/model_name (e.g. gemini/gemini-2.5-flash-lite)
        model_string = f"{provider}/{model_name}"
        if max_output_tokens > 0:
            try:
                from crewai import LLM
                return LLM(model=model_string, max_tokens=max_output_tokens)
            except ImportError:
                pass
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

def _get_task_tools(tool_names, vector_dbs, strip_tools=False, app_record=None):
    """
    Combines _map_tools with dynamic instantiation of VectorSearchTool and TabularQueryTool
    based on the provided vector_dbs list.
    """
    if strip_tools:
        return None
        
    task_tools = _map_tools(tool_names) or []
    
    if 'vector_search' in tool_names:
        if not vector_dbs:
            logging.warning("⚠️ CRITICAL: 'vector_search' tool was requested for a task, but NO Vector DBs were assigned to the task (vector_dbs list is empty). The agent will NOT have access to the RAG database!")
            
        # Lazy imports: these pull optional LangChain/Chroma dependencies.
        from tools.vector_search_tool import VectorSearchTool
        from tools.tabular_query_tool import TabularQueryTool
        for db_id in vector_dbs:
            try:
                db_id_int = int(db_id)
                vdb_record = db.cursor.execute("SELECT * FROM vector_databases WHERE id = %s", (db_id_int,)).fetchone()
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

    # --- App-specific tool instantiation ---
    if 'app_database_query' in tool_names and app_record:
        from tools.app_database_tool import AppDatabaseQueryTool
        db_env_key = app_record.get('db_env_key', '')
        db_conn = os.getenv(db_env_key, '') if db_env_key else ''
        if db_conn:
            tool_instance = AppDatabaseQueryTool(
                name=f"query_db_{app_record.get('name', 'app')}",
                description=f"Query the database of app '{app_record.get('display_name', app_record.get('name', 'External App'))}'. Use action='list_tables' first to discover available tables.",
                connection_string=db_conn,
                db_type=app_record.get('db_type', 'sqlite'),
                app_name=app_record.get('name', '')
            )
            task_tools.append(tool_instance)
            logging.info(f"Injected AppDatabaseQueryTool for app '{app_record.get('name')}'")
        else:
            logging.warning(f"App '{app_record.get('name')}' has no database connection configured (env key: '{db_env_key}')")

    if 'app_api_caller' in tool_names and app_record:
        from tools.app_api_tool import AppApiCallerTool
        api_env_key = app_record.get('api_env_key', '')
        api_key = os.getenv(api_env_key, '') if api_env_key else ''
        api_base_url = app_record.get('api_base_url', '')
        if api_base_url:
            tool_instance = AppApiCallerTool(
                name=f"call_api_{app_record.get('name', 'app')}",
                description=f"Call the REST API of app '{app_record.get('display_name', app_record.get('name', 'External App'))}'. Base URL: {api_base_url}",
                base_url=api_base_url,
                api_key=api_key,
                app_name=app_record.get('name', '')
            )
            task_tools.append(tool_instance)
            logging.info(f"Injected AppApiCallerTool for app '{app_record.get('name')}'")
        else:
            logging.warning(f"App '{app_record.get('name')}' has no API base URL configured")

    return task_tools if task_tools else None

def _build_agent(agent_id, specialization=None, model_id_override=None, task_record=None):
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
    llm_instance = _instantiate_llm(model_id, task_record=task_record) if model_id else None
    
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
        max_iter=5,
        step_callback=check_abort
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
    if specialization or task_model_id is not None or task_record.get('max_input_context', 0) > 0 or task_record.get('max_output_tokens', 0) > 0:
        agent_instance = _build_agent(agent_id, specialization=specialization, model_id_override=task_model_id, task_record=task_record)
    else:
        if agent_id not in agents_cache:
            agents_cache[agent_id] = _build_agent(agent_id)
        agent_instance = agents_cache[agent_id]
    
    # Check if the execution model is local — if so, strip task-level tools too
    agent_record = db.read_agent(agent_id) if agent_id else None
    execution_model_id = task_model_id if task_model_id is not None else (agent_record.get('model_id') if agent_record else None)
    model_record = db.read_model(execution_model_id) if execution_model_id else None
    supports_tools = bool(model_record.get('supports_tools', 1)) if model_record else True

    if not supports_tools:
        task_tools = None  # No tools for local models that don't support them
    else:
        task_tools = _get_task_tools(task_record.get('tools', []), task_record.get('vector_dbs', []), strip_tools=not supports_tools)

    # --- INTER-AGENT COMMUNICATION GUARDRAIL ---
    task_description = task_record['description'] + AGENT_COMMS_DIRECTIVE

    # --- LEARNING MEMORY INJECTION ---
    try:
        from core.learning_memory import get_learning_memory
        lm = get_learning_memory()
        learned_feedback = lm.get_relevant_feedback(task_record['description'])
        if learned_feedback:
            task_description += learned_feedback
            logging.info(f"Injected learning feedback into task {task_id}")
    except Exception as lm_err:
        logging.warning(f"Learning memory injection failed for task {task_id}: {lm_err}")

    # Enhance expected_output to enforce structured format
    # Enhance expected_output for Vector DB structured format
    base_expected = task_record['expected_output']
    vector_format_directive = (
        " FORMAT CRITERIA (For Vector DB): Begin with a clear '# Topic: <Subject>' header, "
        "a 1-line summary, a '[KEYWORDS: ...]' block, and then self-contained, noun-heavy bullet points."
    )
    
    task_tools_list = task_record.get('tools', [])
    if "write_atomic_memory" in task_tools_list and "vector" not in base_expected.lower() and "header" not in base_expected.lower():
        base_expected += vector_format_directive

    task = Task(
        description=task_description,
        expected_output=base_expected,
        agent=agent_instance,
        tools=task_tools,
        async_execution=('[ASYNC]' in task_record.get('name', ''))
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


def execute_run_with_resume(run_id: int, status_callback=None, accumulated_context: str = None, chat_id: str = None, on_flight_change=None) -> str:
    """
    Executes a workflow run task-by-task with MEMORY-CENTRIC communication.
    Now using a DAG Scheduler with VRAM-awareness.
    """
    import concurrent.futures
    import threading
    import os
    import json
    import re
    from dotenv import dotenv_values, find_dotenv

    if chat_id:
        ABORT_FLAGS[str(chat_id)] = False

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

    inputs = {}
    if run.get('inputs'):
        try:
            inputs = json.loads(run['inputs'])
        except Exception:
            pass

    task_outputs = {}
    if run.get('task_outputs'):
        try:
            task_outputs = json.loads(run['task_outputs'])
        except Exception:
            pass

    start_idx = run.get('current_task_idx', 0)
    db.update_run(run_id, status='running', current_task_idx=start_idx, task_outputs=task_outputs)

    # --- DAG NORMALIZATION ---
    dag_nodes = {}
    level_nodes = {}
    for i, step_def in enumerate(task_ids):
        node_id = f"node_{i}"
        depends_on = []
        is_batch = False
        task_id = None
        batch_tasks = []
        batch_size = 5
        source_variable = ""
        execution_level = 1
        
        if isinstance(step_def, int):
            task_id = step_def
            if i > 0: depends_on = [f"node_{i-1}"]
        elif isinstance(step_def, dict):
            node_id = step_def.get("id", f"node_{i}")
            depends_on = step_def.get("depends_on", [])
            execution_level = step_def.get("execution_level", 1)
            if "id" not in step_def: step_def["id"] = node_id
            
            if step_def.get("type") == "batch_loop":
                is_batch = True
                batch_tasks = step_def.get("task_ids", [])
                batch_size = step_def.get("batch_size", 5)
                source_variable = step_def.get("source_variable", "")
            else:
                task_id = step_def.get("task_id")
                
        if execution_level not in level_nodes:
            level_nodes[execution_level] = []
        level_nodes[execution_level].append(node_id)
                
        dag_nodes[node_id] = {
            "step_def": step_def,
            "task_id": task_id,
            "is_batch": is_batch,
            "batch_tasks": batch_tasks,
            "batch_size": batch_size,
            "source_variable": source_variable,
            "depends_on": depends_on,
            "execution_level": execution_level,
            "original_index": i
        }

    for node_id, data in dag_nodes.items():
        lvl = data["execution_level"]
        if lvl > 1 and not data["depends_on"]:
            prev_levels = [l for l in level_nodes.keys() if l < lvl]
            if prev_levels:
                max_prev_lvl = max(prev_levels)
                data["depends_on"] = list(level_nodes[max_prev_lvl])

    in_degree = {n: 0 for n in dag_nodes}
    dependents = {n: [] for n in dag_nodes}
    
    for n_id, data in dag_nodes.items():
        for d in data["depends_on"]:
            if d in dag_nodes:
                in_degree[n_id] += 1
                dependents[d].append(n_id)

    # Memory init
    memory_manager = EphemeralMemoryManager(run_id=run_id)
    if accumulated_context:
        memory_manager.load_from_dump(accumulated_context)
    read_memory_tool = ReadAtomicMemoryTool(memory_manager=memory_manager)
    write_memory_tool = WriteAtomicMemoryTool(memory_manager=memory_manager)

    completed_nodes = set()
    node_outputs = {}
    for n_id, data in dag_nodes.items():
        if data["is_batch"]:
            last_inner = data["batch_tasks"][-1] if data["batch_tasks"] else None
            if last_inner and str(last_inner) in task_outputs:
                completed_nodes.add(n_id)
                node_outputs[n_id] = task_outputs[str(last_inner)]
        else:
            if str(data["task_id"]) in task_outputs:
                completed_nodes.add(n_id)
                node_outputs[n_id] = task_outputs[str(data["task_id"])]

    for n_id in completed_nodes:
        data = dag_nodes[n_id]
        if data["is_batch"]:
            for b_tid in data["batch_tasks"]:
                if str(b_tid) in task_outputs:
                    _auto_save_to_memory(memory_manager, b_tid, task_outputs[str(b_tid)], "Unknown")
        else:
            tid = data["task_id"]
            if str(tid) in task_outputs:
                _auto_save_to_memory(memory_manager, tid, task_outputs[str(tid)], "Unknown")
                
        for dep in dependents[n_id]:
            in_degree[dep] -= 1

    agents_cache = {}
    task_outputs_lock = threading.Lock()
    
    def _execute_task_instance(task_id, current_inputs, log_msg, task_idx=None, parent_output=""):
        task_obj = _build_task(task_id, agents_cache)
        _inject_memory_tools(task_obj.agent, task_obj, read_memory_tool, write_memory_tool)

        def normalize_name(s: str) -> str:
            if not s: return ""
            s_norm = s.lower().replace('_', ' ').replace('-', ' ')
            return " ".join(s_norm.split())

        workflow_tasks = []
        for step in task_ids:
            tids = step.get('task_ids', []) if isinstance(step, dict) and step.get('type') == 'batch_loop' else [step if isinstance(step, int) else step.get('task_id')]
            for tid in tids:
                try:
                    t_rec = db.read_task(tid)
                    if t_rec: workflow_tasks.append(t_rec)
                    else: workflow_tasks.append({'id': tid, 'name': None, 'description': '', 'agent_id': None})
                except Exception:
                    workflow_tasks.append({'id': tid, 'name': None, 'description': '', 'agent_id': None})

        lookup = {}
        with task_outputs_lock:
            for t_rec in workflow_tasks:
                tid = t_rec['id']
                t_name = t_rec.get('name')
                t_out = task_outputs.get(str(tid), "")
                if t_out:
                    if t_name:
                        norm = normalize_name(t_name)
                        if norm: lookup[norm] = t_out
                    lookup[f"task {tid}"] = t_out
                    lookup[f"task_{tid}"] = t_out
                    lookup[str(tid)] = t_out

        lookup["previous task"] = parent_output
        lookup["previous_task"] = parent_output
        lookup["previous"] = parent_output
        lookup["task precedente"] = parent_output
        lookup["task_precedente"] = parent_output

        pattern = re.compile(r'\{task:([^\}]+)\}|\{([^\}]+)\}')
        def repl(match):
            g1 = match.group(1)
            g2 = match.group(2)
            key = g1 if g1 is not None else g2
            if not key: return match.group(0)
            norm_key = normalize_name(key)
            if norm_key in lookup: return lookup[norm_key]
            lower_key = key.strip().lower()
            if lower_key in lookup: return lookup[lower_key]
            return match.group(0)

        def apply_interpolation(text: str) -> str:
            if not text: return text
            for k, v in current_inputs.items():
                text = text.replace(f"{{{k}}}", str(v))
            text = text.replace("{user_input}", current_inputs.get('user_input', ''))
            text = text.replace("{previous_result}", parent_output)
            text = text.replace("{context}", parent_output)
            text = text.replace("{flexible_input}", current_inputs.get('user_input', parent_output))
            text = pattern.sub(repl, text)
            text = re.sub(r'\{([a-zA-Z0-9_]+)\}', r'<>', text)
            return text

        original_desc = apply_interpolation(task_obj.description)
        original_expected = apply_interpolation(task_obj.expected_output)

        index_table = memory_manager.get_memory_index_table()
        if "EPHEMERAL WORKSPACE MEMORY INDEX" not in index_table:
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

        if status_callback and task_idx is not None:
            try:
                agent_role = task_obj.agent.role if task_obj.agent else "Unknown"
                status_callback(task_idx, len(task_ids), agent_role, "running")
            except Exception:
                pass

        single_task_crew = Crew(agents=[task_obj.agent], tasks=[task_obj], verbose=True, process='sequential')

        try:
            result = single_task_crew.kickoff()
        except Exception as e:
            err_str = str(e).lower()
            if "503" in err_str or "unavailable" in err_str or "rate" in err_str:
                logging.warning(f"Rate limit / 503 encountered: {e}")
                raise RateLimitError(f"Model high demand error (503): {e}", 0, task_outputs)
            elif "none or empty" in err_str:
                raise RuntimeError(f"Agent failed to generate a valid output: {e}")
            else:
                raise e
        
        task_out = str(result)
        agent_role = task_obj.agent.role if task_obj.agent else "Unknown"
        _auto_save_to_memory(memory_manager, task_id, task_out, agent_role)

        t_rec = db.read_task(task_id)
        if t_rec and chat_id and t_rec.get('human_validation'):
            from core.master_ai import MasterAI
            from core.human_in_the_loop import request_human_input
            master_ai = MasterAI()
            question, options = master_ai.format_validation_request(
                task_out, 
                task_description=t_rec.get('description', ''), 
                expected_output=t_rec.get('expected_output', '')
            )
            user_feedback = request_human_input(chat_id, question, options=options, task_id=f"task_{task_id}")
            if user_feedback and user_feedback != "SYSTEM_ABORT":
                task_out = master_ai.process_validation_feedback(task_out, user_feedback)
                _auto_save_to_memory(memory_manager, task_id, task_out, f"{agent_role} (Human Edited)")

        with task_outputs_lock:
            task_outputs[str(task_id)] = task_out
            db.update_run(run_id, status='running', result=task_out, current_task_idx=task_idx + 1 if task_idx is not None else 0, task_outputs=task_outputs)
            
        return task_out

    # --- VRAM Management ---
    env_path = find_dotenv() or os.path.join(os.getcwd(), '.env')
    env_vars = dotenv_values(env_path)
    try:
        MAX_VRAM_GB = float(env_vars.get("MAX_VRAM_GB", 24.0))
    except Exception:
        MAX_VRAM_GB = 24.0

    current_vram_usage = 0.0
    vram_lock = threading.Lock()
    vram_condition = threading.Condition(vram_lock)
    
    def get_task_vram_cost(t_id):
        try:
            t = db.read_task(t_id)
            if not t: return 0.0
            m_id = t.get('model_id')
            if not m_id:
                a = db.read_agent(t.get('agent_id'))
                if a: m_id = a.get('model_id')
            if m_id:
                m = db.read_model(m_id)
                if m:
                    is_local = bool(m.get('is_local')) or m.get('provider', '').lower() == 'ollama'
                    if is_local:
                        return float(m.get('vram_gb') or 0.0)
        except Exception:
            pass
        return 0.0

    ready_queue = [n for n, deg in in_degree.items() if deg == 0 and n not in completed_nodes]
    in_flight = set()
    
    def execute_node(n_id):
        nonlocal current_vram_usage
        data = dag_nodes[n_id]
        
        cost = 0.0
        if data["is_batch"]:
            cost = max([get_task_vram_cost(tid) for tid in data["batch_tasks"]], default=0.0)
        else:
            cost = get_task_vram_cost(data["task_id"])
            
        with vram_condition:
            while current_vram_usage + cost > MAX_VRAM_GB and (current_vram_usage > 0 or cost > MAX_VRAM_GB):
                if current_vram_usage == 0 and cost > MAX_VRAM_GB:
                    break
                vram_condition.wait()
            current_vram_usage += cost

        try:
            parent_output = ""
            if data["depends_on"]:
                parent_id = data["depends_on"][-1]
                with task_outputs_lock:
                    parent_output = node_outputs.get(parent_id, "")
                
            if not data["is_batch"]:
                log_msg = f"Executing Node {n_id} (Task {data['task_id']})..."
                out = _execute_task_instance(data["task_id"], inputs, log_msg, task_idx=data["original_index"], parent_output=parent_output)
                node_outputs[n_id] = out
                return out
            else:
                batch_tasks = data["batch_tasks"]
                batch_size = int(data.get('batch_size', 5))
                
                data_str = parent_output
                if data["source_variable"]:
                    # placeholder logic if source is a variable
                    pass
                
                try:
                    json_match = re.search(r'\[.*\]', data_str, re.DOTALL)
                    if json_match:
                        items = json.loads(json_match.group(0))
                    else:
                        items = json.loads(data_str)
                    if not isinstance(items, list):
                        raise ValueError("Extracted data is not a JSON array")
                except Exception as e:
                    logging.error(f"Failed to parse source data for batch loop: {e}")
                    items = [{"raw_data": data_str}]
                
                batch_out = ""
                for batch_start in range(0, len(items), batch_size):
                    batch_chunk = items[batch_start:batch_start+batch_size]
                    chunk_str = json.dumps(batch_chunk)
                    
                    batch_inputs = inputs.copy()
                    batch_inputs['current_batch'] = chunk_str
                    
                    memory_manager.clear_memory()
                    
                    for b_idx, inner_task_id in enumerate(batch_tasks):
                        log_msg = f"Executing Batch Loop (Node {n_id}) - Chunk {batch_start//batch_size + 1} - Inner Task {b_idx+1}/{len(batch_tasks)} (ID: {inner_task_id})"
                        batch_out = _execute_task_instance(inner_task_id, batch_inputs, log_msg, task_idx=data["original_index"], parent_output=batch_out)
                
                node_outputs[n_id] = batch_out
                return batch_out
        finally:
            with vram_condition:
                current_vram_usage -= cost
                vram_condition.notify_all()

    last_output_overall = ""

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {}
        
        def _update_in_flight_db():
            if not run_id: return
            names = []
            for nid in in_flight:
                step = dag_nodes[nid].get("step_def")
                if isinstance(step, dict):
                    names.append(step.get("name") or step.get("description", "")[:30])
                else:
                    names.append(f"Task {dag_nodes[nid].get('task_id', nid)}")
            try:
                db.update_run(run_id, status='running', in_flight_tasks=names)
                if on_flight_change:
                    on_flight_change(names)
            except Exception as e:
                logging.error(f"Failed to update in_flight_tasks: {e}")

        while ready_queue or in_flight:
            if ready_queue:
                for n_id in ready_queue:
                    futures[executor.submit(execute_node, n_id)] = n_id
                    in_flight.add(n_id)
                ready_queue.clear()
                _update_in_flight_db()
            
            if not in_flight:
                break
                
            done, not_done = concurrent.futures.wait(futures.keys(), return_when=concurrent.futures.FIRST_COMPLETED)
            
            for f in done:
                n_id = futures.pop(f)
                in_flight.remove(n_id)
                _update_in_flight_db()
                
                try:
                    res = f.result()
                    if res:
                        last_output_overall = res
                except Exception as e:
                    logging.error(f"Node {n_id} failed: {e}")
                    raise
                    
                completed_nodes.add(n_id)
                for dep in dependents[n_id]:
                    in_degree[dep] -= 1
                    if in_degree[dep] == 0:
                        ready_queue.append(dep)

    all_records = memory_manager.dump_all_records()
    global_context = json.dumps(all_records, indent=2, ensure_ascii=False)

    return last_output_overall, global_context

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
    supports_tools = bool(model_record.get('supports_tools', 1)) if model_record else True

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
            logging.warning(f"Task specifies unknown agent role '{agent_role}'. Skipping task.")
            continue
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
                # Strip tools if model doesn't support them
                if not supports_tools:
                    agent_tools = []
                    logging.info(f"Dynamic Agent '{agent_role}' tools stripped (model doesn't support tools).")
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
                    max_iter=5,
                    step_callback=check_abort
                )
                
                if not specialization:
                    agents_cache[agent_role] = agent_instance
            
            if agent_instance not in actual_crew_agents:
                actual_crew_agents.append(agent_instance)
            
        task_tools = _get_task_tools(agent_info.get('tools', []), task_data.get('vector_dbs', []), strip_tools=not supports_tools)

        task_description = task_data['description'] + AGENT_COMMS_DIRECTIVE

        # --- LEARNING MEMORY INJECTION ---
        try:
            from core.learning_memory import get_learning_memory
            lm = get_learning_memory()
            learned_feedback = lm.get_relevant_feedback(task_data['description'])
            if learned_feedback:
                task_description += learned_feedback
                logging.info(f"Injected learning feedback into dynamic task for agent {agent_role}")
        except Exception as lm_err:
            logging.warning(f"Learning memory injection failed: {lm_err}")

        # Enhance expected_output for Vector DB structured format
        base_expected = task_data.get('expected_output', 'Task Output')
        vector_format_directive = (
            " FORMAT CRITERIA (For Vector DB): Begin with a clear '# Topic: <Subject>' header, "
            "a 1-line summary, a '[KEYWORDS: ...]' block, and then self-contained, noun-heavy bullet points."
        )
        task_tools_list = agent_info.get('tools', [])
        if "write_atomic_memory" in task_tools_list and "vector" not in base_expected.lower() and "header" not in base_expected.lower():
            base_expected += vector_format_directive

        kwargs = {}
        if task_data.get('output_pydantic'):
            pydantic_str = task_data.get('output_pydantic')
            schemas = [s.strip() for s in pydantic_str.split(',') if s.strip()]
            if len(schemas) == 1:
                cls = get_schema_class(schemas[0])
                if cls:
                    kwargs['output_pydantic'] = cls
            elif len(schemas) > 1:
                from pydantic import create_model
                fields = {}
                for s in schemas:
                    cls = get_schema_class(s)
                    if cls:
                        fields[s.lower()] = (cls, ...)
                if fields:
                    DynamicModel = create_model('DynamicOutputSchema', **fields)
                    kwargs['output_pydantic'] = DynamicModel

        task = Task(
            description=task_description,
            expected_output=base_expected,
            agent=agent_instance,
            tools=task_tools,
            async_execution=('[ASYNC]' in task_data.get('name', '')),
            **kwargs
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


def execute_dynamic_crew_with_memory(plan: dict, execution_context: dict = None, default_model_id=None, run_id: int = None, start_idx: int = 0, initial_task_outputs: dict = None, accumulated_context: str = None, chat_id: str = None, progress_callback=None, on_flight_change=None) -> str:
    if not plan or 'agents' not in plan or 'tasks' not in plan:
        raise ValueError("Invalid plan format. Must contain 'agents' and 'tasks'.")

    import concurrent.futures
    import threading
    import os
    import json
    import re
    import time
    from dotenv import dotenv_values, find_dotenv

    if chat_id:
        ABORT_FLAGS[str(chat_id)] = False

    execution_context = execution_context or {}

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
            raise ValueError("No models found in the database.")

    llm_instance = _instantiate_llm(default_model_id)
    model_record = db.read_model(default_model_id)
    supports_tools = bool(model_record.get('supports_tools', 1)) if model_record else True

    agents_data_by_role = {a['role']: a for a in plan.get('agents', [])}
    conciseness_trait = ("\n\nCRITICAL TRAIT: You are extremely concise and data-driven. "
                         "You never ramble. You output structured bullet points, not essays. "
                         "Every sentence must carry unique, actionable information.")

    dynamic_run_id = int(time.time()) % 1_000_000
    memory_manager = EphemeralMemoryManager(run_id=dynamic_run_id)
    if accumulated_context:
        memory_manager.load_from_dump(accumulated_context)
    read_memory_tool = ReadAtomicMemoryTool(memory_manager=memory_manager)
    write_memory_tool = WriteAtomicMemoryTool(memory_manager=memory_manager)

    agents_cache = {}
    task_outputs = initial_task_outputs or {}

    # Normalize DAG for dynamic plan
    dag_nodes = {}
    level_nodes = {}
    tasks = plan['tasks']
    for i, task_data in enumerate(tasks):
        node_id = task_data.get("id", f"node_{i}")
        depends_on = task_data.get("depends_on", [])
        execution_level = task_data.get("execution_level", 1)
        if "id" not in task_data:
            # Legacy tasks without ID keep sequential dependency if empty
            if i > 0 and not depends_on:
                depends_on = [f"node_{i-1}"]
            
        if execution_level not in level_nodes:
            level_nodes[execution_level] = []
        level_nodes[execution_level].append(node_id)
            
        dag_nodes[node_id] = {
            "task_data": task_data,
            "depends_on": depends_on,
            "execution_level": execution_level,
            "original_index": i
        }

    for node_id, data in dag_nodes.items():
        lvl = data["execution_level"]
        if lvl > 1 and not data["depends_on"]:
            prev_levels = [l for l in level_nodes.keys() if l < lvl]
            if prev_levels:
                max_prev_lvl = max(prev_levels)
                data["depends_on"] = list(level_nodes[max_prev_lvl])

    in_degree = {n: 0 for n in dag_nodes}
    dependents = {n: [] for n in dag_nodes}
    for n_id, data in dag_nodes.items():
        for d in data["depends_on"]:
            if d in dag_nodes:
                in_degree[n_id] += 1
                dependents[d].append(n_id)

    completed_nodes = set()
    node_outputs = {}
    for n_id, data in dag_nodes.items():
        idx = data["original_index"]
        if str(idx) in task_outputs:
            completed_nodes.add(n_id)
            node_outputs[n_id] = task_outputs[str(idx)]
            
    for n_id in completed_nodes:
        for dep in dependents[n_id]:
            in_degree[dep] -= 1

    env_path = find_dotenv() or os.path.join(os.getcwd(), '.env')
    env_vars = dotenv_values(env_path)
    try:
        MAX_VRAM_GB = float(env_vars.get("MAX_VRAM_GB", 24.0))
    except Exception:
        MAX_VRAM_GB = 24.0

    current_vram_usage = 0.0
    vram_lock = threading.Lock()
    vram_condition = threading.Condition(vram_lock)
    task_outputs_lock = threading.Lock()
    
    def get_dynamic_task_vram_cost():
        # Dynamic agents usually share the default model
        if model_record and not supports_tools:
            return float(model_record.get('vram_gb') or 0.0)
        return 0.0

    ready_queue = [n for n, deg in in_degree.items() if deg == 0 and n not in completed_nodes]
    in_flight = set()

    def execute_dynamic_node(n_id):
        nonlocal current_vram_usage
        data = dag_nodes[n_id]
        task_data = data["task_data"]
        task_idx = data["original_index"]
        
        cost = get_dynamic_task_vram_cost()
        
        with vram_condition:
            while current_vram_usage + cost > MAX_VRAM_GB and (current_vram_usage > 0 or cost > MAX_VRAM_GB):
                if current_vram_usage == 0 and cost > MAX_VRAM_GB:
                    break
                vram_condition.wait()
            current_vram_usage += cost

        try:
            parent_output = ""
            if data["depends_on"]:
                parent_id = data["depends_on"][-1]
                with task_outputs_lock:
                    parent_output = node_outputs.get(parent_id, "")

            if run_id:
                db.update_run(run_id, status='running', current_task_idx=task_idx, task_outputs=task_outputs)

            agent_role = task_data.get('agent_role')
            specialization = task_data.get('agent_specialization')

            if agent_role not in agents_data_by_role:
                logging.warning(f"Task specifies unknown agent role '{agent_role}'. Skipping.")
                return ""

            agent_info = agents_data_by_role[agent_role]
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
                    effective_backstory = base_backstory + f"\n\nCRITICAL CONTEXT: Your expertise is focused on **{specialization}**."

                if "{specialization}" in base_goal:
                    effective_goal = base_goal.replace("{specialization}", specialization)
                else:
                    effective_goal = f"{base_goal} (specialized in {specialization})"
            else:
                effective_role = base_role.replace(" specialized in {specialization}", "").replace("{specialization}", "").strip()
                effective_backstory = base_backstory.replace("{specialization}", "").strip()
                effective_goal = base_goal.replace("{specialization}", "").strip()

            effective_role = re.sub(r'\{([a-zA-Z0-9_]+)\}', r'<\1>', effective_role)
            effective_backstory = re.sub(r'\{([a-zA-Z0-9_]+)\}', r'<\1>', effective_backstory)
            effective_goal = re.sub(r'\{([a-zA-Z0-9_]+)\}', r'<\1>', effective_goal)

            if not specialization and agent_role in agents_cache:
                agent_instance = agents_cache[agent_role]
            else:
                agent_tools = [] if not supports_tools else _map_tools(agent_info.get('tools', []))
                enhanced_backstory = effective_backstory + conciseness_trait
                agent_instance = Agent(
                    role=effective_role,
                    backstory=enhanced_backstory,
                    goal=effective_goal,
                    llm=llm_instance,
                    tools=agent_tools,
                    verbose=True,
                    allow_delegation=False,
                    max_iter=5,
                    step_callback=check_abort
                )
                if not specialization:
                    agents_cache[agent_role] = agent_instance

            combined_tools = list(set((agent_info.get('tools') or []) + (task_data.get('tools') or [])))
            # LEVEL-BASED MEMORY ARCHITECTURE:
            # If execution_level > 1, auto-inject read_atomic_memory so the agent can read ANY previous level's data.
            # Also auto-inject write_atomic_memory for all tasks so they can persist their output.
            if data["execution_level"] > 1 and "read_atomic_memory" not in combined_tools:
                combined_tools.append("read_atomic_memory")
            if "write_atomic_memory" not in combined_tools:
                combined_tools.append("write_atomic_memory")
                
            task_tools = _get_task_tools(combined_tools, task_data.get('vector_dbs') or [], strip_tools=not supports_tools)
            task_description = task_data['description'] + AGENT_COMMS_DIRECTIVE

            for k, v in execution_context.items():
                task_description = task_description.replace(f"{{{k}}}", str(v))
            task_description = task_description.replace("{user_input}", execution_context.get('user_input', ''))
            task_description = task_description.replace("{previous_result}", parent_output)
            task_description = task_description.replace("{context}", parent_output)

            index_table = memory_manager.get_memory_index_table()
            task_description += (
                "\n\n--- [EPHEMERAL WORKSPACE MEMORY INDEX] ---\n"
                "Results from previous steps are stored in the ephemeral in-memory database.\n"
                "Use the 'read_atomic_memory' tool with the exact 'key' to retrieve data.\n"
                "Use 'write_atomic_memory' to store YOUR output for downstream agents.\n\n"
                f"{index_table}\n"
                "--- [END MEMORY INDEX] ---\n"
            )

            base_expected = task_data.get('expected_output', 'Task Output')
            vector_format_directive = " FORMAT CRITERIA (For Vector DB): Begin with a clear '# Topic: <Subject>' header, a 1-line summary, a '[KEYWORDS: ...]' block, and then self-contained, noun-heavy bullet points."
            if "write_atomic_memory" in combined_tools and "vector" not in base_expected.lower() and "header" not in base_expected.lower():
                base_expected += vector_format_directive

            for k, v in execution_context.items():
                base_expected = base_expected.replace(f"{{{k}}}", str(v))
            base_expected = base_expected.replace("{user_input}", execution_context.get('user_input', ''))
            base_expected = base_expected.replace("{previous_result}", parent_output)
            base_expected = base_expected.replace("{context}", parent_output)

            task_description = re.sub(r'\{([a-zA-Z0-9_]+)\}', r'<\1>', task_description)
            base_expected = re.sub(r'\{([a-zA-Z0-9_]+)\}', r'<\1>', base_expected)

            kwargs = {}
            if task_data.get('output_pydantic'):
                pydantic_str = task_data.get('output_pydantic')
                schemas = [s.strip() for s in pydantic_str.split(',') if s.strip()]
                if len(schemas) == 1:
                    cls = get_schema_class(schemas[0])
                    if cls:
                        kwargs['output_pydantic'] = cls
                elif len(schemas) > 1:
                    from pydantic import create_model
                    fields = {}
                    for s in schemas:
                        cls = get_schema_class(s)
                        if cls:
                            fields[s.lower()] = (cls, ...)
                    if fields:
                        DynamicModel = create_model('DynamicOutputSchema', **fields)
                        kwargs['output_pydantic'] = DynamicModel

            task_obj = Task(
                description=task_description,
                expected_output=base_expected,
                agent=agent_instance,
                tools=task_tools,
                async_execution=('[ASYNC]' in task_data.get('name', '')),
                **kwargs
            )

            _inject_memory_tools(agent_instance, task_obj, read_memory_tool, write_memory_tool)

            single_crew = Crew(
                agents=[agent_instance],
                tasks=[task_obj],
                verbose=True,
                process='sequential'
            )

            if progress_callback:
                try:
                    progress_callback(task_idx, len(plan['tasks']), effective_role, "running")
                except Exception:
                    pass

            max_retries = 2
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    if attempt > 0:
                        time.sleep(10 * attempt)
                        single_crew = Crew(agents=[agent_instance], tasks=[task_obj], verbose=True, process='sequential')
                    result = single_crew.kickoff(inputs=execution_context)
                    last_exception = None
                    break
                except Exception as e:
                    last_exception = e
                    err_str = str(e).lower()
                    is_transient = "503" in err_str or "unavailable" in err_str or "rate" in err_str or "empty" in err_str or "none" in err_str or "model output" in err_str or "resource" in err_str or "overloaded" in err_str
                    if is_transient and attempt < max_retries:
                        continue
                    elif is_transient:
                        if run_id: db.update_run(run_id, status='paused', result=str(e), current_task_idx=task_idx, task_outputs=task_outputs)
                        raise RateLimitError(f"Model transient error after retries: {e}", task_idx, task_outputs)
                    else:
                        raise e

            if last_exception is not None:
                raise last_exception
                    
            task_out = str(result)
            key_name = f"dynamic_task_{task_idx}"
            summary_text = f"Output of dynamic task {task_idx + 1} by agent '{effective_role}': {task_out[:500]}"
            memory_manager.write_record(key=key_name, content_summary=summary_text, structured_data={"raw_output": task_out}, agent_role=effective_role)

            if chat_id and task_data.get('human_validation'):
                from core.master_ai import MasterAI
                from core.human_in_the_loop import request_human_input
                master_ai = MasterAI()
                question, options = master_ai.format_validation_request(task_out, task_description=task_data.get('description', ''), expected_output=task_data.get('expected_output', ''))
                user_feedback = request_human_input(chat_id, question, options=options, task_id=f"dynamic_task_{task_idx}")
                
                if user_feedback and user_feedback != "SYSTEM_ABORT":
                    task_out = master_ai.process_validation_feedback(task_out, user_feedback)
                    memory_manager.write_record(key=key_name, content_summary=f"Output of dynamic task {task_idx + 1} by agent '{effective_role} (Human Edited)': {task_out[:500]}", structured_data={"raw_output": task_out}, agent_role=f"{effective_role} (Human Edited)")
            
            if progress_callback:
                try:
                    progress_callback(task_idx, len(plan['tasks']), effective_role, "completed")
                except Exception:
                    pass
            
            with task_outputs_lock:
                task_outputs[str(task_idx)] = task_out
                if run_id:
                    db.update_run(run_id, status='running', result=task_out, current_task_idx=task_idx + 1, task_outputs=task_outputs)

            node_outputs[n_id] = task_out
            return task_out
        finally:
            with vram_condition:
                current_vram_usage -= cost
                vram_condition.notify_all()

    last_output_overall = ""

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {}
        
        def _update_in_flight_db():
            if not run_id: return
            names = []
            for nid in in_flight:
                tdata = dag_nodes[nid]["task_data"]
                names.append(tdata.get("name") or tdata.get("description", "")[:30])
            try:
                db.update_run(run_id, status='running', in_flight_tasks=names)
                if on_flight_change:
                    on_flight_change(names)
            except Exception as e:
                logging.error(f"Failed to update in_flight_tasks: {e}")

        while ready_queue or in_flight:
            if ready_queue:
                for n_id in ready_queue:
                    futures[executor.submit(execute_dynamic_node, n_id)] = n_id
                    in_flight.add(n_id)
                ready_queue.clear()
                _update_in_flight_db()
            
            if not in_flight:
                break
                
            done, not_done = concurrent.futures.wait(futures.keys(), return_when=concurrent.futures.FIRST_COMPLETED)
            
            for f in done:
                n_id = futures.pop(f)
                in_flight.remove(n_id)
                _update_in_flight_db()
                
                try:
                    res = f.result()
                    if res:
                        last_output_overall = res
                except Exception as e:
                    logging.error(f"Node {n_id} failed: {e}")
                    raise
                    
                completed_nodes.add(n_id)
                for dep in dependents[n_id]:
                    in_degree[dep] -= 1
                    if in_degree[dep] == 0:
                        ready_queue.append(dep)

    all_records = memory_manager.dump_all_records()
    global_context = json.dumps(all_records, indent=2, ensure_ascii=False)

    return last_output_overall, global_context



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