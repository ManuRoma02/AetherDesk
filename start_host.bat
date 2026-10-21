@echo off
setlocal
title AetherDesk - Host Remoto
cd /d "%~dp0"

echo Avvio del Pannello Host AetherDesk...
if exist "venv\Scripts\python.exe" (
    start "" "venv\Scripts\pythonw.exe" "src\host_tray.py"
) else (
    start "" pythonw "src\host_tray.py"
)
exit
