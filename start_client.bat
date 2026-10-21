@echo off
setlocal
title Remote Desktop - Portatile (Client)
cd /d "%~dp0"

echo Avvio dell'Applicazione Client...
if exist "venv\Scripts\python.exe" (
    start "" "venv\Scripts\pythonw.exe" "src\client_viewer.py"
) else (
    start "" pythonw "src\client_viewer.py"
)
exit
