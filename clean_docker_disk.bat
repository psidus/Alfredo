@echo off
title Pulizia Disco e Cache Docker - Alfredo AI OS
cd /d "%~dp0"

echo ======================================================
echo    PULIZIA CACHE E DISCO DOCKER (Alfredo AI OS)
echo ======================================================
echo.
echo Questa procedura eliminera':
echo  - Build cache non utilizzata di Docker (BuildKit)
echo  - Immagini vecchie/dangling e duplicati legacy
echo  - Container e reti non utilizzati (i dati del database e l'immagine alfredo-app sono preservati)
echo.
echo Non verra' toccato nessun file di progetto o del tuo PC.
echo.
set /p CONFIRM="Vuoi procedere? (S/N): "
if /i not "%CONFIRM%"=="s" if /i not "%CONFIRM%"=="si" if /i not "%CONFIRM%"=="y" (
    echo Operazione annullata.
    pause
    exit /b
)

echo.
echo [1/4] Verifico che Docker Desktop sia attivo...
docker info >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo Docker Desktop non e' avviato. Avvio Docker Desktop...
    start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    echo Attendo l'avvio del demone Docker...
    :wait_docker
    timeout /t 3 /nobreak >nul
    docker info >nul 2>&1
    if %ERRORLEVEL% NEQ 0 goto wait_docker
    echo Docker Desktop e' ora operativo!
)

echo.
echo [2/4] Pulizia Build Cache non utilizzata di Docker...
docker builder prune -f

echo.
echo [3/4] Pulizia Container terminati, Immagini orfane e duplicati legacy...
docker rmi alfredo-api:latest alfredo-dashboard:latest alfredo-headroom_proxy:latest alfredo-hub:latest >nul 2>&1
docker image prune -f
docker container prune -f
docker network prune -f

echo.
echo [4/4] Ottimizzazione disco WSL e rilascio spazio su C:...
wsl --shutdown >nul 2>&1
timeout /t 2 /nobreak >nul

echo.
echo ======================================================
echo Pulizia completata con successo!
echo.
echo [SUGGERIMENTO] Per compattare il file disco virtuale di Docker
echo (docker_data.vhdx) e recuperare fino a 40-50 GB fisici su C:,
echo esegui "compact_docker_disk.bat" come Amministratore!
echo ======================================================
echo.
pause
