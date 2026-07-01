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

# --- Headroom AI: Context Compression ---
# If HEADROOM_ENABLED=true in .env, the _compress_messages helper will compress
# prompts before every LiteLLM call, reducing token usage by 60-95%.
# If HEADROOM_PROXY_URL is set, we route traffic to the proxy instead.
_headroom_compress = None
if os.getenv("HEADROOM_ENABLED", "").lower() == "true":
    proxy_url = os.getenv("HEADROOM_PROXY_URL")
    if proxy_url:
        # Gemini requires v1beta for system_instruction. If proxy specifies /v1, adjust it.
        if proxy_url.endswith("/v1"):
            proxy_url = proxy_url[:-3] + "/v1beta"
        if litellm:
            litellm.api_base = proxy_url
        logger.info(f"Headroom AI Proxy Mode enabled at {proxy_url}")
    else:
        try:
            from headroom import compress as _headroom_compress_fn
            _headroom_compress = _headroom_compress_fn
            logger.info("Headroom AI context compression enabled (inline compress).")
        except ImportError:
            logger.warning(
                "HEADROOM_ENABLED=true but headroom-ai is not installed. "
                "Run: pip install headroom-ai[all]"
            )


def _compress_messages(messages: list, model: str) -> list:
    """Compress messages with Headroom AI if enabled; otherwise pass through."""
    if _headroom_compress is None:
        return messages
    try:
        result = _headroom_compress(messages, model=model)
        saved = getattr(result, 'tokens_saved', 0)
        if saved:
            logger.debug(f"Headroom: compressed {saved} tokens for model '{model}'.")
        return result.messages
    except Exception as e:
        logger.warning(f"Headroom compression failed (passthrough): {e}")
        return messages


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

{saved_context_section}

You must maintain a conversational tone in your 'response'.
If the user asks for changes, acknowledge them, update your internal plan, and ask if they agree.
If the user explicitly confirms the plan (e.g., saying "procedi", "confermo", "vai", "ok", "yes", "go", or any affirmative), you MUST set the "status" to "ready" and provide the fully fleshed out plan in the "plan" object.
If the user asks to generate a specific file (e.g. "create a pdf", "write a word document with the resume") based on the SAVED GLOBAL CONTEXT, set "status" to "export", list the formats in "plan.expected_exports", and explain that you are extracting the files.
Otherwise, set the "status" to "planning", provide your conversational response in "response" (e.g. answering questions using the SAVED GLOBAL CONTEXT), and you may leave "plan" null or provide a draft.

*** CRITICAL — READ THIS BEFORE EVERYTHING ELSE ***
NEVER ask the user for information that is listed in a task's "required_inputs" field.
The BOT FRAMEWORK will collect those values automatically AFTER you return status "ready".
Your ONLY job with required_inputs is to LIST them correctly in the task JSON so the bot knows what to ask.
If the user says "go", "proceed", "yes", "ok", or any confirmation, set status to "ready" IMMEDIATELY — even if required_inputs are not yet filled. The bot will handle collecting them.
If you ask conversationally for required inputs (e.g. "what process?", "I need one more thing"), you will BREAK the workflow. DO NOT DO THIS.
*** END CRITICAL ***

AVAILABLE TOOLS:
- read_file
- write_file
- search_web
- ask_operator
- execute_shell_command

STRATEGIC RULES FOR CONCISENESS & SPECIALIZATION:
- All agents MUST be instructed in their 'backstory' to be "concise, factual, and avoid fluff".
- Vector-DB Friendly Outputs: All tasks MUST have an 'expected_output' that specifies a "synthesized, structured report". It should explicitly require agents to include semantic context (headers, keywords, explicit nouns) because their outputs will be stored in a Vector Database for semantic retrieval by downstream agents.
- Avoid dispersion: agents should focus only on the most relevant data needed for the next step.
- Agent Specialization: Use the 'agent_specialization' field in tasks to focus or customize the agent's persona specifically for that task (e.g. chemistry, copywriting, physics). If the same base agent is reused in different tasks, assign distinct 'agent_specialization' strings to give them different personalities/focuses.
- CRITICAL ARCHITECTURE RULE: Agents NEVER write or execute final code files. Agents ONLY produce data, logic, and mathematical blueprints, saving them to the vector database. The final code or document is generated EXCLUSIVELY by the Master AI at the end of the workflow via the export function. Therefore, NEVER create a task that asks an agent to "Audit source code", "Write a python file", or "Run code", because the final code will not exist during the agent workflow.

RULES FOR REQUIRED INPUTS:
- If a task description needs user-provided information (e.g. a dataset name, target compound, research topic), use a {variable_name} placeholder in the description.
- CRITICAL: For EVERY {variable_name} placeholder used in a task description, you MUST add a matching entry in that task's "required_inputs" list, with a clear user-facing "prompt" question.
- NEVER use {variable_name} placeholders to reference the output of previous tasks/agents (e.g., do not write "Use the {blueprint}"). Instead, explicitly instruct the agent to find the data in the Ephemeral Memory Index (e.g., "Use read_atomic_memory to retrieve the blueprint").
- Example: description="Analyze {dataset_name}" → required_inputs=[{"key": "dataset_name", "prompt": "What is the name of the dataset to analyze?"}]
- If a task needs no variable inputs from the user, set "required_inputs": [].
- NEVER ask for these values in your "response" — only define them in the task JSON. The bot will collect them.
- HUMAN VALIDATION (HITL): If a task in the base workflow has "human_validation": true, you MUST preserve it exactly as true in the output JSON. You CANNOT remove it. You may ADD "human_validation": true to tasks if the user explicitly asks for human validation, feedback, or review steps.

