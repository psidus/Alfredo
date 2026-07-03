from langchain_ollama import OllamaEmbeddings
import traceback

def try_init(name, **kwargs):
    print(f"Testing {name}...")
    try:
        embed = OllamaEmbeddings(model="test", **kwargs)
        print(f"  Success: {name}")
    except Exception as e:
        print(f"  Failed: {name} -> {type(e).__name__}: {e}")

try_init("client_kwargs", client_kwargs={"headers": {"Authorization": "Bearer TEST"}})
try_init("headers", headers={"Authorization": "Bearer TEST"})
