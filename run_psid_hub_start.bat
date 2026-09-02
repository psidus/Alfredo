@echo off
title Alfredo PSID Hub - start
cd /d "%~dp0"

echo Starting hub_postgres + hub...
docker compose --profile hub up -d hub_postgres hub
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Failed. Is Docker Desktop running?
    exit /b 1
)

echo Waiting for hub...
timeout /t 5 /nobreak > nul
curl -s http://localhost:8010/hub/health
echo.
echo Hub running at http://localhost:8010  (LAN: http://^<your-ip^>:8010)
exit /b 0
