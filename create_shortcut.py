import os
import subprocess
import sys

def create_shortcut():
    # 1. Configurazioni
    project_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Rilevamento dinamico del Desktop (gestisce OneDrive)
    try:
        desktop = subprocess.check_output(['powershell', '-Command', '[Environment]::GetFolderPath("Desktop")'], text=True).strip()
    except:
        desktop = os.path.join(os.environ['USERPROFILE'], 'Desktop')
        
    shortcut_path = os.path.join(desktop, "Alfredo.lnk")
    bat_path = os.path.join(project_dir, "run_alfredo.bat")
    
    # Rilevamento ambiente virtuale (cerca nomi comuni)
    venv_name = None
    for candidate in ["venv", ".venv", "agents_force_env"]:
        if os.path.exists(os.path.join(project_dir, candidate)):
            venv_name = candidate
            break
    
    if not venv_name:
        print("ERRORE: Non è stato trovato alcun ambiente virtuale (venv, .venv o agents_force_env).")
        print("Crea un ambiente virtuale prima di eseguire questo script.")
        return

    # 2. Creazione del file .bat di avvio
    # Usiamo il percorso diretto all'eseguibile python per massima stabilità
    python_exe = os.path.join(project_dir, venv_name, "Scripts", "python.exe")
    bat_content = f"""@echo off
title Alfredo AI OS - Avvio in corso...
cd /d "{project_dir}"
echo Avvio di Alfredo in corso (ambiente: {venv_name})...
"{python_exe}" -m streamlit run ui/dashboard.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Si e verificato un errore durante l'avvio.
    pause
)
"""
    
    try:
        with open(bat_path, "w") as f:
            f.write(bat_content)
        print(f"File di avvio creato: {bat_path}")
    except Exception as e:
        print(f"Errore nella creazione del file .bat: {e}")
        return

    # 3. Creazione del collegamento sul Desktop tramite PowerShell
    # Usiamo PowerShell perché è già presente su Windows e non richiede pip install
    
    # Se hai un'icona .ico, puoi aggiungerla qui. Altrimenti usa l'icona di sistema.
    icon_path = os.path.join(project_dir, "ui", "favicon.ico") # Modifica se hai un file .ico specifico
    if not os.path.exists(icon_path):
        icon_path = "" # Lascia vuoto per icona predefinita

    ps_script = f"""
    $WshShell = New-Object -ComObject WScript.Shell
    $Shortcut = $WshShell.CreateShortcut("{shortcut_path}")
    $Shortcut.TargetPath = "{bat_path}"
    $Shortcut.WorkingDirectory = "{project_dir}"
    $Shortcut.Description = "Avvia Alfredo AI OS"
    """
    
    if icon_path:
        ps_script += f'\n$Shortcut.IconLocation = "{icon_path}"'
        
    ps_script += "\n$Shortcut.Save()"

    try:
        subprocess.run(["powershell", "-Command", ps_script], check=True)
        print(f"Successo! Icona creata sul Desktop: {shortcut_path}")
    except Exception as e:
        print(f"Errore nella creazione del collegamento: {e}")

if __name__ == "__main__":
    create_shortcut()
