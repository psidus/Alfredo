@echo off
echo Starting Marker API Server...
call venv_rag\Scripts\activate.bat
python tools\marker_api_server.py
