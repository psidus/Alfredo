import os
import shutil
import logging
from typing import List, Dict, Any, Optional

from langchain_community.document_loaders import PyPDFLoader, TextLoader, Docx2txtLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_chroma import Chroma

# Embedding imports
from langchain_openai import OpenAIEmbeddings
from langchain_community.embeddings import OllamaEmbeddings
from core.data_manager import DataManager

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class VectorManager:
    """
    Manages the creation, population, and querying of local vector databases using ChromaDB.
    """
    def __init__(self, storage_dir: str = "storage/vector_dbs"):
        self.storage_dir = storage_dir
        os.makedirs(self.storage_dir, exist_ok=True)
        
    def _get_embedding_function(self, provider: str, model_name: str) -> Any:
        """
        Instantiates the appropriate embedding function based on the provider.
        """
        provider = provider.lower()
        if provider == 'openai':
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                DataManager.load_env()
                api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise ValueError("OPENAI_API_KEY is not set. Cannot use OpenAI embeddings.")
            return OpenAIEmbeddings(model=model_name, api_key=api_key)
        elif provider == 'ollama':
            return OllamaEmbeddings(model=model_name)
        elif provider == 'gemini' or provider == 'google':
            # Needs langchain_google_genai
            try:
                from langchain_google_genai import GoogleGenerativeAIEmbeddings
                api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
                if not api_key:
                    DataManager.load_env()
                    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
                if not api_key:
                    raise ValueError("GEMINI_API_KEY is not set.")
                return GoogleGenerativeAIEmbeddings(model=model_name, google_api_key=api_key)
            except ImportError:
                raise ImportError("Please install langchain-google-genai to use Gemini embeddings.")
        else:
            raise ValueError(f"Unsupported embedding provider: {provider}")

    def load_documents(self, file_paths: List[str]) -> tuple[List[Any], List[str]]:
        """
        Loads documents from various file types. Returns a list of loaded documents and a list of skipped files.
        """
        documents = []
        skipped_files = []
        for file_path in file_paths:
            if not os.path.exists(file_path):
                skipped_files.append(f"File not found: {file_path}")
                continue
            
            ext = os.path.splitext(file_path)[1].lower()
            try:
                if ext == '.pdf':
                    loader = PyPDFLoader(file_path)
                    documents.extend(loader.load())
                elif ext == '.txt' or ext == '.md' or ext == '.csv':
                    loader = TextLoader(file_path, encoding='utf-8')
                    documents.extend(loader.load())
                elif ext == '.docx':
                    loader = Docx2txtLoader(file_path)
                    documents.extend(loader.load())
                else:
                    skipped_files.append(f"Unsupported file type: {file_path}")
            except Exception as e:
                logging.error(f"Error loading {file_path}: {e}")
                skipped_files.append(f"Error processing {os.path.basename(file_path)}: {str(e)}")
                
        return documents, skipped_files

    def create_database(self, db_name: str, file_paths: List[str], provider: str, model_name: str, chunk_size: int = 1000, chunk_overlap: int = 200) -> Dict[str, Any]:
        """
        Creates a new vector database from a list of files with customizable chunking.
        """
        db_path = os.path.join(self.storage_dir, db_name)
        
        # If DB exists, we might want to clear it or append to it.
        # For this implementation, we will append if it exists, or create if new.
        
        # 1. Load documents
        documents, skipped_files = self.load_documents(file_paths)
        
        if not documents:
            return {"status": "error", "message": "No valid documents could be loaded.", "skipped_files": skipped_files}

        # 2. Split documents
        text_splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        chunks = text_splitter.split_documents(documents)
        
        if not chunks:
            return {"status": "error", "message": "Documents were loaded but resulted in 0 text chunks. Check if the files contain readable text.", "skipped_files": skipped_files}

        # 3. Get Embedding function
        try:
            embedding_function = self._get_embedding_function(provider, model_name)
        except Exception as e:
            return {"status": "error", "message": f"Embedding error: {str(e)}", "skipped_files": skipped_files}

        # 4. Create and persist vector store
        try:
            vector_store = Chroma.from_documents(
                documents=chunks,
                embedding=embedding_function,
                persist_directory=db_path
            )
            # In newer langchain-chroma, persistence is automatic.
            
                
            return {
                "status": "success",
                "message": f"Successfully processed {len(documents)} documents into {len(chunks)} chunks.",
                "db_path": db_path,
                "skipped_files": skipped_files
            }
        except Exception as e:
            logging.error(f"Error creating vector database: {e}", exc_info=True)
            return {"status": "error", "message": f"ChromaDB error: {str(e)}", "skipped_files": skipped_files}

    def delete_database(self, db_name: str) -> bool:
        """
        Deletes a local vector database directory.
        """
        db_path = os.path.join(self.storage_dir, db_name)
        if os.path.exists(db_path):
            try:
                shutil.rmtree(db_path)
                return True
            except Exception as e:
                logging.error(f"Error deleting database directory {db_path}: {e}")
                return False
        return True # If it doesn't exist, consider it deleted

    def query_database(self, db_path: str, provider: str, model_name: str, query: str, k: int = 4) -> str:
        """
        Queries a specific vector database and returns a formatted string of results.
        """
        if not os.path.exists(db_path):
            return f"Error: Database at {db_path} not found."
            
        try:
            embedding_function = self._get_embedding_function(provider, model_name)
            vector_store = Chroma(persist_directory=db_path, embedding_function=embedding_function)
            
            results = vector_store.similarity_search(query, k=k)
            
            if not results:
                return "No relevant information found in the database."
                
            formatted_results = []
            for i, doc in enumerate(results):
                source = doc.metadata.get('source', 'Unknown source')
                page = doc.metadata.get('page', 'Unknown page')
                formatted_results.append(f"--- Document {i+1} (Source: {os.path.basename(source)}, Page: {page}) ---\n{doc.page_content}\n")
                
            return "\n".join(formatted_results)
            
        except Exception as e:
            logging.error(f"Error querying database {db_path}: {e}", exc_info=True)
            return f"Error querying database: {str(e)}"
