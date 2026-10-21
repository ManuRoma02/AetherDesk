@echo off
setlocal
title AetherDesk - Client Portatile
cd /d "%~dp0"

echo Avvio di AetherDesk Client...
if exist "venv\Scripts\python.exe" (
    start "" "venv\Scripts\pythonw.exe" "src\client_viewer.py"
) else (
    start "" pythonw "src\client_viewer.py"
)
exit
