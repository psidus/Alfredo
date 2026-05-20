import json
import logging
import os
from typing import Optional, Dict, Any


from core.db_manager import DBManager
from core.data_manager import DataManager

try:
    import litellm
except ImportError:
    litellm = None

logger = logging.getLogger(__name__)

MASTER_PROMPT = """
You are the Master AI Router. Your job is to analyze user requests and route them to the correct Workflow ID.
Available Workflows:
{workflows}

Rules:
- If 'status' is 'success', 'workflow_id' MUST be an integer corresponding to an available workflow.
- If 'status' is 'rejected' or 'not_found', 'workflow_id' MUST be null.
- Ensure 'extracted_params' is always a valid JSON object.
"""

CHAT_PLANNER_PROMPT = """
You are Alfredo, a Strategic Co-Pilot and Master AI Workflow Planner.
Your goal is to help the user design a plan to achieve their objective using a team of AI agents.

{base_workflow_context}

You must maintain a conversational tone in your 'response'.
If the user asks for changes, acknowledge them, update your internal plan, and ask if they agree.
If the user explicitly confirms the plan (e.g., saying "procedi", "confermo", "vai", "ok", "yes"), you MUST set the "status" to "ready" and provide the fully fleshed out plan in the "plan" object.
Otherwise, set the "status" to "planning", provide your conversational response in "response", and you may leave "plan" null or provide a draft.

AVAILABLE TOOLS:
- read_file
- write_file
- search_web
- ask_operator
- execute_shell_command

STRATEGIC RULES FOR CONCISENESS & SPECIALIZATION:
- All agents MUST be instructed in their 'backstory' to be "concise, factual, and avoid fluff".
- All tasks MUST have an 'expected_output' that specifies a "synthesized, structured report or clear list of findings".
- Avoid dispersion: agents should focus only on the most relevant data needed for the next step.
- Agent Specialization: Use the 'agent_specialization' field in tasks to focus or customize the agent's persona specifically for that task (e.g. chemistry, copywriting, physics). If the same base agent is reused in different tasks, assign distinct 'agent_specialization' strings to give them different personalities/focuses.

OUTPUT FORMAT (JSON ONLY):
{
  "status": "planning" | "ready",
  "response": "Your conversational reply to the user (use Markdown if needed, keep it concise and helpful).",
  "plan": {
    "agents": [
      {
        "role": "Agent Role (e.g., Senior Copywriter)",
        "goal": "Agent Goal",
        "backstory": "Agent backstory and expertise",
        "tools": ["list of tool names"]
      }
    ],
    "tasks": [
      {
        "description": "Clear and detailed task description",
        "expected_output": "What the task should produce",
        "agent_role": "MUST match exactly one role from the agents list",
        "agent_specialization": "Optional domain specialization for the agent in this task (e.g., 'Quantum Physics', 'Italian Cooking')"
      }
    ]
  }
}
"""

OUTPUT_REFINER_PROMPT = """
You are Alfredo, the Master AI Editor and Quality Controller.
You receive the RAW output from a team of AI agents who executed a workflow.
Your job is to transform that raw output into a polished, clear, and user-friendly final report.

YOUR RESPONSIBILITIES:
1. **Formatting & Clarity**: Fix syntax, grammar, and structure. Use clear headings, bullet points, and numbered lists. Remove any agent-internal jargon, debugging notes, or redundant reasoning.
2. **Synthesis**: Merge overlapping sections. Remove duplicate information. Ensure the report flows logically from analysis to conclusions to recommendations.
3. **Ethical Review**: Flag any content that is unethical, illegal, harmful, or promotes deceptive practices. If you find issues, add a clearly visible "⚠️ Ethical Note" section at the end.
4. **Actionability**: Ensure the report ends with concrete, prioritized next steps the user can act on.

RULES:
- Do NOT invent new data or analysis. Only restructure and clarify what the agents produced.
- Use Markdown formatting for the output (headings, bold, lists).
- Keep the total output under 3000 characters when possible.
- If the raw output is very short or empty, acknowledge that the agents did not produce substantial results and suggest the user try again with more specific instructions.

RAW AGENT OUTPUT:
---
{raw_output}
---

Produce the refined, user-ready report below:
"""

