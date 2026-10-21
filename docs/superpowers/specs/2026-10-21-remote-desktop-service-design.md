# Design: Servizio di Controllo Remoto Ultra-Leggero (TeamViewer Alternative)

## 1. Intent & Why
L'utente desidera controllare a distanza il proprio computer fisso (Windows) dal proprio computer portatile (anch'esso Windows) quando è fuori casa (a scuola, in biblioteca, in viaggio) per portare avanti i propri progetti di studio e sviluppo software.
Gli strumenti commerciali (TeamViewer, AnyDesk) impongono limitazioni invasive (timeout per presunto uso commerciale, finestre di avviso, installazioni pesanti). L'utente richiede una soluzione **simile a TeamViewer** ma **semplice, veloce, leggera e totalmente gratuita** (zero costi, nessun server da noleggiare), che mantenga **sempre e comunque tutti i permessi** (UAC, finestre amministrative, schermate di blocco, riavvii) e consenta la connessione tramite **ID e Password** da qualsiasi PC Windows.

---

## 2. Goals, Non-Goals & Anti-Goals

### Goals
- **Avvio Automatico all'Accensione (Pre-Login)**: Il servizio sul PC fisso si avvia automaticamente all'avvio del computer prima ancora che venga effettuato il login (`StartupType = Automatic`). L'utente può connettersi anche dalla schermata di login di Windows, digitare la password di Windows ed effettuare il login da remoto.
- **Controllo remoto completo ed elevato**: Accesso senza presidio (*unattended access*). Piena interattività su prompt UAC (*User Account Control*), finestre con privilegi di amministratore e schermata di blocco di Windows (`Winlogon`).
- **Autenticazione ID & Password**: Il PC fisso (host) possiede un ID a 9 cifre persistente e una password sicura impostata dall'utente. Il client si connette digitando unicamente questi due parametri.
- **Infrastruttura 100% Gratuita e Zero-Config**: Connessione da qualsiasi rete (dietro NAT/firewall scolastici o hotspot mobili 4G/5G) senza richiedere l'apertura di porte sul router, IP statici o abbonamenti a server VPS.
- **Crittografia End-to-End a Conoscenza Zero (Zero-Knowledge E2EE)**: La segnalazione è cieca (non conosce la password né vede il video); l'autenticazione e la crittografia sono negoziate direttamente tra Client e Host.
- **Alte prestazioni & Bassa latenza**: Cattura dello schermo accelerata via hardware (DXGI Desktop Duplication API Direct3D 11) a 30-60 FPS con bassissimo utilizzo di CPU e RAM.
- **Client portatile e autonomo**: Singolo file eseguibile `.exe` stand-alone per Windows, senza necessità di installazione, avviabile direttamente o da chiavetta USB.
- **Input & Clipboard completi**: Inoltro fedele di mouse (click, trascinamento, rotella), tastiera (tasti speciali, tasto Windows, Alt+Tab, scorciatoie di sistema) e sincronizzazione della clipboard (testo).

### Non-Goals
- Supporto a sistemi operativi diversi da Windows per questa release iniziale (focus su Windows 10/11 x64).
- Chat vocale o videochiamata integrata.
- Sistema di licenze commerciali, telemetria o pubblicità.

### Anti-Goals
- **Nessun blocco su UAC**: Il software non deve mai perdere il controllo del puntatore o mostrare schermo nero quando compare una finestra con privilegi di amministratore.
- **Nessun limite di sessione**: Nessun distacco automatico dopo 5 minuti o messaggi commerciali.
- **Nessuna dipendenza a pagamento**: Nessun costo di hosting o server per l'utente.

---

## 3. Reverse Engineering & Architecture Analysis (Metodologia REA)

Decomponendo il funzionamento architetturale dei software di controllo remoto (TeamViewer, AnyDesk, RustDesk):

1. **Gestione Sessioni Windows (Session 0 vs Session 1+)**:
   - I servizi Windows girano in Session 0 (isolata, senza interfaccia grafica da Windows Vista in poi).
   - Lo schermo dell'utente e le finestre interattive girano in Session 1 (o sessione attiva RDP/Console).
   - *Soluzione*: Architettura a doppio processo:
     - **Service (Host Daemon)**: Gira come Servizio Windows con privilegi `NT AUTHORITY\SYSTEM`. Si occupa del lifecycle, del riavvio automatico e del monitoraggio.
     - **Session Agent (Worker)**: Viene lanciato dal servizio all'interno della sessione utente attiva (o della sessione `Winlogon` / Secure Desktop) duplicando il token della sessione attiva con privilegi di sistema o flag `uiAccess`.
     - In questo modo, il Worker ha accesso sia al desktop grafico (DXGI) sia al desktop protetto (UAC).
2. **Screen Capture & Stream**:
   - Utilizzo della **DXGI Desktop Duplication API** (Direct3D 11): estrae i frame direttamente dalla VRAM della scheda video in pochi millisecondi, con rilevamento dei rettangoli sporchi (*dirty rects*) per trasmettere solo le variazioni.
   - Compressione frame e streaming WebRTC / WebSocket ad alta efficienza.
