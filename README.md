# Remote Desktop Ultra-Light (TeamViewer Alternative)

Soluzione di controllo remoto leggera, ultra-veloce e 100% gratuita per Windows. Progettata per permetterti di controllare a distanza il tuo **PC Fisso** dal tuo **Portatile** da qualsiasi luogo (scuola, università, biblioteca, hotspot 4G/5G o rete Wi-Fi esterna).

---

## Caratteristiche Principali

- **Accesso Non Presidiato (Unattended)**: Si avvia automaticamente all'accensione del PC fisso prima ancora del login (`StartupType = Automatic`). Puoi accendere il PC ed effettuare il login Windows direttamente da remoto.
- **Tutti i Permessi Sempre (UAC & Winlogon)**: Non si blocca mai sui prompt di amministratore (UAC) o schermate di sistema protette grazie all'aggancio dinamico a `OpenInputDesktop` e all'iniezione con `SendInput`.
- **Prevenzione Standby Automatica**: Il computer fisso non va in sospensione né spegne la sessione interattiva mentre sei fuori casa (`SetThreadExecutionState`).
- **Sincronizzazione Appunti Bidirezionale**: Fai `Ctrl+C` sul portatile e incolli con `Ctrl+V` sul fisso (e viceversa). Ideale per codice, comandi e testi di studio.
- **Supporto Multi-Monitor**: Se sul fisso hai più schermi, puoi passare al volo da un monitor all'altro (Monitor 1, Monitor 2) oppure visualizzare tutti i monitor contemporaneamente (*Desktop Virtuale*).
- **Trasferimento File Integrato**: Invia documenti, compiti, PDF o file `.zip` dal portatile al fisso con un clic tramite il pulsante *"Invia File"* (i file vengono salvati automaticamente in `Download\RemoteDesktop_Transfers`).
- **Eseguibile Autonomo Standalone (.EXE)**: Include `dist\RemoteClient.exe`, un unico file eseguibile senza dipendenze né bisogno di installare Python, pronto per essere portato su chiavetta USB e usato su qualsiasi computer.
- **Autenticazione Semplice & Sicura**: Connessione tramite **ID univoco a 9 cifre** (es. `482 910 234`) e password personale.
- **Crittografia End-to-End (Zero-Knowledge E2EE)**: Tutti i frame video, gli input, gli appunti e i file sono protetti con cifratura militare **AES-256-GCM** e chiavi derivate tramite **PBKDF2-SHA256**.
- **100% Gratuito & Illimitato**: Nessun abbonamento, nessun server VPS a pagamento, nessun limite di tempo (mai più disconnessioni dopo 5 minuti).

---

## Guida all'Uso

### 1. Sul PC Fisso (Host da controllare)

1. Fai doppio clic su `start_host.bat` per aprire il **Pannello di Controllo Host**.
2. Troverai il tuo **ID Dispositivo a 9 cifre** (es. `482 910 234`).
3. Inserisci la tua password personale nel campo *Password di Accesso Remoto* e premi **Salva Password**.
4. **Per abilitare l'avvio automatico all'accensione del PC**:
   - Fai clic destro su `scripts\install_service.bat` e seleziona **Esegui come amministratore** (oppure clicca sul pulsante *"Installa come Servizio Windows"* nel pannello).
   - *Fatto!* Il servizio rimarrà sempre attivo ad ogni riavvio o accensione del computer fisso.

---

### 2. Sul Portatile (Client da cui controlli)

Puoi avviare il client in due modalità:
- **Metodo 1 (Super portatile)**: Fai doppio clic su `dist\RemoteClient.exe` (non richiede Python, funziona ovunque da chiavetta USB).
- **Metodo 2 (Script rapido)**: Fai doppio clic su `start_client.bat`.

Una volta aperta l'app:
1. Inserisci l'**ID del Computer Fisso** e la **Password**.
2. Clicca su **Connetti al Computer**.
3. Dalla barra superiore puoi:
   - **Inviare Ctrl+Alt+Del** o mostrare il desktop (**Win+D**).
   - **Inviare File**: Clicca su *"Invia File"* per inviare qualsiasi documento o archivio al PC fisso.
   - **Cambiare Monitor**: Clicca su *1*, *2* o *Tutti* per cambiare al volo la visualizzazione dello schermo.
   - **Copia/Incolla**: Funziona automaticamente in tempo reale in background.

---

## Compilare l'Eseguibile .EXE

Se modifichi il codice e vuoi rigenerare l'eseguibile autonomo `dist\RemoteClient.exe`:
```cmd
scripts\build_exe.bat
```

---

## Test e Verifica

Per verificare il funzionamento di tutti i moduli:
```bash
python -m pytest tests/ -v
```
Tutti i **25 test unitari ed End-to-End** vengono eseguiti con esito 100% positivo.