LEVEL-BASED & DIRECT DEPENDENCE MEMORY ARCHITECTURE:
When creating or modifying workflows, you MUST enforce this dual-memory architecture:
- **Level Access (Broad Context)**: Group tasks into sequential phases using `execution_level`. Tasks at a higher level automatically have access to all outputs from lower levels via the Ephemeral Memory Index and `read_atomic_memory` tool. Do NOT use `{previous_result}` for general context gathering; explicitly instruct the agent to fetch the specific keys it needs using `read_atomic_memory`.
- **Direct Dependence (Strict Priority)**: If a child task absolutely requires the output of a specific parent task to function, you MUST list the parent's ID in the child's `depends_on` array AND use the `{previous_result}` placeholder in the child's description. The system will explicitly inject the parent's output into `{previous_result}`.
- **DAG Coherence**: If Task B reads a memory key generated by Task A, Task B MUST explicitly list Task A's ID in its 'depends_on' array. This guarantees safe parallel execution and prevents "key not found" race conditions.
- **Parallelism**: If two or more tasks are independent, make them run IN PARALLEL by giving them the same `execution_level` and empty `depends_on`. This is CRITICAL for performance.

OUTPUT FORMAT (JSON ONLY):
{
  "status": "planning" | "ready" | "export",
  "modified": false | true,
  "response": "Your conversational reply to the user (use Markdown if needed, keep it concise and helpful). NEVER ask for required_inputs here.",
  "plan": {
    "expected_exports": ["python", "markdown", "json", "pdf", "docx"],
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
        "id": "node_unique_id",
        "description": "Clear and detailed task description. Use {variable_name} for user-provided values.",
        "expected_output": "What the task should produce",
        "agent_role": "MUST match exactly one role from the agents list",
        "agent_specialization": "Optional domain specialization for the agent in this task",
        "vector_dbs": ["list of vector database IDs (integers) to search, if needed"],
        "human_validation": false,
        "depends_on": ["node_1"],
        "execution_level": 1,
        "required_inputs": [
          {"key": "variable_name", "prompt": "User-facing question to ask before execution"}
        ]
      }
    ]
  }
}
"""

EXPORT_GENERATOR_PROMPT = """
You are Alfredo, the Master AI Data Extractor and Formatter.
Your job is to read the FULL GLOBAL CONTEXT generated by a team of AI agents
and extract the EXACT content required to produce a specific file format.

REQUESTED FORMAT: {format}

USER EXPORT INSTRUCTIONS:
{user_instructions}

EXTRACTION RULES:
1. The GLOBAL CONTEXT below is a JSON array where each entry represents one
   agent's output from a workflow step. Each entry has:
   - "key": a unique identifier (e.g., "task_1", "dynamic_task_0")
   - "agent_role": which agent produced this output
   - "summary": a brief description
   - "data": the structured payload (may contain "raw_output" with the full text)
2. AGENT OUTPUT FORMAT: Agent outputs follow a Vector DB format with '# Topic:' headers,
   '[KEYWORDS: ...]' blocks, and structured bullet points. These are NOT final documents —
   they are dense intermediate data. Your job is to TRANSFORM this raw pipeline data into
   polished, production-ready file content.
3. INTELLIGENTLY SELECT the relevant entries based on the requested format:
   - For a Python (.py) file: look for agent outputs containing mathematical blueprints,
     logical steps, equations, parameter lists, or model architectures. SYNTHESIZE these
     into a complete, executable Python script with proper imports, classes, and functions.
   - For Excel (.xlsx): look for tabular data, metrics, numerical results.
   - For Word (.docx) / Markdown (.md): synthesise a professional report from all relevant entries.
   - For JSON: extract structured data and parameters.
   - For email: compose a professional summary email of the workflow results.
4. If USER EXPORT INSTRUCTIONS are provided, follow them to determine what to extract.
5. Do NOT wrap the response in Markdown blocks (like ```python ... ```). Output the raw text/code ONLY.
6. If it's a Python file, ensure it is syntactically valid with all imports present.
7. If it's JSON, ensure it is valid, parseable JSON.
8. If the global context does not contain enough information, create a placeholder
   file explaining what was missing as comments or text.

GLOBAL CONTEXT (all agent outputs):
---
{global_context}
---

GENERATE THE {format} FILE CONTENT BELOW:
"""

OUTPUT_REFINER_PROMPT = """
You are Alfredo, the Master AI Editor and Quality Controller.
You receive the RAW output from a team of AI agents who executed a workflow.
Your job is to transform that raw output into a polished, clear, and user-friendly final report.

YOUR RESPONSIBILITIES:
1. **Formatting & Clarity**: Fix syntax, grammar, and structure. Use clear headings, bullet points, and numbered lists. Remove any agent-internal jargon, debugging notes, or redundant reasoning.
2. **Translation & Natural Language**: The final report MUST be written in the user's native conversational language (e.g., Italian if they requested it in Italian). Strip out "Vector DB jargon" like `# Topic:` or `[KEYWORDS: ...]` tags, and rewrite rigid bullet points into flowing, natural language.
3. **Code & Tool Outputs**: If the raw output contains code blocks, scripts, or direct tool outputs, PRESERVE the code blocks exactly as they are. Adapt and expand the surrounding explanations to provide clear context for the code.
4. **Synthesis**: Merge overlapping sections. Remove duplicate information. Ensure the report flows logically from analysis to conclusions to recommendations.
5. **Ethical Review**: Flag any content that is unethical, illegal, harmful, or promotes deceptive practices. If you find issues, add a clearly visible "⚠️ Ethical Note" section at the end.
6. **Actionability**: Ensure the report ends with concrete, prioritized next steps the user can act on.

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
6. CRITICAL: The optimized backstory MUST include the directive that the agent outputs its findings in Vector DB format: '# Topic: <Subject>' header, 1-line summary, '[KEYWORDS: ...]' block, then self-contained noun-heavy bullet points. This ensures pipeline coherence between agents.

Input Agent Details:
- Role: {role}
- Goal: {goal}
- Backstory: {backstory}

