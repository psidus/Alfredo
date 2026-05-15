from typing import Type
from pydantic import BaseModel, Field
from crewai.tools import BaseTool
from core.vector_manager import VectorManager

class VectorSearchInput(BaseModel):
    """Input schema for VectorSearchTool."""
    query: str = Field(..., description="The search query or question to ask the knowledge base.")

class VectorSearchTool(BaseTool):
    name: str = "vector_search"
    description: str = "Search a specific local vector database for information to answer questions. Use this tool when you need context from uploaded documents."
    args_schema: Type[BaseModel] = VectorSearchInput
    
    # Custom fields needed for execution
    db_path: str = Field(..., description="Path to the ChromaDB directory.")
    provider: str = Field(..., description="The embedding model provider.")
    model_name: str = Field(..., description="The embedding model name.")
    
    def _run(self, query: str) -> str:
        manager = VectorManager()
        return manager.query_database(
            db_path=self.db_path,
            provider=self.provider,
            model_name=self.model_name,
            query=query
        )
