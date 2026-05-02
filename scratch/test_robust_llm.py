from langchain_google_genai import ChatGoogleGenerativeAI
from crewai import Agent
from typing import Any, Optional
import pydantic

class RobustLLM(ChatGoogleGenerativeAI):
    fallback_model: Optional[Any] = None

try:
    llm = RobustLLM(model="gemini-1.5-flash", google_api_key="fake")
    agent = Agent(
        role="test",
        goal="test",
        backstory="test",
        llm=llm
    )
    print("Agent creation successful")
except Exception as e:
    print(f"Agent creation failed: {e}")
