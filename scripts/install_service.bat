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

set "SERVICE_NAME=RemoteDesktopHost"
set "DISPLAY_NAME=Remote Desktop Unattended Host Service"
set "BIN_PATH=%~dp0..\venv\Scripts\python.exe"

if not exist "%BIN_PATH%" (
    for /f "tokens=*" %%i in ('where python') do set "BIN_PATH=%%i" & goto :found_python
)
:found_python

set "SCRIPT_PATH=%~dp0..\src\host_service.py"

echo Rimozione eventuale istanza precedente...
schtasks /delete /tn "%SERVICE_NAME%" /f >nul 2>&1
sc.exe stop %SERVICE_NAME% >nul 2>&1
sc.exe delete %SERVICE_NAME% >nul 2>&1
timeout /t 1 >nul

echo Creazione attivita' di sistema ad avvio automatico all'accensione (SYSTEM)...
schtasks /create /tn "%SERVICE_NAME%" /tr "\"%BIN_PATH%\" \"%SCRIPT_PATH%\"" /sc onstart /ru SYSTEM /rl highest /f >nul 2>&1

if %errorLevel% equ 0 (
    echo [OK] Attivita' pianificata creata con successo (Accesso non presidiato attivo prima del login).
    echo Avvio immediato...
    schtasks /run /tn "%SERVICE_NAME%" >nul 2>&1
) else (
    echo Configurazione fallback tramite Windows Service Manager (SCM)...
    sc.exe create %SERVICE_NAME% binPath= "\"%BIN_PATH%\" \"%SCRIPT_PATH%\"" start= auto DisplayName= "%DISPLAY_NAME%" >nul 2>&1
    sc.exe start %SERVICE_NAME% >nul 2>&1
)

echo.
echo [OK] Servizio non presidiato installato e avviato!
echo Il servizio rimarra' attivo ad ogni avvio o accensione del computer.
echo.
pause
