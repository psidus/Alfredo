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
    elif "LMSTUDIO" in env_key or "LM_STUDIO" in env_key:
        if env_key in ("LMSTUDIO_API_BASE", "LM_STUDIO_API_BASE"):
            return _fetch_lmstudio(api_key="", base_url_override=api_key)
        return _fetch_lmstudio(api_key=api_key)
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

def _ollama_candidate_urls(base_url_override: str = None) -> list:
    """Return ordered, deduplicated Ollama base URLs to probe."""
    import os

    raw_candidates = [
        base_url_override,
        os.getenv("OLLAMA_API_BASE"),
        "http://127.0.0.1:11434",
        "http://localhost:11434",
        "http://host.docker.internal:11434",
    ]
    seen = set()
    candidates = []
    for raw in raw_candidates:
        val = str(raw or "").strip().rstrip("/")
        if not val or val in seen:
            continue
        seen.add(val)
        candidates.append(val)
    return candidates


def list_local_ollama_manifest_models(ollama_models_dir: str = None, use_cli_fallback: bool = True) -> dict:
    """Discover downloaded Ollama models from local manifest files and optionally `ollama list` CLI."""
    import json
    import os
    import subprocess

    base_dir = (
        str(ollama_models_dir).strip()
        if ollama_models_dir
        else (os.getenv("OLLAMA_MODELS") or "").strip()
    )
    search_roots = (
        [base_dir]
        if base_dir
        else [
            os.path.join(os.path.expanduser("~"), ".ollama", "models"),
            "/usr/share/ollama/.ollama/models",
            "C:\\ProgramData\\Ollama\\models",
        ]
    )

    discovered = {}
    for root in search_roots:
        manifests_dir = os.path.join(root, "manifests")
        if not os.path.isdir(manifests_dir):
            continue
        for dirpath, _dirnames, filenames in os.walk(manifests_dir):
            for fname in filenames:
                if fname.startswith("."):
                    continue
                fpath = os.path.join(dirpath, fname)
                rel = os.path.relpath(fpath, manifests_dir)
                parts = rel.replace("\\", "/").split("/")
                if len(parts) == 4:
                    registry, namespace, model, tag = parts
                    if registry == "registry.ollama.ai" and namespace == "library":
                        model_id = f"{model}:{tag}"
                    elif registry == "registry.ollama.ai":
                        model_id = f"{namespace}/{model}:{tag}"
                    else:
                        model_id = f"{registry}/{namespace}/{model}:{tag}"
                elif len(parts) == 3:
                    namespace, model, tag = parts
                    model_id = f"{model}:{tag}" if namespace == "library" else f"{namespace}/{model}:{tag}"
                elif len(parts) == 2:
                    model_id = f"{parts[0]}:{parts[1]}"
                else:
                    continue

                size_gb = 0.0
                try:
                    with open(fpath, "r", encoding="utf-8") as mf:
                        mdata = json.load(mf)
                    total_bytes = int((mdata.get("config") or {}).get("size") or 0)
                    for layer in (mdata.get("layers") or []):
                        total_bytes += int(layer.get("size") or 0)
                    if total_bytes > 0:
                        size_gb = round(total_bytes / (1024**3), 1)
                except Exception:
                    pass
                discovered.setdefault(model_id, size_gb)

    if not discovered and use_cli_fallback:
        try:
            proc = subprocess.run(
                ["ollama", "list"],
                capture_output=True,
                text=True,
                timeout=4,
            )
            if proc.returncode == 0 and proc.stdout:
                for line in proc.stdout.splitlines()[1:]:
                    cols = line.split()
                    if not cols:
                        continue
                    model_id = cols[0].strip()
                    if not model_id or model_id.upper() == "NAME":
                        continue
                    size_gb = 0.0
                    for idx, tok in enumerate(cols[:-1]):
                        nxt = cols[idx + 1].upper()
                        try:
                            val = float(tok)
                        except ValueError:
                            continue
                        if nxt == "GB":
                            size_gb = round(val, 1)
                            break
                        if nxt == "MB":
                            size_gb = round(val / 1024.0, 1)
                            break
                    discovered.setdefault(model_id, size_gb)
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            pass

    chat_models = []
    embed_models = []
    chat_models_detailed = []
    for model_id, size_gb in discovered.items():
        low = model_id.lower()
        if "embed" in low or "nomic" in low or "mxbai" in low:
            embed_models.append(model_id)
        else:
            chat_models.append(model_id)
            chat_models_detailed.append({"name": model_id, "size_gb": size_gb})

    chat_models.sort()
    embed_models.sort()
    chat_models_detailed.sort(key=lambda x: x["name"])
    return {
        "chat_models": chat_models,
        "embed_models": embed_models,
        "chat_models_detailed": chat_models_detailed,
    }


