@echo off
title Compatta Disco Virtuale Docker WSL2 - Risparmio Spazio C:
cd /d "%~dp0"

echo ================================================================
echo    COMPATTAZIONE DISCO VIRTUALE DOCKER / WSL2 (Rilascio Spazio C:)
echo ================================================================
echo.

:: Check Admin privileges and auto-elevate if needed
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Richiesta permessi di Amministratore per diskpart...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

:: Find VHDX file
set VHDX_PATH=
if exist "%LOCALAPPDATA%\Docker\wsl\disk\docker_data.vhdx" set "VHDX_PATH=%LOCALAPPDATA%\Docker\wsl\disk\docker_data.vhdx"
if not defined VHDX_PATH (
    if exist "%LOCALAPPDATA%\Docker\wsl\data\ext4.vhdx" set "VHDX_PATH=%LOCALAPPDATA%\Docker\wsl\data\ext4.vhdx"
)

if not defined VHDX_PATH (
    echo [ERRORE] File del disco virtuale Docker non trovato in:
    echo   %LOCALAPPDATA%\Docker\wsl\disk\docker_data.vhdx
    echo   %LOCALAPPDATA%\Docker\wsl\data\ext4.vhdx
    echo.
    pause
    exit /b 1
)

echo File disco trovato: "%VHDX_PATH%"
echo.
echo Arresto WSL2 per rilasciare l'accesso esclusivo al file...
wsl --shutdown >nul 2>&1
timeout /t 3 /nobreak >nul

echo Compattazione del disco virtuale in corso (diskpart)...
echo [Attendere: l'operazione puo' richiedere 1-3 minuti]...

(
echo select vdisk file="%VHDX_PATH%"
echo attach vdisk readonly
echo compact vdisk
echo detach vdisk
) > "%TEMP%\compact_docker_script.txt"

diskpart /s "%TEMP%\compact_docker_script.txt"
del "%TEMP%\compact_docker_script.txt" >nul 2>&1

echo.
echo ================================================================
echo Compattazione completata con successo!
echo Lo spazio inutilizzato e' stato restituito al tuo disco C:.
echo ================================================================
echo.
pause
