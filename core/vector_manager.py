import os
import re
import shutil
import logging
import time
import math
from typing import List, Dict, Any, Optional, Callable

import pandas as pd
from langchain_community.document_loaders import PyPDFLoader, TextLoader, Docx2txtLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter, MarkdownTextSplitter
from langchain_chroma import Chroma
import requests
try:
    from langchain_qdrant import QdrantVectorStore
except ImportError:
    pass

# Embedding imports
from langchain_openai import OpenAIEmbeddings
try:
    from langchain_ollama import OllamaEmbeddings
except ImportError:
    from langchain_community.embeddings import OllamaEmbeddings
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from core.data_manager import DataManager

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# File extensions that should be stored as structured CSV data, not vectorized
TABULAR_EXTENSIONS = {'.csv', '.xlsx', '.xls'}

# --- Batch Embedding Constants ---
DEFAULT_BATCH_SIZE = 50       # Chunks per API call
MAX_RETRIES = 5               # Max retries per batch on transient errors
INITIAL_BACKOFF_SECONDS = 10  # First retry wait (doubles each attempt)

class VectorManager:
    """
    Manages the creation, population, and querying of local vector databases using ChromaDB.
    """
    def __init__(self, storage_dir: str = "storage/vector_dbs"):
        self.storage_dir = storage_dir
        os.makedirs(self.storage_dir, exist_ok=True)
        

    def _get_qdrant_client_params(self, db_name: str):
        """Check if Docker Qdrant is available, otherwise fallback to local path."""
        url = "http://localhost:6333"
        try:
            response = requests.get(url, timeout=1)
            if response.status_code == 200:
                return {"url": url, "collection_name": db_name}
        except Exception:
            pass
        return {"path": os.path.join(self.storage_dir, "qdrant_local"), "collection_name": db_name}

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
            kwargs = {"model": model_name}
            base_url = os.getenv("OLLAMA_API_BASE")
            if base_url:
                kwargs["base_url"] = base_url
                
            api_key = os.getenv("OLLAMA_API_KEY")
            if api_key:
                if "langchain_ollama" in OllamaEmbeddings.__module__:
                    kwargs["client_kwargs"] = {"headers": {"Authorization": f"Bearer {api_key}"}}
                else:
                    kwargs["headers"] = {"Authorization": f"Bearer {api_key}"}
                
            return OllamaEmbeddings(**kwargs)
        elif provider == 'gemini' or provider == 'google':
            # Needs langchain_google_genai
            try:
                
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

    @staticmethod
    def _is_retryable_error(error: Exception) -> bool:
        """Check if an error is a transient/rate-limit error worth retrying."""
        error_str = str(error).lower()
        retryable_keywords = [
            "429", "resource_exhausted", "rate limit", "quota",
            "too many requests", "overloaded", "503", "service unavailable",
            "timeout", "timed out", "connection", "temporarily unavailable",
            "readonly", "locked", "malformed"
        ]
        return any(kw in error_str for kw in retryable_keywords)

    @staticmethod
    def _extract_retry_delay(error: Exception) -> Optional[float]:
        """Try to extract a suggested retry delay from the error message."""
        import re as _re
        error_str = str(error)
        # Look for patterns like "retry in 52.284350429s" or "retryDelay": "52s"
        match = _re.search(r'retry\s*(?:in|Delay["\s:]*)\s*["\']?(\d+\.?\d*)\s*s', error_str, _re.IGNORECASE)
        if match:
            return float(match.group(1))
        return None

    def _embed_in_batches(
        self,
        chunks: List[Any],
        embedding_function: Any,
        db_path: str,
        batch_size: int = DEFAULT_BATCH_SIZE,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        existing_db: bool = False,
        vectordb_type: str = "chroma"
    ) -> Dict[str, Any]:
        """
        Embeds chunks into ChromaDB in batches with automatic retry and exponential backoff.
        
        Args:
            chunks: List of LangChain Document objects to embed.
            embedding_function: The embedding function to use.
            db_path: Path to the ChromaDB persist directory.
            batch_size: Number of chunks per batch.
            progress_callback: Optional callback(current_batch, total_batches, message).
            existing_db: If True, the DB already exists on disk (adding files).
            
        Returns:
            Dict with 'status' ('success'/'partial'/'error'), 'embedded_count', 'failed_batches'.
        """
        total_chunks = len(chunks)
        total_batches = math.ceil(total_chunks / batch_size)
        embedded_count = 0
        failed_batches = []
        vector_store = None
        db_initialized = existing_db  # If adding to existing DB, skip from_documents path

        for batch_idx in range(total_batches):
            start = batch_idx * batch_size
            end = min(start + batch_size, total_chunks)
            batch = chunks[start:end]
            batch_label = f"Batch {batch_idx + 1}/{total_batches} (chunks {start+1}-{end})"

            if progress_callback:
                progress_callback(batch_idx + 1, total_batches, f"⏳ Embedding {batch_label}...")

            success = False
            for attempt in range(1, MAX_RETRIES + 1):
                try:

                    if vector_store is None:
                        if vectordb_type == "qdrant":
                            db_name = os.path.basename(db_path)
                            qdrant_params = self._get_qdrant_client_params(db_name)
                            vector_store = QdrantVectorStore(embedding=embedding_function, **qdrant_params)
                            vector_store.add_documents(batch)
                            db_initialized = True
                        else:
                            if db_initialized:
                                logging.info(f"🔄 Reconnecting to ChromaDB at {db_path}...")
                                vector_store = Chroma(persist_directory=db_path, embedding_function=embedding_function)
                                vector_store.add_documents(batch)
                            else:
                                vector_store = Chroma.from_documents(documents=batch, embedding=embedding_function, persist_directory=db_path)
                                db_initialized = True
                    else:
                        vector_store.add_documents(batch)


                    embedded_count += len(batch)
                    logging.info(f"✅ {batch_label} embedded successfully ({embedded_count}/{total_chunks} total)")
                    success = True
                    break  # Exit retry loop on success

                except Exception as e:
                    if self._is_retryable_error(e) and attempt < MAX_RETRIES:
                        # Calculate wait time: use API-suggested delay or exponential backoff
                        suggested_delay = self._extract_retry_delay(e)
                        wait_time = suggested_delay if suggested_delay else INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
                        wait_time = min(wait_time, 120)  # Cap at 2 minutes

                        # If it's a DB-level error (readonly/locked/malformed from OneDrive sync),
                        # reset the vector_store so the next attempt reconnects fresh to ChromaDB.
                        error_lower = str(e).lower()
                        if any(kw in error_lower for kw in ("readonly", "locked", "malformed")):
                            logging.warning(f"⚠️ {batch_label} — DB connection error, will reconnect on retry.")
                            vector_store = None

                        logging.warning(
                            f"⚠️ {batch_label} — Attempt {attempt}/{MAX_RETRIES} failed. "
                            f"Retrying in {wait_time:.0f}s... Error: {str(e)[:120]}"
                        )
                        if progress_callback:
                            progress_callback(
                                batch_idx + 1, total_batches,
                                f"⏸️ Retryable error on {batch_label}. "
                                f"Waiting {wait_time:.0f}s before retry ({attempt}/{MAX_RETRIES})..."
                            )
                        time.sleep(wait_time)
                    else:
                        # Non-retryable error or max retries exhausted
                        logging.error(f"❌ {batch_label} failed permanently: {e}")
                        failed_batches.append({
                            "batch": batch_idx + 1,
                            "chunks": f"{start+1}-{end}",
                            "error": str(e)
                        })
                        break  # Move to next batch

        # Determine final status
        if embedded_count == total_chunks:
            status = "success"
        elif embedded_count > 0:
            status = "partial"
        else:
            status = "error"

        return {
            "status": status,
            "embedded_count": embedded_count,
            "total_chunks": total_chunks,
            "failed_batches": failed_batches
        }

    def load_documents(self, file_paths: List[str], progress_callback: Optional[Callable[[int, int, str], None]] = None) -> tuple[List[Any], List[str]]:
        """
        Loads vectorizable documents from various file types.
        CSV, XLSX, and XLS files are intentionally excluded here — they are
        handled separately by _process_tabular_files and saved as cleaned CSVs.
        Returns a list of loaded documents and a list of skipped files.
        """
        documents = []
        skipped_files = []
        total_files = len(file_paths)
        
        for i, file_path in enumerate(file_paths):
            if progress_callback:
                progress_callback(i + 1, total_files, f"📄 Extracting text from {os.path.basename(file_path)}...")
                
            if not os.path.exists(file_path):
                skipped_files.append(f"File not found: {file_path}")
                continue

            ext = os.path.splitext(file_path)[1].lower()

            # Tabular files are processed separately — skip them here
            if ext in TABULAR_EXTENSIONS:
                continue

            try:
                if ext == '.pdf':
                    loader = PyPDFLoader(file_path)
                    documents.extend(loader.load())
                elif ext in ('.txt', '.md', '.js', '.py', '.json', '.html', '.css'):
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

    def _process_tabular_files(self, file_paths: List[str], structured_dir: str) -> tuple[List[str], List[str]]:
        """
        Cleans and converts CSV/Excel files to standard UTF-8 CSVs saved in `structured_dir`.
        Returns (list of saved CSV paths, list of error messages).

        Basic cleanup applied:
        - Strip whitespace from column names
        - Remove fully empty rows and columns
        - Strip leading/trailing spaces from string cell values
        - Sanitize output filenames
        """
        os.makedirs(structured_dir, exist_ok=True)
        saved_paths = []
        errors = []

        for file_path in file_paths:
            ext = os.path.splitext(file_path)[1].lower()
            if ext not in TABULAR_EXTENSIONS:
                continue

            base_name = os.path.splitext(os.path.basename(file_path))[0]
            # Sanitize: lowercase, replace spaces/special chars with underscores
            safe_base = re.sub(r'[^a-z0-9_-]', '_', base_name.lower()).strip('_')

            try:
                if ext in ('.xlsx', '.xls'):
                    excel = pd.ExcelFile(file_path)
                    for sheet_name in excel.sheet_names:
                        df = excel.parse(sheet_name)
                        df = self._clean_dataframe(df)
                        if df.empty:
                            logging.warning(f"Sheet '{sheet_name}' in '{file_path}' is empty after cleanup. Skipping.")
                            continue
                        safe_sheet = re.sub(r'[^a-z0-9_-]', '_', sheet_name.lower()).strip('_')
                        out_name = f"{safe_base}_{safe_sheet}.csv"
                        out_path = os.path.join(structured_dir, out_name)
                        df.to_csv(out_path, index=False, encoding='utf-8')
                        saved_paths.append(out_path)
                        logging.info(f"Saved structured file: {out_path}")

                elif ext == '.csv':
                    df = pd.read_csv(file_path, encoding='utf-8', on_bad_lines='skip')
                    df = self._clean_dataframe(df)
                    if df.empty:
                        errors.append(f"CSV '{os.path.basename(file_path)}' is empty after cleanup. Skipping.")
                        continue
                    out_name = f"{safe_base}.csv"
                    out_path = os.path.join(structured_dir, out_name)
                    df.to_csv(out_path, index=False, encoding='utf-8')
                    saved_paths.append(out_path)
                    logging.info(f"Saved structured file: {out_path}")

            except Exception as e:
                logging.error(f"Error processing tabular file {file_path}: {e}")
                errors.append(f"Error processing '{os.path.basename(file_path)}': {str(e)}")

        return saved_paths, errors

    @staticmethod
    def _clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
        """Applies basic cleanup to a DataFrame before saving as CSV."""
        # Strip whitespace from column names
        df.columns = [str(c).strip() for c in df.columns]
        # Drop fully empty rows and columns
        df.dropna(how='all', inplace=True)
        df.dropna(axis=1, how='all', inplace=True)
        # Strip whitespace from string cells
        for col in df.select_dtypes(include='object').columns:
            df[col] = df[col].map(lambda x: x.strip() if isinstance(x, str) else x)
        df.reset_index(drop=True, inplace=True)
        return df

    def create_database(self, db_name: str, file_paths: List[str], provider: str, model_name: str, chunk_size: int = 1000, chunk_overlap: int = 200, batch_size: int = DEFAULT_BATCH_SIZE, progress_callback: Optional[Callable] = None, scientific_mode: bool = False, scientific_config: Dict[str, Any] = None) -> Dict[str, Any]:
        if scientific_config is None:
            scientific_config = {}
        """
        Creates a new vector database from a list of files with customizable chunking.
        Excel/CSV files are NOT vectorized — they are cleaned and saved as CSVs in a
        `structured/` subfolder inside the database directory.
        
        If scientific_mode is True, uses ScientificParser to extract tables/graphs via VLMs.
        
        Uses batched embedding with automatic retry and exponential backoff to handle
        API rate limits (429 errors) gracefully without losing progress.
        """
        db_path = os.path.join(self.storage_dir, db_name)
        
        # Prevent embedding mismatch corruption: If the user creates a DB that already exists, 
        # wipe it clean before creating it to ensure no mixed embeddings crash the system.
        if os.path.exists(db_path):
            shutil.rmtree(db_path)
            
        structured_dir = os.path.join(db_path, "structured")
        os.makedirs(db_path, exist_ok=True)
        
        # Save advanced parameters to config.json for future consistency when adding files
        config_path = os.path.join(db_path, "config.json")
        try:
            import json
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump({
                    "chunk_size": chunk_size,
                    "chunk_overlap": chunk_overlap,
                    "provider": provider,
                    "model_name": model_name,
                    "vectordb": scientific_config.get("vectordb", "chroma"),
                    "scientific_mode": scientific_mode,
                    "scientific_config": scientific_config
                }, f, indent=4)
        except Exception as e:
            logging.error(f"Could not save config.json to {db_path}: {e}")

        all_skipped: List[str] = []
        summary_parts: List[str] = []

        # --- 1. Handle tabular files (CSV / Excel) ---
        tabular_files = [p for p in file_paths if os.path.splitext(p)[1].lower() in TABULAR_EXTENSIONS]
        if tabular_files:
            saved_csvs, tabular_errors = self._process_tabular_files(tabular_files, structured_dir)
            all_skipped.extend(tabular_errors)
            if saved_csvs:
                summary_parts.append(f"{len(saved_csvs)} structured file(s) saved to structured/")
            logging.info(f"Processed {len(tabular_files)} tabular file(s) → {len(saved_csvs)} CSV(s) saved.")

        # --- 2. Handle vectorizable documents (PDF, TXT, DOCX, MD) ---
        vectorizable_files = [p for p in file_paths if os.path.splitext(p)[1].lower() not in TABULAR_EXTENSIONS]

        if not vectorizable_files:
            # Only tabular files were uploaded — skip Chroma creation entirely
            if summary_parts:
                return {
                    "status": "success",
                    "message": "No vectorizable documents found. " + " | ".join(summary_parts),
                    "db_path": db_path,
                    "skipped_files": all_skipped
                }
            return {
                "status": "error",
                "message": "No valid documents could be loaded and no tabular files were processed.",
                "skipped_files": all_skipped
            }

        if progress_callback:
            progress_callback(0, 0, "📄 Loading documents...")

        if scientific_mode:
            from core.scientific_parser import ScientificParser
            if not scientific_config:
                scientific_config = {}
            parser = ScientificParser(
                models_config=scientific_config.get("models_config", {}),
                graph_points=scientific_config.get("graph_points", 10),
                db_path=db_path,
                parser_type=scientific_config.get("parser", "pdfplumber")
            )
            documents = []
            load_skipped = []
            for f in vectorizable_files:
                if f.lower().endswith(".pdf"):
                    if progress_callback:
                        progress_callback(0, 0, f"🧪 Scientifically parsing {os.path.basename(f)}...")
                    docs = parser.parse_pdf(f)
                    if not docs:
                        load_skipped.append(f"Failed to parse PDF scientifically: {f}")
                    documents.extend(docs)
                else:
                    docs, skip = self.load_documents([f], progress_callback=progress_callback)
                    documents.extend(docs)
                    load_skipped.extend(skip)
        else:
            documents, load_skipped = self.load_documents(vectorizable_files, progress_callback=progress_callback)

        all_skipped.extend(load_skipped)

        if not documents:
            if summary_parts:
                # Tabular OK, but text documents failed — still a partial success
                return {
                    "status": "success",
                    "message": "No text documents loaded. " + " | ".join(summary_parts),
                    "db_path": db_path,
                    "skipped_files": all_skipped
                }
            return {"status": "error", "message": "No valid documents could be loaded.", "skipped_files": all_skipped}

        # 3. Split documents into chunks
        if progress_callback:
            progress_callback(0, 0, f"✂️ Splitting {len(documents)} document(s) into chunks...")

        if scientific_mode:
            # MarkdownTextSplitter is smarter for scientific mode since output is rich markdown
            # It splits at markdown headers, code blocks, tables before splitting paragraphs
            text_splitter = MarkdownTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        else:
            text_splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
            
        chunks = text_splitter.split_documents(documents)

        if not chunks:
            return {
                "status": "error",
                "message": "Documents were loaded but resulted in 0 text chunks. Check if the files contain readable text.",
                "skipped_files": all_skipped
            }

        # 4. Get embedding function
        try:
            embedding_function = self._get_embedding_function(provider, model_name)
        except Exception as e:
            return {"status": "error", "message": f"Embedding error: {str(e)}", "skipped_files": all_skipped}

        # 5. Embed in batches with automatic retry
        logging.info(f"Starting batched embedding: {len(chunks)} chunks in batches of {batch_size}")
        vectordb_type = scientific_config.get('vectordb', 'chroma')
        embed_result = self._embed_in_batches(
            chunks=chunks,
            embedding_function=embedding_function,
            db_path=db_path,
            batch_size=batch_size,
            progress_callback=progress_callback,
            vectordb_type=vectordb_type
        )

        embedded = embed_result["embedded_count"]
        total = embed_result["total_chunks"]

        if embed_result["status"] == "success":
            summary_parts.insert(0, f"{len(documents)} document(s) → {total} chunk(s) vectorized")
            return {
                "status": "success",
                "message": "Successfully processed: " + " | ".join(summary_parts),
                "db_path": db_path,
                "skipped_files": all_skipped
            }
        elif embed_result["status"] == "partial":
            failed_info = "; ".join([f"Batch {fb['batch']}: {fb['error'][:80]}" for fb in embed_result["failed_batches"]])
            summary_parts.insert(0, f"{embedded}/{total} chunks embedded (some batches failed)")
            return {
                "status": "partial",
                "message": "Partially processed: " + " | ".join(summary_parts) + f" | Failures: {failed_info}",
                "db_path": db_path,
                "skipped_files": all_skipped,
                "failed_batches": embed_result["failed_batches"]
            }
        else:
            failed_info = embed_result["failed_batches"][0]["error"] if embed_result["failed_batches"] else "Unknown error"
            return {"status": "error", "message": f"Embedding failed: {failed_info}", "skipped_files": all_skipped}

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
        If the database contains only structured CSV files (no Chroma index), returns a
        helpful message directing the agent to use the tabular_query tool instead.
        """
        if not os.path.exists(db_path):
            return f"Error: Database at {db_path} not found."

        # Check if there is a Chroma vector index in this directory
        chroma_db_file = os.path.join(db_path, "chroma.sqlite3")
        structured_dir = os.path.join(db_path, "structured")
        if not os.path.exists(chroma_db_file):
            if os.path.isdir(structured_dir):
                csv_files = [f for f in os.listdir(structured_dir) if f.endswith('.csv')]
                if csv_files:
                    return (
                        f"This database contains only structured tabular data (no text embeddings). "
                        f"Use the 'tabular_query' tool to interact with it. "
                        f"Available tables: {', '.join(csv_files)}"
                    )
            return f"Error: No vector index found at {db_path}."

        try:
            embedding_function = self._get_embedding_function(provider, model_name)
            
            # Check if Qdrant collection exists (by checking config or assuming Chroma fallback)
            config = self.get_database_config(db_path)
            vectordb_type = config.get("vectordb", "chroma")
            
            if vectordb_type == "qdrant":
                db_name = os.path.basename(db_path)
                qdrant_params = self._get_qdrant_client_params(db_name)
                vector_store = QdrantVectorStore(embedding=embedding_function, **qdrant_params)
            else:
                vector_store = Chroma(persist_directory=db_path, embedding_function=embedding_function)

            results = vector_store.similarity_search(query, k=k)

            if not results:
                return "No relevant information found in the database."

            formatted_results = []
            for i, doc in enumerate(results):
                source = doc.metadata.get('source', 'Unknown source')
                page = doc.metadata.get('page', 'Unknown page')
                formatted_results.append(
                    f"--- Document {i+1} (Source: {os.path.basename(source)}, Page: {page}) ---\n{doc.page_content}\n"
                )

            return "\n".join(formatted_results)

        except Exception as e:
            logging.error(f"Error querying database {db_path}: {e}", exc_info=True)
            return f"Error querying database: {str(e)}"

    def get_database_files(self, db_path: str, provider: str, model_name: str) -> Dict[str, List[str]]:
        """
        Retrieves files currently registered in the database, separated into 'vectorized' and 'structured'.
        """
        results = {'vectorized': [], 'structured': []}
        
        # 1. Structured files
        structured_dir = os.path.join(db_path, "structured")
        if os.path.isdir(structured_dir):
            results['structured'] = sorted([
                f for f in os.listdir(structured_dir) if f.endswith('.csv')
            ])
            
        # 2. Vectorized files
        config = self.get_database_config(db_path)
        vectordb_type = config.get("vectordb", "chroma")
        
        if vectordb_type == "qdrant":
            try:
                from qdrant_client import QdrantClient
                from qdrant_client.http.models import FieldCondition, MatchValue, Filter
                db_name = os.path.basename(db_path)
                params = self._get_qdrant_client_params(db_name)
                
                # Setup client
                if "url" in params:
                    client = QdrantClient(url=params["url"])
                else:
                    client = QdrantClient(path=params["path"])
                
                # Fetch distinct sources
                res, _ = client.scroll(collection_name=db_name, limit=10000, with_payload=True, with_vectors=False)
                sources = set()
                for record in res:
                    if record.payload and "metadata" in record.payload and "source" in record.payload["metadata"]:
                        sources.add(record.payload["metadata"]["source"])
                results['vectorized'] = sorted(list(sources))
            except Exception as e:
                logging.error(f"Error reading vectorized files from Qdrant: {e}")
                
        else:
            chroma_db_file = os.path.join(db_path, "chroma.sqlite3")
            if os.path.exists(chroma_db_file):
                try:
                    import sqlite3
                    conn = sqlite3.connect(chroma_db_file)
                    cur = conn.cursor()
                    cur.execute("SELECT DISTINCT string_value FROM embedding_metadata WHERE key = 'source';")
                    sources = {row[0] for row in cur.fetchall() if row[0]}
                    conn.close()
                    results['vectorized'] = sorted(list(sources))
                except Exception as e:
                    logging.warning(f"Failed to query unique sources via SQLite: {e}. Falling back to LangChain vector_store.get")
                    try:
                        embedding_function = self._get_embedding_function(provider, model_name)
                        vector_store = Chroma(persist_directory=db_path, embedding_function=embedding_function)
                        # Fetch metadatas to extract unique sources
                        data = vector_store.get(include=['metadatas'])
                        sources = set()
                        if data and 'metadatas' in data:
                            for meta in data['metadatas']:
                                if meta and 'source' in meta:
                                    sources.add(meta['source'])
                        results['vectorized'] = sorted(list(sources))
                    except Exception as ex:
                        logging.error(f"Error reading vectorized files from Chroma: {ex}")
                
        return results

    def remove_file_from_database(self, db_path: str, provider: str, model_name: str, file_type: str, file_identifier: str) -> bool:
        """
        Removes a file from the database.
        - If file_type is 'structured', deletes the CSV file from disk.
        - If file_type is 'vectorized', deletes all chunks from the vector store matching the source file path.
        """
        if file_type == 'structured':
            file_path = os.path.join(db_path, "structured", file_identifier)
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    return True
                except Exception as e:
                    logging.error(f"Error removing structured file {file_path}: {e}")
                    return False
            return True
            
        elif file_type == 'vectorized':
            config = self.get_database_config(db_path)
            vectordb_type = config.get("vectordb", "chroma")
            
            if vectordb_type == "qdrant":
                try:
                    from qdrant_client import QdrantClient
                    from qdrant_client.http.models import FieldCondition, MatchValue, Filter
                    db_name = os.path.basename(db_path)
                    params = self._get_qdrant_client_params(db_name)
                    
                    if "url" in params:
                        client = QdrantClient(url=params["url"])
                    else:
                        client = QdrantClient(path=params["path"])
                        
                    client.delete(
                        collection_name=db_name,
                        points_selector=Filter(
                            must=[
                                FieldCondition(key="metadata.source", match=MatchValue(value=file_identifier))
                            ]
                        )
                    )
                    return True
                except Exception as e:
                    logging.error(f"Error deleting vectorized file {file_identifier} from Qdrant: {e}")
                    return False
            else:
                try:
                    embedding_function = self._get_embedding_function(provider, model_name)
                    vector_store = Chroma(persist_directory=db_path, embedding_function=embedding_function)
                    # Delete chunks matching the source file path
                    vector_store.delete(where={"source": file_identifier})
                    return True
                except Exception as e:
                    logging.error(f"Error deleting vectorized file {file_identifier} from Chroma: {e}")
                    return False
                
        return False

    def get_database_config(self, db_path: str) -> Dict[str, Any]:
        """
        Reads advanced parameters (chunk_size, chunk_overlap) from config.json.
        Returns defaults if not found.
        """
        config_path = os.path.join(db_path, "config.json")
        try:
            import json
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logging.error(f"Error reading config.json from {db_path}: {e}")
            
        return {"chunk_size": 1000, "chunk_overlap": 200}

    def add_files_to_database(self, db_path: str, provider: str, model_name: str, file_paths: List[str], chunk_size: int = 1000, chunk_overlap: int = 200, progress_callback: Optional[Callable] = None) -> Dict[str, Any]:
        """
        Adds new files (tabular or vectorized) to an existing database directory.
        Uses batched embedding with retry for rate-limit resilience.
        """
        structured_dir = os.path.join(db_path, "structured")
        all_skipped = []
        summary_parts = []
        
        # 1. Tabular files
        tabular_files = [p for p in file_paths if os.path.splitext(p)[1].lower() in TABULAR_EXTENSIONS]
        if tabular_files:
            saved_csvs, tabular_errors = self._process_tabular_files(tabular_files, structured_dir)
            all_skipped.extend(tabular_errors)
            if saved_csvs:
                summary_parts.append(f"{len(saved_csvs)} structured file(s) added")
                
        # 2. Vectorized files
        vectorizable_files = [p for p in file_paths if os.path.splitext(p)[1].lower() not in TABULAR_EXTENSIONS]
        if vectorizable_files:
            if progress_callback:
                progress_callback(0, 0, "📄 Loading additional documents...")
                
            config = self.get_database_config(db_path)
            scientific_mode = config.get("scientific_mode", False)
            scientific_config = config.get("scientific_config", {})
            
            if scientific_mode:
                from core.scientific_parser import ScientificParser
                parser = ScientificParser(
                    models_config=scientific_config.get("models_config", {}),
                    graph_points=scientific_config.get("graph_points", 10),
                    db_path=db_path,
                    parser_type=scientific_config.get("parser", "pdfplumber")
                )
                documents = []
                load_skipped = []
                for f in vectorizable_files:
                    if f.lower().endswith(".pdf"):
                        if progress_callback:
                            progress_callback(0, 0, f"🧪 Scientifically parsing {os.path.basename(f)}...")
                        docs = parser.parse_pdf(f)
                        if not docs:
                            load_skipped.append(f"Failed to parse PDF scientifically: {f}")
                        documents.extend(docs)
                    else:
                        docs, skip = self.load_documents([f], progress_callback=progress_callback)
                        documents.extend(docs)
                        load_skipped.extend(skip)
            else:
                documents, load_skipped = self.load_documents(vectorizable_files, progress_callback=progress_callback)
                
            all_skipped.extend(load_skipped)
            
            if documents:
                config = self.get_database_config(db_path)
                if config.get("parser", "pdfplumber") in ["marker", "marker_vlm"]:
                    text_splitter = MarkdownTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
                else:
                    text_splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
                    
                chunks = text_splitter.split_documents(documents)
                if chunks:
                    try:
                        embedding_function = self._get_embedding_function(provider, model_name)
                        config = self.get_database_config(db_path)
                        vectordb_type = config.get("vectordb", "chroma")
                        # Use batched embedding with retry (existing_db=True since we're adding to an existing DB)
                        embed_result = self._embed_in_batches(
                            chunks=chunks,
                            embedding_function=embedding_function,
                            db_path=db_path,
                            progress_callback=progress_callback,
                            existing_db=True,
                            vectordb_type=vectordb_type
                        )
                        if embed_result["status"] in ("success", "partial"):
                            summary_parts.append(
                                f"{embed_result['embedded_count']}/{embed_result['total_chunks']} chunk(s) from "
                                f"{len(documents)} document(s) vectorized"
                            )
                        if embed_result["failed_batches"]:
                            for fb in embed_result["failed_batches"]:
                                all_skipped.append(f"Batch {fb['batch']} failed: {fb['error'][:100]}")
                    except Exception as e:
                        all_skipped.append(f"Vectorization error: {str(e)}")
                        
        if summary_parts:
            return {
                "status": "success",
                "message": "Successfully added: " + " | ".join(summary_parts),
                "skipped_files": all_skipped
            }
        else:
            return {
                "status": "error",
                "message": "No new files were successfully processed.",
                "skipped_files": all_skipped
            }
