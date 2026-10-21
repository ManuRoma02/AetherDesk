# Servizio di Controllo Remoto Ultra-Leggero (TeamViewer Alternative) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Costruire un servizio di controllo remoto per Windows completo, leggero e ultra-veloce (alternativa open-source a TeamViewer), con accesso non presidiato (avvio automatico all'accensione prima del login), permessi amministrativi completi (UAC e schermate di blocco), autenticazione tramite ID e Password, e connessione gratuita via Internet.

**Architecture:** Architettura modulare composta da: 1) Windows Service in background avviato all'accensione come `SYSTEM` con prevenzione dello standby (`SetThreadExecutionState`); 2) Motore di cattura desktop ad alte prestazioni con aggancio dinamico all'input desktop (`OpenInputDesktop` per gestione trasparente di UAC e login) e iniezione input elevata (`SendInput`); 3) Canale di trasporto cifrato punto-a-punto a conoscenza zero con broker di segnalazione gratuito; 4) GUI Host per gestione ID/Password e Client portatile autonomo in un unico eseguibile.

**Tech Stack:** Python 3.13, Win32 API (`ctypes`, `pywin32`), OpenCV / TurboJPEG, WebSockets / asyncio, Cryptography (PBKDF2/AES-GCM), Tkinter / CustomTkinter per GUI nativa leggera.

