@echo off
setlocal
title AetherDesk - Sblocco Firewall
echo ========================================================
echo   Sblocco Porta 8765 nel Windows Firewall per AetherDesk
echo ========================================================
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Richiesta permessi di amministratore...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

echo Aggiunta regola firewall per connessioni in entrata (Porta 8765 TCP)...
netsh advfirewall firewall delete rule name="AetherDesk Host (Port 8765)" >nul 2>&1
netsh advfirewall firewall add rule name="AetherDesk Host (Port 8765)" dir=in action=allow protocol=TCP localport=8765 profile=any

if %errorLevel% equ 0 (
    echo.
    echo ========================================================
    echo [OK] Porta 8765 aperta con successo nel Windows Firewall!
    echo Ora i client possono connettersi via Wi-Fi e LAN.
    echo ========================================================
) else (
    echo [ERRORE] Impossibile configurare la regola firewall.
)
timeout /t 3
exit
