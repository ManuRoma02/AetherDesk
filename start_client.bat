@echo off
setlocal
title AetherDesk - Client Portatile
cd /d "%~dp0"
set "PYTHONPATH=%~dp0"

echo Avvio di AetherDesk Client...
if exist "dist\AetherDesk.exe" (
    start "" "dist\AetherDesk.exe"
) else if exist "AetherDesk.exe" (
    start "" "AetherDesk.exe"
) else if exist "venv\Scripts\python.exe" (
    start "" "venv\Scripts\pythonw.exe" "src\client_viewer.py"
) else if exist "F:\miniforge3\pythonw.exe" (
    start "" "F:\miniforge3\pythonw.exe" "src\client_viewer.py"
) else (
    start "" pythonw "src\client_viewer.py"
)
exit