Optimize and return a JSON object with:
{{
  "role": "Optimized role description",
  "goal": "Optimized goal, clear and focused",
  "backstory": "Optimized backstory, concise and professional, describing key expertise, tone, and directives (e.g. be concise, avoid fluff, output in Vector DB format)"
}}
"""

CONTEXT_SUMMARIZER_PROMPT = """
You are Alfredo, a Master AI Summarizer.
You receive the FULL global context from a completed workflow — a JSON array of all agents' outputs.
Your job is to produce a VERY BRIEF, DENSE SUMMARY of the key results.

RULES:
1. Maximum 500 words. Be extremely concise.
2. Focus on: key findings, numerical results, decisions, formulas, parameters, conclusions.
3. Ignore internal metadata, keywords blocks, and formatting markers.
4. Use dense bullet points, no prose.
5. Preserve ALL numerical values, equations, and proper nouns exactly.
6. Write in the SAME language as the original content.
7. The summary must be self-contained — a reader with no prior context should understand
   the key outcomes of the workflow.

GLOBAL CONTEXT:
---
{global_context}
---

PRODUCE THE SUMMARY BELOW:
"""

TASK_OPTIMIZER_PROMPT = """
You are Alfredo, a Senior Prompt Engineer and Workflow Architect.
Your job is to optimize the Description and Expected Output of an AI task to ensure they are:
1. Optimized to minimize token consumption. Focus purely on required inputs, steps, and structure.
2. Structured for basic, dense intermediate pipeline communication between agents.
3. Specific, unambiguous, and focused on producing measurable outcomes.
4. Written in the SAME language as the input (keep it Italian if input is Italian, English if input is English, etc.).
5. CRITICAL: Do NOT resolve, edit, or remove any variables/placeholders in curly braces like `{{nome_variabile}}` or `{{user_input}}` or `{{previous_result}}`. Keep them exactly as they are.
6. CRITICAL ARCHITECTURE RULE: Agents NEVER write or execute final code files. Agents ONLY produce data, logic, and mathematical blueprints, saving them to the vector database. The final code is generated EXCLUSIVELY by the Master AI at the end of the workflow. If the task asks an agent to "Audit source code", "Write a python file", or "Run code", you MUST rewrite it to focus on outputting a "logical blueprint" or "validation rules" to the vector DB instead.
7. VECTOR DB FORMAT REQUIRED: The optimized 'expected_output' MUST explicitly instruct the agent to output its findings in the following format:
   - Begin with a clear '# Topic: <Subject>' header
   - Provide a 1-line summary
   - Provide a '[KEYWORDS: ...]' block
   - Provide the main content as self-contained, noun-heavy bullet points or structured data.

Input Task Details:
- Description: {description}
- Expected Output: {expected_output}

Optimize and return a JSON object with:
{{
  "description": "Optimized task description, preserving all curly brace placeholders",
  "expected_output": "Optimized expected output description, preserving all placeholders, and strictly requiring the Vector DB format (Topic, summary, KEYWORDS, bullet points)"
}}
"""

TASK_DECOMPOSER_PROMPT = """
You are Alfredo, a Senior Workflow Architect.
Your job is to analyze an AI task and decide if it is too complex or multi-step, which might cause the executing agent to produce a lazy, incomplete, or rushed output.

If the task is simple and atomic (e.g. just reading a single file, running a single simple command, or writing a basic draft), return it as-is and set "is_complex": false.

If the task is complex (e.g. designing AND implementing a feature, writing complex logic, setting up multiple files, writing code and writing tests, analyzing massive data and writing a report), you MUST decompose it into 2 to 4 sequential, highly focused subtasks.

For each generated subtask, you must:
1. Define a highly specific 'name' and 'description' that focuses on exactly ONE atomic action.
2. Formulate explicit instructions on how to use the ephemeral workspace memory:
   - For downstream subtasks, explain what key to read from the memory (e.g. read the database schema written under key 'task_schema_tablename').
   - For all subtasks, explain what key they must write their findings to (e.g. write the finalized code to key 'task_code_codename').
3. Define a clear, measurable 'expected_output'.
4. Assign it to one of the available agents.
5. Provide a specific 'agent_specialization' to tailor their persona for this micro-step.
6. Crucial: Do NOT resolve, edit, or remove any variables/placeholders in curly braces like {{nome_variabile}} or {{user_input}}. Keep them exactly as they are in the original task description so they can be resolved at runtime.
7. CRITICAL ARCHITECTURE RULE: Agents NEVER write or execute final code files. Agents ONLY produce data, logic, and mathematical blueprints, saving them to the vector database. The final code is generated EXCLUSIVELY by the Master AI at the end of the workflow. If the original task asks an agent to "Audit source code", "Write a python file", or "Run code", you MUST decompose it into subtasks that focus on generating "logical blueprints" or "validation rules" for the vector DB instead.
8. CRITICAL: The original task has a list of 'required_inputs'. You MUST distribute these required inputs into the 'required_inputs' array of the relevant subtasks where they are needed.
9. AI OPTIMIZER TOOL VERIFICATION: You must verify and assign tools. Single agents DO NOT have write or production tools. They only research and populate the ephemeral database. If a task implies web search, add 'search_web'. If it implies vector DB search, add 'vector_search'. Distribute the original tools and vector_dbs, or add new ones as strictly necessary.
10. HUMAN VALIDATION (HITL): The original task has a 'human_validation' flag. If it is true, at least ONE of your decomposed subtasks MUST have 'human_validation': true. Do NOT lose this flag.

Available Agents:
{available_agents}

Task to analyze:
- Title/Name: {name}
- Description: {description}
- Expected Output: {expected_output}
- Assigned Agent Role: {agent_role}
- Required Inputs: {required_inputs}
- Original Tools: {original_tools}
- Original Vector DBs: {original_vector_dbs}
- Human Validation: {human_validation}

