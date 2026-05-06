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

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Initialize DB
db = DBManager()

# --- Security Helper: Hardcoded Tool Registry ---
# Define a strict, immutable mapping of allowed tools to prevent injection attacks.
ALLOWED_TOOLS = {
    "read_file": local_tools.read_file,
    "write_file": local_tools.write_file,
    "search_web": local_tools.search_web,
    "ask_operator": local_tools.ask_operator,
    "execute_shell_command": terminal_executor.execute_shell_command
}

def _instantiate_llm(model_id):
    """
    Securely creates an LLM object (ChatOpenAI or Ollama) based on model_id.
    Ensures environment variables are loaded and API keys are present for OpenAI.
    """
    DataManager.load_env() # Ensure environment variables are loaded

    model_record = db.read_model(model_id)
    if not model_record:
        raise ValueError(f"Model ID {model_id} not found in database.")

    provider = model_record['provider'].lower()
    model_name = model_record['model_name']
    
    # --- PROVIDER & MODEL NORMALIZATION ---
    # Normalize provider names to match LiteLLM/CrewAI expectations
    provider_mapping = {
        'google': 'gemini',
        'google_vertex': 'vertex_ai',
        'mistralai': 'mistral'
    }
    provider = provider_mapping.get(provider, provider)

    # Clean model name (remove prefixes like 'models/' common in Google API)
    if provider == 'gemini' and model_name.startswith('models/'):
        model_name = model_name.replace('models/', '')

    if provider == 'openai':
        if not os.getenv("OPENAI_API_KEY"):
            raise EnvironmentError("OPENAI_API_KEY is missing from .env. Cannot instantiate OpenAI LLM.")
        return ChatOpenAI(model_name=model_name, temperature=0.7)
    elif provider == 'ollama':
        return f"ollama/{model_name}"
    else:
        # Standard LiteLLM format: provider/model_name (e.g. anthropic/claude-3, groq/llama3)
        return f"{provider}/{model_name}"

def _map_tools(tool_names):
    """
    Safely converts a list of tool names into a list of callable Python functions
    using a strict whitelist (ALLOWED_TOOLS).
    """
    if not tool_names:
        return []

    instantiated_tools = []
    for tool_name in tool_names:
        if tool_name in ALLOWED_TOOLS:
            instantiated_tools.append(ALLOWED_TOOLS[tool_name])
        else:
            logging.warning(f"Attempted to use unknown or disallowed tool: '{tool_name}'. Skipping.")
    
    return instantiated_tools

def _build_agent(agent_id):
    """
    Constructs a CrewAI Agent object from database records.
    """
    agent_record = db.read_agent(agent_id)
    if not agent_record:
        raise ValueError(f"Agent ID {agent_id} not found in database.")

    llm_instance = _instantiate_llm(agent_record['model_id'])
    agent_tools = _map_tools(agent_record.get('tools', []))

    agent = Agent(
        role=agent_record['role'],
        backstory=agent_record['backstory'],
        goal=f"Act as {agent_record['name']} with the role: {agent_record['role']}", # Use name as part of goal for clarity
        llm=llm_instance,
        tools=agent_tools,
        verbose=True, # Agents verbosity for debugging
        allow_delegation=False # Prevent infinite loops in automated remote executions
    )
    logging.info(f"Built CrewAI Agent: {agent_record['name']} (ID: {agent_id})")
    return agent

def _build_task(task_id, agents_cache):
    """
    Constructs a CrewAI Task object and ensures agents are reused via memory reference.
    """
    task_record = db.read_task(task_id)
    if not task_record:
        raise ValueError(f"Task ID {task_id} not found in database.")

    agent_id = task_record['agent_id']
    if agent_id not in agents_cache:
        agents_cache[agent_id] = _build_agent(agent_id)
    
    agent_instance = agents_cache[agent_id]
    
    # Task tools override agent tools if specified
    task_tools = _map_tools(task_record.get('tools', []))

    task = Task(
        description=task_record['description'],
        expected_output=task_record['expected_output'],
        agent=agent_instance,
        tools=task_tools if task_tools else None,
        # Set async=False for sequential processing in a single workflow
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
    
    agents_cache = {}
    crew_agents = []
    
    for agent_data in plan['agents']:
        role = agent_data['role']
        agent_tools = _map_tools(agent_data.get('tools', []))
        
        agent = Agent(
            role=role,
            backstory=agent_data.get('backstory', ''),
            goal=agent_data.get('goal', ''),
            llm=llm_instance,
            tools=agent_tools,
            verbose=True,
            allow_delegation=False
        )
        agents_cache[role] = agent
        crew_agents.append(agent)
        logging.info(f"Built Dynamic Agent: {role}")
        
    crew_tasks = []
    for task_data in plan['tasks']:
        agent_role = task_data.get('agent_role')
        if agent_role not in agents_cache:
            logging.warning(f"Task specifies unknown agent role '{agent_role}'.")
            agent_instance = None
        else:
            agent_instance = agents_cache[agent_role]
            
        task = Task(
            description=task_data['description'],
            expected_output=task_data.get('expected_output', 'Task Output'),
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