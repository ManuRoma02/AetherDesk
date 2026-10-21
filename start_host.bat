@echo off
setlocal
title Remote Desktop - Computer Fisso (Host)
cd /d "%~dp0"

echo Avvio del Pannello di Controllo Host...
if exist "venv\Scripts\python.exe" (
    start "" "venv\Scripts\pythonw.exe" "src\host_tray.py"
) else (
    start "" pythonw "src\host_tray.py"
)
exit
