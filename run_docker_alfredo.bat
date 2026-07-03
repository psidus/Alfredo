@echo off
title Alfredo AI OS - Docker launcher
cd /d "%~dp0"
echo Starting Alfredo Docker containers...

if not exist .env (
    echo Creating .env file from .env.example...
    copy .env.example .env
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
echo [To STOP Alfredo, close this window or press any key to shutdown containers]
echo.
pause

echo Stopping Alfredo Docker containers...
docker compose down
exit
