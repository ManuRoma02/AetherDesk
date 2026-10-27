# Architettura AetherDesk Realtime & Protocollo di Sicurezza

Questo documento specifica l'architettura tecnica ad alte prestazioni e il protocollo di sicurezza implementati nel repository.

---

## 1. Percorso Reale dei Dati (Dalla Cattura al Rendering)

```
[Host Desktop]
      │ (DXGI Desktop Duplication: IDXGIOutput1::DuplicateOutput)
      ▼
[GPU Desktop Texture] ──(context.CopyResource)──► [Retained Working Texture]
      │                                                     │
(dup.ReleaseFrame: sblocco immediato compositore)           ▼
                                                  [Media Foundation H.264 MFT]
                                                  - Probing hardware (NVENC/QSV/AMF)
                                                  - Fallback trasparente software MFT
                                                  - Zero B-frames, Low-Latency Mode
                                                            │
                                                            ▼ (H.264 Annex B / NALU)
                                                  [WebRTC Video Track]
                                                  - P2P ICE (STUN / TURN autenticato)
                                                  - Segnalazione rendezvous via Broker
                                                            │
                                                   (Rete UDP / SRTP)
                                                            │
                                                            ▼
                                                  [WebRTC Receiver (Client)]
                                                            │
                                                            ▼
                                                  [H.264 Decoder & Rendering]
```

### Dettaglio del Ciclo di Cattura ed Encoding:
1. **DXGI Desktop Duplication (`dxgi_capture.rs`)**:
   - `IDXGIOutput1::DuplicateOutput` acquisisce il frame desktop direttamente su GPU (`ID3D11Texture2D`, formato `B8G8R8A8_UNORM`).
   - Per impedire deadlock o corruzione del frame, il texture viene copiato tramite `context.CopyResource` su una texture interna trattenuta (`working_texture`).
   - `duplication.ReleaseFrame()` viene invocato immediatamente dopo la copia, restituendo il frame al compositore DWM di Windows senza ritardi.
   - Viene mantenuta una `staging_texture` CPU-readable per il fallback di readback memoria solo in caso di encoder software.
   - Gestione completa di `DXGI_ERROR_ACCESS_LOST` (switch a secure desktop/UAC, cambio risoluzione o monitor topology) con reinizializzazione automatica.
2. **Media Foundation H.264 MFT Encoder (`mf_encoder.rs`)**:
   - Enumerazione tramite `MFTEnumEx` con categoria `MFT_CATEGORY_VIDEO_ENCODER`.
   - Cerca prima un encoder hardware (`MFT_ENUM_FLAG_HARDWARE | MFT_ENUM_FLAG_SORTANDFILTER`). Se presente, lo attiva (`is_hardware = true`).
   - Quando l'encoder hardware è disponibile, l'input viene associato direttamente dalla GPU D3D11 texture (`working_texture`) al sample Media Foundation tramite `MFCreateDXGISurfaceBuffer`, realizzando un percorso di codifica zero-copy GPU senza CPU readback.
   - Se assente o l'attivazione fallisce, passa a encoder sincroni software (`MFT_ENUM_FLAG_SYNCMFT`, es. Microsoft H.264 Soft MFT) (`is_hardware = false`) utilizzando memoria buffer con staging texture readback.
   - Configurazione rigida a bassa latenza tramite `ICodecAPI`:
     - `CODECAPI_AVLowLatencyMode = VARIANT_TRUE` (disabilita il buffering interno).
     - `CODECAPI_AVEncMPVDefaultBPictureCount = 0` (zero B-frames, pipeline a 1-frame).
     - `CODECAPI_AVEncCommonRateControlMode = CBR / LowDelay`.
     - `CODECAPI_AVEncCommonMeanBitRate = target_bitrate`.
   - Esecuzione tramite `ProcessInput` e `ProcessOutput`, con misurazione monotona reale del tempo di codifica in nanosecondi via `std::time::Instant`.
