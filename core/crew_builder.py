import json
import os
import logging

from crewai import Agent, Task, Crew
from langchain_openai import ChatOpenAI
from langchain_community.llms import Ollama

from core.db_manager import DBManager
from core.data_manager import DataManager
import tools.local_tools as local_tools
import tools.terminal_executor as terminal_executor
import tools.office_tool as office_tool
import tools.email_tool as email_tool
from tools.vector_search_tool import VectorSearchTool
from tools.tabular_query_tool import TabularQueryTool

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Initialize DB
db = DBManager()

# --- GLOBAL INTER-AGENT COMMUNICATION DIRECTIVE ---
# This is appended to EVERY task to enforce concise, structured outputs
# optimized for AI-to-AI communication (not human-readable fluff).
AGENT_COMMS_DIRECTIVE = """

--- COMMUNICATION PROTOCOL ---
You are part of a sequential AI agent pipeline. Your output will be read by the NEXT AI agent, not a human.
RULES:
1. Output ONLY the essential findings as a structured list.
2. Use bullet points (•) for each key finding.
3. Start with a one-line SUMMARY of your conclusion.
4. Maximum 10 bullet points. Prioritize by relevance.
5. NO preamble, NO "In conclusion...", NO filler phrases.
6. If you reference data, cite it inline (source, number, date).
"""

