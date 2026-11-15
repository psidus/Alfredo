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

REM 2. Verifica se la porta 8000 e' gia' attiva
netstat -ano | find "8000" | find "LISTENING" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo [OK] Il server API di Alfredo e' gia' attivo sulla porta 8000.
) else (
    echo [Avvio] Avvio del server API di Alfredo sulla porta 8000...
    start "Alfredo API Server" /min "%PYTHON_EXE%" -c "from core.api_server import start_api_server; start_api_server()"
    timeout /t 3 /nobreak >nul
)

REM 3. Rileva eseguibile ngrok
set "NGROK_EXE=ngrok"
if exist "%~dp0ngrok.exe" set "NGROK_EXE=%~dp0ngrok.exe"

REM 4. Verifica e chiudi eventuali processi ngrok orfani in background (evita ERR_NGROK_334)
tasklist /fi "imagename eq ngrok.exe" 2>nul | find /i "ngrok.exe" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo [Pulizia] Trovata istanza ngrok precedente in background. Riavvio pulito...
    taskkill /f /im ngrok.exe >nul 2>&1
    timeout /t 1 /nobreak >nul
)

REM 5. Apri il browser locale per installazione PWA su PC
echo.
echo [Browser Locale] Apertura interfaccia PWA locale per il PC...
start http://localhost:8000/app

echo.
echo ===============================================================
echo [AVVIO TUNNEL NGROK]
echo Indirizzo statico assegnato:
echo https://inexpugnable-marvis-nonvalid.ngrok-free.app/app
echo.
echo ATTENZIONE: NON CHIUDERE QUESTA FINESTRA!
echo Puoi ridurla a icona. Se la chiudi, il tunnel si scollega
echo e sul telefono comparira' l'errore ERR_NGROK_3200 (Offline).
echo ===============================================================
echo.

"%NGROK_EXE%" http 8000 --url inexpugnable-marvis-nonvalid.ngrok-free.app
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ===============================================================
    echo [ATTENZIONE NGROK] Il tunnel e' terminato o ha restituito errore [Codice: %ERRORLEVEL%].
    echo Se il dominio e' occupato altrove, controlla: https://dashboard.ngrok.com/endpoints
    echo ===============================================================
    echo.
)
pause