def _fetch_ollama(api_key: str = "", base_url_override: str = None):
    import os

    candidates = _ollama_candidate_urls(base_url_override=base_url_override)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    errors = []

    for base_url in candidates:
        url = f"{base_url}/api/tags"
        try:
            response = requests.get(url, headers=headers, timeout=4)
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
            os.environ["OLLAMA_API_BASE"] = base_url
            return {
                "success": True,
                "chat_models": chat_models,
                "embed_models": embed_models,
                "chat_models_detailed": chat_models_detailed,
                "resolved_base_url": base_url,
            }
        except Exception as e:
            errors.append(f"{url}: {e}")

    local = list_local_ollama_manifest_models()
    if local["chat_models"] or local["embed_models"]:
        return {
            "success": True,
            "chat_models": local["chat_models"],
            "embed_models": local["embed_models"],
            "chat_models_detailed": local["chat_models_detailed"],
            "resolved_base_url": candidates[0] if candidates else "http://localhost:11434",
            "offline_manifest_fallback": True,
            "warning": f"Ollama API unreachable ({errors[0] if errors else 'connection failed'}); loaded local models from disk/CLI.",
        }

    return {"success": False, "error": f"Failed to fetch Ollama models from {'; '.join(errors)}"}

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


def openai_compatible_v1_base(base_url: str) -> str:
    """Normalize a local OpenAI-compatible base URL to .../v1."""
    u = (base_url or "").strip().rstrip("/")
    if not u:
        return "http://127.0.0.1:1234/v1"
    if u.endswith("/v1"):
        return u
    return f"{u}/v1"


def estimate_vram_gb_from_model_id(model_id: str) -> float:
    """
    Best-effort VRAM estimate (GB) from model id when API metadata has no size.
    Example ids: qwen3.8-27b-q4_k_m, llama-3.1-8b-instruct-fp16.
    """
    import re

    mid = (model_id or "").lower()
    params_b = None
    m = re.search(r"(\d+(?:\.\d+)?)\s*b(?:\b|[^a-z0-9])", mid)
    if m:
        try:
            params_b = float(m.group(1))
        except Exception:
            params_b = None

    if not params_b or params_b <= 0:
        return 4.0

    quant_factor = 1.0  # Roughly Q6/K as neutral baseline
    if "fp16" in mid or "f16" in mid or "bf16" in mid:
        quant_factor = 2.0
    elif "q8" in mid:
        quant_factor = 1.25
    elif "q6" in mid:
        quant_factor = 1.0
    elif "q5" in mid:
        quant_factor = 0.86
    elif "q4" in mid:
        quant_factor = 0.72
    elif "q3" in mid:
        quant_factor = 0.58
    elif "q2" in mid:
        quant_factor = 0.45

    overhead = 1.5 if params_b >= 20 else 1.0
    estimated = (params_b * quant_factor) + overhead
    return round(max(1.0, estimated), 1)


def estimate_supports_tools_from_model_id(model_id: str, provider: str = "") -> bool:
    """
    Best-effort estimate for tool/function-calling support.
    Used as an automatic default for local models; users can still override in UI.
    """
    mid = (model_id or "").lower()
    prov = (provider or "").lower()

    # Embedding models are never agent tool-call models.
    if any(k in mid for k in ("embed", "embedding", "nomic-embed", "mxbai-embed")):
        return False

    # Families/variants commonly shipped with tool-calling support.
    strong_positive = (
        "tool", "function", "fncall", "functioncall",
        "gpt-oss", "qwen3", "qwen2.5", "qwen2.5-coder",
        "llama-3.1", "llama3.1", "llama-3.2", "llama3.2",
        "mistral-nemo", "mistral-small-3", "command-r"
    )
    if any(k in mid for k in strong_positive):
        return True

    # Older local families are usually weak on native tool-calling reliability.
    likely_negative = ("phi3", "phi-3", "gemma2")
    if any(k in mid for k in likely_negative):
        return False

    # Runtime defaults: modern Ollama / LM Studio models are often tool-capable.
    if prov in ("ollama", "lmstudio", "lm_studio", "bionic", "vllm"):
        return True

    return True


