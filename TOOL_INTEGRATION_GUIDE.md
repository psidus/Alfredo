# Guida all'Integrazione dei Tools (AI OS)

Questa guida spiega come il sistema gestisce gli strumenti (tools) che permettono agli agenti IA di interagire con il computer locale e con servizi esterni (es. Gmail, Google Drive, ecc.).

## 1. Architettura dei Tool

I tool in questo progetto sono basati sull'architettura di **CrewAI**.
Un tool è semplicemente una funzione Python decorata con `@tool` (da `crewai_tools`).
- **Il nome e la docstring (`"""..."""`)** della funzione sono cruciali: l'IA li legge per capire a cosa serve lo strumento e come usarlo.
- **I parametri della funzione** definiscono cosa l'IA deve fornire (es. `file_path`, `command_str`).

I tool attualmente implementati sono:
- `tools/local_tools.py`: Lettura/scrittura file (con sandboxing in `/workspace`) e ricerca web.
- `tools/terminal_executor.py`: Esecuzione di comandi terminale (con rigidi controlli di sicurezza e blocco dei comandi distruttivi).

## 2. Il Registro dei Tool (`config/tools_map.yaml`)

Per rendere un tool disponibile nell'interfaccia grafica (UI) e agli agenti, deve essere registrato nel file `config/tools_map.yaml`.

Questo file funge da "Anagrafe" e gestisce la sicurezza delle credenziali:

```yaml
tools_registry:
  search_web:
    display_name: "Web: DuckDuckGo Search"
    description: "Ricerca gratuita su internet."
    required_secrets: [] # Nessuna chiave API necessaria
    
  gmail_send:
    display_name: "Google: Gmail Sender"
    description: "Invia email tramite Gmail API."
    required_secrets: ["GMAIL_API_KEY", "GMAIL_SENDER_EMAIL"]
```

### Come funziona la gestione dei Segreti:
1.  **Sicurezza**: Le chiavi API non vengono mai salvate nel database o mostrate nell'interfaccia utente.
2.  **API Vault (Dashboard)**: L'interfaccia legge il file YAML e controlla nel tuo file `.env` se le chiavi richieste (es. `GMAIL_API_KEY`) sono presenti. 
3.  Se mancano, il tool viene segnalato con un semaforo rosso 🔴 nella UI.

## 3. Come aggiungere un NUOVO Tool (Tutorial)

Supponiamo che tu voglia aggiungere un tool per leggere file PDF.

**Passo 1: Crea il codice del Tool**
Crea una nuova funzione in `tools/custom_tools.py` (o in `local_tools.py` se affine):
```python
from crewai_tools import tool
import os

@tool("PDF Reader")
def read_pdf(file_path: str) -> str:
    \"\"\"Legge il contenuto testuale di un file PDF dal workspace.\"\"\"
    # ... logica python ...
    return text
```

**Passo 2: Registralo nel YAML**
Aggiungi il tool al file `config/tools_map.yaml`:
```yaml
  read_pdf:
    display_name: "Local: PDF Reader"
    description: "Legge file PDF."
    required_secrets: []
```

**Passo 3: Mappalo nell'Executor**
Affinché il sistema di esecuzione sappia quale funzione Python corrisponde alla stringa "read_pdf", aggiorna il file `agents/executor.py`:
```python
# agents/executor.py
from tools.custom_tools import read_pdf

TOOL_REGISTRY = {
    # ... altri tool ...
    "read_pdf": read_pdf 
}
```

## 4. Assegnazione e Sicurezza (Importante!)

**Come funziona l'assegnazione:**
- Tramite il **Task Builder (Dashboard)**, puoi assegnare specifici tool a specifiche task.
- Se crei una task "Scrivi un report" e le assegni il tool `write_file`, l'agente che eseguirà quella task avrà il permesso di scrivere file *solo durante quell'operazione*.

**Accesso al Computer (Sicurezza):**
Dare agli agenti IA accesso al terminale o ai file locali è estremamente pericoloso se non controllato.
1.  **File System**: Tutti i file tools (`read_file`, `write_file`) sono bloccati tramite una funzione di validazione (`_is_path_safe`) che costringe l'agente a operare SOLO nella cartella `workspace/`. Qualsiasi tentativo di accedere a file di sistema (es. `/etc/passwd` o `C:\Windows`) verrà bloccato.
2.  **Terminale**: Il `Terminal Executor` ha una blacklist di comandi (come `rm`, `sudo`, `format`) e operatori (`>`, `|`, `&&`) e impedisce l'accesso ai file del database o ai file `.env`.

Questa architettura ti permette di avere un sistema modulare, sicuro e facile da espandere con qualsiasi API esterna (aggiungendo la chiave nel `.env` e registrandola nel YAML).
