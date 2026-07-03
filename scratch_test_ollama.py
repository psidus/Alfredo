from langchain_community.embeddings import OllamaEmbeddings

try:
    print("Testing with headers=...")
    embed = OllamaEmbeddings(model="test", headers={"Authorization": "Bearer TEST"})
    print("Success with headers!")
except Exception as e:
    print(f"Failed with headers: {e}")

try:
    print("Testing with client_kwargs=...")
    embed = OllamaEmbeddings(model="test", client_kwargs={"headers": {"Authorization": "Bearer TEST"}})
    print("Success with client_kwargs!")
except Exception as e:
    print(f"Failed with client_kwargs: {e}")
