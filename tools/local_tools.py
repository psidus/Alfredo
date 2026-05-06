import os
import requests # Included as per plan, though duckduckgo-search abstracts HTTP.
from duckduckgo_search import DDGS
from crewai.tools import tool

# Lead Systems & Security Architect's Validated Implementation Plan
# File: tools/local_tools.py
#
# CRITICAL SECURITY NOTES:
# 1. SANDBOXING: Unrestricted file access for an AI agent is a severe security vulnerability.
#    All file I/O operations (read/write) MUST be sandboxed to a designated, non-critical
#    directory. This prevents the agent from accessing or overwriting sensitive files
#    like `.env`, `database.sqlite`, application source code, or system files.
# 2. PATH VALIDATION: A robust path validation function `_is_path_safe` has been implemented.
#    It resolves all paths to their absolute form and strictly checks if they are within
#    the defined workspace. This mitigates path traversal attacks (e.g., `../`).
# 3. PRINCIPLE OF LEAST PRIVILEGE: The agent's tools should only have the permissions
#    necessary to perform their intended function. Granting filesystem-wide access is a
#    violation of this principle. The workspace acts as a chroot jail for the agent.

# --- Architect's Security Implementation ---
# Define a safe directory for the agent's file operations.
# This directory should be gitignored and treated as ephemeral workspace.
WORKSPACE_DIR = os.path.abspath("workspace")

# Ensure the workspace directory exists on startup.
os.makedirs(WORKSPACE_DIR, exist_ok=True)

def _is_path_safe(file_path: str) -> bool:
    """
    Validates if the resolved file_path is within the WORKSPACE_DIR.
    Prevents path traversal attacks.
    """
    # Resolve the absolute path of the intended file
    resolved_path = os.path.abspath(os.path.join(WORKSPACE_DIR, file_path))
    # Check if the resolved path is within the workspace directory
    return resolved_path.startswith(WORKSPACE_DIR)

# --- Tool Definitions ---

@tool("Read File Tool")
def read_file(file_path: str) -> str:
    """
    Reads the content of a specified file from the workspace and returns it as a string.
    Use this tool to access data from files you have previously saved in the workspace.
    Example: read_file('my_notes.txt')
    """
    if not _is_path_safe(file_path):
        return "Error: Security violation. Path is outside the designated workspace."

    full_path = os.path.join(WORKSPACE_DIR, file_path)
    try:
        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return content
    except FileNotFoundError:
        return f"Error: File not found at path '{file_path}' in the workspace."
    except IOError as e:
        return f"Error reading file '{file_path}': {e}"
    except Exception as e:
        return f"An unexpected error occurred while reading '{file_path}': {e}"

@tool("Write File Tool")
def write_file(file_path: str, content: str) -> str:
    """
    Writes the given content to a specified file within the workspace.
    It will create the file and any necessary subdirectories if they do not exist,
    and will overwrite the file if it already exists. Use this to save work or data.
    Example: write_file('report.md', '# Q3 Financial Report\n...')
    """
    if not _is_path_safe(file_path):
        return "Error: Security violation. Path is outside the designated workspace."

    full_path = os.path.join(WORKSPACE_DIR, file_path)
    try:
        # Ensure the directory for the file exists within the workspace
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return f"Content successfully written to '{file_path}' in the workspace."
    except IOError as e:
        return f"Error writing to file '{file_path}': {e}"
    except Exception as e:
        return f"An unexpected error occurred while writing to '{file_path}': {e}"

@tool("Web Search Tool")
def search_web(query: str) -> str:
    """
    Performs a web search for the given query using DuckDuckGo and returns the top 5 results.
    Use this to find up-to-date information, answer questions, or research topics.
    """
    try:
        search_results = []
        with DDGS() as ddgs:
            results = ddgs.text(keywords=query, max_results=5)
            if not results:
                return f"No results found for query: '{query}'"

            for i, r in enumerate(results):
                search_results.append(
                    f"Result {i+1}: {r.get('title', 'N/A')}\n"
                    f"Link: {r.get('href', 'N/A')}\n"
                    f"Snippet: {r.get('body', 'N/A')}\n"
                    f"---"
                )
        
        return "\n".join(search_results)
    except Exception as e:
        return f"An error occurred during web search for '{query}': {e}"

@tool("Ask Operator")
def ask_operator(question: str) -> str:
    """
    Use this tool to ask the human operator a question when you need clarification,
    when you find multiple files with the same name, or when you need permission.
    The tool will pause your execution until the human replies.
    Example: ask_operator('I found 3 files named report.doc. Which path should I use?')
    """
    from core.human_in_the_loop import request_human_input
    
    chat_id = os.environ.get("CURRENT_CHAT_ID")
    if not chat_id:
        return "Error: Cannot reach operator. Assume default or abort."
    
    answer = request_human_input(chat_id, question)
    return f"Human replied: {answer}"

# --- Final Instructions ---
#
# The `duckduckgo-search` library must be added to `requirements.txt`.
# Add the following line to `requirements.txt`:
# duckduckgo-search>=5.1.0
#
# The `workspace` directory should be added to `.gitignore` to prevent
# agent-generated files from being committed to version control.
# Add the following line to `.gitignore`:
# /workspace/