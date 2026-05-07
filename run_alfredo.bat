@echo off
title Alfredo AI OS - Avvio in corso...
cd /d "C:\Users\pietr\OneDrive\Documenti\GitHub\Alfredo"
echo Avvio di Alfredo in corso (ambiente: venv)...
"C:\Users\pietr\OneDrive\Documenti\GitHub\Alfredo\venv\Scripts\python.exe" -m streamlit run ui/dashboard.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Si e verificato un errore durante l'avvio.
    pause
)