3. **Elevated Input Injection**:
   - Win32 API `SendInput` eseguita da un processo con manifest `uiAccess="true"` o con token di sistema, permettendo l'interazione senza ostacoli con qualsiasi finestra con privilegi elevati (Task Manager, Registry Editor, installatori UAC).
4. **Signaling & NAT Traversal Gratuito**:
   - WebRTC P2P con server STUN pubblici gratuiti (es. Google STUN: `stun.l.google.com:19302`) combinato con un broker di segnalazione leggero e gratuito.
   - La connessione dati video/input viaggia direttamente punto-a-punto a piena velocità tra portatile e fisso, azzerando i costi di banda su server esterni.

---

## 4. User Decisions & Builder Decisions

### Scelte dell'Utente
- **Piattaforme**: Host e Client sono entrambi sistemi **Windows** (Windows 10/11).
- **Accesso**: Connessione da remoto tramite **ID a 9 cifre e Password**, per potersi connettere anche da altri computer.
- **Budget & Hosting**: **Totalmente gratuita**, senza dover noleggiare VPS o pagare servizi cloud.
- **Permessi**: Accesso non presidiato (*unattended*), attivo all'avvio, con tutti i permessi amministrativi e persistenza ai riavvii.

### Scelte del Builder (Decisioni Tecniche Sottostanti)
- **Modello di Rete**: Architettura WebRTC Data/Video Channels con STUN gratuito di Google per il passaggio P2P attraverso i NAT. Segnalazione stateless a scambio rapido di token (Zero-Knowledge: la segnalazione inoltra solo SDP e candidati ICE; non conosce la password dell'host).
- **Crittografia & Handshake**: Negoziazione con chiave derivata da PBKDF2/SHA-256 della password host ed encryption DTLS-SRTP / AES-GCM 256-bit tra i due dispositivi.
- **Stato Schermo & Risparmio Energetico**: Il servizio Host invoca `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)` per evitare che il computer fisso vada in standby durante l'assenza dell'utente.
- **Interfaccia Host**: Creazione di una comoda GUI di controllo/Tray icon sul PC fisso (in Sessione Utente) che comunica via Named Pipe locale con il Servizio di sistema in Sessione 0 per mostrare chiaramente l'ID del dispositivo, permettere di cambiare password e verificare lo stato del servizio.

---

## 5. Components Architecture

```
+-------------------------------------------------------------+
|                      PC FISSO (HOST)                        |
|                                                             |
|  +-------------------------------------------------------+  |
|  | Windows Service (LocalSystem - Session 0)             |  |
|  |  - Auto-start all'avvio del PC                        |  |
|  |  - Mantiene attivo lo stato di veglia (no sleep)      |  |
|  |  - Spawna e supervisiona il Session Worker            |  |
|  |  - Gestisce configurazione locale sicura (ID, Hash)   |  |
|  +---------------------------+---------------------------+  |
|                              | Named Pipe / Token Dup       |
|                              v                              |
|  +-------------------------------------------------------+  |
|  | Session Worker (Sessione Utente / Desktop Protetto)   |  |
|  |  - Cattura schermo GPU DXGI (Direct3D 11)             |  |
|  |  - Input injection elevato (SendInput con uiAccess)   |  |
|  |  - Sincronizzazione Clipboard testo                   |  |
|  +---------------------------+---------------------------+  |
|                              ^                              |
|                              | Named Pipe                   |
|  +---------------------------+---------------------------+  |
|  | Host Tray Control Panel (Interfaccia Utente Locale)   |  |
|  |  - Mostra il tuo ID (es. 482 910 234)                 |  |
|  |  - Permette di impostare la password                  |  |
|  |  - Mostra stato di connessione                        |  |
|  +-------------------------------------------------------+  |
+------------------------------|------------------------------+
                               | WebRTC P2P (Google STUN)
                               | [Cifratura DTLS/AES-GCM]
                               v
+-------------------------------------------------------------+
|                    PORTATILE (CLIENT)                       |
|                                                             |
|  +-------------------------------------------------------+  |
|  | Windows Client (.exe autonomo)                        |  |
|  |  - Schermata di login: Inserimento ID & Password      |  |
|  |  - Visualizzatore Desktop fluido a schermo intero/rid |  |
|  |  - Inoltro totale tastiera (scorciatoie Windows)      |  |
|  |  - Inoltro mouse (click, doppio click, drag, scroll)   |  |
|  |  - Barra rapida: "Invia Ctrl+Alt+Del", "Disconnetti"  |  |
|  +-------------------------------------------------------+  |
+-------------------------------------------------------------+
```

---

## 6. What's Left to the Builder (Dettagli Implementativi)

- Implementazione pulita in Python/C++ compilato (o C# .NET AOT) per massime prestazioni senza dipendenze esterne ingombranti.
- Script di installazione automatica a un clic (`install.bat` / `install.ps1`) con richiesta automatica di privilegi di amministratore.
- Script di disinstallazione pulita (`uninstall.bat`).
- Gestione automatica del ridimensionamento dinamico del flusso video in base alle dimensioni della finestra del client.
