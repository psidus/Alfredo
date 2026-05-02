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
        self.model_name = "gpt-4o"  # Default fallback model
        self.model_provider = "openai"  # Default fallback provider

        # Attempt to get model details from DB if model_id is provided
        if model_id:
            model_data = self.db_manager.get_model(model_id)
            if model_data:
                self.model_name = model_data['model_name']
                self.model_provider = model_data['provider']
            else:
                logger.warning(f"Model ID {model_id} not found in DB. Using default model {self.model_name}.")
        else:
            logger.info(f"No model_id provided for MasterAI. Using default model {self.model_name}.")

        # Securely retrieve API key via DataManager
        api_key = self.data_manager.get_api_key(self.model_provider)
        if not api_key:
            logger.error(f"API key for {self.model_provider} not found in DataManager. Check 'ui/dashboard.py' API Vault.")
            raise ValueError(f"API key for {self.model_provider} is required but not found.")

        # Initialize LLM routing via LiteLLM
        if not litellm:
            logger.error("litellm is required for MasterAI but not installed.")
            raise ImportError("Please install litellm.")
            
        logger.info(f"MasterAI initialized with model: {self.model_provider}/{self.model_name}")

    def _fetch_workflows_context(self) -> str:
        """
        Retrieves and formats workflows from the database into a structured JSON string
        for the LLM to understand its options.
        """
        workflows = self.db_manager.get_all_workflows()
        if not workflows:
            return "No workflows currently available in the system."

        formatted_workflows = []
        for wf in workflows:
            # For the router, providing just ID and Name is usually sufficient.
            # More detailed task descriptions could be added if the LLM needs deeper context.
            formatted_workflows.append(
                {
                    "id": wf['id'],
                    "name": wf['name'],
                    # "description": "A brief description of what this workflow does." # Placeholder for future
                }
            )
        return json.dumps(formatted_workflows, indent=2)

    def _sanitize_json(self, text: str) -> str:
        """
        Strips markdown backticks (```json) and any surrounding text from an LLM response
        to extract a clean JSON string.
        """
        import re
        match = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL)
        if match:
            return match.group(1).strip()
        
        match = re.search(r'```\s*(.*?)\s*```', text, re.DOTALL)
        if match:
            return match.group(1).strip()
            
        return text.strip()

    def evaluate_intent(self, user_prompt: str) -> Dict[str, Any]:
        """
        Evaluates the user's prompt, checks for ethical constraints, and maps it
        to a workflow ID. Returns a dictionary with routing instructions.
        """
        workflows_json = self._fetch_workflows_context()
        
        system_prompt = MASTER_PROMPT.replace("{workflows}", workflows_json)
        
        # Prepare the LiteLLM call
        model_string = f"{self.model_provider}/{self.model_name}"
        if self.model_provider == "openai":
            model_string = self.model_name # LiteLLM uses just the name for openai
            
        try:
            response = litellm.completion(
                model=model_string,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.0, # Deterministic routing
                response_format={"type": "json_object"}
            )
            
            raw_output = response.choices[0].message.content
            clean_json = self._sanitize_json(raw_output)
            
            return json.loads(clean_json)
            
        except Exception as e:
            logger.error(f"MasterAI Routing Error: {e}")
            return {
                "status": "error",
                "message": f"Internal routing error: {str(e)}",
                "workflow_id": None,
                "extracted_params": {}
            }