Return ONLY a JSON object with this exact structure:
{{
  "is_complex": true,
  "subtasks": [
    {{
      "id": "node_sub_1",
      "name": "Subtask Name (e.g. Design Database Schema)",
      "description": "Specific atomic subtask description with memory read/write instructions. MUST preserve all original curly brace variables (e.g. {{dataset_name}}) if they are relevant to this step.",
      "expected_output": "Measurable expected output",
      "agent_role": "Role of the assigned agent (must match one of the roles in the available agents list)",
      "agent_specialization": "Micro-specialization or null",
      "tools": ["list of required tools, e.g. 'search_web', 'vector_search'"],
      "vector_dbs": ["list of vector database IDs (strings or integers) if vector_search is assigned"],
      "human_validation": false,
      "depends_on": ["list of previous subtask IDs this subtask depends on (e.g. ['node_sub_0'])"],
      "execution_level": 1,
      "required_inputs": [
        {{ "key": "dataset_name", "prompt": "Please enter dataset name" }}
      ]
    }}
  ]
}}

If the task is not complex, return this exact structure:
{{
  "is_complex": false,
  "subtasks": []
}}
}}
"""

WORKFLOW_COHERENCE_OPTIMIZER_PROMPT = """
You are Alfredo, a Senior Workflow Architect and Coherence Optimizer.
Your job is to analyze an expanded workflow plan (a sequential list of tasks) and ensure PERFECT coherence between inputs, outputs, and tools across the entire pipeline.

RULES:
1. MEMORY KEY COHERENCE:
   - Each agent writes its findings to an ephemeral memory key (defined in 'expected_output' or instructions).
   - Downstream agents read from these memory keys (defined in 'description' or 'required_inputs').
   - You MUST ensure the exact naming of these keys is consistent. If Agent 1 produces 'strategic_plan', Agent 2 MUST NOT ask for 'draft_action_plan'. Fix the 'description' or 'required_inputs' of downstream tasks to use the EXACT key name generated by upstream tasks.
2. TOOL ASSIGNMENT:
   - If a task's description requires reading from memory, ensure 'read_atomic_memory' is in the 'tools' array.
   - If a task produces an output, ensure 'write_atomic_memory' is in the 'tools' array.
   - If a task requires searching the web, ensure 'search_web' is in the 'tools' array.
   - Do NOT give write tools to agents that only read.
3. DAG COHERENCE (CRITICAL):
   - If Task B reads a memory key generated by Task A, you MUST ensure that Task A's ID is present in Task B's 'depends_on' array. This prevents race conditions during parallel execution.
4. HUMAN VALIDATION (HITL): If any task has 'human_validation': true, you MUST preserve it exactly as true. Do NOT remove it.
5. Do NOT change the logical order of tasks or delete tasks. Only modify keys, descriptions, expected outputs, and tools to ensure perfect alignment.
6. Keep all existing placeholders in curly braces like {dataset_name}.
7. Write all task outputs in the SAME language as the input.

Input Workflow Plan (JSON):
{expanded_plan}

Return the optimized workflow plan in the EXACT same JSON structure:
{
  "agents": [...],
  "tasks": [
    {
      "id": "...",
      "description": "...",
      "expected_output": "...",
      "agent_role": "...",
      "agent_specialization": "...",
      "tools": [...],
      "vector_dbs": [...],
      "human_validation": true/false,
      "depends_on": [...],
      "execution_level": 1,
      "required_inputs": [...]
    }
  ]
}
"""

VALIDATION_FORMATTER_PROMPT = """
You are Alfredo, the Master AI.
An AI agent just completed a task that requires HUMAN VALIDATION.
Your job is to read the agent's raw output and translate it into a clear, concise summary for the user.

TASK DESCRIPTION:
{task_description}

EXPECTED OUTPUT:
{expected_output}

RAW OUTPUT:
{raw_output}

CRITICAL INSTRUCTIONS:
1. You MUST output a valid JSON object strictly matching this schema:
{{
  "message": "A clear, natural language summary of what was achieved and what the user needs to decide. Do NOT include technical artifacts or terms like '\\n', 'user_messages'.",
  "options": ["Option 1", "Option 2"] 
}}
2. If the RAW OUTPUT contains a list of choices (e.g., languages, paths, options), extract them as a list of strings in the `options` array. If there are no options, leave the array empty `[]`.
3. Do NOT wrap the JSON in Markdown code blocks like ```json. Output ONLY the raw JSON object.
"""

VALIDATION_PROCESSOR_PROMPT = """
You are Alfredo, the Master AI Editor.
An agent produced an output, and the user provided feedback on it.
Your job is to rewrite the agent's output to strictly incorporate the user's feedback (e.g., keeping only the selected items, removing ignored ones, or fixing values).

CRITICAL RULES:
1. If the original output uses a structured Vector DB format (e.g., `# Topic:`, `[KEYWORDS: ]`, bullet points), you MUST maintain that format. If it does not, use plain text.
2. If the user explicitly selects an option (like a language, a specific path, or an item), update the output to clearly state that selection (e.g., "User selected: English"). Preserve the context around it so the next agent understands the decision.
3. DO NOT execute the downstream task! Your ONLY job is to RECORD the user's choice so the next agent in the pipeline knows what to do.
   - BAD example: If the user selects "Italian", do NOT translate the text into Italian yourself.
   - GOOD example: Rewrite the output as "User selected target language: Italian. The following content should be translated: [original content]"
4. If the user simply approves without changes (e.g., "ok", "approve", "va bene", "good"), return the original RAW OUTPUT unchanged. Do NOT rephrase or rewrite it.

RAW OUTPUT:
{raw_output}

USER FEEDBACK:
{user_feedback}

