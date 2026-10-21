@echo off
setlocal
echo ========================================================
echo   Disinstallazione Servizio RemoteDesktop
echo ========================================================

:: Verifica privilegi di amministratore
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Richiesta permessi di amministratore...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

set "SERVICE_NAME=RemoteDesktopHost"

echo Arresto del servizio...
sc.exe stop %SERVICE_NAME%

echo Rimozione del servizio...
sc.exe delete %SERVICE_NAME%

echo.
echo [OK] Servizio rimosso con successo.
echo.
pause