3. **WebRTC Transport & Depacketizzazione RFC 6184 (`webrtc_transport.rs`)**:
   - WebRTC Peer Connection diretta P2P sia su Host (`WebRtcHostSession`) sia su Client (`WebRtcClientSession`), con supporto TURN relay se necessario.
   - Traccia video dedicata `video-desktop` per H.264 con negoziazione SDP reale e scambio candidati ICE affidabile (coda bounded a 64 con attesa asincrona per evitare perdite silenziose).
   - Ricezione RTP conforme a **RFC 6184**:
     - Supporto per Single NAL Units (1..=23), aggregazione STAP-A (24) e frammentazione FU-A (28).
     - Rilevamento perdite di pacchetto e fuori ordine tramite verifica di continuità dei numeri di sequenza a 16 bit (`wrapping_add(1)`).
     - Rilevamento discontinuità temporale (timestamp RTP variato senza marker bit sull'Access Unit precedente).
     - **Regola rigorosa di recupero IDR**: né i set di parametri SPS (7) / PPS (8) da soli, né l'inizio di una frammentazione FU-A di tipo 5 azzerano lo stato di attesa keyframe. Tale stato viene azzerato **esclusivamente** quando l'intera Access Unit contenente la slice IDR è stata completamente e validamente riassemblata con marker bit. In caso di corruzione o perdita precedente, i frame dipendenti (P-frames) vengono scartati alla radice per non corrompere la catena di riferimento del decoder.
     - **Coda Video Rigorosamente Bounded Latest-Wins (`LatestWinsFrameQueue`) con Gestione Perdita Frame di Riferimento**:
       - Capacità di 2 frame e limite di memoria di 32 MB.
       - Quando la coda è satura, l'evizione di un frame di riferimento non ancora decodificato spezza la catena di predizione temporale dei successivi P-frame.
       - A differenza di un'evizione cieca, la coda rileva la rottura di dipendenza: svuota immediatamente tutti i frame dipendenti accumulati, attiva lo stato `reference_loss_active`, forza il depacketizer in `force_waiting_for_idr()` e solleva `keyframe_req`.
       - All'arrivo della richiesta di keyframe, il decoder client esegue un `flush()` per resettare lo stato di riferimento.
       - Nuovi P-frame in arrivo vengono scartati fino alla ricezione di una nuova Access Unit IDR completa e valida, prevenendo qualsiasi artefatto visivo o corruzione dello stream.
   - La segnalazione (SDP Offer/Answer e candidati ICE) transita dal broker solo durante l'handshake. Una volta stabilita la sessione WebRTC (stato ICE `Connected` o `Completed`), il flusso video scorre direttamente su UDP/SRTP e non transita più dal WebSocket del broker!
4. **Media Foundation H.264 MFT Decoder (`mf_decoder.rs`)**:
   - Sul client, riceve le Access Unit H.264 Annex B dalla coda video latest-wins.
   - Inizializza il decoder Media Foundation standard `CLSID_CMSH264DecoderMFT` con negoziazione dinamica della risoluzione (gestione `MF_E_TRANSFORM_STREAM_CHANGE`, ricalcolo output buffer e copia esatta dei byte).
   - Converte i frame decodificati direttamente in formato Little-Endian BGRA (Windows RGB32) e mappati tramite Pillow `Image.frombytes("RGBA", dims, raw, "raw", "BGRA")`, eliminando le inversioni tra canali rosso e blu, e misurando con precisione il tempo effettivo di decodifica in nanosecondi.

---

## 2. Percorso dell'Input di Ritorno e Segregazione dei Canali

```
[Client UI / Viewer]
   │
   ├── Movimenti Mouse (125 Hz) ──────► Canale "cursor" (Unreliable, Unordered, max_retransmits=0)
   │                                           │ (Protocollo: [seq, x, y, timestamp])
   │                                           ▼
   │                                  Host: InputWorker Queue (Latest-Wins Coalescing)
   │                                  InputWorker -> InputHandler.move_mouse(x, y)
   │
   └── Click, Tasti, Scorciatoie ─────► Canale "control" (Reliable, Ordered)
                                               │
                                               ▼
                                      Host: InputWorker Queue (Ordinata, Lock-Safe)
                                      Single-Call SendInput / Scorciatoie OS
```

1. **Canale Mouse `cursor` (Bassa Latenza, Latest-Wins, Lock-Safe)**:
   - Configurato con `ordered: false` e `max_retransmits: 0`.
   - Invia la posizione del puntatore con timestamp e numero di sequenza incrementale.
   - L'host confronta la sequenza a livello nativo (scarto immediato se sequenza $\le$ ultima elaborata).
   - Instradato direttamente attraverso `InputWorker` (non bypassa il gestore dell'input):
     - `CoalescingQueue` applica la politica latest-wins sovrascrivendo le coordinate in-place, senza accumulare movimenti obsoleti.
     - Viene garantita l'assenza totale di chiamate concorrenti tra movimenti del mouse e click/tasti verso l'iniettore OS (`SendInput` atomico sotto `_exec_lock`).
     - Preservato il coalescing a 125 Hz (`< 0.008s`) nel viewer client e la singola chiamata a `SendInput`.
2. **Canale Discreto `control` (Affidabile, Ordinato) & Gestione Backpressure Senza Race**:
   - Configurato con `ordered: true` e consegna garantita (SCTP affidabile).
   - Gestisce `key_down`, `key_up`, `mouse_down`, `mouse_up`, rotella, clipboard e scorciatoie di sistema (`win_d`, `alt_tab`, ecc.).
   - Inserito in `InputWorker` dell'host, che garantisce l'ordine serializzato ed evita rilasci disordinati o tasti bloccati. Preservato il percorso a singola chiamata atomica `SendInput`.
   - **Backpressure Asincrona e Prevenzione Race Condition**:
     - I messaggi discreti non vengono mai scartati silenciosamente in caso di congestione.
     - Il canale applica backpressure asincrona al layer SCTP (fino a 3000 ms). Se la capacità del canale rimane saturata oltre la soglia temporale, la sessione fallisce in modo esplicito (fail-closed, `is_control_saturated = true`) chiudendo la connessione.
     - **Controllo Prioritario nel Lifecycle**: lo stato di saturazione viene controllato prima di qualsiasi interrogazione dello stato ICE e prima di qualsiasi ramo di fallback. In questo modo, la chiusura della PeerConnection e il passaggio dello stato ICE a `Closed` non saltano la procedura di emergenza: `_safe_release_inputs(timeout=2.0)` viene sempre invocato.
     - Se `release_all` va in timeout a causa di un comando in esecuzione, il rilascio viene schedulato come `_release_pending` ed eseguito non appena il lock è libero dal thread del worker, senza mai effettuare chiamate concorrenti né bypassare il lock.
     - Nel loop di streaming host, i messaggi di controllo vengono svuotati in modalità burst (fino a 64 per tick) per azzerare la latenza anche durante digitazioni rapide o sequenze di click.
     - La sessione terminata per saturazione svuota lo stato (`session_state.clear()`), impedendo che una sessione chiusa venga etichettata come attiva o che vengano accettati ulteriori comandi.
     - **Latch di Saturazione con Ambito di Sessione (`_control_saturated`)**:
       - Rimane attivo per l'intera durata della sessione saturata; tutti i comandi successivi del client vengono rifiutati con errore `CONTROL_CHANNEL_SATURATED`.
       - Può essere azzerato esclusivamente da un nuovo evento `CLIENT_ARRIVED` autenticato dal broker (verifica del `broker_token`).
       - L'azzeramento (`reset_session_saturation()`) avviene in modo atomico solo dopo la distruzione della sessione nativa precedente e la verifica che tutti gli input siano stati rilasciati con successo e che nessun rilascio sia ancora pendente (`release_ok and not input_worker._release_pending`).
       - Se il recupero non è completo o fallisce, il sistema permane in stato fail-closed (`_control_saturated = True`), rifiuta la nuova connessione con errore esplicito `HOST_RECOVERY_INCOMPLETE` e impedisce l'iniezione di input.
     - **Instradamento Cursore di Produzione (`_poll_and_enqueue_webrtc_cursor`)**:
       - L'helper unificato di produzione esegue il polling del cursore WebRTC nativo, normalizza le coordinate rispetto alle dimensioni del display e le accoda direttamente in `InputWorker`.
       - La `CoalescingQueue` garantisce la politica latest-wins mantenendo la dimensione della coda a 1 per movimenti consecutivi del puntatore, senza accumulare latenza e garantendo l'assenza di concorrenza con click o tasti grazie al lock atomico di esecuzione.

---

## 3. Protocollo di Sicurezza End-to-End e Modello di Fiducia

### Protezione contro Man-in-the-Middle e Broker Malevolo:
1. **Identità Persistente Host (Ed25519)**:
   - Ogni host possiede una coppia di chiavi d'identità Ed25519 gestita da `SecurityManager` e protetta tramite DPAPI su disco.
2. **Prova di Possesso al Broker (Proof of Possession)**:
   - Il broker richiede una prova crittografica sia alla prima registrazione che alle registrazioni successive.
   - Il broker genera un nonce casuale a 32 byte (`HOST_CHALLENGE`). L'host firma il nonce con la propria chiave privata Ed25519 (`HOST_CHALLENGE_RESPONSE`).
   - Il broker verifica la firma con la chiave pubblica dell'host. Solo a verifica avvenuta la chiave viene pinnata e salvata su file persistente (`broker_pinned_hosts.json`).
   - Qualsiasi tentativo di impersonare l'ID con una chiave diversa viene respinto con `HOST_IMPERSONATION_REJECTED`.
3. **Verifica Client-Side e Rifiuto del TOFU Cieco (Known Hosts + Explicit Fingerprint Verification)**:
   - Il semplice TOFU (Trust-On-First-Use) automatico o non supervisionato non costituisce una protezione sufficiente contro un broker malevolo o compromesso, poiché un intermediario ostile potrebbe sostituire la chiave pubblica già al primo contatto.
   - All'arrivo di `HOST_FOUND`, il client verifica la chiave pubblica dell'host contro il proprio archivio locale persistente `known_hosts` (`SecurityManager.check_host_pin_status`).
   - Se la chiave è già nota e identica: `PINNED_OK` (autenticazione immediata).
   - Se è il primo contatto (`UNPINNED`): il client **rifiuta l'auto-approvazione** (fail-closed se manca il gestore di verifica o se la finestra UI è assente) e richiede obbligatoriamente la conferma esplicita dell'utente tramite finestra modale che mostra l'impronta calcolata in SHA-256 completa a 32 byte / 64 caratteri esadecimali (`on_verify_fingerprint_callback`). Solo previa conferma esplicita dell'utente la chiave viene registrata in modo persistente in `known_hosts`. Se l'utente rifiuta, annulla, chiude la modale, va in timeout (30s) o non è presente una UI attiva, la connessione si arresta immediatamente senza alcun salvataggio.
   - Se la chiave è diversa da quella precedentemente memorizzata per quell'ID: la connessione viene **immediatamente interrotta** con allarme critico di sicurezza (`KEY_CHANGED`), senza alcun fallback o downgrade, bloccando qualsiasi tentativo del broker o di un intermediario di sostituire la chiave dell'host!
4. **Handshake Forward-Secure (X25519 ECDH + Transcript HMAC)**:
   - Scambio di chiavi effimere X25519 firmate con la chiave d'identità Ed25519 e autenticate tramite HMAC legato al transcript di handshake. Anche in caso di compromissione futura, le sessioni passate non sono decifrabili.
5. **Configurazione TURN Sicura (Zero Secret Hard-Coded)**:
   - Eliminate tutte le credenziali fittizie.
   - Configurazione tramite variabili d'ambiente: `AETHER_STUN_SERVERS`, `AETHER_TURN_URL`, `AETHER_TURN_USERNAME`, `AETHER_TURN_CREDENTIAL`.
   - Se le credenziali TURN non sono configurate, il sistema opera in P2P diretto con STUN e non inietta credenziali fittizie.

---

## 4. Telemetria e Misurazioni Reali

Tutte le metriche sono raccolte tramite clock monotoni (`std::time::Instant` in Rust, `time.perf_counter_ns()` in Python):
- `capture_ms`: durata reale dell'acquisizione DXGI e copia texture.
- `encode_ms`: durata reale dell'elaborazione MFT in `ProcessInput` / `ProcessOutput`.
- `queue_ms`: tempo di accodamento sul canale/traccia prima della trasmissione.
- `network_rtt_ms`: stima del round-trip time calcolata tramite algoritmo NTP a 4 timestamp (RFC 5905).
- `clock_offset_ms`: stima dello scostamento di clock tra client e host.
- `decode_ms` & `render_ms`: tempi effettivi di decodifica (Media Foundation MFT Decoder) e rendering su canvas Tkinter del client.
- `is_hardware_encoder`: booleano che riflette se l'encoder attivo è hardware (NVENC/QSV/AMF) o software fallback.
- **Onestà delle metriche**: qualsiasi metrica non misurata (es. RTT prima della ricezione di frame o frame fallback senza telemetria di capture) viene restituita rigorosamente come `None` (o visualizzata come "N/A"), mai come `0.0` o come costanti fittizie (1.2, 2.5, 12.0). Zero numeri inventati o costanti spacciate per misure.

---

## 5. Ciclo di Vita e Chiusura Ordinata

1. **Host Daemon Shutdown**:
   - `stop_async()` chiude la sessione WebRTC nativa (`destroy_host_session()`), disattiva il flag di streaming, annulla i task asincroni, e offload lo stop bloccante di `InputWorker` all'executor di thread per non bloccare l'event loop asyncio.
   - `_sync_cleanup()` garantisce il rilascio sicuro delle risorse in contesti sincroni.
2. **Client Teardown**:
   - Chiusura ordinata della finestra Tkinter (`_on_main_window_close`): disconnessione client, stop loop di rete, join dei thread con timeout deterministico.
3. **Politica Singolo Client**:
   - Il broker respinge qualsiasi secondo client con `HOST_BUSY` senza corrompere la tabella di routing del client già connesso.
