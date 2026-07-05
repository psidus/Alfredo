import os
import re

def patch_file():
    filepath = r"c:\Users\Pietro\Documents\GitHub\Alfredo\core\vector_manager.py"
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    # Update get_database_files
    old_get = '''        # 2. Vectorized files
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
                    logging.error(f"Error reading vectorized files from Chroma: {ex}")'''
                    
    new_get = '''        # 2. Vectorized files
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
                        logging.error(f"Error reading vectorized files from Chroma: {ex}")'''
    content = content.replace(old_get, new_get)

    # Update remove_file_from_database
    old_remove = '''        elif file_type == 'vectorized':
            try:
                embedding_function = self._get_embedding_function(provider, model_name)
                vector_store = Chroma(persist_directory=db_path, embedding_function=embedding_function)
                # Delete chunks matching the source file path
                vector_store.delete(where={"source": file_identifier})
                return True
            except Exception as e:
                logging.error(f"Error deleting vectorized file {file_identifier} from Chroma: {e}")
                return False'''
    new_remove = '''        elif file_type == 'vectorized':
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
                    return False'''
    content = content.replace(old_remove, new_remove)

    # Update add_files_to_database
    old_add = '''                        # Use batched embedding with retry (existing_db=True since we're adding to an existing DB)
                        embed_result = self._embed_in_batches(
                            chunks=chunks,
                            embedding_function=embedding_function,
                            db_path=db_path,
                            progress_callback=progress_callback,
                            existing_db=True
                        )'''
    new_add = '''                        config = self.get_database_config(db_path)
                        vectordb_type = config.get("vectordb", "chroma")
                        # Use batched embedding with retry (existing_db=True since we're adding to an existing DB)
                        embed_result = self._embed_in_batches(
                            chunks=chunks,
                            embedding_function=embedding_function,
                            db_path=db_path,
                            progress_callback=progress_callback,
                            existing_db=True,
                            vectordb_type=vectordb_type
                        )'''
    content = content.replace(old_add, new_add)

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)

if __name__ == "__main__":
    patch_file()
    print("Patch 2 successful.")
