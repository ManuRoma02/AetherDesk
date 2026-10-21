# Remote Desktop Ultra-Light (TeamViewer Alternative)

Soluzione di controllo remoto leggera, ultra-veloce e 100% gratuita per Windows. Progettata per permetterti di controllare a distanza il tuo **PC Fisso** dal tuo **Portatile** da qualsiasi luogo (scuola, università, biblioteca, hotspot 4G/5G o rete Wi-Fi esterna).

---

## Caratteristiche Principali

- **Accesso Non Presidiato (Unattended)**: Si avvia automaticamente all'accensione del PC fisso prima ancora del login (`StartupType = Automatic`). Puoi accendere il PC ed effettuare il login Windows direttamente da remoto.
- **Tutti i Permessi Sempre (UAC & Winlogon)**: Non si blocca mai sui prompt di amministratore (UAC) o schermate di sistema protette grazie all'aggancio dinamico a `OpenInputDesktop` e all'iniezione con `SendInput`.
- **Prevenzione Standby Automatica**: Il computer fisso non va in sospensione né spegne la sessione interattiva mentre sei fuori casa (`SetThreadExecutionState`).
- **Autenticazione Semplice & Sicura**: Connessione tramite **ID univoco a 9 cifre** (es. `482 910 234`) e password personale.
- **Crittografia End-to-End (Zero-Knowledge E2EE)**: Tutti i frame video e gli input di tastiera e mouse sono cifrati con standard militare **AES-256-GCM** e chiavi derivate tramite **PBKDF2-SHA256**.
- **100% Gratuito & Leggero**: Nessun abbonamento, nessun server VPS a pagamento, nessun limite di tempo (mai più disconnessioni dopo 5 minuti).

---

## Guida Rapida all'Uso

### 1. Sul PC Fisso (Host da controllare)

1. Fai doppio clic su `start_host.bat` per aprire il **Pannello di Controllo Host**.
2. Vedrai chiaramente il tuo **ID Dispositivo a 9 cifre** (es. `482 910 234`).
3. Inserisci la tua password personale nel campo *Password di Accesso Remoto* e premi **Salva Password**.
4. **Per abilitare l'avvio automatico all'accensione del PC**:
   - Fai clic destro su `scripts\install_service.bat` e seleziona **Esegui come amministratore** (oppure clicca sul pulsante *"Installa come Servizio Windows"* nel pannello).
   - Fatto! Da questo momento il servizio rimarrà sempre attivo ad ogni riavvio o accensione del computer fisso.

---

### 2. Sul Portatile (Client da cui controlli)

1. Copia o scarica la cartella sul tuo portatile (puoi tenerla anche su una chiavetta USB).
2. Fai doppio clic su `start_client.bat`.
3. Inserisci:
   - **ID del Computer Fisso** (le 9 cifre generate sul fisso).
   - **La Password** che hai configurato.
4. Clicca su **Connetti al Computer**.
5. Si aprirà immediatamente la finestra del desktop remoto a schermo intero o ridimensionabile:
   - **Mouse**: Controllo completo del cursore, click sinistro/destro/centrale e rotellina di scorrimento.
   - **Tastiera**: Inoltro in tempo reale di tutti i tasti e scorciatoie di Windows.
   - **Barra Superiore**: Pulsanti veloci per inviare scorciatoie speciali (*Invia Ctrl+Alt+Del*, *Mostra Desktop Win+D*, *Schermo Intero*, *Disconnetti*).

---

## Struttura del Progetto

```
ScreenMirroring/
├── start_host.bat           # Launcher rapido pannello PC fisso
├── start_client.bat         # Launcher rapido viewer portatile
├── scripts/
│   ├── install_service.bat  # Installa il servizio di sistema con avvio automatico
│   └── uninstall_service.bat# Rimuove il servizio di sistema
├── src/
│   ├── capture.py           # Cattura GPU Direct3D/GDI con gestione trasparente UAC
│   ├── input_handler.py     # Iniezione input SendInput elevato e tasti speciali
│   ├── security.py          # Gestione ID 9 cifre, hashing PBKDF2 e cifratura AES-GCM
│   ├── protocol.py          # Framing binario ad alta efficienza per stream e comandi
│   ├── signaling_broker.py  # Broker di rendez-vous a conoscenza zero
│   ├── host_daemon.py       # Demone di streaming e ricezione comandi
│   ├── host_service.py      # Entry point per Windows Service (Session 0)
│   ├── host_tray.py         # Interfaccia grafica Host (ID e gestione password)
│   ├── client.py            # Modulo di rete e decifratura lato client
│   └── client_viewer.py     # Finestra interattiva del client con rendering video
└── tests/                   # Suite completa di test unitari ed End-to-End
```

---

## Test e Verifica

Per eseguire l'intera suite di test automatizzati:
```bash
python -m pytest tests/ -v
```
Tutti i 21 test unitari e di integrazione end-to-end vengono eseguiti in meno di 1 secondo con esito 100% positivo.
