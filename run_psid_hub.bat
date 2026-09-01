@echo off
title Alfredo PSID Hub - Docker launcher
cd /d "%~dp0"

echo ==========================================
echo  Alfredo PSID Company Hub (LAN)
echo ==========================================
echo.

if not exist .env (
    echo [WARN] .env not found. Copy deploy\psid\.env.hub.example settings into .env first.
    echo.
)

echo Starting hub_postgres + hub containers...
docker compose --profile hub up -d hub_postgres hub

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Failed to start hub. Is Docker Desktop running?
    echo.
    pause
    exit /b 1
)

echo.
echo Waiting for hub to become ready...
timeout /t 5 /nobreak > nul

echo.
echo Health check:
curl -s http://localhost:8010/hub/health
echo.
echo.

echo Hub URL (after hosts file):  http://psid.us:8010/hub/health
echo Hub URL (LAN IP):            http://<your-lan-ip>:8010/hub/health
echo.
echo [Hub is running in background. Press any key to STOP hub containers.]
pause

echo Stopping hub containers...
docker compose --profile hub stop hub hub_postgres
exit /b 0