def default_lmstudio_models_dir() -> str:
    """Per-machine GGUF folder: env override, else ~/.lmstudio/models for the current user."""
    import os
    custom = (os.getenv("LMSTUDIO_MODELS_DIR") or "").strip().strip('"')
    if custom:
        return os.path.expanduser(custom)
    return os.path.join(os.path.expanduser("~"), ".lmstudio", "models")


def _fetch_lmstudio(api_key: str = "", base_url_override: str = None):
    """List models from LM Studio / Bionic OpenAI-compatible local server."""
    import os
    base_url = (
        base_url_override
        or os.getenv("LMSTUDIO_API_BASE")
        or os.getenv("LM_STUDIO_API_BASE")
        or ("http://host.docker.internal:1234" if os.path.exists("/.dockerenv") else "http://127.0.0.1:1234")
    )
    if os.path.exists("/.dockerenv") and ("localhost" in base_url or "127.0.0.1" in base_url):
        base_url = base_url.replace("localhost", "host.docker.internal").replace("127.0.0.1", "host.docker.internal")
    url = f"{openai_compatible_v1_base(base_url)}/models"
    headers = {"Authorization": f"Bearer {api_key or 'lm-studio'}"}
    try:
        response = requests.get(url, headers=headers, timeout=2)
        if response.status_code in [401, 403]:
            return {"success": False, "error": "Invalid API Key"}
        response.raise_for_status()

        data = response.json().get("data", [])
        chat_models = []
        embed_models = []
        chat_models_detailed = []

        for m in data:
            model_id = m.get("id", "")
            if not model_id:
                continue
            size_gb = 0.0
            meta_size = m.get("size") or (m.get("meta") or {}).get("size")
            if isinstance(meta_size, (int, float)) and meta_size > 0:
                size_gb = round(meta_size / (1024 ** 3), 1)
            else:
                size_gb = estimate_vram_gb_from_model_id(model_id)
            if "embed" in model_id.lower() or "nomic" in model_id.lower() or "mxbai" in model_id.lower():
                embed_models.append(model_id)
            else:
                chat_models.append(model_id)
                chat_models_detailed.append({"name": model_id, "size_gb": size_gb})

        chat_models.sort()
        embed_models.sort()
        chat_models_detailed.sort(key=lambda x: x["name"])
        return {
            "success": True,
            "chat_models": chat_models,
            "embed_models": embed_models,
            "chat_models_detailed": chat_models_detailed,
        }
    except Exception as e:
        return {"success": False, "error": f"Failed to fetch LM Studio models from {url}: {e}"}


def list_local_gguf_files(root_dir: str):
    """Scan a folder (e.g. LM Studio models dir) for GGUF weights, skipping mmproj files."""
    import os
    results = []
    if not root_dir or not os.path.isdir(root_dir):
        return results
    for dirpath, _dirnames, filenames in os.walk(root_dir):
        for fn in filenames:
            lower = fn.lower()
            if not lower.endswith(".gguf"):
                continue
            if "mmproj" in lower:
                continue
            path = os.path.join(dirpath, fn)
            try:
                size_gb = round(os.path.getsize(path) / (1024 ** 3), 1)
            except OSError:
                size_gb = 0.0
            results.append({
                "name": fn,
                "path": path,
                "rel": os.path.relpath(path, root_dir),
                "size_gb": size_gb,
            })
    results.sort(key=lambda x: x["rel"].lower())
    return results


def import_gguf_to_ollama(gguf_path: str, model_name: str, timeout: int = 1800):
    """Register an already-downloaded GGUF with local Ollama (no network pull)."""
    import os
    import subprocess
    import tempfile

    if not gguf_path or not os.path.isfile(gguf_path):
        return {"success": False, "error": f"GGUF not found: {gguf_path}"}
    model_name = (model_name or "").strip().replace(" ", "-")
    if not model_name:
        return {"success": False, "error": "Model name is required."}

    abs_path = os.path.abspath(gguf_path).replace("\\", "/")
    with tempfile.TemporaryDirectory() as tmp:
        modelfile_path = os.path.join(tmp, "Modelfile")
        with open(modelfile_path, "w", encoding="utf-8") as mf:
            mf.write(f'FROM "{abs_path}"\n')
        try:
            result = subprocess.run(
                ["ollama", "create", model_name, "-f", modelfile_path],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError:
            return {
                "success": False,
                "error": "Ollama CLI not found. Install or update Ollama on Windows, then retry.",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            return {"success": False, "error": err or "ollama create failed"}
        return {
            "success": True,
            "model_name": model_name,
            "message": (result.stdout or "").strip() or f"Created {model_name}",
        }
