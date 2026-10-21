@echo off
setlocal
echo ========================================================
echo   Installazione Servizio RemoteDesktop (Avvio Automatico)
echo ========================================================

:: Verifica privilegi di amministratore
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Richiesta permessi di amministratore...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

cd /d "%~dp0\.."

set "PYTHON_EXE=python.exe"
set "SERVICE_NAME=RemoteDesktopHost"
set "DISPLAY_NAME=Remote Desktop Unattended Host Service"
set "BIN_PATH=\"%~dp0..\venv\Scripts\python.exe\" \"%~dp0..\src\host_service.py\""

:: Se non siamo in un venv, usa il python di sistema
if not exist "%~dp0..\venv\Scripts\python.exe" (
    for /f "tokens=*" %%i in ('where python') do set "SYS_PYTHON=%%i" & goto :found_python
)
:found_python
if defined SYS_PYTHON (
    set "BIN_PATH=\"%SYS_PYTHON%\" \"%~dp0..\src\host_service.py\""
)

echo Rimozione eventuale istanza precedente...
sc.exe stop %SERVICE_NAME% >nul 2>&1
sc.exe delete %SERVICE_NAME% >nul 2>&1
timeout /t 1 >nul

echo Creazione servizio di sistema con avvio automatico all'accensione...
sc.exe create %SERVICE_NAME% binPath= "%BIN_PATH%" start= auto DisplayName= "%DISPLAY_NAME%"
sc.exe description %SERVICE_NAME% "Consente il controllo remoto sicuro del computer anche da fuori casa, attivo all'avvio prima del login."
sc.exe failure %SERVICE_NAME% reset= 86400 actions= restart/5000/restart/10000/restart/30000

echo Avvio del servizio...
sc.exe start %SERVICE_NAME%

echo.
echo [OK] Servizio installato e avviato con successo!
echo Il servizio si avviera' automaticamente ad ogni accensione del PC.
echo.
pause
