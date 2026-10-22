import os
import re
import glob
import logging
import requests
try:
    from duckduckgo_search import DDGS
except ImportError:
    try:
        from ddgs import DDGS  # newer package name fallback
    except ImportError:
        DDGS = None
from crewai.tools import tool

logger = logging.getLogger(__name__)

# --- Sandboxed Workspace (for write operations) ---
WORKSPACE_DIR = os.path.abspath("workspace")
os.makedirs(WORKSPACE_DIR, exist_ok=True)


def _is_path_safe(file_path: str) -> bool:
    """Validates if the resolved file_path is strictly inside WORKSPACE_DIR."""
    try:
        resolved_path = os.path.abspath(os.path.join(WORKSPACE_DIR, file_path))
        return os.path.commonpath([resolved_path, WORKSPACE_DIR]) == WORKSPACE_DIR
    except ValueError:
        return False


# ===========================================================================
# SANDBOXED TOOLS (only within workspace/)
# ===========================================================================

@tool
def read_file(file_path: str) -> str:
    """
    Reads the content of a specified file from the agent workspace and returns it as a string.
    Use this tool to access data from files you have previously saved in the workspace.
    The path is relative to the workspace directory (e.g., 'my_notes.txt').
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


@tool
def write_file(file_path: str, content: str) -> str:
    """
    Writes the given content to a specified file within the agent workspace.
    It will create the file and any necessary subdirectories if they do not exist,
    and will overwrite the file if it already exists.
    The path is relative to the workspace directory (e.g., 'report.md').
    """
    if not _is_path_safe(file_path):
        return "Error: Security violation. Path is outside the designated workspace."

    full_path = os.path.join(WORKSPACE_DIR, file_path)
    try:
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return f"Content successfully written to '{file_path}' in the workspace."
    except IOError as e:
        return f"Error writing to file '{file_path}': {e}"
    except Exception as e:
        return f"An unexpected error occurred while writing to '{file_path}': {e}"


# ===========================================================================
# EXTENDED FILE SYSTEM TOOLS (read-only, full PC access)
# ===========================================================================

@tool
def read_file_anywhere(file_path: str) -> str:
    """
    Reads the content of any text-based file anywhere on the computer (full path required).
    This is READ-ONLY — it cannot modify files.
    Use this to read documents, logs, scripts, or any text file on the system.
    Example: read_file_anywhere('C:/Users/MyUser/Documents/report.txt')
    Binary files (images, videos) are not supported and will return an error.
    """
    abs_path = os.path.abspath(file_path)
    
    # Safety: block access to the project's own sensitive files
    sensitive_dirs = [
        os.path.abspath("db"),
        os.path.abspath(".env"),
    ]
    for s in sensitive_dirs:
        try:
            if abs_path == s or os.path.commonpath([abs_path, s]) == s:
                return f"Error: Access to '{file_path}' is restricted for security reasons."
        except ValueError:
            pass

    if not os.path.isfile(abs_path):
        return f"Error: File not found at '{abs_path}'."

    # Check file size — limit to 2MB to avoid loading huge files
    size_mb = os.path.getsize(abs_path) / (1024 * 1024)
    if size_mb > 2:
        return (
            f"Error: File '{abs_path}' is {size_mb:.1f}MB, which exceeds the 2MB read limit. "
            "Use a more specific query to read a smaller portion."
        )

    # Try reading as text with multiple encodings
    for encoding in ['utf-8', 'latin-1', 'cp1252']:
        try:
            with open(abs_path, 'r', encoding=encoding) as f:
                content = f.read()
            return f"[File: {abs_path}]\n\n{content}"
        except UnicodeDecodeError:
            continue
        except IOError as e:
            return f"Error reading '{abs_path}': {e}"

    return f"Error: Could not read '{abs_path}' as text. It may be a binary file."


@tool
def search_files(pattern: str, search_dir: str = "C:\\") -> str:
    """
    Searches for files matching a name pattern across the computer.
    Use this to locate files before reading them.
    
    Args:
        pattern: Filename pattern to search for (e.g., '*.docx', 'report*.pdf', 'budget2024.xlsx').
        search_dir: Directory to start the search from. Defaults to C:\\ (entire C drive).
    
    Returns a list of matching file paths (max 50 results).
    Example: search_files('budget*.xlsx', 'C:/Users/MyUser/Documents')
    """
    # Block searching in sensitive project directories
    abs_search = os.path.abspath(search_dir)
    
    results = []
    try:
        for root, dirs, files in os.walk(abs_search):
            # Skip hidden and system directories to avoid permission errors and speed up search
            dirs[:] = [
                d for d in dirs
                if not d.startswith('.') and d not in (
                    'Windows', '$Recycle.Bin', 'System Volume Information',
                    '__pycache__', '.git', 'node_modules', 'venv'
                )
            ]
            for filename in files:
                if glob.fnmatch.fnmatch(filename.lower(), pattern.lower()):
                    results.append(os.path.join(root, filename))
                    if len(results) >= 50:
                        break
            if len(results) >= 50:
                break

        if not results:
            return f"No files matching '{pattern}' found in '{abs_search}'."

        header = f"Found {len(results)} file(s) matching '{pattern}' in '{abs_search}'"
        if len(results) == 50:
            header += " (showing first 50 results)"
        return header + ":\n" + "\n".join(results)

    except PermissionError:
        return f"Error: Permission denied accessing '{abs_search}'. Try a more specific subdirectory."
    except Exception as e:
        return f"Error during file search: {e}"


# ===========================================================================
# WEB & COMMUNICATION TOOLS
# ===========================================================================

def is_hallucinated_or_dummy_url(url: str) -> bool:
    """
    Detects obvious placeholder or synthetic URLs generated by LLMs.
    Examples of synthetic patterns:
    - arxiv.org/abs/2401.00123
    - PMC12345678, PMC12345679
    - 123456 in DOI suffix
    - Literal placeholder strings like '[Exact URL]', '[URL]'
    """
    if not url or not isinstance(url, str):
        return True

    url_lower = url.strip().lower()
    dummy_markers = [
        "[exact url]", "[url]", "example.com", "doi.org/10.xxxx",
        "pmc12345678", "pmc12345679", "00123", "00345", "00456", "00789", "00890", "123456"
    ]
    return any(marker in url_lower for marker in dummy_markers)


@tool
def search_scientific_literature(query: str, max_results: int = 8) -> str:
    """
    Searches peer-reviewed academic literature and preprints via Crossref API.
    Returns verified scientific papers with authentic titles, authors, publication years, DOIs, and direct links.
    Use this tool for scientific, academic, and engineering topics instead of general web search.
    Example: search_scientific_literature('Machine Learning Process Engineering Modeling')
    """
    clean_query = query.strip()
    if not clean_query:
        return "Error: Empty search query provided."

    url = "https://api.crossref.org/works"
    headers = {
        "User-Agent": "AlfredoScientificScout/1.0 (mailto:admin@alfredo.com)"
    }
    params = {
        "query": clean_query,
        "rows": max(1, min(int(max_results), 10)),
        "select": "title,DOI,URL,author,created,container-title,abstract"
    }

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=12)
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("message", {}).get("items", [])
            if items:
                results = []
                for i, item in enumerate(items, 1):
                    raw_title = item.get("title", ["N/A"])
                    title = raw_title[0].strip().replace("\n", " ") if raw_title else "N/A"
                    doi = item.get("DOI", "").strip()
                    article_url = item.get("URL") or (f"https://doi.org/{doi}" if doi else "URL not available")

                    authors_list = []
                    for author in item.get("author", [])[:3]:
                        given = author.get("given", "")
                        family = author.get("family", "")
                        if family:
                            authors_list.append(f"{given} {family}".strip())
                    authors_str = ", ".join(authors_list) if authors_list else "Unknown Authors"

                    year = "Unknown Year"
                    created = item.get("created", {}).get("date-parts", [])
                    if created and created[0]:
                        year = str(created[0][0])

                    container = item.get("container-title", [])
                    journal = container[0] if container else "Academic Publication"
                    abstract = item.get("abstract") or ""
                    abstract = re.sub(r"<[^>]+>", " ", str(abstract))
                    abstract = re.sub(r"\s+", " ", abstract).strip()
                    if len(abstract) > 500:
                        abstract = abstract[:500] + "..."
                    abstract_line = abstract or "Not stated in the retrieved record"

                    results.append(
                        f"Paper {i}:\n"
                        f"- Title: {title}\n"
                        f"- Authors: {authors_str}\n"
                        f"- Journal/Source: {journal} ({year})\n"
                        f"- DOI: {doi}\n"
                        f"- Verified URL: {article_url}\n"
                        f"- Abstract: {abstract_line}\n"
                    )

                return "\n".join(results)
    except Exception as e:
        logger.error(f"Error querying Crossref API for '{clean_query}': {e}")

    # Fallback to DuckDuckGo academic search if Crossref encounters issues
    try:
        with DDGS() as ddgs:
            ddg_results = list(ddgs.text(keywords=f"{clean_query} research paper doi", max_results=max_results))
            if ddg_results:
                formatted = []
                for idx, r in enumerate(ddg_results, 1):
                    formatted.append(
                        f"Paper {idx} (Web Fallback):\n"
                        f"- Title: {r.get('title', 'N/A')}\n"
                        f"- Verified URL: {r.get('href', 'URL not available')}\n"
                        f"- Snippet: {r.get('body', 'N/A')}\n"
                    )
                return "\n".join(formatted)
    except Exception as fallback_err:
        logger.error(f"Fallback search failed for '{clean_query}': {fallback_err}")

    return f"Failed to retrieve scientific literature for query: '{clean_query}'"


@tool
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



@tool
def ask_operator(question: str) -> str:
    """
    Use this tool to ask the human operator a question when you need clarification,
    when you find multiple files with the same name, or when you need permission.
    The tool will pause your execution until the human replies.
    Example: ask_operator('I found 3 files named report.doc. Which path should I use?')
    """
    from core.human_in_the_loop import request_human_input

    chat_id = os.environ.get("CURRENT_CHAT_ID") or "dashboard"

    answer = request_human_input(chat_id, question)
    return f"Human replied: {answer}"


@tool
def calculate(expression: str) -> str:
    """
    Evaluates a mathematical expression safely and returns the result.
    Supported operations: addition (+), subtraction (-), multiplication (*), division (/), modulo (%), exponentiation (**).
    Supported functions: sqrt(), abs(), round(), min(), max(), sum(), pow(), sin(), cos(), tan(), log(), log10(), exp().
    Supported constants: pi, e.
    Example: '2 * (3 + 5)' or 'sqrt(16) + log(e)'
    """
    import math
    safe_dict = {
        'abs': abs,
        'round': round,
        'min': min,
        'max': max,
        'sum': sum,
        'pow': pow,
        'sqrt': math.sqrt,
        'sin': math.sin,
        'cos': math.cos,
        'tan': math.tan,
        'log': math.log,
        'log10': math.log10,
        'exp': math.exp,
        'pi': math.pi,
        'e': math.e,
    }
    
    # Basic input sanitation (clean spaces)
    expression = expression.strip()
    
    try:
        # Evaluate safely in sandbox
        result = eval(expression, {"__builtins__": None}, safe_dict)
        return str(result)
    except Exception as e:
        return f"Error evaluating expression '{expression}': {e}"


@tool
def write_python_file(file_path: str, content: str) -> str:
    """
    Saves generated Python code as a .py file inside the sandboxed agent workspace.
    Path is relative to workspace/ (e.g. 'scripts/modello_pirolisi.py').
    Only '.py' files are allowed.
    """
    if not file_path.endswith('.py'):
        return "Error: FileWriteTool only allows writing Python files ending with '.py'."

    # Confine all python writes to workspace/ (same sandbox as write_file)
    rel = file_path.lstrip("/\\")
    if not _is_path_safe(rel):
        return "Error: Security violation. Path is outside the designated workspace."

    full_path = os.path.join(WORKSPACE_DIR, rel)
    try:
        os.makedirs(os.path.dirname(full_path) or WORKSPACE_DIR, exist_ok=True)
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return f"Python file successfully written to '{rel}' in the workspace."
    except IOError as e:
        return f"Error writing to file '{file_path}': {e}"
    except Exception as e:
        return f"An unexpected error occurred while writing '{file_path}': {e}"


@tool
def read_python_file(file_path: str) -> str:
    """
    Permette all'agente di leggere il contenuto di un file .py generato o salvato nel progetto (es. modello_pirolisi.py).
    Il percorso può essere relativo (es. 'modello_pirolisi.py') o assoluto all'interno del progetto.
    Questo tool consente esclusivamente la lettura di file Python con estensione '.py'.
    """
    # Force extension to be .py
    if not file_path.endswith('.py'):
        return "Error: FileReadTool only allows reading Python files ending with '.py'."
        
    # Resolve absolute path relative to current working directory (project root)
    project_root = os.path.abspath(os.getcwd())
    
    # If the path is relative, resolve it against the project root
    if not os.path.isabs(file_path):
        full_path = os.path.abspath(os.path.join(project_root, file_path))
    else:
        full_path = os.path.abspath(file_path)
        
    # Security checks
    try:
        if os.path.commonpath([full_path, project_root]) != project_root:
            return "Error: Security violation. Path is outside the designated project directory."
    except ValueError:
        return "Error: Security violation. Path is outside the designated project directory."
        
    # Block reading from sensitive directories (db, .env, venv, .git)
    sensitive_dirs = [
        os.path.join(project_root, "db"),
        os.path.join(project_root, "venv"),
        os.path.join(project_root, ".git"),
    ]
    for s in sensitive_dirs:
        try:
            if full_path == s or os.path.commonpath([full_path, s]) == s:
                return f"Error: Reading from sensitive directory '{os.path.basename(s)}' is restricted for security reasons."
        except ValueError:
            pass

    if not os.path.isfile(full_path):
        return f"Error: File not found at path '{file_path}' in the project root."

    try:
        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return content
    except IOError as e:
        return f"Error reading file '{file_path}': {e}"
    except Exception as e:
        return f"An unexpected error occurred while reading '{file_path}': {e}"



@tool
def python_repl(code: str) -> str:
    """
    Esegue il codice Python fornito in un ambiente isolato (subprocess) e restituisce lo standard output e standard error.
    Può essere usato per fare un "test di compilazione" veloce, verificare sintassi, importare librerie (es. Pydantic)
    e verificare funzioni/classi prima di salvare il file definitivo. Il tempo limite di esecuzione è di 10 secondi.
    """
    import subprocess
    import sys
    
    # First, do a syntax check using built-in compile
    try:
        compile(code, "<string>", "exec")
    except SyntaxError as e:
        return f"Syntax Error: {e.msg} at line {e.lineno}, col {e.offset}\nCode line: {e.text}"
    except Exception as e:
        return f"Compilation Error: {e}"

    # If syntax is valid, execute the code in a subprocess inside WORKSPACE_DIR
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=WORKSPACE_DIR,
            capture_output=True,
            text=True,
            timeout=10
        )
        output = []
        if result.stdout:
            output.append(f"--- STDOUT ---\n{result.stdout}")
        if result.stderr:
            output.append(f"--- STDERR ---\n{result.stderr}")
            
        if not output:
            return "Execution completed successfully with no output (exit code 0)."
            
        return "\n".join(output)
    except subprocess.TimeoutExpired:
        return "Error: Execution timed out (limit: 10 seconds)."
    except Exception as e:
        return f"Runtime Error during execution: {e}"


@tool
def edit_file_diff(file_path: str, old_text: str, new_text: str) -> str:
    """
    Modifica chirurgicamente (stile Cursor / Claude Code) una sezione specifica di un file
    nel workspace senza dover riscrivere l'intero file.
    Cerca `old_text` dentro `file_path` (relativo a workspace/) e lo sostituisce con `new_text`.
    Restituisce conferma della modifica e attiva il versioning Git del workspace.
    """
    if not _is_path_safe(file_path):
        return "Error: Security violation. Path is outside the designated workspace."

    full_path = os.path.join(WORKSPACE_DIR, file_path)
    if not os.path.isfile(full_path):
        return f"Error: File '{file_path}' does not exist in workspace. Use write_file to create it first."

    try:
        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return f"Error reading '{file_path}': {e}"

    if not old_text:
        return "Error: 'old_text' cannot be empty. Provide the exact text block to replace."

    if old_text not in content:
        # Try normalized line endings
        norm_content = content.replace("\r\n", "\n")
        norm_old = old_text.replace("\r\n", "\n")
        if norm_old not in norm_content:
            return (
                f"Error: Exact block 'old_text' was not found in '{file_path}'. "
                "Read the file first with read_file to inspect exact lines."
            )
        updated = norm_content.replace(norm_old, new_text.replace("\r\n", "\n"), 1)
    else:
        updated = content.replace(old_text, new_text, 1)

    try:
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(updated)
        old_lines = len(old_text.splitlines())
        new_lines = len(new_text.splitlines())
        return (
            f"Successfully applied surgical edit to '{file_path}' "
            f"(replaced {old_lines} lines with {new_lines} lines)."
        )
    except Exception as e:
        return f"Error writing surgical edit to '{file_path}': {e}"


@tool
def git_workspace_time_travel(action: str = "diff", target_commit: str = "", message: str = "") -> str:
    """
    Gestisce il controllo versione automatico Git del workspace (Time-Travel, Diff, Checkpoint, Rollback)
    senza richiedere comandi Git manuali.
    Azioni supportate (`action`):
    - 'diff': mostra le modifiche correnti ai file del workspace (inclusi Word .docx, Excel .xlsx, Markdown, CSV).
    - 'log': elenca gli ultimi checkpoint salvati nel workspace con i relativi ID (`short_sha`).
    - 'checkpoint': salva immediatamente un checkpoint Git con descrizione `message`.
    - 'rollback': ripristina il workspace allo stato del checkpoint indicato in `target_commit` (SHA).
    """
    try:
        from core.git_workspace import get_git_workspace
        git_ws = get_git_workspace()
    except Exception as e:
        return f"Error initializing GitWorkspaceManager: {e}"

    act = (action or "diff").strip().lower()
    if act == "diff":
        diff_info = git_ws.get_smart_diff()
        if not diff_info.get("has_changes"):
            return "Workspace pulito: nessuna modifica rispetto all'ultimo checkpoint Git."
        return f"Riepilogo: {diff_info.get('summary')}\n\n{diff_info.get('diff_text')}"

    if act == "log":
        cps = git_ws.list_checkpoints(limit=15)
        if not cps:
            return "Nessun checkpoint Git trovato nel workspace."
        lines = ["⏱️ CRONOLOGIA CHECKPOINT WORKSPACE (Git Time-Travel):"]
        for cp in cps:
            files_str = ", ".join(cp["changed_files"][:3]) if cp["changed_files"] else "nessun file"
            lines.append(f"- [{cp['short_sha']}] ({cp['timestamp']}) {cp['title']} | File: {files_str}")
        return "\n".join(lines)

    if act == "checkpoint":
        cp = git_ws.checkpoint_step(
            run_id=None,
            step_label="Agent-Checkpoint",
            agent_role="Agent",
            task_name=message or "Checkpoint richiesto dall'agente",
        )
        if cp.get("committed"):
            return f"✅ Checkpoint creato [{cp.get('short_sha')}]: {cp.get('summary')}"
        return "ℹ️ Nessuna modifica da salvare nel workspace."

    if act == "rollback":
        if not target_commit.strip():
            return "Error: specifica 'target_commit' (es. lo short_sha ottenuto con action='log') per eseguire il rollback."
        res = git_ws.rollback_to_commit(target_commit.strip(), reason=message or "Agent/Workflow Rollback")
        if res.get("success"):
            return f"⏪ {res.get('message')} (Nuovo commit: {res.get('new_short_sha')})"
        return f"Error: {res.get('error')}"

    return f"Error: Azione '{action}' non riconosciuta. Usa 'diff', 'log', 'checkpoint' o 'rollback'."
