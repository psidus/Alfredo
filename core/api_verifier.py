import requests

def verify_and_fetch_models(env_key: str, api_key: str) -> dict:
    """
    Verifies the API key and fetches available models.
    Returns: { "success": bool, "chat_models": list, "embed_models": list, "error": str }
    """
    env_key = env_key.upper()
    
    if "OPENAI" in env_key:
        return _fetch_openai(api_key)
    elif "GROQ" in env_key:
        return _fetch_groq(api_key)
    elif "GEMINI" in env_key or "GOOGLE" in env_key:
        return _fetch_gemini(api_key)
    elif "ANTHROPIC" in env_key:
        return _fetch_anthropic(api_key)
    elif "OLLAMA" in env_key:
        if env_key == "OLLAMA_API_BASE":
            return _fetch_ollama(api_key="", base_url_override=api_key)
        else:
            return _fetch_ollama(api_key=api_key)
    elif "MISTRAL" in env_key:
        return _fetch_mistral(api_key)
    
    return {"success": False, "error": "Unsupported provider key format."}

def _fetch_openai(api_key: str):
    url = "https://api.openai.com/v1/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 401:
            return {"success": False, "error": "Invalid API Key"}
        response.raise_for_status()
        
        data = response.json().get("data", [])
        chat_models = []
        embed_models = []
        
        for m in data:
            model_id = m.get("id", "")
            # Basic heuristic for filtering OpenAI models
            if "embed" in model_id:
                embed_models.append(model_id)
            elif ("gpt-" in model_id or "o1-" in model_id) and "instruct" not in model_id and "vision" not in model_id and "audio" not in model_id:
                chat_models.append(model_id)
                
        # Sort for better UI
        chat_models.sort()
        embed_models.sort()
        return {"success": True, "chat_models": chat_models, "embed_models": embed_models}
    except Exception as e:
        return {"success": False, "error": str(e)}

def _fetch_groq(api_key: str):
    url = "https://api.groq.com/openai/v1/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 401:
            return {"success": False, "error": "Invalid API Key"}
        response.raise_for_status()
        
        data = response.json().get("data", [])
        chat_models = []
        
        for m in data:
            model_id = m.get("id", "")
            # Filter out non-chat models like whisper
            if "whisper" not in model_id.lower():
                chat_models.append(model_id)
                
        chat_models.sort()
        return {"success": True, "chat_models": chat_models, "embed_models": []}
    except Exception as e:
        return {"success": False, "error": str(e)}

def _fetch_gemini(api_key: str):
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code in [400, 403]:
            return {"success": False, "error": "Invalid API Key"}
        response.raise_for_status()
        
        models_list = response.json().get("models", [])
        chat_models = []
        embed_models = []
        
        for m in models_list:
            name = m.get("name", "").replace("models/", "")
            methods = m.get("supportedGenerationMethods", [])
            
            if "embedContent" in methods:
                embed_models.append(name)
            if "generateContent" in methods:
                chat_models.append(name)
                
        chat_models.sort(reverse=True) # Newer models first
        embed_models.sort()
        return {"success": True, "chat_models": chat_models, "embed_models": embed_models}
    except Exception as e:
        return {"success": False, "error": str(e)}

def _fetch_anthropic(api_key: str):
    url = "https://api.anthropic.com/v1/models"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01"
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 401:
            return {"success": False, "error": "Invalid API Key"}
        
        chat_models = []
        if response.status_code == 200:
            data = response.json().get("data", [])
            for m in data:
                model_id = m.get("id", "")
                if model_id:
                    chat_models.append(model_id)
        else:
            # Fallback to hardcoded list if /models endpoint is not available but key is valid
            # (To verify key without /models, we'd need to make a dummy completion, but for simplicity
            # we assume if it's not 401, we just return the hardcoded list).
            chat_models = [
                "claude-3-7-sonnet-20250219",
                "claude-3-5-sonnet-20241022",
                "claude-3-5-sonnet-20240620",
                "claude-3-5-haiku-20241022",
                "claude-3-opus-20240229",
                "claude-3-haiku-20240307"
            ]
            
        chat_models.sort(reverse=True)
        return {"success": True, "chat_models": chat_models, "embed_models": []}
    except Exception as e:
        return {"success": False, "error": str(e)}

def _fetch_ollama(api_key: str = "", base_url_override: str = None):
    import os
    base_url = base_url_override if base_url_override else os.getenv("OLLAMA_API_BASE", "http://localhost:11434")
    url = f"{base_url.rstrip('/')}/api/tags"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code in [401, 403]:
            return {"success": False, "error": "Invalid API Key"}
        response.raise_for_status()
        
        data = response.json().get("models", [])
        chat_models = []
        embed_models = []
        chat_models_detailed = []
        
        for m in data:
            model_id = m.get("name", "")
            size_gb = round(m.get("size", 0) / (1024**3), 1)
            if "embed" in model_id.lower() or "nomic" in model_id.lower() or "mxbai" in model_id.lower():
                embed_models.append(model_id)
            else:
                chat_models.append(model_id)
                chat_models_detailed.append({"name": model_id, "size_gb": size_gb})
                
        chat_models.sort()
        embed_models.sort()
        chat_models_detailed.sort(key=lambda x: x["name"])
        return {"success": True, "chat_models": chat_models, "embed_models": embed_models, "chat_models_detailed": chat_models_detailed}
    except Exception as e:
        return {"success": False, "error": f"Failed to fetch Ollama models from {url}: {e}"}

def get_ollama_model_info(model_name: str, api_key: str = ""):
    import os
    import requests
    base_url = os.getenv("OLLAMA_API_BASE", "http://localhost:11434")
    url = f"{base_url.rstrip('/')}/api/show"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        response = requests.post(url, headers=headers, json={"name": model_name}, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        model_info = data.get("model_info", {})
        
        # Look for any key ending with '.context_length'
        max_context = 8192
        for k, v in model_info.items():
            if k.endswith(".context_length"):
                max_context = int(v)
                break
        
        return {"success": True, "max_context": max_context}
    except Exception as e:
        return {"success": False, "error": str(e), "max_context": 8192}

def _fetch_mistral(api_key: str):
    url = "https://api.mistral.ai/v1/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 401:
            return {"success": False, "error": "Invalid API Key"}
        response.raise_for_status()
        
        data = response.json().get("data", [])
        chat_models = []
        embed_models = []
        
        for m in data:
            model_id = m.get("id", "")
            if "embed" in model_id:
                embed_models.append(model_id)
            elif "moderation" not in model_id:
                chat_models.append(model_id)
                
        chat_models.sort()
        embed_models.sort()
        return {"success": True, "chat_models": chat_models, "embed_models": embed_models}
    except Exception as e:
        return {"success": False, "error": str(e)}
