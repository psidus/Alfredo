try:
    from langchain_ollama import OllamaEmbeddings
    print("Module is:", OllamaEmbeddings.__module__)
except ImportError:
    from langchain_community.embeddings import OllamaEmbeddings
    print("Module is:", OllamaEmbeddings.__module__)
