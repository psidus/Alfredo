# agents/executor.py

import os
from typing import List, Dict, Any, Optional
from crewai import Agent, Task, Crew
from core.db_manager import DBManager
from dotenv import load_dotenv

# --- Tool Imports ---
from tools.local_tools import read_file, write_file, search_web
from tools.terminal_executor import execute_shell_command

TOOL_REGISTRY = {
    "read_file": read_file,
    "write_file": write_file,
    "search_web": search_web,
    "execute_shell_command": execute_shell_command
}

class WorkflowRunner:
    def __init__(self):
        self.db = DBManager()
        load_dotenv()

    def _get_llm_string(self, model_data: Dict[str, Any]) -> str:
        """
        Converts DB model data into a string compatible with CrewAI/LiteLLM.
        """
        provider = model_data.get('provider', '').lower()
        model_name = model_data.get('model_name', '')
        
        if model_data.get('is_local'):
            return f"ollama/{model_name}"
        
        if provider == "openai":
            return model_name
        if provider == "anthropic":
            return f"anthropic/{model_name}"
        if provider == "groq":
            return f"groq/{model_name}"
        if provider == "gemini":
            return f"gemini/{model_name}"
            
        return model_name

    def run_workflow(self, workflow_id: int, user_input: str, session_id: Optional[str] = None) -> str:
        """
        Executes a workflow by replacing placeholders and running a Crew.
        Supports session_id for sequential workflow chaining.
        """
        workflow = self.db.read_workflow(workflow_id)
        if not workflow:
            return "Workflow not found."

        # Fetch context if session_id is provided
        context_data = ""
        if session_id:
            ctx = self.db.get_context(session_id)
            if ctx and ctx.get('last_output'):
                context_data = ctx['last_output']

        tasks_data = self.db.read_all_tasks()
        agents_data = self.db.read_all_agents()
        models_data = self.db.read_all_models()

        # Filter tasks belonging to this workflow and maintain order
        workflow_task_ids = workflow.get('task_ids', [])
        workflow_tasks = []
        for tid in workflow_task_ids:
            task = next((t for t in tasks_data if t['id'] == tid), None)
            if task:
                workflow_tasks.append(task)

        if not workflow_tasks:
            return "No tasks found for this workflow."

        # Create Agent and Task objects for CrewAI
        crew_agents = {}
        crew_tasks = []

        for task_dict in workflow_tasks:
            agent_id = task_dict.get('agent_id')
            if not agent_id:
                continue
            
            # 1. Setup Agent if not already done
            if agent_id not in crew_agents:
                agent_info = next((a for a in agents_data if a['id'] == agent_id), None)
                if not agent_info:
                    continue
                
                model_info = next((m for m in models_data if m['id'] == agent_info['model_id']), None)
                llm_str = self._get_llm_string(model_info) if model_info else "gpt-3.5-turbo"
                
                # Apply global security and control functions (Retry on 503, etc.)
                from agents.identities import RobustLLM
                robust_llm = RobustLLM(model=llm_str)
                
                crew_agents[agent_id] = Agent(
                    role=agent_info['role'],
                    goal=agent_info['name'], # Name is often used as goal identifier in simple setups
                    backstory=agent_info['backstory'],
                    llm=robust_llm,
                    verbose=True,
                    allow_delegation=False
                )

            # 2. Setup Task
            description = task_dict['description']
            # Smart fallback: if user_input is empty, use context_data
            flexible_input = user_input if user_input.strip() else context_data

            # Replace primary input
            description = description.replace("{user_input}", user_input)
            # Replace context from previous workflows
            description = description.replace("{previous_result}", context_data)
            description = description.replace("{context}", context_data)
            # Replace flexible input (smart fallback)
            description = description.replace("{flexible_input}", flexible_input)
            # Resolve tools for this task
            task_tool_names = task_dict.get('tools', [])
            actual_tools = []
            for t_name in task_tool_names:
                if t_name in TOOL_REGISTRY:
                    actual_tools.append(TOOL_REGISTRY[t_name])
            
            crew_tasks.append(Task(
                description=description,
                expected_output=task_dict['expected_output'],
                agent=crew_agents[agent_id],
                tools=actual_tools if actual_tools else None
            ))

        if not crew_tasks:
            return "Could not initialize tasks for execution."

        # 3. Run Crew
        crew = Crew(
            agents=list(crew_agents.values()),
            tasks=crew_tasks,
            verbose=True,
            max_rpm=20
        )

        result = crew.kickoff()
        final_output = str(result)
        
        # Save output to context memory if session_id is provided
        if session_id:
            self.db.update_context(session_id, final_output)
            
        return final_output
