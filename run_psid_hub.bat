@echo off
title Alfredo PSID Hub - Docker launcher
cd /d "%~dp0"

echo ==========================================
echo  Alfredo PSID Company Hub (LAN)
echo ==========================================
echo.
echo Use run_psid_hub_start.bat to start in background.
echo Use run_psid_hub_stop.bat to stop.
echo.

call run_psid_hub_start.bat
if %ERRORLEVEL% NEQ 0 pause
exit /b %ERRORLEVEL%
