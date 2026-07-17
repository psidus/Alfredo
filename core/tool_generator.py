"""
core/tool_generator.py

Uses an LLM to generate Python code for custom CrewAI tools.
"""

import logging
import os
import litellm
import re
from core.db_manager import DBManager
from core.data_manager import DataManager

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert Python developer specialized in creating custom tools for the CrewAI framework.
The user will provide a description of what the tool should do.
Your goal is to output ONLY valid Python code containing a single function decorated with @tool from crewai.tools.

Requirements:
1. Import @tool: `from crewai.tools import tool`
2. Use type hints for all arguments.
3. Include a very clear docstring describing what the tool does and its arguments. The docstring is used by the LLM agent to understand how to use the tool.
4. If the tool needs libraries, import them INSIDE the function.
5. If the tool needs API keys, use `os.environ.get('API_KEY_NAME')`.
6. Return a string in case of success or a string with "Error: ..." in case of failure.
7. Output ONLY the Python code inside a ```python block. No preamble, no explanation.

Example:
```python
from crewai.tools import tool
import os

@tool
def custom_weather_tool(city: str) -> str:
    \"\"\"Fetches the current weather for a given city.\"\"\"
    import requests
    api_key = os.environ.get("OPENWEATHER_API_KEY")
    if not api_key:
        return "Error: OPENWEATHER_API_KEY not found."
    
    url = f"http://api.openweathermap.org/data/2.5/weather?q={city}&appid={api_key}"
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        return f"The weather in {city} is {data['weather'][0]['description']}."
    except Exception as e:
        return f"Error fetching weather: {e}"
```
"""

def generate_tool_code(prompt: str, model_id: int) -> str:
    """
    Generates Python code for a CrewAI tool using the specified model.
    """
    db = DBManager()
    DataManager.load_env()
    
    model_record = db.read_model(model_id)
    if not model_record:
        raise ValueError(f"Model ID {model_id} not found in database.")
        
    provider = model_record['provider'].lower()
    model_name = model_record['model_name']
    
    model_string = model_name
    if provider == "openai":
        model_string = model_name
    elif provider == "gemini" or provider == "google":
        model_string = f"gemini/{model_name.split('/')[-1]}" if "/" in model_name else f"gemini/{model_name}"
    else:
        model_string = f"{provider}/{model_name}"

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt}
    ]

    completion_kwargs = {
        "model": model_string,
        "messages": messages,
        "temperature": 0.2,
        "timeout": 30
    }
    
    if provider in ["ollama", "lmstudio", "vllm"] or model_record.get('is_local'):
        api_base_env_var = f"{provider.upper()}_API_BASE"
        if os.environ.get(api_base_env_var):
            completion_kwargs["api_base"] = os.environ.get(api_base_env_var)

    try:
        response = litellm.completion(**completion_kwargs)
        
        content = response.choices[0].message.content.strip()
        
        # Extract code from markdown block if present
        match = re.search(r"```python\s*(.*?)\s*```", content, re.DOTALL)
        if match:
            return match.group(1).strip()
        
        match_generic = re.search(r"```\s*(.*?)\s*```", content, re.DOTALL)
        if match_generic:
            return match_generic.group(1).strip()
            
        return content
        
    except Exception as e:
        logger.error(f"Error generating tool code: {e}")
        raise e

def generate_tool_code_stream(prompt: str, model_id: int):
    """
    Generates Python code for a CrewAI tool and yields chunks (streaming).
    """
    db = DBManager()
    DataManager.load_env()
    
    model_record = db.read_model(model_id)
    if not model_record:
        raise ValueError(f"Model ID {model_id} not found in database.")
        
    provider = model_record['provider'].lower()
    model_name = model_record['model_name']
    
    model_string = model_name
    if provider == "openai":
        model_string = model_name
    elif provider == "gemini" or provider == "google":
        model_string = f"gemini/{model_name.split('/')[-1]}" if "/" in model_name else f"gemini/{model_name}"
    else:
        model_string = f"{provider}/{model_name}"

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt}
    ]

    completion_kwargs = {
        "model": model_string,
        "messages": messages,
        "temperature": 0.2,
        "stream": True,
        "max_tokens": 2000,
        "timeout": 30
    }
    
    # Load API keys fresh from file in case they were updated in UI
    from dotenv import dotenv_values, find_dotenv
    env_path = find_dotenv()
    current_env = dotenv_values(env_path) if env_path else {}
    
    provider_key_env_map = {
        "openai": "OPENAI_API_KEY",
        "google": "GEMINI_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "groq": "GROQ_API_KEY",
        "mistral": "MISTRAL_API_KEY",
    }
    env_var_name = provider_key_env_map.get(provider, f"{provider.upper()}_API_KEY")
    api_key = current_env.get(env_var_name) or os.environ.get(env_var_name)
    
    if api_key:
        completion_kwargs["api_key"] = api_key
    
    if provider in ["ollama", "lmstudio", "vllm"] or model_record.get('is_local'):
        api_base_env_var = f"{provider.upper()}_API_BASE"
        if os.environ.get(api_base_env_var):
            completion_kwargs["api_base"] = os.environ.get(api_base_env_var)

    import time
    RETRY_WAITS = [5, 10, 15]
    TRANSIENT_KEYWORDS = ("503", "429", "high demand", "unavailable", "rate limit", "overloaded")

    def _is_transient(err: Exception) -> bool:
        return any(kw in str(err).lower() for kw in TRANSIENT_KEYWORDS)

    last_err = None
    for attempt, wait in enumerate([0] + RETRY_WAITS):
        if wait > 0:
            logger.warning(f"ToolGenerator [Stream] Retry {attempt}/{len(RETRY_WAITS)} — waiting {wait}s...")
            time.sleep(wait)
        try:
            response = litellm.completion(**completion_kwargs)
            for chunk in response:
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta
            return # Success, exit retry loop
        except Exception as e:
            last_err = e
            if _is_transient(e) and attempt < len(RETRY_WAITS):
                logger.warning(f"ToolGenerator [Stream] Transient error (attempt {attempt+1}): {str(e)[:80]}")
                continue
            else:
                break
                
    logger.error(f"Error streaming tool code after retries: {last_err}")
    raise last_err
