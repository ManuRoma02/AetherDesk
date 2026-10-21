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

echo Arresto e rimozione servizio / attivita' pianificata...
schtasks /end /tn "%SERVICE_NAME%" >nul 2>&1
schtasks /delete /tn "%SERVICE_NAME%" /f >nul 2>&1
sc.exe stop %SERVICE_NAME% >nul 2>&1
sc.exe delete %SERVICE_NAME% >nul 2>&1

echo.
echo [OK] Servizio e avvio automatico rimossi con successo.
echo.
pause
