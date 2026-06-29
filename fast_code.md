# ==========================================
# COMANDI PER HEADROOM AI E STATISTICHE
# ==========================================

# Prima di tutto, per attivare l'ambiente virtuale (venv) nel terminale usa:
# Se usi PowerShell:
.\venv\Scripts\Activate.ps1
# Se usi il Prompt dei comandi (CMD):
.\venv\Scripts\activate.bat

# 1. Apri un NUOVO terminale nella cartella di Alfredo, attiva il venv e avvia il Proxy:
headroom proxy --port 8787

# 2. Apri un SECONDO terminale, attiva il venv e controlla i risparmi:
headroom perf

# 3. Apri un TERZO terminale e avvia Alfredo normalmente:
.\run_alfredo.bat

# ==========================================
# SE USI DOCKER (invece dei comandi sopra)
# ==========================================
# Una volta che i container sono in esecuzione con `.\run_docker_alfredo.bat`, 
# per vedere la "Live Dashboard" (le statistiche) apri un terminale e lancia:
docker exec -it alfredo_headroom_proxy headroom perf
