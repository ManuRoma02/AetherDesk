@echo off
setlocal
echo ==============================================================================
echo [ERRORE BLOCCANTE: INSTALLAZIONE SERVIZIO NON PRESIDIATO SOSPESA]
echo ==============================================================================
echo L'installazione di AetherDesk come Servizio di Sistema (SYSTEM in Session 0)
echo e' bloccata per vincoli architetturali del sistema operativo Windows:
echo.
echo 1. Isolamento Session 0: Windows esegue i servizi di sistema in Session 0,
echo    un ambiente privo di desktop grafico utente (Session 1+) e privo di
echo    instradamento per gli input generati tramite SendInput verso le app utente.
echo.
echo 2. Assenza del Session Agent: La cattura DXGI Desktop Duplication e il controllo
echo    remoto del desktop richiedono un processo interattivo "Session Agent" avviato
echo    all'interno della sessione utente attiva tramite WTSQueryUserToken e
echo    CreateProcessAsUserW. Tale architettura non e' implementata nel repository.
echo.
echo 3. L'avvio in modalita' headless non risolve l'accesso desktop, ma evita solo
echo    il caricamento della GUI Tkinter nel servizio.
echo.
echo 4. Nessuna modifica al sistema e' stata effettuata (nessun servizio rimosso
echo    o alterato).
echo.
echo PERCORSO SUPPORTATO:
echo L'accesso e controllo remoto host e' supportato avviando AetherDesk
echo direttamente all'interno della sessione grafica dell'utente loggato:
echo.
echo    start_host.bat     oppure    dist\AetherDesk.exe --host
echo ==============================================================================
echo.
exit /b 1
