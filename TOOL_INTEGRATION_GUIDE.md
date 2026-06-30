# Tool Integration Guide (AI OS)

This guide explains how the system manages the tools that allow AI agents to interact with the local computer and external services (e.g., Gmail, Google Drive, etc.).

## 1. Tool Architecture

The tools in this project are based on the **CrewAI** architecture.
A tool is simply a Python function decorated with `@tool` (from `crewai_tools`).
- **The name and docstring (`"""..."""`)** of the function are crucial: the AI reads them to understand what the tool is for and how to use it.
- **The function parameters** define what the AI needs to provide (e.g., `file_path`, `command_str`).

Currently implemented tools include:
- `tools/local_tools.py`: File reading/writing (with sandboxing in `/workspace`) and web search.
- `tools/terminal_executor.py`: Terminal command execution (with strict security controls and destructive command blocking).

## 2. The Tool Registry (`config/tools_map.yaml`)

To make a tool available in the graphical interface (UI) and to agents, it must be registered in the `config/tools_map.yaml` file.

This file acts as a registry and manages credential security:

```yaml
tools_registry:
  search_web:
    display_name: "Web: DuckDuckGo Search"
    description: "Free internet search."
    required_secrets: [] # No API key required
    
  gmail_send:
    display_name: "Google: Gmail Sender"
    description: "Sends emails via Gmail API."
    required_secrets: ["GMAIL_API_KEY", "GMAIL_SENDER_EMAIL"]
```

### How Secret Management Works:
1.  **Security**: API keys are never saved in the database or displayed in the user interface.
2.  **API Vault (Dashboard)**: The interface reads the YAML file and checks your `.env` file to see if the required keys (e.g., `GMAIL_API_KEY`) are present. 
3.  If they are missing, the tool is marked with a red traffic light 🔴 in the UI.

## 3. How to Add a NEW Tool (Tutorial)

Suppose you want to add a tool to read PDF files.

**Step 1: Create the Tool Code**
Create a new function in `tools/custom_tools.py` (or in `local_tools.py` if related):
```python
from crewai_tools import tool
import os

@tool("PDF Reader")
def read_pdf(file_path: str) -> str:
    """Reads the textual content of a PDF file from the workspace."""
    # ... python logic ...
    return text
```

**Step 2: Register it in the YAML**
Add the tool to the `config/tools_map.yaml` file:
```yaml
  read_pdf:
    display_name: "Local: PDF Reader"
    description: "Reads PDF files."
    required_secrets: []
```

**Step 3: Map it in the Executor**
For the execution system to know which Python function corresponds to the string "read_pdf", update the `agents/executor.py` file:
```python
# agents/executor.py
from tools.custom_tools import read_pdf

TOOL_REGISTRY = {
    # ... other tools ...
    "read_pdf": read_pdf 
}
```

## 4. Assignment and Security (Important!)

**How Assignment Works:**
- Via the **Task Builder (Dashboard)**, you can assign specific tools to specific tasks.
- If you create a task "Write a report" and assign it the `write_file` tool, the agent executing that task will have permission to write files *only during that operation*.

**Computer Access (Security):**
Giving AI agents access to the terminal or local files is extremely dangerous if uncontrolled.
1.  **File System**: All file tools (`read_file`, `write_file`) are locked via a validation function (`_is_path_safe`) that forces the agent to operate ONLY in the `workspace/` folder. Any attempt to access system files (e.g., `/etc/passwd` or `C:\Windows`) will be blocked.
2.  **Terminal**: The `Terminal Executor` has a blacklist of commands (like `rm`, `sudo`, `format`) and operators (`>`, `|`, `&&`) and prevents access to database files or `.env` files.

This architecture allows you to have a modular, secure system that is easy to expand with any external API (by adding the key to `.env` and registering it in the YAML).
