@echo off
title Alfredo AI OS - Avvio in corso...
cd /d "%~dp0"
echo Preparazione dell'ambiente e caricamento esempi...
call venv\Scripts\python.exe seed_startup_example.py
echo Avvio di Alfredo in corso (ambiente: venv)...
call venv\Scripts\python.exe -m streamlit run ui/dashboard.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Si e verificato un errore durante l'avvio.
    pause
)
