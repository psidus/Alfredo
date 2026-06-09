import os
import logging
from crewai.tools import tool
from core.human_in_the_loop import request_human_input

logger = logging.getLogger(__name__)

# Assumiamo che il workspace/progetto di riferimento sia la cartella corrente
PROJECT_ROOT = os.path.abspath(os.getcwd())

def _is_safe_path(path: str) -> bool:
    """Verifica che il path sia all'interno della cartella di progetto."""
    resolved_path = os.path.abspath(os.path.join(PROJECT_ROOT, path))
    return resolved_path.startswith(PROJECT_ROOT)

@tool
def explore_codebase(target_type: str, target_path: str = "", query: str = "") -> str:
    """
    Strumento in SOLA LETTURA per esplorare il codice di progetto.
    Utilizza questo tool per esaminare file, cartelle, la struttura generale o per CERCARE file specifici.
    
    Args:
        target_type (str): "file", "folder", "structure", o "search".
        target_path (str): Il percorso relativo alla root del progetto (es. "core/", o "main.py"). Opzionale per "search".
        query (str): Usato SOLO se target_type="search". La parola o estensione da cercare (es. "components" o "*.py").
        
    Returns:
        str: Il contenuto formattato (codice, lista file, albero, o risultati della ricerca).
    """
    if target_type not in ["file", "folder", "structure", "search"]:
        return "Errore: 'target_type' deve essere 'file', 'folder', 'structure', o 'search'."
        
    if not _is_safe_path(target_path):
        return "Errore di sicurezza: path fuori dalla cartella di progetto."
        
    full_path = os.path.abspath(os.path.join(PROJECT_ROOT, target_path))
    
    if not os.path.exists(full_path):
        return f"Errore: Il percorso '{target_path}' non esiste nel progetto."

    if target_type == "file":
        if not os.path.isfile(full_path):
            return f"Errore: '{target_path}' non è un file."
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                content = f.read()
            # Formattazione utile per la UI in Markdown
            return f"### File: {target_path}\n```python\n{content}\n```"
        except Exception as e:
            return f"Errore durante la lettura del file: {e}"

    elif target_type == "folder":
        if not os.path.isdir(full_path):
            return f"Errore: '{target_path}' non è una cartella."
        try:
            items = os.listdir(full_path)
            formatted_items = "\n".join([f"- {item}" for item in items])
            return f"### Contenuto della cartella: {target_path}\n{formatted_items}"
        except Exception as e:
            return f"Errore durante la lettura della cartella: {e}"

    elif target_type == "structure":
        if not os.path.isdir(full_path):
            return f"Errore: '{target_path}' non è una cartella. La struttura si estrae da cartelle."
        
        # Genera un albero (max depth 2 o 3 per non inondare il token limit)
        tree = []
        max_depth = 2
        root_depth = full_path.rstrip(os.path.sep).count(os.path.sep)
        
        for dirpath, dirnames, filenames in os.walk(full_path):
            # Filtra directory ignorate
            dirnames[:] = [d for d in dirnames if not d.startswith('.') and d not in ('__pycache__', 'venv', 'node_modules')]
            
            current_depth = dirpath.rstrip(os.path.sep).count(os.path.sep) - root_depth
            if current_depth > max_depth:
                del dirnames[:]
                continue
                
            indent = "  " * current_depth
            folder_name = os.path.basename(dirpath) if current_depth > 0 else (target_path or "/")
            tree.append(f"{indent}📂 {folder_name}/")
            
            sub_indent = "  " * (current_depth + 1)
            for f in filenames:
                tree.append(f"{sub_indent}📄 {f}")
                
        return f"### Struttura (profondità {max_depth}): {target_path or 'Root'}\n" + "\n".join(tree)

    elif target_type == "search":
        import glob
        if not query:
            return "Errore: Inserisci una 'query' per eseguire la ricerca (es. 'database' o '*.py')."
        
        results = []
        search_root = full_path if os.path.isdir(full_path) else PROJECT_ROOT
        
        for root, dirs, files in os.walk(search_root):
            # Filtra directory ignorate
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('__pycache__', 'venv', 'node_modules')]
            
            for filename in files:
                # Ricerca per substringa o per glob
                if query.lower() in filename.lower() or glob.fnmatch.fnmatch(filename.lower(), query.lower()):
                    rel_p = os.path.relpath(os.path.join(root, filename), PROJECT_ROOT)
                    results.append(rel_p)
                    
        if not results:
            return f"Nessun file trovato corrispondente a '{query}' in '{target_path or 'Root'}'."
            
        formatted_results = "\n".join([f"- {r}" for r in results])
        return f"### Risultati Ricerca per '{query}':\n{formatted_results}\n\n*Usa target_type='file' con uno di questi percorsi per leggerne il contenuto.*"


@tool
def modify_project_file(file_path: str, target_text: str, replacement_text: str) -> str:
    """
    Strumento in SCRITTURA (Master AI). Sostituisce un blocco esatto di testo in un file.
    RICHIEDE L'APPROVAZIONE UMANA prima di salvare effettivamente i cambiamenti.
    
    Args:
        file_path (str): Il percorso del file da modificare.
        target_text (str): Il testo ESATTO che deve essere sostituito.
        replacement_text (str): Il nuovo testo da inserire.
    """
    if not _is_safe_path(file_path):
        return "Errore di sicurezza: path fuori dalla cartella di progetto."
        
    full_path = os.path.abspath(os.path.join(PROJECT_ROOT, file_path))
    
    if not os.path.isfile(full_path):
        return f"Errore: Il file '{file_path}' non esiste."

    try:
        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        return f"Errore nella lettura del file originale: {e}"

    if target_text not in content:
        return f"Errore: Il 'target_text' fornito non è stato trovato esattamente nel file. Controlla gli spazi e i ritorni a capo."

    # Verifica per HITL
    chat_id = os.environ.get("CURRENT_CHAT_ID")
    if not chat_id:
        return "Errore: 'CURRENT_CHAT_ID' non impostato. Impossibile contattare l'operatore umano."

    # Costruzione della "Patch" per l'umano
    diff_message = (
        f"L'agente vuole modificare il file: <b>{file_path}</b>\n\n"
        f"🔴 <b>Testo originale:</b>\n<pre>{target_text}</pre>\n\n"
        f"🟢 <b>Nuovo testo:</b>\n<pre>{replacement_text}</pre>\n\n"
        f"Vuoi applicare questa modifica? Rispondi SI o NO."
    )

    # Chiede all'umano e attende (si blocca finchè non riceve risposta tramite DB)
    answer = request_human_input(chat_id, diff_message)
    
    if answer.lower().strip() in ['si', 'sì', 'yes', 'y']:
        # Applica modifica
        new_content = content.replace(target_text, replacement_text, 1) # rimpiazza la prima occorrenza
        try:
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            return f"Modifica applicata con successo al file '{file_path}'."
        except Exception as e:
            return f"Errore durante il salvataggio del file modificato: {e}"
    else:
        return f"Modifica rifiutata dall'operatore umano (Motivazione o risposta: {answer}). Nessuna modifica apportata."
