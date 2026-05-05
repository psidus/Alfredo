import json
import logging
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

class MasterAI:
    """
    The MasterAI class acts as the intelligent gatekeeper and router for the system.
    It receives natural language requests from the user, evaluates them against
    strict ethical constraints, and maps approved intents to the most appropriate
    existing Workflow ID pulled from the SQLite database.

    Architectural Constraint: The Master AI must never write directly to the database
    or execute tools itself. It is strictly a read-only router and intent parser.
    """

    def __init__(self, model_id: Optional[int] = None):
        self.db_manager = DBManager()
        self.data_manager = DataManager()
        self.llm_client = None
        self.model_name = "gemini-1.5-flash"  # Default fallback model for Gemini
        self.model_provider = "gemini"  # Default fallback provider
        self.fallback_model = "gemini/gemini-1.5-flash" # Absolute fallback

        # Attempt to get model details from DB if model_id is provided
        if model_id:
            model_data = self.db_manager.read_model(model_id)
            if model_data:
                self.model_name = model_data['model_name']
                self.model_provider = model_data['provider']
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
                    response_format={"type": "json_object"} if "gemini" not in model.lower() else None # Gemini handles JSON better via prompt
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

    def evaluate_intent(self, user_prompt: str) -> Dict[str, Any]:
        """
        Evaluates the user's prompt using robust logic and maps it to a workflow ID.
        Also identifies which required inputs are missing.
        """
        workflows_json, requirements_map = self._fetch_workflows_context()
        
        extended_prompt = MASTER_PROMPT + "\n\nCRITICAL: If the selected workflow has 'required_placeholders', check if the user's message provides them. Return any missing placeholder keys in a 'missing_inputs' list in your JSON output."
        system_prompt = extended_prompt.replace("{workflows}", workflows_json)
        
        # Prepare the LiteLLM call
        model_string = f"{self.model_provider}/{self.model_name}"
        if self.model_provider == "openai":
            model_string = self.model_name
            
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