**Spec:** [docs/superpowers/specs/2026-10-21-remote-desktop-service-design.md](file:///c:/Users/manue/Dropbox/ScreenMirroring/docs/superpowers/specs/2026-10-21-remote-desktop-service-design.md)

## Global Constraints
- **Piattaforma di esecuzione:** Windows 10/11 x64.
- **Autenticazione:** ID persistente a 9 cifre (es. `123 456 789`) e Password scelta dall'utente.
- **Costi e hosting:** 100% gratuito, zero abbonamenti, nessun server a pagamento necessario.
- **Permessi UAC & Login:** Nessun congelamento o schermo nero sui prompt di elevazione amministrativa o blocco Windows.
- **Avvio:** Avvio automatico all'accensione del PC fisso prima del login utente (`StartupType = Automatic`).

## Review Focus
1. **Transizione Desktop UAC/Winlogon:** Quando compare un prompt di amministratore, il desktop passa a un desktop sicuro; il worker deve ri-agganciare `OpenInputDesktop` senza generare eccezioni o interruzioni nel flusso video.
2. **Disconnessione e Riconnessione di Rete:** Se la connessione Wi-Fi del portatile o del fisso cade temporaneamente, il client e l'host devono tentare la riconnessione automatica senza bloccare il servizio.
3. **Mappatura Coordinate Mouse con DPI Scaling:** Con schermi a risoluzioni diverse o scaling DPI (es. 125% o 150%), le coordinate del mouse inviate dal client devono scalare con precisione millimetrica sul desktop host.
4. **Tentativi Brute-force Password:** Protezione sul lato host contro tentativi ripetuti di inserimento password errata (rate-limiting e blocco temporaneo).
5. **Prevenzione Standby:** Il PC fisso non deve entrare in sospensione (S3/sleep) mentre l'utente è fuori casa.

---

### Task 1: Motore di Cattura Desktop & Aggancio Desktop Sicuro (UAC)

**Files:**
- Create: `src/capture.py`
- Test: `tests/test_capture.py`

**Interfaces:**
- Produces: `class DesktopCapture(width=None, height=None, quality=80)`
  - `DesktopCapture.switch_desktop() -> bool`
  - `DesktopCapture.capture_frame() -> tuple[bytes, tuple[int, int], tuple[int, int]]` (jpeg_bytes, (screen_w, screen_h), (cursor_x, cursor_y))
  - `DesktopCapture.close()`

- [ ] **Step 1: Scrivere i test unitari in `tests/test_capture.py`**

```python
import pytest
from src.capture import DesktopCapture

def test_desktop_capture_initialization():
    cap = DesktopCapture(quality=75)
    assert cap.quality == 75
    cap.close()

def test_desktop_capture_frame():
    cap = DesktopCapture(quality=70)
    jpeg_data, screen_size, cursor_pos = cap.capture_frame()
    assert isinstance(jpeg_data, bytes)
    assert len(jpeg_data) > 1000
    assert screen_size[0] > 0 and screen_size[1] > 0
    assert isinstance(cursor_pos, tuple)
    cap.close()

def test_desktop_switch_desktop():
    cap = DesktopCapture()
    res = cap.switch_desktop()
    assert isinstance(res, bool)
    cap.close()
```

- [ ] **Step 2: Eseguire i test per verificare il fallimento**

Run: `pytest tests/test_capture.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'src'`

- [ ] **Step 3: Implementare `src/capture.py`**

Implementare la classe `DesktopCapture` utilizzando Win32 GDI via `ctypes` con:
- `OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP | DESKTOP_WRITEOBJECTS | ...)` e `SetThreadDesktop` per seguire attivamente UAC e schermata di blocco.
- `StretchBlt` / `GetDIBits` per estrarre la bitmap direttamente dal DC desktop.
- `GetCursorInfo` per disegnare la posizione e forma del cursore hardware direttamente sul fotogramma prima della compressione.
- Compressione JPEG ad alta velocità con `cv2.imencode`.

- [ ] **Step 4: Eseguire i test per verificare il superamento**

Run: `pytest tests/test_capture.py -v`
Expected: PASS

- [ ] **Step 5: Commit Git**

```bash
git add src/capture.py tests/test_capture.py
git commit -m "feat(capture): implement fast desktop capture with secure UAC desktop switching"
```

---

### Task 2: Modulo di Iniezione Input Elevato (Mouse e Tastiera)

**Files:**
- Create: `src/input_handler.py`
- Test: `tests/test_input.py`

**Interfaces:**
- Produces: `class InputHandler`
  - `InputHandler.move_mouse(norm_x: float, norm_y: float)`
  - `InputHandler.mouse_down(button: str)` (button: "left", "right", "middle")
  - `InputHandler.mouse_up(button: str)`
  - `InputHandler.mouse_wheel(delta: int)`
  - `InputHandler.key_down(vk_code: int)`
  - `InputHandler.key_up(vk_code: int)`
  - `InputHandler.send_special(command: str)` (e.g. "ctrl_alt_del", "win_d", "alt_tab")

- [ ] **Step 1: Scrivere i test unitari in `tests/test_input.py`**

```python
import pytest
from src.input_handler import InputHandler

def test_input_handler_coordinates():
    handler = InputHandler()
    abs_x, abs_y = handler.normalize_to_screen(0.5, 0.5)
    assert abs_x > 0 and abs_y > 0

def test_input_handler_mouse_events():
    handler = InputHandler()
    # Test valid calls don't raise
    handler.move_mouse(0.1, 0.1)
    handler.mouse_down("left")
    handler.mouse_up("left")
    handler.mouse_wheel(120)

def test_input_handler_special_keys():
    handler = InputHandler()
    res = handler.send_special("win_d")
    assert res is True
```

- [ ] **Step 2: Eseguire i test per verificare il fallimento**

Run: `pytest tests/test_input.py -v`
Expected: FAIL con `ModuleNotFoundError`

- [ ] **Step 3: Implementare `src/input_handler.py`**

Implementare `InputHandler` con:
- `user32.SendInput` per mouse (`MOUSEEVENTF_ABSOLUTE`, `MOUSEEVENTF_MOVE`, `MOUSEEVENTF_LEFTDOWN`, ecc.) e tastiera (`KEYEVENTF_KEYDOWN`, `KEYEVENTF_KEYUP`).
- Mappatura coordinate normalizzate (0.0 a 1.0) sulle coordinate assolute dello schermo virtuale (per supportare qualsiasi risoluzione o scaling del client).
- Iniezione sequenze speciali come combinazioni di tasti di sistema.

- [ ] **Step 4: Eseguire i test per verificare il superamento**

Run: `pytest tests/test_input.py -v`
Expected: PASS

- [ ] **Step 5: Commit Git**

```bash
git add src/input_handler.py tests/test_input.py
git commit -m "feat(input): implement elevated SendInput handler with coordinate normalization"
```

---

### Task 3: Modulo di Sicurezza, Autenticazione & Configurazione Persistente

**Files:**
- Create: `src/security.py`
- Test: `tests/test_security.py`

**Interfaces:**
- Produces: `class SecurityManager`
  - `SecurityManager.get_or_create_device_id() -> str` (es. "482 910 234")
  - `SecurityManager.set_password(password: str) -> None`
  - `SecurityManager.verify_password(password: str) -> bool`
  - `SecurityManager.derive_session_key(password: str, salt: bytes) -> bytes`
  - `SecurityManager.encrypt_packet(data: bytes, key: bytes) -> bytes`
  - `SecurityManager.decrypt_packet(encrypted: bytes, key: bytes) -> bytes`
  - `SecurityManager.is_rate_limited(client_ip: str) -> bool`

- [ ] **Step 1: Scrivere i test unitari in `tests/test_security.py`**

```python
import pytest
from src.security import SecurityManager

def test_device_id_format(tmp_path):
    mgr = SecurityManager(config_dir=str(tmp_path))
    dev_id = mgr.get_or_create_device_id()
    clean_id = dev_id.replace(" ", "")
    assert len(clean_id) == 9
    assert clean_id.isdigit()
    # Verify persistence
    assert mgr.get_or_create_device_id() == dev_id

def test_password_hashing(tmp_path):
    mgr = SecurityManager(config_dir=str(tmp_path))
    mgr.set_password("MioProgetto2026!")
    assert mgr.verify_password("MioProgetto2026!") is True
    assert mgr.verify_password("PasswordErrata") is False

def test_packet_encryption(tmp_path):
    mgr = SecurityManager(config_dir=str(tmp_path))
    salt = b"random_salt_1234"
    key = mgr.derive_session_key("MioSegreto", salt)
    plaintext = b"Test packet payload"
    ciphertext = mgr.encrypt_packet(plaintext, key)
    assert ciphertext != plaintext
    decrypted = mgr.decrypt_packet(ciphertext, key)
    assert decrypted == plaintext
```

- [ ] **Step 2: Eseguire i test per verificare il fallimento**

Run: `pytest tests/test_security.py -v`
Expected: FAIL con `ModuleNotFoundError`

- [ ] **Step 3: Implementare `src/security.py`**

Implementare `SecurityManager` con:
- Salvataggio file protetto `config.json` in cartella dedicata `%ProgramData%\RemoteDesktop` (o directory configurabile per test).
- Generazione deterministica di un ID univoco a 9 cifre formattato `XXX XXX XXX`.
- Hashing password con PBKDF2-HMAC-SHA256 (100.000 iterazioni) e salt crittografico casuale.
- Cifratura simmetrica AES-GCM (256-bit) per i frame e i pacchetti di input.
- Contatore tentativi falliti per rate-limiting (blocco 60 secondi dopo 5 tentativi errati).

- [ ] **Step 4: Eseguire i test per verificare il superamento**

Run: `pytest tests/test_security.py -v`
Expected: PASS

- [ ] **Step 5: Commit Git**

```bash
git add src/security.py tests/test_security.py
git commit -m "feat(security): implement device id generation, password hashing and AES-GCM E2EE"
```

---

### Task 4: Protocollo di Comunicazione & Broker di Segnalazione Gratuito

**Files:**
- Create: `src/protocol.py`
- Create: `src/signaling_broker.py`
- Test: `tests/test_protocol.py`

**Interfaces:**
- Produces: `class ProtocolMessage`
  - `ProtocolMessage.serialize(msg_type: str, payload: dict, binary_data: bytes = None) -> bytes`
  - `ProtocolMessage.deserialize(raw_bytes: bytes) -> tuple[str, dict, bytes]`
- Produces: `class SignalingBroker`
  - Standalone lightweight WebSocket server per associare ID Host e Client.

- [ ] **Step 1: Scrivere i test unitari in `tests/test_protocol.py`**

```python
import pytest
from src.protocol import ProtocolMessage

def test_protocol_serialization_json_only():
    data = ProtocolMessage.serialize("HELLO", {"device_id": "123456789"})
    msg_type, payload, binary = ProtocolMessage.deserialize(data)
    assert msg_type == "HELLO"
    assert payload["device_id"] == "123456789"
    assert binary is None

def test_protocol_serialization_with_binary():
    bin_payload = b"\xff\xd8\xff\xe0JPEGDATA"
    data = ProtocolMessage.serialize("FRAME", {"w": 1920, "h": 1080}, binary_data=bin_payload)
    msg_type, payload, binary = ProtocolMessage.deserialize(data)
    assert msg_type == "FRAME"
    assert payload["w"] == 1920
    assert binary == bin_payload
```

- [ ] **Step 2: Eseguire i test per verificare il fallimento**

Run: `pytest tests/test_protocol.py -v`
Expected: FAIL con `ModuleNotFoundError`

- [ ] **Step 3: Implementare `src/protocol.py` e `src/signaling_broker.py`**

- Formato frame binario compatto: `[4 bytes magic + 4 bytes header_len + JSON header + binary payload]`.
- Implementare `SignalingBroker` basato su `websockets`:
  - Gestisce messaggi `REGISTER_HOST`, `CONNECT_CLIENT`, `FORWARD_SIGNAL`, `RELAY_DATA`.
  - Architettura a conoscenza zero (Zero-Knowledge): il broker inoltra solo i pacchetti cifrati e le informazioni di rendezvous senza conoscere le chiavi.

- [ ] **Step 4: Eseguire i test per verificare il superamento**

Run: `pytest tests/test_protocol.py -v`
Expected: PASS

- [ ] **Step 5: Commit Git**

```bash
git add src/protocol.py src/signaling_broker.py tests/test_protocol.py
git commit -m "feat(network): implement binary framing protocol and zero-knowledge signaling broker"
```

---

### Task 5: Servizio di Sistema Windows Host & Prevenzione Standby

**Files:**
- Create: `src/host_daemon.py`
- Create: `src/host_service.py`
- Create: `scripts/install_service.bat`
- Create: `scripts/uninstall_service.bat`
- Test: `tests/test_daemon.py`

**Interfaces:**
- Produces: `class HostDaemon`
  - `HostDaemon.start()`
  - `HostDaemon.stop()`
  - `HostDaemon.prevent_sleep()` (chiama `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)`)
- Produces: Script per registrazione servizio Windows con `sc.exe` o `win32serviceutil` (`StartupType = Automatic`).

- [ ] **Step 1: Scrivere test per il demone in `tests/test_daemon.py`**

```python
import pytest
from src.host_daemon import HostDaemon

def test_daemon_sleep_prevention():
    daemon = HostDaemon(headless=True)
    res = daemon.prevent_sleep(True)
    assert res is True
    daemon.prevent_sleep(False)

def test_daemon_status():
    daemon = HostDaemon(headless=True)
    status = daemon.get_status()
    assert "device_id" in status
    assert "running" in status
```

- [ ] **Step 2: Eseguire i test per verificare il fallimento**

Run: `pytest tests/test_daemon.py -v`
Expected: FAIL con `ModuleNotFoundError`

- [ ] **Step 3: Implementare `src/host_daemon.py`, `src/host_service.py` e gli script `.bat`**

- Invocazione `ctypes.windll.kernel32.SetThreadExecutionState` per mantenere il PC fisso sempre sveglio e pronto alle connessioni remote.
- Ciclo di streaming: quando un client autenticato è connesso, cattura i frame via `DesktopCapture`, li cifra via `SecurityManager` e li trasmette; riceve pacchetti di input e li inoltra a `InputHandler`.
- `install_service.bat`: registra il servizio di sistema con `StartupType=Automatic` (avvio all'accensione del PC) e avvia il demone.

- [ ] **Step 4: Eseguire i test per verificare il superamento**

Run: `pytest tests/test_daemon.py -v`
Expected: PASS

- [ ] **Step 5: Commit Git**

```bash
git add src/host_daemon.py src/host_service.py scripts/ tests/test_daemon.py
git commit -m "feat(host): implement auto-start host daemon with sleep prevention and windows service registration"
```

---

### Task 6: Applicazione Client Portatile (.exe)

**Files:**
- Create: `src/client.py`
- Create: `src/client_viewer.py`
- Test: `tests/test_client.py`

**Interfaces:**
- Produces: `class RemoteClient`
  - Finestra di connessione pulita: Campo "ID Computer" (9 cifre), Campo "Password", pulsante "Connetti".
  - Finestra di visualizzazione remota ad alte prestazioni (rendering frame a 30-60 FPS).
  - Inoltro completo eventi mouse (click, drag, wheel) e tastiera (key codes Win32, tasto Windows, Alt+Tab).
  - Barra superiore retrattile con opzioni veloci: Invia Ctrl+Alt+Del, Invia Win+D, Schermo Intero, Disconnetti.

- [ ] **Step 1: Scrivere test per la logica del client in `tests/test_client.py`**

```python
import pytest
from src.client import ClientState

def test_client_id_validation():
    assert ClientState.validate_id("123 456 789") is True
    assert ClientState.validate_id("123456789") is True
    assert ClientState.validate_id("abc") is False

def test_client_connection_handshake_state():
    state = ClientState()
    assert state.is_connected is False
    state.set_connecting(True)
    assert state.is_connecting is True
```

- [ ] **Step 2: Eseguire i test per verificare il fallimento**

Run: `pytest tests/test_client.py -v`
Expected: FAIL con `ModuleNotFoundError`

- [ ] **Step 3: Implementare `src/client.py` e `src/client_viewer.py`**

- GUI moderna, scattante e nativa con Tkinter / Canvas accelerato.
- Gestione cattura input trasparente: `bind("<Motion>")`, `bind("<Button-1>")`, `bind("<Key>")`, `bind("<KeyRelease>")`.
- Decodifica veloce JPEG e visualizzazione fluida con ridimensionamento proporzionale (aspect ratio conservato).

- [ ] **Step 4: Eseguire i test per verificare il superamento**

Run: `pytest tests/test_client.py -v`
Expected: PASS

- [ ] **Step 5: Commit Git**

```bash
git add src/client.py src/client_viewer.py tests/test_client.py
git commit -m "feat(client): implement portable remote desktop client with low-latency viewer and input forwarding"
```

---

### Task 7: Host Tray Manager & Pannello di Controllo Locale

**Files:**
- Create: `src/host_tray.py`
- Create: `start_host.bat`
- Create: `start_client.bat`

**Interfaces:**
- Produces: Interfaccia utente grafica locale sul PC fisso:
  - Mostra il proprio ID ben visibile con pulsante "Copia ID".
  - Permette di impostare o cambiare la password di accesso.
  - Mostra se il servizio è attivo e i client attualmente collegati.
  - Icona nella barra delle applicazioni (System Tray).

- [ ] **Step 1: Implementare `src/host_tray.py`**

Pannello di controllo compatto per il PC fisso che interagisce con la configurazione di `SecurityManager` e mostra lo stato in tempo reale.

- [ ] **Step 2: Creare i launcher rapidi `start_host.bat` e `start_client.bat`**

Script `.bat` a un clic per avviare il pannello Host e il Client senza dover usare il prompt dei comandi.

- [ ] **Step 3: Verificare il funzionamento locale del pannello**

Run: `python -c "from src.host_tray import HostControlPanel; print('Host Tray GUI loaded successfully')"`
Expected: Output `Host Tray GUI loaded successfully`

- [ ] **Step 4: Commit Git**

```bash
git add src/host_tray.py start_host.bat start_client.bat
git commit -m "feat(ui): implement host control panel and one-click launch scripts"
```

---

### Task 8: Test di Integrazione End-to-End & Verifica Completa

**Files:**
- Create: `tests/test_e2e.py`
- Modify: `README.md`

- [ ] **Step 1: Scrivere test end-to-end in `tests/test_e2e.py`**

Verificare il flusso completo:
1. Avvio Host Daemon in locale.
2. Generazione ID e impostazione Password.
3. Connessione del Client tramite ID e Password corretti.
4. Trasmissione di un frame di test con verifica integrità AES-GCM.
5. Inoltro di un comando mouse e tastiera con verifica ricezione.
6. Test tentativo di connessione con password errata (verifica rifiuto).

- [ ] **Step 2: Eseguire la suite di test completa**

Run: `pytest tests/ -v`
Expected: 100% test superati (ALL PASS)

- [ ] **Step 3: Compilare documentazione chiara in `README.md`**

Istruzioni dettagliate in italiano su:
- Come installare e avviare il servizio sul PC fisso (`install_service.bat`).
- Come configurare la password personale.
- Come avviare il client sul portatile per collegarsi da scuola o fuori casa.

- [ ] **Step 4: Commit finale**

```bash
git add tests/test_e2e.py README.md
git commit -m "docs: complete e2e integration verification and user guide"
```