Rewrite and output ONLY the updated text incorporating the user's feedback. Do not include markdown code block wrappers (like ```md).
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
        # Cascade: primary → gemini-2.5-flash → gemini-3.5-flash (latest GA model)
        if complexity == "complex":
            self.model_name = "gemini-2.5-pro"           # Best reasoning, for complex tasks
            self.model_provider = "gemini"
            self.fallback_chain = [
                "gemini/gemini-2.5-flash",               # Tier-2: stable Flash
                "gemini/gemini-3.5-flash",               # Tier-3: latest GA model
            ]
        else:
            self.model_name = "gemini-2.5-flash-lite"    # Fastest, lowest traffic, stable
            self.model_provider = "gemini"
            self.fallback_chain = [
                "gemini/gemini-2.5-flash",               # Tier-2: stronger Flash
                "gemini/gemini-3.5-flash",               # Tier-3: latest GA model
            ]
        # Keep self.fallback_model for backward compat (first in chain)
        self.fallback_model = self.fallback_chain[0] if self.fallback_chain else None

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

    def _call_llm_with_retry(self, model: str, messages: list, temperature: float = 0.0, force_json_mode: bool = True) -> str:
        """
        Robust LLM call with exponential backoff retry and a multi-tier fallback cascade.

        Retry strategy per model:
          - 503 / 429 / "high demand": exponential backoff → 10s, 30s, 60s (max 3 attempts)
          - Other errors: no retry, fall through immediately to next fallback

        Fallback cascade:
          primary_model → fallback_chain[0] → fallback_chain[1] → ...
          Each tier gets its own independent retry budget.
        """
        import time

        # --- Retry waits for transient errors (seconds) ---
        RETRY_WAITS = [5, 10, 15]  # Aggressive: Google 503s typically last 30-60s, but we don't want to hang the bot
        TRANSIENT_KEYWORDS = ("503", "429", "high demand", "unavailable", "rate limit", "overloaded")

        def _is_transient(err: Exception) -> bool:
            msg = str(err).lower()
            return any(kw in msg for kw in TRANSIENT_KEYWORDS)

        def _try_model(m: str, use_json_mode: bool = True) -> str:
            """Attempt a single model with retry on transient errors."""
            last_err = None
            for attempt, wait in enumerate([0] + RETRY_WAITS):
                if wait > 0:
                    logger.warning(
                        f"MasterAI [{m}] Retry {attempt}/{len(RETRY_WAITS)} — "
                        f"waiting {wait}s before retry..."
                    )
                    time.sleep(wait)
                try:
                    call_kwargs = dict(
                        model=m,
                        messages=messages,
                        temperature=temperature,
                        timeout=45, # Prevent hanging indefinitely
                    )
                    if use_json_mode:
                        call_kwargs["response_format"] = {"type": "json_object"}
                    # Compress messages before sending to the LLM
                    call_kwargs["messages"] = _compress_messages(call_kwargs["messages"], model=m)
                    response = litellm.completion(**call_kwargs)
                    return response.choices[0].message.content
                except Exception as e:
                    last_err = e
                    if _is_transient(e) and attempt < len(RETRY_WAITS):
                        logger.warning(
                            f"MasterAI [{m}] Transient error (attempt {attempt+1}): "
                            f"{str(e)[:80]}"
                        )
                        continue  # retry with wait
                    else:
                        # Non-transient or budget exhausted — stop retrying this model
                        break
            raise last_err

        # Build the full ordered list: primary + fallback chain
        fallback_chain = getattr(self, 'fallback_chain', [])
        all_models = [model] + [fb for fb in fallback_chain if fb != model]

        last_exception = None
        for i, candidate in enumerate(all_models):
            try:
                logger.info(f"MasterAI — calling model tier {i+1}/{len(all_models)}: {candidate}")
                # Only use json_mode for the primary model call if force_json_mode is True
                result = _try_model(candidate, use_json_mode=(i == 0 and force_json_mode))
                if i > 0:
                    logger.info(f"MasterAI — succeeded on fallback tier {i+1} ({candidate}).")
                return result
            except Exception as e:
                last_exception = e
                logger.error(
                    f"MasterAI — tier {i+1} ({candidate}) exhausted all retries: "
                    f"{str(e)[:120]}"
                )

        # All tiers failed
        logger.critical(
            f"MasterAI — ALL {len(all_models)} model tiers failed. Last error: {last_exception}"
        )
        raise last_exception

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
                    {"role": "user", "content": f"SYSTEM INSTRUCTIONS:\n{system_prompt}\n\nUSER INPUT:\n{user_prompt}"}
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
            # Merged system into user to prevent Gemini system_instruction errors
            refined = self._call_llm_with_retry(
                model=model_string,
                messages=[
                    {"role": "user", "content": f"SYSTEM INSTRUCTIONS:\n{system_prompt}\n\nPlease refine the above raw output into a clear, polished report."}
                ],
                temperature=0.1,  # Low creativity — focus on restructuring, not inventing
                force_json_mode=False
            )
            logger.info(f"MasterAI Output refinement complete ({len(refined)} chars).")
            return refined
        except Exception as e:
            logger.error(f"MasterAI Output Refinement failed: {e}. Returning raw output.")
            # Graceful fallback: return the raw output if refinement fails
            return raw_str

    def summarize_global_context(self, global_context: str) -> str:
        """
        Compresses the full global context (JSON array of all agents' outputs)
        into a very brief, dense summary. This prevents massive token consumption
        during conversational follow-ups while retaining key facts and parameters.
        """
        if not global_context or len(global_context.strip()) < 100:
            return global_context

        system_prompt = CONTEXT_SUMMARIZER_PROMPT.replace("{global_context}", global_context)
        
        model_string = f"{self.model_provider}/{self.model_name}"
        if self.model_provider == "openai":
            model_string = self.model_name
        elif self.model_provider == "gemini" and "/" in self.model_name:
            model_string = self.model_name

        logger.info(f"MasterAI Summarizing context ({len(global_context)} chars) with: {model_string}")
        
        try:
            summary = self._call_llm_with_retry(
                model=model_string,
                messages=[
                    {"role": "user", "content": f"SYSTEM INSTRUCTIONS:\n{system_prompt}\n\nPlease produce the concise summary of the global context."}
                ],
                temperature=0.1,
                force_json_mode=False
            )
            logger.info(f"MasterAI Context summary complete ({len(summary)} chars).")
            return summary
        except Exception as e:
            logger.error(f"MasterAI Context summarization failed: {e}. Returning raw context.")
            return global_context

    def chat_plan(self, user_message: str, chat_history: list = None, base_workflow: dict = None, saved_context: str = None) -> Dict[str, Any]:
        """
        Engages in a conversational planning phase with the user to dynamically build
        or refine a crew of agents and tasks.
        
        Args:
            user_message: The latest message from the user.
            chat_history: List of dicts [{"role": "user"|"assistant", "content": "..."}]
            base_workflow: Optional dictionary containing an existing workflow to start from.
            saved_context: Optional JSON dump of the previous workflow's vector database.
        """
        chat_history = chat_history or []
        
        base_workflow_context = ""
        if base_workflow:
            # Fetch tasks and agents to give the LLM the exact base workflow representation
            task_ids = base_workflow.get('task_ids')
            if not task_ids and 'task_ids_json' in base_workflow:
                try:
                    task_ids = json.loads(base_workflow['task_ids_json'])
                except Exception:
                    task_ids = []
            
            wf_tasks = []
            def _build_task_rep(step):
                if isinstance(step, int):
                    t_id = step
                    dag_props = {"id": f"node_{t_id}", "execution_level": 1, "depends_on": []}
                elif isinstance(step, dict) and step.get("type") == "batch_loop":
                    tasks_rep = []
                    for inner_step in step.get("task_ids", []):
                        rep = _build_task_rep(inner_step)
                        if rep: tasks_rep.append(rep)
                    return {
                        "type": "batch_loop",
                        "id": step.get("id"),
                        "execution_level": step.get("execution_level", 1),
                        "depends_on": step.get("depends_on", []),
                        "batch_size": step.get("batch_size"),
                        "source_variable": step.get("source_variable"),
                        "tasks": tasks_rep
                    }
                else:
                    t_id = step.get("task_id")
                    dag_props = {
                        "id": step.get("id"),
                        "execution_level": step.get("execution_level", 1),
                        "depends_on": step.get("depends_on", [])
                    }
                
                t_rec = self.db_manager.read_task(int(t_id))
                if not t_rec: return None
                
                a_rec = self.db_manager.read_agent(t_rec['agent_id']) if t_rec.get('agent_id') else None
                agent_info = None
                if a_rec:
                    agent_info = {
                        "role": a_rec.get("role"),
                        "goal": a_rec.get("goal"),
                        "backstory": a_rec.get("backstory"),
                        "tools": a_rec.get("tools") or []
                    }
                
                return {
                    **dag_props,
                    "task_name": t_rec.get("name"),
                    "description": t_rec.get("description"),
                    "expected_output": t_rec.get("expected_output"),
                    "agent_specialization": t_rec.get("agent_specialization"),
                    "vector_dbs": t_rec.get("vector_dbs") or [],
                    "tools": t_rec.get("tools") or [],
                    "required_inputs": t_rec.get("required_inputs") or [],
                    "human_validation": bool(t_rec.get("human_validation")),
                    "agent": agent_info
                }

            for step in (task_ids or []):
                rep = _build_task_rep(step)
                if rep: wf_tasks.append(rep)
            
            base_workflow_context = f"""
BASE WORKFLOW LOADED:
The user has selected the following predefined workflow as a starting point:
Name: {base_workflow.get('name')}
Description: {base_workflow.get('description')}

Here are the EXACT agents, tasks, and tools defined in the database for this workflow:
{json.dumps(wf_tasks, indent=2)}

CRITICAL INSTRUCTIONS FOR PREDEFINED WORKFLOW:
1. Present/Propose this predefined workflow to the user in a natural, user-friendly language summary (without modifying its structure, tasks, agents, or tools).
2. If the user did not explicitly request changes/modifications to this predefined workflow, you MUST:
   - Keep the returned "plan" object identical in agents, tasks, and tools to the loaded base workflow.
   - Set "modified": false in your JSON output.
3. If the user explicitly asks for changes/customizations (e.g. adding tasks, changing agent goals, adding tools), modify the "plan" accordingly, describe the changes in your "response", and set "modified": true in your JSON output.
"""
        else:
            base_workflow_context = "No base workflow loaded. You must design a custom plan from scratch based on the user's request, and set \"modified\": true."
            
        saved_context_section = ""
        if saved_context:
            saved_context_section = f"""
SAVED GLOBAL CONTEXT (from previous run):
{saved_context}

CRITICAL: The user's prompt might reference this data. You can answer questions about it directly in your 'response'. If they want to start a new workflow based on this data, assume the agents will have access to it via memory and plan accordingly.
"""

        system_prompt = CHAT_PLANNER_PROMPT.replace("{base_workflow_context}", base_workflow_context).replace("{saved_context_section}", saved_context_section)
        
        # Merge system prompt into first user message to avoid Gemini errors
        messages = [{"role": "user", "content": f"SYSTEM INSTRUCTIONS:\n{system_prompt}\n\nUSER INPUT:\nPlease process the chat history."}]
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
            if "modified" not in result:
                result["modified"] = True if not base_workflow else False
                
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
                    {"role": "user", "content": f"SYSTEM INSTRUCTIONS:\n{system_prompt}\n\nUSER INPUT:\nOptimize the agent's prompts."}
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

    def format_validation_request(self, raw_output: str, task_description: str = "", expected_output: str = "") -> tuple[str, list]:
        """Translates raw agent output into a clear question for the user to validate, using context."""
        if not raw_output:
            return "⚠️ The agent produced no output. What should I do next?", []
            
        system_prompt = VALIDATION_FORMATTER_PROMPT.format(
            raw_output=raw_output,
            task_description=task_description,
            expected_output=expected_output
        )
        model_string = f"{self.model_provider}/{self.model_name}"
        if self.model_provider == "openai":
            model_string = self.model_name
        elif self.model_provider == "gemini" and "/" in self.model_name:
            model_string = self.model_name
            
        try:
            raw_content = self._call_llm_with_retry(
                model=model_string,
                messages=[{"role": "user", "content": system_prompt}],
                temperature=0.3
            )
            raw_str = raw_content.strip()
            # Try to strip markdown code blocks if the model ignored instructions
            if raw_str.startswith("```json"):
                raw_str = raw_str[7:]
            if raw_str.startswith("```"):
                raw_str = raw_str[3:]
            if raw_str.endswith("```"):
                raw_str = raw_str[:-3]
            raw_str = raw_str.strip()
            
            parsed = json.loads(raw_str)
            msg = parsed.get("message", "⚠️ Could not parse message.")
            opts = parsed.get("options", [])
            if not isinstance(opts, list):
                opts = []
            return msg, opts
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON validation request: {e}. Raw content: {raw_content}")
            return raw_content.strip(), []
        except Exception as e:
            logger.error(f"Failed to format validation request: {e}")
            return f"⚠️ **Human Validation Required**\n\nRaw Output:\n```\n{raw_output[:1000]}...\n```\n\nPlease provide your feedback:", []

    def process_validation_feedback(self, raw_output: str, user_feedback: str) -> str:
        """Rewrites the raw agent output incorporating the user's feedback."""
        system_prompt = VALIDATION_PROCESSOR_PROMPT.format(
            raw_output=raw_output, 
            user_feedback=user_feedback
        )
        model_string = f"{self.model_provider}/{self.model_name}"
        if self.model_provider == "openai":
            model_string = self.model_name
        elif self.model_provider == "gemini" and "/" in self.model_name:
            model_string = self.model_name
            
        try:
            new_output = self._call_llm_with_retry(
                model=model_string,
                messages=[
                    {"role": "user", "content": f"SYSTEM INSTRUCTIONS:\n{system_prompt}\n\nUSER INPUT:\nOriginal Output: {raw_output}\nUser Feedback: {user_feedback}"}
                ],
                temperature=0.1
            )
            new_output = new_output.strip()
            if new_output.startswith("```"):
                lines = new_output.split("\n")
                if len(lines) > 2:
                    new_output = "\n".join(lines[1:-1])
            return new_output.strip()
        except Exception as e:
            logger.error(f"Failed to process validation feedback: {e}")
            # Fallback: append the feedback manually
            return f"{raw_output}\n\n[USER FEEDBACK APPLIED]: {user_feedback}"

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
                    {"role": "user", "content": f"SYSTEM INSTRUCTIONS:\n{system_prompt}\n\nUSER INPUT:\nOptimize the task's prompts."}
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

    def generate_export_files(self, final_text: str, expected_exports: list, output_dir: str, global_context: str = None, export_instructions: str = None) -> list:
        """
        Reads the accumulated global context (or falls back to final_text) and
        generates physical files for each format requested in `expected_exports`.

        Args:
            final_text:          The last agent's output (used as fallback).
            expected_exports:    List of format keys (e.g. ["python", "excel"]).
            output_dir:          Directory where files will be written.
            global_context:      JSON string with the full memory dump from all agents.
            export_instructions: Optional user instructions for the Master AI.

        Returns a list of generated file paths.
        """
        if not expected_exports:
            return []
            
        if isinstance(expected_exports, str):
            try:
                expected_exports = json.loads(expected_exports)
                if isinstance(expected_exports, str):
                    expected_exports = [expected_exports]
            except json.JSONDecodeError:
                expected_exports = [expected_exports]
                
        try:
            from core.export_tools import EXPORT_TOOL_MAP
        except ImportError:
            logger.error("Could not import EXPORT_TOOL_MAP from core.export_tools.")
            EXPORT_TOOL_MAP = {}
            
        generated_files = []
        os.makedirs(output_dir, exist_ok=True)
        
        model_string = f"{self.model_provider}/{self.model_name}"
        if self.model_provider == "openai":
            model_string = self.model_name
        elif self.model_provider == "gemini" and "/" in self.model_name:
            model_string = self.model_name

        # Use global context if available, otherwise fall back to final_text
        context_for_export = global_context if global_context else final_text
        instructions_text = export_instructions.strip() if export_instructions else "No specific instructions provided. Use your best judgement."

        for ext in expected_exports:
            ext = ext.strip().lower()
            if not ext:
                continue
                
            logger.info(f"MasterAI generating export file for format: {ext}")
            system_prompt = EXPORT_GENERATOR_PROMPT.format(
                format=ext,
                global_context=context_for_export,
                user_instructions=instructions_text
            )
            
            try:
                raw_content = self._call_llm_with_retry(
                    model=model_string,
                    messages=[
                        {"role": "user", "content": system_prompt}
                    ],
                    temperature=0.1
                )
                
                # Strip leading/trailing markdown blocks if the LLM ignored instructions
                raw_content = raw_content.strip()
                if raw_content.startswith(f"```{ext}"):
                    raw_content = raw_content[len(ext)+3:]
                elif raw_content.startswith("```"):
                    raw_content = raw_content[3:]
                if raw_content.endswith("```"):
                    raw_content = raw_content[:-3]
                    
                raw_content = raw_content.strip()
                
                # Check if the requested export is supported in our tool map
                if ext in EXPORT_TOOL_MAP:
                    tool = EXPORT_TOOL_MAP[ext]
                    func = tool["func"]
                    actual_ext = tool["ext"]
                    file_path = os.path.join(output_dir, f"output.{actual_ext}")
                    
                    # Execute the mapped function
                    generated_path = func(raw_content, file_path)
                    generated_files.append(generated_path)
                else:
                    # Fallback for unknown extensions
                    logger.warning(f"No specific tool found for export '{ext}'. Falling back to raw text.")
                    file_path = os.path.join(output_dir, f"output.{ext}")
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(raw_content)
                    generated_files.append(file_path)
            except Exception as e:
                logger.error(f"Failed to generate export file {ext}: {e}")
                
        return generated_files

    def decompose_task_if_complex(self, task: Dict[str, Any], available_agents: list) -> list:
        """
        Analyzes a single task and decomposes it into 2-4 atomic subtasks if it is complex.
        Returns a list of task dicts (either the original task or the decomposed subtasks).
        """
        name = task.get('name') or task.get('title') or "Unnamed Task"
        description = task.get('description') or ""
        expected_output = task.get('expected_output') or ""
        agent_role = task.get('agent_role') or ""

        # Format agents list for the LLM
        formatted_agents = []
        for agent in available_agents:
            formatted_agents.append({
                "role": agent.get("role"),
                "goal": agent.get("goal"),
                "backstory": agent.get("backstory", "")[:200] + "..." if len(agent.get("backstory", "")) > 200 else agent.get("backstory", "")
            })

        system_prompt = TASK_DECOMPOSER_PROMPT.format(
            available_agents=json.dumps(formatted_agents, indent=2),
            name=name,
            description=description,
            expected_output=expected_output,
            agent_role=agent_role,
            required_inputs=json.dumps(task.get('required_inputs') or [], indent=2),
            original_tools=json.dumps(task.get('tools') or [], indent=2),
            original_vector_dbs=json.dumps(task.get('vector_dbs') or [], indent=2),
            human_validation=task.get('human_validation', False)
        )

        model_string = f"{self.model_provider}/{self.model_name}"
        if self.model_provider == "openai":
            model_string = self.model_name
        elif self.model_provider == "gemini" and "/" in self.model_name:
            model_string = self.model_name

        logger.info(f"MasterAI analyzing complexity of task '{name}' with {model_string}...")
        try:
            raw_output = self._call_llm_with_retry(
                model=model_string,
                messages=[
                    {"role": "user", "content": f"SYSTEM INSTRUCTIONS:\n{system_prompt}\n\nUSER INPUT:\nAnalyze and decompose this task if it is complex."}
                ],
                temperature=0.2
            )
            clean_json = self._sanitize_json(raw_output)
            result = json.loads(clean_json)

            if result.get("is_complex") and result.get("subtasks"):
                subtasks = result["subtasks"]
                logger.info(f"Task '{name}' decomposed successfully into {len(subtasks)} subtasks!")
                return subtasks
            else:
                logger.info(f"Task '{name}' is not complex. Keeping original.")
                return [task]
        except Exception as e:
            logger.error(f"Failed to decompose task '{name}': {e}. Returning original.")
            return [task]

    def decompose_workflow_plan(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        """
        Takes a full workflow plan (agents and tasks), evaluates each task,
        decomposes the complex ones, and returns an expanded/atomized plan.
        """
        if not plan or 'tasks' not in plan:
            return plan

        agents = plan.get('agents') or []
        
        # If agents list is empty, try to fetch all agents from the database as fallback
        if not agents:
            try:
                db_agents = self.db_manager.read_all_agents()
                for a in db_agents:
                    agents.append({
                        "role": a.get("role"),
                        "goal": a.get("goal"),
                        "backstory": a.get("backstory"),
                        "tools": a.get("tools") or []
                    })
                logger.info(f"Workflow plan had no agents. Loaded {len(agents)} agents from DB as fallback.")
            except Exception as e:
                logger.error(f"Failed to load fallback agents from DB: {e}")

        # Ensure we have a list of available agents
        available_agents = agents

        expanded_tasks = []
        for task in plan.get('tasks', []):
            decomposed = self.decompose_task_if_complex(task, available_agents)
            expanded_tasks.extend(decomposed)

        expanded_plan = {
            "agents": agents,
            "tasks": expanded_tasks
        }
        logger.info(f"Workflow expansion complete. Total tasks: {len(plan.get('tasks', []))} -> {len(expanded_tasks)}. Starting Coherence Optimizer...")
        
        # Apply Coherence Optimizer
        optimized_plan = self.optimize_workflow_coherence(expanded_plan)
        
        return optimized_plan

    def optimize_workflow_coherence(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        """
        Takes an expanded workflow plan and runs a final LLM pass to ensure
        all memory keys (read/write) are coherent and tools are correctly distributed.
        """
        plan_json = json.dumps(plan, indent=2, ensure_ascii=False)
        system_prompt = WORKFLOW_COHERENCE_OPTIMIZER_PROMPT.replace("{expanded_plan}", plan_json)
        
        model_string = f"{self.model_provider}/{self.model_name}"
        if self.model_provider == "openai":
            model_string = self.model_name
        elif self.model_provider == "gemini" and "/" in self.model_name:
            model_string = self.model_name
            
        logger.info(f"MasterAI: Running Workflow Coherence Optimizer with model: {model_string}")
        try:
            raw_output = self._call_llm_with_retry(
                model=model_string,
                messages=[
                    {"role": "user", "content": f"SYSTEM INSTRUCTIONS:\nYou are the Workflow Coherence Optimizer.\n\nUSER INPUT:\n{system_prompt}"}
                ]
            )
            clean_json = self._sanitize_json(raw_output)
            optimized_plan = json.loads(clean_json)
            logger.info("MasterAI: Workflow Coherence Optimizer completed successfully.")
            return optimized_plan
        except Exception as e:
            logger.error(f"Failed to optimize workflow coherence: {e}. Returning unoptimized expanded plan.")
            return plan