# --- Security Helper: Hardcoded Tool Registry ---
# Define a strict, immutable mapping of allowed tools to prevent injection attacks.
ALLOWED_TOOLS = {
    # Workspace (sandboxed)
    "read_file": local_tools.read_file,
    "write_file": local_tools.write_file,
    # Full PC (read-only)
    "read_file_anywhere": local_tools.read_file_anywhere,
    "search_files": local_tools.search_files,
    # Web & communication
    "search_web": local_tools.search_web,
    "ask_operator": local_tools.ask_operator,
    # Terminal
    "execute_shell_command": terminal_executor.execute_shell_command,
    # Office (write = confirmation required)
    "create_word_document": office_tool.create_word_document,
    "edit_word_document": office_tool.edit_word_document,
    "create_excel_document": office_tool.create_excel_document,
    # Screenshot
    "take_screenshot": office_tool.take_screenshot,
    # Email (send = confirmation required)
    "manage_email": None,  # Sentinel — expanded at task build time (see _build_task)
    "read_emails": email_tool.read_emails,
    "search_emails": email_tool.search_emails,
    "send_email": email_tool.send_email,
    # Vector search — expanded at task build time into VectorSearchTool instances
    "vector_search": None,  # Sentinel
    # Tabular query — auto-injected at task build time when structured CSVs exist
    "tabular_query": None,  # Sentinel
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
        # 'manage_email' is a UI label — expand to the actual email tools
        if tool_name == "manage_email":
            instantiated_tools.extend([
                email_tool.read_emails,
                email_tool.search_emails,
                email_tool.send_email,
            ])
            logging.info("Expanded 'manage_email' sentinel into 3 email tools.")
        elif tool_name in ALLOWED_TOOLS:
            tool_fn = ALLOWED_TOOLS[tool_name]
            if tool_fn is not None:  # Skip None sentinels
                instantiated_tools.append(tool_fn)
        else:
            logging.warning(f"Attempted to use unknown or disallowed tool: '{tool_name}'. Skipping.")

    return instantiated_tools

def _build_agent(agent_id, specialization=None):
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

    model_record = db.read_model(agent_record['model_id']) if agent_record.get('model_id') else None
    llm_instance = _instantiate_llm(agent_record['model_id'])
    
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
    base_role = agent_record['role']
    base_backstory = agent_record['backstory']

    if specialization:
        effective_role = f"{base_role} specialized in {specialization}"
        effective_backstory = (
            base_backstory +
            f"\n\nCRITICAL CONTEXT: For this specific task your area of expertise is "
            f"focused on **{specialization}**. Apply all your base skills strictly within "
            f"this specialized domain. Do not stray outside it."
        )
        logging.info(f"Agent '{agent_record['name']}' specialized as: '{effective_role}'")
    else:
        effective_role = base_role
        effective_backstory = base_backstory

    # --- BACKSTORY INJECTION: Enforce conciseness at identity level ---
    conciseness_trait = ("\n\nCRITICAL TRAIT: You are extremely concise and data-driven. "
                         "You never ramble. You output structured bullet points, not essays. "
                         "Every sentence must carry unique, actionable information.")
    enhanced_backstory = effective_backstory + conciseness_trait

    agent = Agent(
        role=effective_role,
        backstory=enhanced_backstory,
        goal=f"Act as {agent_record['name']} with the role: {effective_role}",
        llm=llm_instance,
        tools=agent_tools,
        verbose=True,
        allow_delegation=False,
        max_iter=5  # Prevent infinite reasoning loops
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

    # --- SPECIALIZATION CACHE BYPASS ---
    # If this task defines an agent_specialization, we must build a fresh, task-specific
    # agent clone rather than pulling from the shared cache. This prevents the specialized
    # role/backstory from leaking into other tasks that use the same base agent.
    specialization = task_record.get('agent_specialization')
    if specialization:
        agent_instance = _build_agent(agent_id, specialization=specialization)
    else:
        if agent_id not in agents_cache:
            agents_cache[agent_id] = _build_agent(agent_id)
        agent_instance = agents_cache[agent_id]
    
    # Check if the agent's model is local — if so, strip task-level tools too
    agent_record = db.read_agent(agent_id) if agent_id else None
    model_record = db.read_model(agent_record['model_id']) if agent_record and agent_record.get('model_id') else None
    is_local_model = False
    if model_record:
        is_local_model = bool(model_record.get('is_local')) or \
                         model_record.get('provider', '').lower() == 'ollama'

    if is_local_model:
        task_tools = None  # No tools for local models
    else:
        # Map standard tools
        task_tools = _map_tools(task_record.get('tools', [])) or []
        
        # Handle custom dynamic tools like vector_search
        if 'vector_search' in task_record.get('tools', []):
            vector_dbs = task_record.get('vector_dbs', [])
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

                        # --- Auto-inject TabularQueryTool if structured CSVs exist ---
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
        
        if not task_tools:
            task_tools = None

    # --- INTER-AGENT COMMUNICATION GUARDRAIL ---
    task_description = task_record['description'] + AGENT_COMMS_DIRECTIVE

    # Enhance expected_output to enforce structured format
    base_expected = task_record['expected_output']
    if "bullet" not in base_expected.lower() and "list" not in base_expected.lower():
        base_expected += " Format: Start with a 1-line summary, then key findings as bullet points (max 10)."

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

        for task_id in task_ids:
            crew_tasks.append(_build_task(task_id, agents_cache))
        
        # Extract all unique Agent objects from the cache
        crew_agents = list(agents_cache.values())

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

def execute_run_with_resume(run_id: int, status_callback=None) -> str:
    """
    Executes a workflow run task-by-task. Saves progress after each task.
    If the run is already partially completed, it resumes from the next uncompleted task.
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
        # Already completed
        return run.get('result', '')
        
    # Mark run as running
    db.update_run(run_id, status='running', current_task_idx=start_idx, task_outputs=task_outputs)
    
    agents_cache = {}
    last_output = ""
    if start_idx > 0:
        # Re-establish last_output from previously completed task
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
        
        # Inject inputs into task description
        original_desc = task_obj.description
        
        # Format task description with current inputs and previous result
        for k, v in inputs.items():
            original_desc = original_desc.replace(f"{{{k}}}", str(v))
        original_desc = original_desc.replace("{user_input}", inputs.get('user_input', ''))
        original_desc = original_desc.replace("{previous_result}", last_output)
        original_desc = original_desc.replace("{context}", last_output)
        original_desc = original_desc.replace("{flexible_input}", inputs.get('user_input', last_output))
        
        # If there is a last_output from a previous step, append it to the task description explicitly
        if last_output:
            original_desc += f"\n\n[CONTEXT FROM PREVIOUS STEP]:\n{last_output}"
            
        task_obj.description = original_desc
        
        # Create a single task Crew and execute it
        single_task_crew = Crew(
            agents=[task_obj.agent],
            tasks=[task_obj],
            verbose=True,
            process='sequential'
        )
        
        # Kick off!
        result = single_task_crew.kickoff()
        last_output = str(result)
        
        # Save output
        task_outputs[str(task_id)] = last_output
        
        # Save progress checkpoint to database
        db.update_run(run_id, status='running', result=last_output, current_task_idx=i + 1, task_outputs=task_outputs)
        
    # Final output
    return last_output

def build_dynamic_crew(plan: dict, default_model_id=None):
    """
    Builds a CrewAI Crew dynamically from a JSON plan generated by Master AI.
    Does not require database records for Agents or Tasks.
    """
    if not plan or 'agents' not in plan or 'tasks' not in plan:
        raise ValueError("Invalid plan format. Must contain 'agents' and 'tasks'.")
        
    # Pick a default model if not provided
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
    crew_agents = []
    
    for agent_data in plan['agents']:
        role = agent_data['role']
        # Strip tools if model is local
        if is_local_model:
            agent_tools = []
            logging.info(f"Dynamic Agent '{role}' tools stripped (local model).")
        else:
            agent_tools = _map_tools(agent_data.get('tools', []))
        
        # --- BACKSTORY INJECTION for dynamic agents ---
        conciseness_trait = ("\n\nCRITICAL TRAIT: You are extremely concise and data-driven. "
                             "You never ramble. You output structured bullet points, not essays. "
                             "Every sentence must carry unique, actionable information.")
        enhanced_backstory = agent_data.get('backstory', '') + conciseness_trait

        agent = Agent(
            role=role,
            backstory=enhanced_backstory,
            goal=agent_data.get('goal', ''),
            llm=llm_instance,
            tools=agent_tools,
            verbose=True,
            allow_delegation=False,
            max_iter=5
        )
        agents_cache[role] = agent
        crew_agents.append(agent)
        logging.info(f"Built Dynamic Agent: {role} (local={is_local_model})")
        
    crew_tasks = []
    for task_data in plan['tasks']:
        agent_role = task_data.get('agent_role')
        if agent_role not in agents_cache:
            logging.warning(f"Task specifies unknown agent role '{agent_role}'.")
            agent_instance = None
        else:
            agent_instance = agents_cache[agent_role]
            
        task_description = task_data['description'] + AGENT_COMMS_DIRECTIVE

        # Enhance expected_output for structured format
        base_expected = task_data.get('expected_output', 'Task Output')
        if "bullet" not in base_expected.lower() and "list" not in base_expected.lower():
            base_expected += " Format: Start with a 1-line summary, then key findings as bullet points (max 10)."

        task = Task(
            description=task_description,
            expected_output=base_expected,
            agent=agent_instance,
            async_execution=False
        )
        crew_tasks.append(task)
        logging.info(f"Built Dynamic Task for Agent: {agent_role}")
        
    if not crew_agents:
        raise ValueError("No agents could be built from the dynamic plan.")
    if not crew_tasks:
        raise ValueError("No tasks could be built from the dynamic plan.")
        
    crew = Crew(
        agents=crew_agents,
        tasks=crew_tasks,
        verbose=True,
        process='sequential'
    )
    
    logging.info("Successfully built Dynamic Crew!")
    return crew

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