AGENT_OPTIMIZER_PROMPT = """
You are Alfredo, a Senior Prompt Engineer and Agent Architect.
Your job is to optimize the Role, Goal, and Backstory of an AI agent to ensure they are:
1. Highly concise, factual, and focused on core expertise to minimize token usage.
2. Structured to avoid "fluff", conversational preamble, or unnecessary detail.
3. Designed to follow CrewAI best practices.
4. Written in the SAME language as the input (keep it Italian if input is Italian, English if input is English, etc.).
5. Aligning with the system philosophy: agents communicate using basic, dense tokens, and final response expansion is handled by the Master AI.

Input Agent Details:
- Role: {role}
- Goal: {goal}
- Backstory: {backstory}

Optimize and return a JSON object with:
{{
  "role": "Optimized role description",
  "goal": "Optimized goal, clear and focused",
  "backstory": "Optimized backstory, concise and professional, describing key expertise, tone, and directives (e.g. be concise, avoid fluff)"
}}
"""

TASK_OPTIMIZER_PROMPT = """
You are Alfredo, a Senior Prompt Engineer and Workflow Architect.
Your job is to optimize the Description and Expected Output of an AI task to ensure they are:
1. Optimized to minimize token consumption. Focus purely on required inputs, steps, and structure.
2. Structured for basic, dense intermediate pipeline communication between agents (no conversational outputs, no preambles, structured lists only).
3. Specific, unambiguous, and focused on producing measurable outcomes.
4. Written in the SAME language as the input (keep it Italian if input is Italian, English if input is English, etc.).
5. CRITICAL: Do NOT resolve, edit, or remove any variables/placeholders in curly braces like `{{nome_variabile}}` or `{{user_input}}` or `{{previous_result}}`. Keep them exactly as they are.

Input Task Details:
- Description: {description}
- Expected Output: {expected_output}

Optimize and return a JSON object with:
{{
  "description": "Optimized task description, preserving all curly brace placeholders",
  "expected_output": "Optimized expected output description, preserving all curly brace placeholders"
}}
"""

