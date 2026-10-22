@echo off
setlocal
title AetherDesk - Host Remoto
cd /d "%~dp0"
set "PYTHONPATH=%~dp0"

echo Avvio del Pannello Host AetherDesk...
if exist "dist\AetherDesk.exe" (
    start "" "dist\AetherDesk.exe" --host
) else if exist "AetherDesk.exe" (
    start "" "AetherDesk.exe" --host
) else if exist "venv\Scripts\python.exe" (
    start "" "venv\Scripts\pythonw.exe" "src\host_tray.py"
) else if exist "F:\miniforge3\pythonw.exe" (
    start "" "F:\miniforge3\pythonw.exe" "src\host_tray.py"
) else (
    start "" pythonw "src\host_tray.py"
)
exit
