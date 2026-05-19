import os
import re
import shutil
import logging
from typing import List, Dict, Any, Optional

import pandas as pd
from langchain_community.document_loaders import PyPDFLoader, TextLoader, Docx2txtLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_chroma import Chroma

# Embedding imports
from langchain_openai import OpenAIEmbeddings
from langchain_community.embeddings import OllamaEmbeddings
from core.data_manager import DataManager

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# File extensions that should be stored as structured CSV data, not vectorized
TABULAR_EXTENSIONS = {'.csv', '.xlsx', '.xls'}

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
        Loads vectorizable documents from various file types.
        CSV, XLSX, and XLS files are intentionally excluded here — they are
        handled separately by _process_tabular_files and saved as cleaned CSVs.
        Returns a list of loaded documents and a list of skipped files.
        """
        documents = []
        skipped_files = []
        for file_path in file_paths:
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
                elif ext in ('.txt', '.md'):
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

    def create_database(self, db_name: str, file_paths: List[str], provider: str, model_name: str, chunk_size: int = 1000, chunk_overlap: int = 200) -> Dict[str, Any]:
        """
        Creates a new vector database from a list of files with customizable chunking.
        Excel/CSV files are NOT vectorized — they are cleaned and saved as CSVs in a
        `structured/` subfolder inside the database directory.
        """
        db_path = os.path.join(self.storage_dir, db_name)
        structured_dir = os.path.join(db_path, "structured")
        os.makedirs(db_path, exist_ok=True)

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

        documents, load_skipped = self.load_documents(vectorizable_files)
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

        # 5. Create and persist vector store
        try:
            Chroma.from_documents(
                documents=chunks,
                embedding=embedding_function,
                persist_directory=db_path
            )
            summary_parts.insert(0, f"{len(documents)} document(s) → {len(chunks)} chunk(s) vectorized")
            return {
                "status": "success",
                "message": "Successfully processed: " + " | ".join(summary_parts),
                "db_path": db_path,
                "skipped_files": all_skipped
            }
        except Exception as e:
            logging.error(f"Error creating vector database: {e}", exc_info=True)
            return {"status": "error", "message": f"ChromaDB error: {str(e)}", "skipped_files": all_skipped}

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

    def add_files_to_database(self, db_path: str, provider: str, model_name: str, file_paths: List[str], chunk_size: int = 1000, chunk_overlap: int = 200) -> Dict[str, Any]:
        """
        Adds new files (tabular or vectorized) to an existing database directory.
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
            documents, load_skipped = self.load_documents(vectorizable_files)
            all_skipped.extend(load_skipped)
            
            if documents:
                text_splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
                chunks = text_splitter.split_documents(documents)
                if chunks:
                    try:
                        embedding_function = self._get_embedding_function(provider, model_name)
                        vector_store = Chroma(persist_directory=db_path, embedding_function=embedding_function)
                        vector_store.add_documents(chunks)
                        summary_parts.append(f"{len(documents)} document(s) → {len(chunks)} chunk(s) vectorized")
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
