@echo off
title Alfredo Mobile PWA ^& Ngrok Tunnel
cd /d "%~dp0"

echo ===============================================================
echo            ALFREDO MOBILE PWA ^& NGROK LAUNCHER
echo ===============================================================
echo.

:: 1. Rileva Python venv
set "PYTHON_EXE="
if exist "%~dp0.venv\Scripts\python.exe" set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not defined PYTHON_EXE if exist "%~dp0venv\Scripts\python.exe" set "PYTHON_EXE=%~dp0venv\Scripts\python.exe"
if not defined PYTHON_EXE if exist "%USERPROFILE%\OneDrive - UNIVERSIDAD DE SEVILLA\Desktop\PROJECT_MOEVE\.venv\Scripts\python.exe" set "PYTHON_EXE=%USERPROFILE%\OneDrive - UNIVERSIDAD DE SEVILLA\Desktop\PROJECT_MOEVE\.venv\Scripts\python.exe"
if not defined PYTHON_EXE if exist "%USERPROFILE%\Desktop\PROJECT_MOEVE\.venv\Scripts\python.exe" set "PYTHON_EXE=%USERPROFILE%\Desktop\PROJECT_MOEVE\.venv\Scripts\python.exe"
if not defined PYTHON_EXE set "PYTHON_EXE=python"

:: 2. Verifica se la porta 8000 e' gia' attiva
netstat -ano | find "8000" | find "LISTENING" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo [OK] Il server API di Alfredo e' gia' attivo sulla porta 8000.
) else (
    echo [Avvio] Avvio del server API di Alfredo (porta 8000)...
    start "Alfredo API Server" /min "%PYTHON_EXE%" -c "from core.api_server import start_api_server; start_api_server()"
    timeout /t 3 /nobreak >nul
)

:: 3. Apri il browser locale per installazione PWA su PC
echo.
echo [Browser Locale] Apertura interfaccia PWA locale per il PC...
start http://localhost:8000/app

echo.
echo ===============================================================
echo [AVVIO TUNNEL NGROK]
echo Copia l'indirizzo HTTPS (es: https://xxxx.ngrok-free.app) e 
echo aggiungi /app alla fine (es: https://xxxx.ngrok-free.app/app)
echo per aprirlo e installarlo sul tuo telefono Android!
echo ===============================================================
echo.

ngrok http 8000
