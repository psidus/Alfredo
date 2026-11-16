@echo off
title Alfredo Mobile Tunnel (ngrok)
cd /d "%~dp0"
echo ========================================================
echo Avvio del tunnel ngrok per Alfredo Mobile App...
echo URL: https://inexpugnable-marvis-nonvalid.ngrok-free.app
echo Target locale: http://127.0.0.1:8000
echo.
echo NOTA: Lascia aperta questa finestra mentre usi l'app sul cellulare!
echo ========================================================
echo.
ngrok.exe http 127.0.0.1:8000 --url=inexpugnable-marvis-nonvalid.ngrok-free.app --host-header=rewrite
pause
