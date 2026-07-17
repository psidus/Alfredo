import logging
from typing import List, Dict, Any, Optional
from crewai.tools import tool
import core.vector_manager

@tool
def read_rag_chunks(db_name: str, offset: int = 0, limit: int = 50) -> str:
    """
    Reads sequential text chunks from a Qdrant Vector Database linearly (Pagination/Scroll).
    Use this to read a book sequentially without semantic search.
    - db_name: the name of the vector database (e.g., 'Electrolite', 'Perry', 'CR_chemical').
    - offset: the starting chunk index (default: 0).
    - limit: the number of chunks to read (default: 50).
    Returns a concatenated string of the chunks or an indication if the end of document is reached.
    """
    try:
        vm = core.vector_manager.VectorManager()
        params = vm._get_qdrant_client_params(db_name)
        client = vm._get_qdrant_client(params)
        collection_name = params["collection_name"]
        
        if not client.collection_exists(collection_name):
            return f"Error: Database '{db_name}' does not exist or has no chunks."
            
        # Qdrant client scroll
        records, next_page_offset = client.scroll(
            collection_name=collection_name,
            limit=limit,
            offset=offset,
            with_payload=True,
            with_vectors=False
        )
        
        if not records:
            return f"END_OF_DOCUMENT. No more chunks found at offset {offset}."
            
        texts = []
        for i, record in enumerate(records):
            # Langchain Qdrant stores the text usually in payload['page_content']
            page_content = record.payload.get('page_content', '')
            if not page_content and 'text' in record.payload:
                page_content = record.payload['text']
                
            metadata = record.payload.get('metadata', {})
            source = metadata.get('source', 'Unknown Source')
            page = metadata.get('page', 'Unknown Page')
            
            chunk_header = f"--- Chunk {offset + i} (Source: {source}, Page: {page}) ---"
            texts.append(f"{chunk_header}\n{page_content}")
            
        result = "\n\n".join(texts)
        if next_page_offset is None:
             result += "\n\n*** END_OF_DOCUMENT REACHED ***"
        else:
             result += f"\n\n*** NEXT_OFFSET_HINT: {next_page_offset} ***"
             
        return result
        
    except Exception as e:
        logging.error(f"Error reading chunks from {db_name}: {e}")
        return f"Error reading from vector DB: {str(e)}"
