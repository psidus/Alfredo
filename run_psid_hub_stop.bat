@echo off
title Alfredo PSID Hub - stop
cd /d "%~dp0"
docker compose --profile hub stop hub hub_postgres
echo Hub stopped.
exit /b 0
