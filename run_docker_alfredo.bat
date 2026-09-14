@echo off
title Alfredo AI OS - Docker launcher
cd /d "%~dp0"
echo Starting Alfredo Docker containers...

if not exist .env (
    echo Creating .env file from .env.example...
    copy .env.example .env
)

:: Ensure Docker Desktop is running
docker info >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo Docker Desktop is not running. Starting Docker Desktop...
    start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    echo Waiting for Docker engine to initialize...
    :wait_docker_ready
    timeout /t 3 /nobreak >nul
    docker info >nul 2>&1
    if %ERRORLEVEL% NEQ 0 goto wait_docker_ready
    echo Docker is ready!
)

:: Start containers in detached mode (smart mount allows code changes without rebuilding)
docker compose up -d

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Failed to start Docker containers. 
    echo Please make sure Docker Desktop is open and running!
    echo.
    pause
    exit /b
)

echo Waiting for Alfredo to start...
timeout /t 4 /nobreak > nul

echo Opening Alfredo Dashboard in your browser...
start http://localhost:8501

echo.
echo ==========================================
echo Alfredo AI OS is running in Docker!
echo ==========================================
echo.
if exist config\advanced_rag_setup.json (
    echo [Marker GPU Enabled] Starting Marker API Server in background...
    start /min "Marker GPU API Server" cmd /c "start_marker_api.bat"
)

echo [To STOP Alfredo, close this window or press any key to shutdown containers]
echo.
pause

echo Stopping Alfredo Docker containers...
docker compose down

if exist config\advanced_rag_setup.json (
    echo Stopping Marker API Server...
    taskkill /FI "WINDOWTITLE eq Marker GPU API Server*" /F >nul 2>&1
    :: Fallback to stop the specific python process if window title kill fails
    for /f "tokens=5" %%a in ('netstat -aon ^| find "8001" ^| find "LISTENING"') do taskkill /f /pid %%a >nul 2>&1
)
exit