class MasterAI:
    """
    The MasterAI class acts as the intelligent gatekeeper and router for the system.
    It receives natural language requests from the user, evaluates them against
    strict ethical constraints, and maps approved intents to the most appropriate
    existing Workflow ID pulled from the SQLite database.

    Architectural Constraint: The Master AI must never write directly to the database
    or execute tools itself. It is strictly a read-only router and intent parser.
    """

    def __init__(self, model_id: Optional[int] = None, complexity: str = "simple"):
        self.db_manager = DBManager()
        self.data_manager = DataManager()
        self.llm_client = None
        
        # Load from .env if model_id is not explicitly provided
        if model_id is None:
            try:
                from dotenv import dotenv_values, find_dotenv
                env_path = find_dotenv() or os.path.join(os.getcwd(), '.env')
                current_env = dotenv_values(env_path)
                env_model_id = current_env.get("MASTER_AI_MODEL_ID")
                if env_model_id:
                    model_id = int(env_model_id)
            except Exception as e:
                logger.warning(f"Error loading MASTER_AI_MODEL_ID from environment: {e}")
        
        # Default Robust Configuration (Simple vs Complex)
        # Source: https://ai.google.dev/gemini-api/docs/models (May 2026)
        # gemini-2.0-flash and gemini-2.0-flash-lite are DEPRECATED — use 2.5 series.
        if complexity == "complex":
            self.model_name = "gemini-2.5-pro"          # Best reasoning, for complex tasks
            self.model_provider = "gemini"
            self.fallback_model = "gemini/gemini-2.5-flash"
        else:
            self.model_name = "gemini-2.5-flash-lite"   # Fastest, lowest traffic, stable
            self.model_provider = "gemini"
            self.fallback_model = "gemini/gemini-2.5-flash"  # Fallback to stronger flash

        # Attempt to get model details from DB if model_id is provided
        if model_id:
            model_data = self.db_manager.read_model(model_id)
            if model_data:
                self.model_name = model_data['model_name']
                self.model_provider = model_data['provider'].lower()
                
                # --- PROVIDER & MODEL NORMALIZATION ---
                provider_mapping = {
                    'google': 'gemini',
                    'mistralai': 'mistral'
                }
                self.model_provider = provider_mapping.get(self.model_provider, self.model_provider)

                # Clean 'models/' prefix common in Google/Gemini models
                if self.model_provider == 'gemini' and self.model_name.startswith('models/'):
                    self.model_name = self.model_name.replace('models/', '')
            else:
                logger.warning(f"Model ID {model_id} not found in DB. Using default model {self.model_name}.")
        else:
            logger.info(f"No model_id provided for MasterAI. Using default model {self.model_name}.")

        # Securely retrieve API key via DataManager
        self.api_key = self.data_manager.load_api_key(f"{self.model_provider.upper()}_API_KEY")
        if not self.api_key:
            # Fallback to general GEMINI_API_KEY if specific one fails
            self.api_key = self.data_manager.load_api_key("GEMINI_API_KEY")
            
        if not self.api_key:
            logger.error(f"API key for {self.model_provider} not found in DataManager. Check 'ui/dashboard.py' API Vault.")
            raise ValueError(f"API key for {self.model_provider} is required but not found.")

        # --- CRITICAL: Inject the key into os.environ so LiteLLM can find it ---
        # LiteLLM reads keys from environment variables, not from our internal self.api_key.
        # We must set the correct env var name for each provider.
        provider_key_env_map = {
            "gemini": "GEMINI_API_KEY",
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "groq": "GROQ_API_KEY",
            "mistral": "MISTRAL_API_KEY",
        }
        env_var_name = provider_key_env_map.get(self.model_provider, f"{self.model_provider.upper()}_API_KEY")
        os.environ[env_var_name] = self.api_key
        logger.info(f"API key for provider '{self.model_provider}' injected into environment as '{env_var_name}'.")

        # Initialize LLM routing via LiteLLM
        if not litellm:
            logger.error("litellm is required for MasterAI but not installed.")
            raise ImportError("Please install litellm.")
            
        logger.info(f"MasterAI initialized with model: {self.model_provider}/{self.model_name}")

    def _fetch_workflows_context(self):
        """
        Retrieves and formats workflows from the database.
        Returns:
            (str): JSON formatted list of workflows for the LLM.
            (dict): Mapping of workflow_id to its required_inputs list.
        """
        workflows = self.db_manager.read_all_workflows()
        tasks_list = self.db_manager.read_all_tasks()
        task_map = {t['id']: t for t in tasks_list}
        
        if not workflows:
            return "No workflows currently available in the system.", {}

        formatted_workflows = []
        requirements_map = {}
        
        for wf in workflows:
            wf_id = wf['id']
            # Aggregate all required inputs from tasks in this workflow
            all_reqs = []
            for t_id in wf.get('task_ids', []):
                task = task_map.get(int(t_id))
                if task and task.get('required_inputs'):
                    all_reqs.extend(task['required_inputs'])
            
            requirements_map[wf_id] = all_reqs
            
            formatted_workflows.append({
                "id": wf_id,
                "name": wf['name'],
                "required_placeholders": [r['key'] for r in all_reqs]
            })
            
        return json.dumps(formatted_workflows, indent=2), requirements_map

    def _sanitize_json(self, text: str) -> str:
        """
        Strips markdown backticks (```json) and any surrounding text from an LLM response.
        """
        import re
        match = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL)
        if match:
            return match.group(1).strip()
        
        match = re.search(r'```\s*(.*?)\s*```', text, re.DOTALL)
        if match:
            return match.group(1).strip()
            
        return text.strip()

    def _call_llm_with_retry(self, model: str, messages: list, temperature: float = 0.0) -> str:
        """
        Robust LLM call with Retry logic (503/429) and Fallback mechanism.
        Mimics 'RobustLLM' logic from identities.py.
        """
        import time
        max_retries = 3
        
        for attempt in range(max_retries):
            try:
                response = litellm.completion(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    response_format={"type": "json_object"} # Force JSON mode for all providers
                )
                return response.choices[0].message.content
            except Exception as e:
                err_msg = str(e)
                # Check for temporary errors (503 Service Unavailable, 429 Rate Limit)
                if ("503" in err_msg or "429" in err_msg or "high demand" in err_msg.lower()) and attempt < max_retries - 1:
                    wait = (attempt + 1) * 3
                    logger.warning(f"MasterAI Retry {attempt+1}/{max_retries} due to: {err_msg[:50]}... Waiting {wait}s")
                    time.sleep(wait)
                    continue
                
                # If retry failed or it's a critical error, try fallback
                if model != self.fallback_model:
                    logger.error(f"MasterAI Critical Error with {model}: {e}. Attempting fallback to {self.fallback_model}...")
                    try:
                        response = litellm.completion(
                            model=self.fallback_model,
                            messages=messages,
                            temperature=temperature
                        )
                        return response.choices[0].message.content
                    except Exception as fe:
                        logger.error(f"MasterAI Fallback also failed: {fe}")
                
                raise e

    def evaluate_intent(self, user_prompt: str, previous_context: str = None) -> Dict[str, Any]:
        """
        Evaluates the user's prompt using robust logic and maps it to a workflow ID.
        Also identifies which required inputs are missing.
        """
        workflows_json, requirements_map = self._fetch_workflows_context()
        
        extended_prompt = MASTER_PROMPT + "\n\nCRITICAL: If the selected workflow has 'required_placeholders', check if the user's message provides them. Return any missing placeholder keys in a 'missing_inputs' list in your JSON output."
        
        if previous_context:
            extended_prompt += f"\n\nPREVIOUS CONTEXT:\nThe user is continuing from a previous workflow. The previous output was:\n---\n{previous_context}\n---\nIf the new workflow requires inputs, see if this previous output provides the necessary information. Also, automatically map the 'previous_result' parameter to this context."

        system_prompt = extended_prompt.replace("{workflows}", workflows_json)
        
        # Prepare the LiteLLM call
        model_string = f"{self.model_provider}/{self.model_name}"
        if self.model_provider == "openai":
            model_string = self.model_name
        elif self.model_provider == "gemini" and "/" in self.model_name:
            model_string = self.model_name # Already has prefix
            
        logger.info(f"MasterAI Attempting Intent Evaluation with: {model_string}")
        try:
            raw_output = self._call_llm_with_retry(
                model=model_string,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ]
            )
            
            clean_json = self._sanitize_json(raw_output)
            result = json.loads(clean_json)
            
            # Post-process missing_inputs to include prompts from DB
            wf_id = result.get("workflow_id")
            if wf_id and wf_id in requirements_map:
                missing_keys = result.get("missing_inputs", [])
                all_reqs = requirements_map[wf_id]
                result["missing_inputs_details"] = [req for req in all_reqs if req['key'] in missing_keys]
                
            return result
            
        except Exception as e:
            logger.error(f"MasterAI Final Routing Failure: {e}")
            return {
                "status": "error",
                "message": f"Critical routing error: {str(e)}",
                "workflow_id": None,
                "extracted_params": {}
            }

    def refine_output(self, raw_output: str) -> str:
        """
        Post-processing pipeline: takes the raw output from a CrewAI execution
        and refines it through the Master AI for:
        1. Formatting & clarity cleanup
        2. Ethical review
        3. Synthesis into a user-friendly report
        
        This runs automatically after every crew execution — no need to add 
        it as a task in the workflow.
        
        Args:
            raw_output: The raw string output from crew.kickoff().
        Returns:
            str: The refined, polished report ready for the end user.
        """
        raw_str = str(raw_output).strip()
        if not raw_str:
            return "⚠️ The workflow completed but produced no output. Please try again with more specific instructions."

        system_prompt = OUTPUT_REFINER_PROMPT.replace("{raw_output}", raw_str)
        
        model_string = f"{self.model_provider}/{self.model_name}"
        if self.model_provider == "openai":
            model_string = self.model_name
        elif self.model_provider == "gemini" and "/" in self.model_name:
            model_string = self.model_name

        logger.info(f"MasterAI Refining output ({len(raw_str)} chars) with: {model_string}")
        
        try:
            # Use plain text completion (no JSON mode) for the refiner
            response = litellm.completion(
                model=model_string,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": "Please refine the above raw output into a clear, polished report."}
                ],
                temperature=0.1  # Low creativity — focus on restructuring, not inventing
            )
            refined = response.choices[0].message.content.strip()
            logger.info(f"MasterAI Output refinement complete ({len(refined)} chars).")
            return refined
        except Exception as e:
            logger.error(f"MasterAI Output Refinement failed: {e}. Returning raw output.")
            # Graceful fallback: return the raw output if refinement fails
            return raw_str

    def chat_plan(self, user_message: str, chat_history: list = None, base_workflow: dict = None) -> Dict[str, Any]:
        """
        Engages in a conversational planning phase with the user to dynamically build
        or refine a crew of agents and tasks.
        
        Args:
            user_message: The latest message from the user.
            chat_history: List of dicts [{"role": "user"|"assistant", "content": "..."}]
            base_workflow: Optional dictionary containing an existing workflow to start from.
        """
        chat_history = chat_history or []
        
        base_workflow_context = ""
        if base_workflow:
            base_workflow_context = f"""
BASE WORKFLOW LOADED:
The user has selected the following predefined workflow as a starting point:
Name: {base_workflow.get('name')}
Description: {base_workflow.get('description')}
Please propose this base workflow to the user, adapt it to their specific request, and ask if they want to modify it.
"""
        else:
            base_workflow_context = "No base workflow loaded. You must design a custom plan from scratch based on the user's request."
            
        system_prompt = CHAT_PLANNER_PROMPT.replace("{base_workflow_context}", base_workflow_context)
        
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(chat_history)
        messages.append({"role": "user", "content": user_message})
        
        model_string = f"{self.model_provider}/{self.model_name}"
        if self.model_provider == "openai":
            model_string = self.model_name
        elif self.model_provider == "gemini" and "/" in self.model_name:
            model_string = self.model_name

        logger.info(f"MasterAI Attempting Planning with: {model_string}")
        try:
            # Use temperature 0.2 for a bit more creativity in planning while keeping JSON stable
            raw_output = self._call_llm_with_retry(
                model=model_string,
                messages=messages,
                temperature=0.2
            )
            
            clean_json = self._sanitize_json(raw_output)
            
            try:
                result = json.loads(clean_json)
            except json.JSONDecodeError:
                # Robust Fallback: If the LLM failed to give JSON, wrap the raw text into a valid response
                logger.warning("MasterAI failed to return valid JSON. Using fallback wrapper.")
                result = {
                    "status": "planning",
                    "response": clean_json,
                    "plan": None
                }
            
            # Ensure required fields exist
            if not isinstance(result, dict):
                result = {"status": "planning", "response": str(result), "plan": None}
                
            if "status" not in result:
                result["status"] = "planning"
            if "response" not in result:
                result["response"] = "I'm processing the plan..."
                
            return result
            
        except Exception as e:
            logger.error(f"MasterAI Planning Failure: {e}")
            return {
                "status": "planning",
                "response": f"⚠️ Error in planning phase occurred: {str(e)}",
                "plan": None
            }

    def optimize_agent_fields(self, role: str, goal: str, backstory: str) -> Dict[str, str]:
        """
        Optimizes an agent's role, goal, and backstory prompts using the LLM.
        """
        system_prompt = AGENT_OPTIMIZER_PROMPT.format(role=role, goal=goal, backstory=backstory)
        model_string = f"{self.model_provider}/{self.model_name}"
        if self.model_provider == "openai":
            model_string = self.model_name
        elif self.model_provider == "gemini" and "/" in self.model_name:
            model_string = self.model_name

        logger.info(f"MasterAI Optimizing agent fields with: {model_string}")
        try:
            raw_output = self._call_llm_with_retry(
                model=model_string,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": "Optimize the agent's prompts."}
                ],
                temperature=0.3
            )
            clean_json = self._sanitize_json(raw_output)
            result = json.loads(clean_json)
            return {
                "role": result.get("role", role),
                "goal": result.get("goal", goal),
                "backstory": result.get("backstory", backstory)
            }
        except Exception as e:
            logger.error(f"Failed to optimize agent prompts: {e}")
            return {
                "role": role,
                "goal": goal,
                "backstory": backstory
            }

    def optimize_task_fields(self, description: str, expected_output: str) -> Dict[str, str]:
        """
        Optimizes a task's description and expected output prompts using the LLM.
        """
        system_prompt = TASK_OPTIMIZER_PROMPT.format(description=description, expected_output=expected_output)
        model_string = f"{self.model_provider}/{self.model_name}"
        if self.model_provider == "openai":
            model_string = self.model_name
        elif self.model_provider == "gemini" and "/" in self.model_name:
            model_string = self.model_name

        logger.info(f"MasterAI Optimizing task fields with: {model_string}")
        try:
            raw_output = self._call_llm_with_retry(
                model=model_string,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": "Optimize the task's prompts."}
                ],
                temperature=0.3
            )
            clean_json = self._sanitize_json(raw_output)
            result = json.loads(clean_json)
            return {
                "description": result.get("description", description),
                "expected_output": result.get("expected_output", expected_output)
            }
        except Exception as e:
            logger.error(f"Failed to optimize task prompts: {e}")
            return {
                "description": description,
                "expected_output": expected_output
            }