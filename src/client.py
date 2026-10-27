"""
AetherDesk Client Network Layer.

Architectural Boundary:
- Client Runtime Layer: Manages asynchronous network communication, E2EE session key derivation,
  frame reception, backpressure ACK generation, and task lifecycle (receive loop, reconnect loop, aux tasks).
- Isolated from GUI thread: Invoked by ClientViewerGUI via threadsafe dispatch (run_coroutine_threadsafe).
"""

import re
import time
import json
import secrets
import asyncio
import logging
from typing import Optional, Callable, Any
import websockets
try:
    from websockets.exceptions import ConnectionClosed
except ImportError:
    class ConnectionClosed(Exception):
        pass

import os
import tempfile
from pathlib import Path
from src.security import SecurityManager, is_test_environment
from src.protocol import ProtocolMessage
from src.transfer import ClipboardManager, FileTransferManager
from src.ota import get_app_version, compute_file_sha256
from src.native_engine import get_native_engine, NativeRealtimeEngine

logger = logging.getLogger("RemoteClient")

class ClientSettings:
    """Manages persistent client settings like saved device ID, password, address book, and broker URL."""

    def __init__(self, config_dir: Optional[str] = None):
        if config_dir:
            self.config_dir = Path(config_dir)
        elif is_test_environment():
            self._test_temp_dir = tempfile.TemporaryDirectory(prefix="aether_test_client_cfg_")
            self.config_dir = Path(self._test_temp_dir.name)
        else:
            appdata = Path(os.environ.get("APPDATA", os.path.expanduser("~")))
            legacy = appdata / "RemoteDesktopClient"
            primary = appdata / "AetherDeskClient"
            self.config_dir = legacy if legacy.exists() and not primary.exists() else primary

        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_file = self.config_dir / "client_settings.json"

    def _read_data(self) -> dict:
        if not self.config_file.exists():
            return {}
        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save(self, device_id: str, password: str, remember: bool = True, broker_url: str = "ws://127.0.0.1:8765"):
        data = self._read_data()
        data["remember"] = remember
        data["device_id"] = device_id if remember else ""
        data["password"] = password if remember else ""
        data["broker_url"] = broker_url if broker_url else "ws://127.0.0.1:8765"

        # Update or add to address book if remembered
        if remember and device_id.strip():
            devices = data.get("devices", [])
            clean_id = re.sub(r"\s+", "", device_id.strip())
            found = False
            for d in devices:
                if re.sub(r"\s+", "", d.get("id", "")) == clean_id:
                    d["password"] = password
                    d["broker_url"] = broker_url
                    found = True
                    break
            if not found:
                devices.append({
                    "id": device_id.strip(),
                    "name": f"Computer {device_id.strip()}",
                    "password": password,
                    "broker_url": broker_url
                })
            data["devices"] = devices

        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def load(self) -> tuple[str, str, bool]:
        data = self._read_data()
        return (
            data.get("device_id", ""),
            data.get("password", ""),
            data.get("remember", False)
        )

    def get_broker_url(self) -> str:
        data = self._read_data()
        return data.get("broker_url", "ws://127.0.0.1:8765")

    def get_devices(self) -> list[dict]:
        data = self._read_data()
        return data.get("devices", [])

    def save_device(self, device_id: str, name: str, password: str, broker_url: str):
        data = self._read_data()
        devices = data.get("devices", [])
        clean_id = re.sub(r"\s+", "", device_id.strip())
        found = False
        for d in devices:
            if re.sub(r"\s+", "", d.get("id", "")) == clean_id:
                d["name"] = name
                d["password"] = password
                d["broker_url"] = broker_url
                found = True
                break
        if not found:
            devices.append({
                "id": device_id.strip(),
                "name": name if name else f"Computer {device_id.strip()}",
                "password": password,
                "broker_url": broker_url
            })
        data["devices"] = devices
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def delete_device(self, device_id: str):
        data = self._read_data()
        clean_id = re.sub(r"\s+", "", device_id.strip())
        devices = [d for d in data.get("devices", []) if re.sub(r"\s+", "", d.get("id", "")) != clean_id]
        data["devices"] = devices
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

class ClientState:
    """Tracks state and validation for the client connection."""

    def __init__(self):
        self.is_connected = False
        self.is_connecting = False
        self.is_reconnecting = False
        self.session_key: Optional[bytes] = None
        self.target_id: Optional[str] = None
        self.host_dims = (1920, 1080)

    @staticmethod
    def clean_id(device_id: str) -> str:
        return re.sub(r"\s+", "", device_id.strip())

    @staticmethod
    def is_ip_or_host(val: str) -> bool:
        clean = val.strip()
        if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?$", clean):
            return True
        if clean.lower() in ("localhost", "127.0.0.1") or clean.lower().startswith("127.0.0.1:"):
            return True
        return False

    @staticmethod
    def validate_id(device_id: str) -> bool:
        cleaned = ClientState.clean_id(device_id)
        if len(cleaned) == 9 and cleaned.isdigit():
            return True
        return ClientState.is_ip_or_host(device_id)

    def set_connecting(self, connecting: bool):
        self.is_connecting = connecting

    def set_reconnecting(self, reconnecting: bool):
        self.is_reconnecting = reconnecting
        if reconnecting:
            self.is_connected = False

    def set_connected(self, connected: bool, session_key: Optional[bytes] = None):
        self.is_connected = connected
        self.is_connecting = False
        self.is_reconnecting = False
        self.session_key = session_key

class RemoteClient:
    """Network client for connecting to a remote host via ID & password."""

    def __init__(
        self,
        broker_url: str = "ws://127.0.0.1:8765",
        auto_lan_discovery: bool = False,
        ws_connector: Optional[Any] = None,
        security: Optional[SecurityManager] = None,
        config_dir: Optional[str] = None,
        dpapi_engine: Optional[Any] = None,
    ):
        self.broker_url = broker_url
        self.auto_lan_discovery = auto_lan_discovery
        self.ws_connector = ws_connector
        self.state = ClientState()
        self.security = security or SecurityManager(config_dir=config_dir, dpapi_engine=dpapi_engine)
        self.settings = ClientSettings(config_dir=config_dir)
        self.native_engine: NativeRealtimeEngine = get_native_engine()
        self.clipboard = ClipboardManager()
        self.file_transfer = FileTransferManager()
        self.websocket = None
        self.on_frame_callback: Optional[Callable[[bytes, tuple[int, int], tuple[int, int]], None]] = None
        self.on_status_callback: Optional[Callable[[str], None]] = None
        self.on_clipboard_callback: Optional[Callable[[str], None]] = None
        self.on_file_saved_callback: Optional[Callable[[str], None]] = None
        self.on_error_callback: Optional[Callable[[str, dict], None]] = None
        self.on_verify_fingerprint_callback: Optional[Callable[[str, str], bool]] = None
        self.on_disconnect_callback: Optional[Callable[[], None]] = None
        self._webrtc_active: bool = False
        self.on_ota_info_callback: Optional[Callable[[dict], None]] = None
        self.on_ota_progress_callback: Optional[Callable[[int, int], None]] = None
        self.on_ota_complete_callback: Optional[Callable[[bool, str], None]] = None
        self.on_monitors_callback: Optional[Callable[[list], None]] = None
        self._ota_file_size = 0
        self._ota_sha256 = ""
        self._ota_received_bytes = 0
        self._ota_temp_file = None
        self._ota_fp = None
        self.last_dev_id = ""
        self.last_password = ""
        self.auto_reconnect = True
        self.max_reconnect_attempts = 10
        self._receive_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._aux_tasks: set[asyncio.Task] = set()
        self._is_manual_disconnect: bool = False
        self._disconnect_lock: Optional[asyncio.Lock] = None

    def _track_aux_task(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._aux_tasks.add(task)
        task.add_done_callback(self._aux_tasks.discard)
        return task

    def _set_status(self, text: str):
        if self.on_status_callback:
            self.on_status_callback(text)

    async def connect(self, device_id: str, password: str, broker_url: Optional[str] = None) -> bool:
        """Perform handshake and authenticate with remote host."""
        clean_id = ClientState.clean_id(device_id)

        # 0a. Check if direct IP was entered as device_id
        if ClientState.is_ip_or_host(device_id):
            ip_candidate = clean_id
            if not broker_url or "127.0.0.1" in broker_url or "localhost" in broker_url:
                target_url = ip_candidate if ip_candidate.startswith("ws://") else f"ws://{ip_candidate}"
                if ":" not in target_url.split("://")[1]:
                    target_url = f"{target_url}:8765"
                self.broker_url = target_url
        elif broker_url:
            clean_brk = broker_url.strip()
            if not clean_brk.startswith("ws://") and not clean_brk.startswith("wss://"):
                clean_brk = f"ws://{clean_brk}"
            host_part = clean_brk.split("://", 1)[1]
            if ":" not in host_part.split("/", 1)[0]:
                clean_brk = f"{clean_brk}:8765"
            self.broker_url = clean_brk

        # 0b. Auto-resolve peer broker via LAN Discovery if enabled and no explicit broker passed
        if getattr(self, "auto_lan_discovery", False) and not broker_url:
            try:
                from src.discovery import get_discovery_listener
                lan_broker = get_discovery_listener().get_peer_broker_url(clean_id)
                if lan_broker:
                    logger.info(f"Auto-resolved peer {clean_id} on LAN via UDP Discovery at {lan_broker}")
                    self.broker_url = lan_broker
            except Exception as e:
                logger.debug(f"LAN discovery lookup skipped: {e}")

        if not ClientState.validate_id(device_id):
            self._set_status("ID non valido. Inserire 9 cifre o IP computer.")
            return False

        self.last_dev_id = device_id
        self.last_password = password
        self.auto_reconnect = True
        self._is_manual_disconnect = False
        self.state.set_connecting(True)
        self._set_status(f"Connessione a {self.broker_url}...")

        try:
            if self.ws_connector:
                conn_target = self.ws_connector()
                if asyncio.iscoroutine(conn_target):
                    conn_target = await conn_target
                self.websocket = conn_target
            else:
                # Enforce 4.0s connection timeout so the client never hangs indefinitely
                self.websocket = await asyncio.wait_for(
                    websockets.connect(self.broker_url, open_timeout=4.0, max_size=16 * 1024 * 1024),
                    timeout=5.0
                )
            # 1. Ask broker to connect to host ID
            salt = secrets.token_bytes(16)
            client_pubkey_hex = self.security.get_identity_public_key_hex()
            conn_msg = ProtocolMessage.serialize("CONNECT_CLIENT", {
                "device_id": clean_id,
                "salt": salt.hex(),
                "client_pubkey": client_pubkey_hex
            })
            await self.websocket.send(conn_msg)

            # Wait for response with timeout
            resp_raw = await asyncio.wait_for(self.websocket.recv(), timeout=5.0)
            msg_type, payload, _ = ProtocolMessage.deserialize(resp_raw)

            if msg_type == "HOST_NOT_FOUND":
                self.state.set_connecting(False)
                self._set_status(f"Dispositivo {clean_id} non trovato o offline su {self.broker_url}.")
                await self.websocket.close()
                return False

            if msg_type == "HOST_BUSY":
                self.state.set_connecting(False)
                self._set_status(f"Dispositivo {clean_id} occupato in un'altra sessione.")
                await self.websocket.close()
                return False

            if msg_type == "HOST_FOUND":
                self.security.reset_replay_window()
                self._set_status("Computer trovato. Autenticazione...")

                # End-to-end host public key verification against persistent known_hosts (TOFU + explicit pairing)
                host_pubkey_hex = payload.get("host_pubkey", "")
                if not host_pubkey_hex:
                    logger.error(f"Host {clean_id} did not provide public key!")
                    self.state.set_connecting(False)
                    self._set_status("ATTENZIONE DI SICUREZZA: L'host non ha fornito la chiave crittografica d'identità.")
                    await self.websocket.close()
                    return False

                pin_status, fingerprint = self.security.check_host_pin_status(clean_id, host_pubkey_hex)
                if pin_status == "KEY_CHANGED":
                    logger.critical(f"SECURITY ALERT: Host identity mismatch for {clean_id}! Fingerprint: {fingerprint}")
                    self.state.set_connecting(False)
                    self._set_status("ATTENZIONE DI SICUREZZA: Chiave host cambiata! Possibile attacco MITM o broker compromesso.")
                    await self.websocket.close()
                    return False
                elif pin_status == "UNPINNED":
                    logger.info(f"First-time pairing with host {clean_id}. Fingerprint: {fingerprint}")
                    if not self.on_verify_fingerprint_callback:
                        logger.error(f"First pairing rejected: no verification callback provided for host {clean_id}")
                        self.state.set_connecting(False)
                        self._set_status(f"Connessione rifiutata: verifica fingerprint {fingerprint} richiesta ma nessun gestore di conferma disponibile.")
                        await self.websocket.close()
                        return False

                    verified = False
                    try:
                        verified = bool(self.on_verify_fingerprint_callback(clean_id, fingerprint))
                    except Exception as e:
                        logger.error(f"Fingerprint callback error: {e}")
                        verified = False

                    if not verified:
                        logger.warning(f"User rejected or canceled fingerprint verification for host {clean_id}")
                        self.state.set_connecting(False)
                        self._set_status(f"Connessione annullata: fingerprint {fingerprint} non confermato.")
                        await self.websocket.close()
                        return False

                    pinned = self.security.pin_host_key(clean_id, host_pubkey_hex)
                    if not pinned:
                        logger.error(f"Failed to pin host key for {clean_id}")
                        self.state.set_connecting(False)
                        self._set_status("Errore interno durante il salvataggio della chiave host.")
                        await self.websocket.close()
                        return False
                elif pin_status not in ("VERIFIED", "PINNED_OK"):
                    logger.error(f"Invalid host key or pin status for {clean_id}: {pin_status}")
                    self.state.set_connecting(False)
                    self._set_status("Errore di validazione crittografica della chiave host.")
                    await self.websocket.close()
                    return False

                # Also update devices list in settings
                saved_devs = self.settings.get_devices()
                for d in saved_devs:
                    if ClientState.clean_id(d.get("id", "")) == clean_id:
                        d["pinned_pubkey"] = host_pubkey_hex.lower()
                        self.settings.save_device(clean_id, d.get("name", ""), password, self.broker_url)
                        break

                # Generate Ephemeral X25519 ECDH keypair
                client_eph_priv, client_eph_pub = self.security.create_ephemeral_ecdh()

                # Derive session key with entered password and host salt
                host_salt_hex = payload.get("host_salt", "")
                host_salt = bytes.fromhex(host_salt_hex) if host_salt_hex else b""
                master_key = self.security.derive_master_key(password, host_salt) if host_salt else password.encode("utf-8")
                session_key = self.security.derive_session_key(master_key, salt)

                # Send authentication challenge token with forward-secure ephemeral ECDH
                auth_token = self.security.encrypt_packet(b"AETHER_AUTH_V1", session_key)
                auth_req = ProtocolMessage.serialize("AUTH_REQUEST", {
                    "salt": salt.hex(),
                    "target_id": clean_id,
                    "client_eph": client_eph_pub.hex(),
                }, binary_data=auth_token)
                await self.websocket.send(auth_req)

                # Validate host response (drain any stale in-flight messages until AUTH_OK or AUTH_FAILED)
                established_session_key = session_key
                try:
                    for _ in range(6):
                        auth_resp_raw = await asyncio.wait_for(self.websocket.recv(), timeout=3.5)
                        auth_type, auth_payload, auth_bin = ProtocolMessage.deserialize(auth_resp_raw)
                        if auth_type == "AUTH_FAILED":
                            self.state.set_connecting(False)
                            self._set_status("Password errata. Impossibile accedere all'host remoto.")
                            await self.websocket.close()
                            return False
                        elif auth_type == "AUTH_OK":
                            # Check if host responded with forward-secure X25519 parameters
                            host_eph_hex = auth_payload.get("host_eph")
                            host_sig_hex = auth_payload.get("host_sig")
                            if host_eph_hex and host_sig_hex and host_pubkey_hex:
                                host_eph_bytes = bytes.fromhex(host_eph_hex)
                                host_sig_bytes = bytes.fromhex(host_sig_hex)
                                host_pub_bytes = bytes.fromhex(host_pubkey_hex)
                                valid_sig = SecurityManager.verify_data_signature(
                                    host_pub_bytes, host_eph_bytes + client_eph_pub + salt, host_sig_bytes
                                )
                                if not valid_sig:
                                    logger.error("Host cryptographic signature on ephemeral key is invalid!")
                                    self.state.set_connecting(False)
                                    self._set_status("ATTENZIONE DI SICUREZZA: Firma dell'host non valida!")
                                    await self.websocket.close()
                                    return False
                                # Derive forward-secure ECDH key
                                established_session_key = self.security.derive_ecdh_shared_key(
                                    client_eph_priv, host_eph_bytes, salt, context=b"AetherDesk-v2-ForwardSecure"
                                )
                                logger.info("Forward-secure X25519 + Ed25519 session established successfully.")

                            # Handle WebRTC offer with real native client peer connection
                            webrtc_offer = auth_payload.get("webrtc_offer")
                            if webrtc_offer and self.native_engine.is_native_loaded:
                                logger.info("Host offered WebRTC session. Initializing real client peer connection...")
                                if self.native_engine.create_client_session():
                                    if self.native_engine.set_client_offer(webrtc_offer):
                                        ans_sdp = self.native_engine.create_client_answer()
                                        if ans_sdp:
                                            ans_msg = ProtocolMessage.serialize("WEBRTC_ANSWER", {"sdp": ans_sdp})
                                            await self.websocket.send(ans_msg)
                                            logger.info("Sent real negotiated WebRTC Answer to host.")
                            else:
                                self._webrtc_active = False
                            break
                        elif auth_type == "FRAME" and auth_bin:
                            try:
                                first_jpeg = self.security.decrypt_packet(auth_bin, established_session_key, check_replay=False)
                                break
                            except Exception:
                                pass
                except asyncio.TimeoutError:
                    pass

                self.state.set_connected(True, established_session_key)
                self.state.target_id = clean_id
                self._set_status("Connesso con successo!")

                # Cancel previous receive loop if running
                if self._receive_task and not self._receive_task.done():
                    self._receive_task.cancel()
                    try:
                        await self._receive_task
                    except (asyncio.CancelledError, Exception):
                        pass

                # Start background receiver loop tracked
                self._receive_task = asyncio.create_task(self._receive_loop())
                # Query host for OTA update availability
                self._track_aux_task(self.check_ota_update())
                # Request list of available physical monitors on remote host
                self._track_aux_task(self.request_monitors_list())
                return True

        except asyncio.TimeoutError:
            logger.error("Connection timed out")
            self.state.set_connecting(False)
            self._set_status(
                f"Timeout: {self.broker_url} non risponde.\n"
                f"Verifica che AetherDesk sia aperto sull'host e la porta 8765 non sia bloccata dal Firewall."
            )
            if self.websocket:
                try:
                    await self.websocket.close()
                except Exception:
                    pass
            return False
        except (OSError, websockets.exceptions.WebSocketException) as e:
            logger.error(f"Connection failed: {e}")
            self.state.set_connecting(False)
            self._set_status(
                f"Host non raggiungibile ({self.broker_url}).\n"
                f"Verifica che l'applicazione sia aperta e l'IP corretto."
            )
            if self.websocket:
                await self.websocket.close()
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            self.state.set_connecting(False)
            self._set_status(f"Errore di connessione: {e}")
            if self.websocket:
                await self.websocket.close()
        return False

    async def _receive_loop(self):
        """Listen for incoming encrypted video frames and WebRTC peer connection packets."""
        async def _ws_receiver():
            while self.state.is_connected and self.websocket:
                raw = await self.websocket.recv()
                msg_type, payload, binary = ProtocolMessage.deserialize(raw)

                if msg_type == "ICE_CANDIDATE":
                    cand = payload.get("candidate", "") if isinstance(payload, dict) else ""
                    if cand and self.native_engine.is_native_loaded and self.native_engine.client_handle:
                        self.native_engine.add_client_ice_candidate(cand)

                elif msg_type == "FRAME" and binary and self.state.session_key:
                    t_recv = time.time()
                    # Backpressure / Flow control: send FRAME_ACK immediately to prevent bufferbloat
                    fid = payload.get("fid", 0) if isinstance(payload, dict) else 0
                    ack_msg = ProtocolMessage.serialize("FRAME_ACK", {"fid": fid, "target_id": self.state.target_id})
                    try:
                        await self.websocket.send(ack_msg)
                    except Exception as ack_err:
                        logger.debug(f"Failed to send FRAME_ACK: {ack_err}")

                    # 1. Decrypt packet with targeted error handling
                    t_dec_start = time.perf_counter_ns()
                    try:
                        jpeg_bytes = self.security.decrypt_packet(binary, self.state.session_key, check_replay=False)
                        self._decrypt_failures = 0
                    except Exception as dec_err:
                        logger.error(f"Frame decrypt error: {dec_err}")
                        self._decrypt_failures = getattr(self, "_decrypt_failures", 0) + 1
                        if self._decrypt_failures >= 3:
                            self._set_status("Password errata o chiave di sessione non valida.")
                            if self.on_status_callback:
                                self.on_status_callback("Password errata. Impossibile decifrare il video.")
                            self._is_manual_disconnect = True
                            self.auto_reconnect = False
                            self.state.set_connected(False)
                            self._webrtc_active = False
                            if self.on_disconnect_callback:
                                try:
                                    self.on_disconnect_callback()
                                except Exception:
                                    pass
                            break
                        continue

                    t_dec_ms = round((time.perf_counter_ns() - t_dec_start) / 1e6, 2)
                    size = (payload.get("w", 1920), payload.get("h", 1080)) if isinstance(payload, dict) else (1920, 1080)
                    cur = (payload.get("cx", 0), payload.get("cy", 0)) if isinstance(payload, dict) else (0, 0)
                    self.state.host_dims = size

                    # 2. Telemetry extraction with strict non-breaking isolation
                    telemetry = {
                        "fid": fid,
                        "t_send": payload.get("t_send") if isinstance(payload, dict) else None,
                        "t_recv": t_recv,
                        "cap_ms": payload.get("cap_ms") if isinstance(payload, dict) else None,
                        "enc_ms": payload.get("enc_ms") if isinstance(payload, dict) else None,
                        "dec_ms": t_dec_ms,
                        "rtt_ms": None,
                        "clock_offset_ms": None,
                        "jitter_ms": None,
                        "size_bytes": len(binary),
                        "webrtc": False,
                    }
                    try:
                        host_rtt = payload.get("rtt_ms") if isinstance(payload, dict) else None
                        if host_rtt is not None and isinstance(host_rtt, (int, float)) and host_rtt > 0:
                            telemetry["rtt_ms"] = round(float(host_rtt), 2)
                        elif hasattr(self, "native_engine") and self.native_engine and hasattr(self.native_engine, "clock_estimator"):
                            est_rtt = self.native_engine.clock_estimator.rtt_ms
                            if est_rtt is not None and isinstance(est_rtt, (int, float)) and est_rtt > 0:
                                telemetry["rtt_ms"] = round(float(est_rtt), 2)
                            est_offset = self.native_engine.clock_estimator.offset_ms
                            if est_offset is not None and isinstance(est_offset, (int, float)) and telemetry["rtt_ms"] is not None:
                                telemetry["clock_offset_ms"] = round(float(est_offset), 3)
                            est_jitter = self.native_engine.clock_estimator.jitter_ms
                            if est_jitter is not None and isinstance(est_jitter, (int, float)) and telemetry["rtt_ms"] is not None:
                                telemetry["jitter_ms"] = round(float(est_jitter), 2)
                    except Exception as telem_err:
                        logger.debug(f"Telemetry extraction error (non-fatal): {telem_err}")

                    # 3. Always dispatch frame callback regardless of telemetry availability
                    if self.on_frame_callback:
                        try:
                            self.on_frame_callback(jpeg_bytes, size, cur, telemetry)
                        except TypeError:
                            try:
                                self.on_frame_callback(jpeg_bytes, size, cur)
                            except Exception as cb_err:
                                logger.error(f"Error in on_frame_callback (fallback signature): {cb_err}")
                        except Exception as cb_err:
                            logger.error(f"Error in on_frame_callback: {cb_err}")

                elif msg_type == "CLIPBOARD" and binary and self.state.session_key:
                    try:
                        decrypted = self.security.decrypt_packet(binary, self.state.session_key)
                        text = decrypted.decode("utf-8")
                        self.clipboard.set_text(text, update_last=True)
                        if self.on_clipboard_callback:
                            self.on_clipboard_callback(text)
                    except Exception as e:
                        logger.error(f"Clipboard decrypt error: {e}")

                elif msg_type == "FILE_SAVED":
                    saved_path = payload.get("saved_path", "")
                    if self.on_file_saved_callback:
                        self.on_file_saved_callback(saved_path)

                elif msg_type in ("ERROR", "INPUT_ERROR", "INPUT_REJECTED"):
                    err_code = payload.get("error", "unknown_error")
                    err_message = payload.get("message", "")
                    logger.warning(f"Ricevuto errore dall'host: {err_code} ({payload})")
                    if err_code == "CONTROL_CHANNEL_SATURATED":
                        self._is_manual_disconnect = True
                        self.auto_reconnect = False
                        self._set_status("Sessione terminata: canale di controllo host saturo.")
                        self.state.set_connected(False)
                        self._webrtc_active = False
                        if self.on_disconnect_callback:
                            try:
                                self.on_disconnect_callback()
                            except Exception:
                                pass
                        break
                    elif err_code in ("CAPTURE_FAILED", "STREAM_ERROR", "HOST_STREAM_TERMINATED"):
                        status_str = f"Sessione interrotta: {err_message}" if err_message else f"Sessione interrotta: errore di streaming host ({err_code})."
                        self._set_status(status_str)
                        self._is_manual_disconnect = True
                        self.auto_reconnect = False
                        self.state.set_connected(False)
                        self._webrtc_active = False
                        if self.on_disconnect_callback:
                            try:
                                self.on_disconnect_callback()
                            except Exception:
                                pass
                        break
                    else:
                        self._set_status(f"Errore host: {err_code}")
                    if self.on_error_callback:
                        try:
                            self.on_error_callback(err_code, payload)
                        except Exception:
                            pass

                elif msg_type == "MONITORS_LIST":
                    mons = payload.get("monitors", [])
                    self.state.monitors = mons
                    if self.on_monitors_callback:
                        self.on_monitors_callback(mons)

                elif msg_type == "OTA_INFO":
                    if self.on_ota_info_callback:
                        self.on_ota_info_callback(payload)

                elif msg_type == "OTA_START":
                    self._ota_file_size = payload.get("filesize", 0)
                    self._ota_sha256 = payload.get("sha256", "")
                    self._ota_received_bytes = 0
                    if self._ota_fp:
                        try:
                            self._ota_fp.close()
                        except Exception:
                            pass
                    tf = tempfile.NamedTemporaryFile(delete=False, suffix=".exe")
                    self._ota_temp_file = tf.name
                    tf.close()
                    self._ota_fp = open(self._ota_temp_file, "wb")
                    if self.on_ota_progress_callback:
                        self.on_ota_progress_callback(0, self._ota_file_size)

                elif msg_type == "OTA_CHUNK" and binary:
                    if self._ota_fp:
                        chunk = binary
                        if self.state.session_key:
                            try:
                                chunk = self.security.decrypt_packet(binary, self.state.session_key)
                            except Exception:
                                chunk = binary
                        self._ota_fp.write(chunk)
                        self._ota_received_bytes += len(chunk)
                        if self.on_ota_progress_callback:
                            self.on_ota_progress_callback(self._ota_received_bytes, self._ota_file_size)

                elif msg_type == "OTA_FINISH":
                    if self._ota_fp:
                        try:
                            self._ota_fp.close()
                        except Exception:
                            pass
                        self._ota_fp = None
                    if self._ota_temp_file and Path(self._ota_temp_file).exists():
                        try:
                            os.unlink(self._ota_temp_file)
                        except Exception:
                            pass
                        self._ota_temp_file = None
                    logger.warning("OTA_FINISH ricevuto da peer: aggiornamento P2P respinto fail-closed (manca firma digitale fornitore).")
                    if self.on_ota_complete_callback:
                        self.on_ota_complete_callback(
                            False,
                            "Rifiuto OTA P2P fail-closed: il file trasmesso dall'host non possiede una firma digitale del fornitore verificabile."
                        )

                elif msg_type == "OTA_ERROR":
                    if self._ota_fp:
                        try:
                            self._ota_fp.close()
                        except Exception:
                            pass
                        self._ota_fp = None
                    err = payload.get("error", "Errore durante l'aggiornamento.")
                    if self.on_ota_complete_callback:
                        self.on_ota_complete_callback(False, err)

                elif msg_type == "CLIENT_DISCONNECTED":
                    logger.info("Host disconnected from broker.")
                    self._is_manual_disconnect = True
                    self.auto_reconnect = False
                    self.state.set_connected(False)
                    self._webrtc_active = False
                    self._set_status("Il computer remoto si e' disconnesso.")
                    if self.on_status_callback:
                        self.on_status_callback("Il computer remoto si e' disconnesso.")
                    if self.on_disconnect_callback:
                        try:
                            self.on_disconnect_callback()
                        except Exception:
                            pass
                    break

        async def _webrtc_pump():
            while self.state.is_connected:
                if self.native_engine.is_native_loaded and self.native_engine.client_handle:
                    # 1. Forward outgoing local ICE candidates to host via broker
                    cand = self.native_engine.poll_client_ice_candidate()
                    if cand and self.websocket:
                        try:
                            cand_msg = ProtocolMessage.serialize("ICE_CANDIDATE", {
                                "target_id": self.state.target_id,
                                "candidate": cand
                            })
                            await self.websocket.send(cand_msg)
                        except Exception as cand_err:
                            logger.debug(f"Failed to forward client ICE candidate: {cand_err}")

                    # 2. Monitor ICE connection state
                    ice_state = self.native_engine.get_client_ice_state()
                    if ice_state in (2, 3):  # Connected or Completed
                        if not self._webrtc_active:
                            self._webrtc_active = True
                            logger.info("Client WebRTC P2P direct transport activated (ICE connected).")
                    elif ice_state in (4, 5, 6):  # Failed, Disconnected, or Closed
                        if self._webrtc_active:
                            self._webrtc_active = False
                            logger.warning(f"Client WebRTC ICE state {ice_state}, falling back to WebSocket stream.")

                    # 3. Poll decoded video frame from native pipeline
                    if self._webrtc_active:
                        frame_res = self.native_engine.poll_client_video_frame()
                        if frame_res:
                            raw_bytes, dims, dec_ms = frame_res
                            self.state.host_dims = dims
                            rtt_measured = round(self.native_engine.clock_estimator.rtt_ms, 2) if self.native_engine.clock_estimator.rtt_ms > 0 else None
                            clock_offset = round(self.native_engine.clock_estimator.offset_ms, 3) if self.native_engine.clock_estimator.rtt_ms > 0 else None
                            jitter_measured = round(self.native_engine.clock_estimator.jitter_ms, 2) if self.native_engine.clock_estimator.rtt_ms > 0 else None

                            telemetry = {
                                "fid": None,
                                "t_send": None,
                                "t_recv": time.time(),
                                "cap_ms": None,
                                "enc_ms": None,
                                "dec_ms": dec_ms if dec_ms > 0 else None,
                                "rtt_ms": rtt_measured,
                                "clock_offset_ms": clock_offset,
                                "jitter_ms": jitter_measured,
                                "size_bytes": len(raw_bytes),
                                "webrtc": True,
                            }
                            if self.on_frame_callback:
                                try:
                                    self.on_frame_callback(raw_bytes, dims, (0, 0), telemetry)
                                except TypeError:
                                    self.on_frame_callback(raw_bytes, dims, (0, 0))

                await asyncio.sleep(0.005)

        receiver_task = None
        pump_task = None
        should_schedule_disconnect = False
        try:
            receiver_task = asyncio.create_task(_ws_receiver())
            pump_task = asyncio.create_task(_webrtc_pump())

            try:
                done, pending = await asyncio.wait(
                    [receiver_task, pump_task],
                    return_when=asyncio.FIRST_COMPLETED
                )
                # Terminate and gather whichever sibling task is still pending
                for t in pending:
                    t.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

                # Propagate any exception from completed tasks
                for t in done:
                    if not t.cancelled() and t.exception():
                        raise t.exception()

                # If completed without exceptions, check if an inbound terminal message
                # marked the session as terminated (e.g. CLIENT_DISCONNECTED, SATURATED, CAPTURE_FAILED)
                if not self.state.is_connected and not self.state.is_reconnecting:
                    should_schedule_disconnect = True
            except asyncio.CancelledError:
                # Intentional cancellation during external disconnect() or reconnect cleanup.
                # Do NOT schedule teardown: the cancelling task owns the teardown.
                should_schedule_disconnect = False
                raise
            except (ConnectionClosed, Exception) as e:
                logger.warning(f"Connection lost: {e}")
                if self.auto_reconnect and self.last_dev_id and not self.state.is_reconnecting and not self._is_manual_disconnect:
                    # Hand over lifecycle to reconnection task; teardown is deferred to reconnect outcome
                    if self._reconnect_task and not self._reconnect_task.done():
                        self._reconnect_task.cancel()
                    self._reconnect_task = asyncio.create_task(self._attempt_reconnection())
                    should_schedule_disconnect = False
                else:
                    self._is_manual_disconnect = True
                    self.auto_reconnect = False
                    self.state.set_connected(False)
                    self._webrtc_active = False
                    self._set_status("Connessione interrotta dal computer remoto.")
                    if self.on_disconnect_callback:
                        try:
                            self.on_disconnect_callback()
                        except Exception:
                            pass
                    should_schedule_disconnect = True
        finally:
            # Guarantee that both child tasks are cancelled and awaited before _receive_loop exits
            child_tasks = [t for t in (receiver_task, pump_task) if t is not None]
            for t in child_tasks:
                if not t.done():
                    t.cancel()
            if child_tasks:
                await asyncio.gather(*child_tasks, return_exceptions=True)

            if self._ota_fp:
                try:
                    self._ota_fp.close()
                except Exception:
                    pass
                self._ota_fp = None

            # Schedule definitive teardown exactly once if receive loop owned the termination
            if should_schedule_disconnect and not self.state.is_reconnecting:
                self._track_aux_task(self.disconnect())

    async def _attempt_reconnection(self):
        self.state.set_reconnecting(True)
        try:
            for attempt in range(1, self.max_reconnect_attempts + 1):
                if self._is_manual_disconnect or not self.auto_reconnect:
                    break
                self._set_status(f"Connessione persa. Riconnessione in corso... ({attempt}/{self.max_reconnect_attempts})")
                await asyncio.sleep(2.0)
                if self._is_manual_disconnect or not self.auto_reconnect:
                    break
                try:
                    ok = await self.connect(self.last_dev_id, self.last_password)
                    if ok:
                        self._set_status("Riconnesso con successo!")
                        return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass
        finally:
            self.state.set_reconnecting(False)
            if not self.state.is_connected:
                self._set_status("Impossibile riconnettersi al computer remoto.")
                if not self._is_manual_disconnect:
                    self._is_manual_disconnect = True
                    self.auto_reconnect = False
                    if self.on_disconnect_callback:
                        try:
                            self.on_disconnect_callback()
                        except Exception:
                            pass
                    self._track_aux_task(self.disconnect())

    async def toggle_privacy_mode(self, enabled: bool):
        """Turn on/off physical monitor on host PC."""
        if not self.state.is_connected or not self.websocket:
            return
        msg = ProtocolMessage.serialize("PRIVACY_MODE", {"target_id": self.state.target_id, "enabled": enabled})
        await self.websocket.send(msg)

    async def send_clipboard(self, text: str):
        """Send local clipboard text to host."""
        if not self.state.is_connected or not self.websocket or not self.state.session_key:
            return
        enc = self.security.encrypt_packet(text.encode("utf-8"), self.state.session_key)
        msg = ProtocolMessage.serialize("CLIPBOARD", {"target_id": self.state.target_id}, binary_data=enc)
        await self.websocket.send(msg)

    async def switch_monitor(self, monitor_id: int):
        """Request host to switch active screen."""
        if not self.state.is_connected or not self.websocket:
            return
        msg = ProtocolMessage.serialize("SWITCH_MONITOR", {"target_id": self.state.target_id, "monitor_id": monitor_id})
        await self.websocket.send(msg)

    async def request_monitors_list(self):
        """Ask host for list of connected physical display monitors."""
        if not self.state.is_connected or not self.websocket:
            return
        msg = ProtocolMessage.serialize("GET_MONITORS", {"target_id": self.state.target_id})
        await self.websocket.send(msg)

    async def set_quality(self, quality: int):
        """Set JPEG compression quality on remote host (1-100)."""
        if not self.state.is_connected or not self.websocket:
            return
        msg = ProtocolMessage.serialize("SET_QUALITY", {"target_id": self.state.target_id, "quality": quality})
        await self.websocket.send(msg)

    async def send_file(self, file_path: str, progress_callback=None):
        """Stream a file chunk by chunk to the host with minimal memory overhead."""
        if not self.state.is_connected or not self.websocket or not self.state.session_key:
            return False

        meta = self.file_transfer.prepare_file_info(file_path)
        meta["target_id"] = self.state.target_id

        # Send start
        start_msg = ProtocolMessage.serialize("FILE_START", meta)
        await self.websocket.send(start_msg)

        total_bytes = meta["filesize"]
        sent_bytes = 0

        # Stream chunks one-by-one (O(1) memory usage)
        for offset, chunk_data in self.file_transfer.iter_file_chunks(file_path, chunk_size=65536):
            enc_chunk = self.security.encrypt_packet(chunk_data, self.state.session_key)
            chunk_msg = ProtocolMessage.serialize("FILE_CHUNK", {
                "target_id": self.state.target_id,
                "file_id": meta["file_id"],
                "offset": offset
            }, binary_data=enc_chunk)
            await self.websocket.send(chunk_msg)
            sent_bytes += len(chunk_data)
            if progress_callback:
                progress_callback(sent_bytes, total_bytes)
            await asyncio.sleep(0.001)

        # Send end
        end_msg = ProtocolMessage.serialize("FILE_END", {
            "target_id": self.state.target_id,
            "file_id": meta["file_id"]
        })
        await self.websocket.send(end_msg)
        return True

    async def send_input(self, input_dict: dict):
        """Encrypt and send mouse/keyboard input to host over segregated channels."""
        if not self.state.is_connected or not self.state.session_key:
            return

        # Segregated transport: when native WebRTC is active, route through dedicated channels
        if self._webrtc_active and self.native_engine.is_native_loaded and self.native_engine.client_handle:
            if input_dict.get("type") == "mouse_move":
                self._cursor_seq = getattr(self, "_cursor_seq", 0) + 1
                seq = self._cursor_seq
                norm_x = float(input_dict.get("x", 0.0))
                norm_y = float(input_dict.get("y", 0.0))
                w, h = self.state.host_dims
                px = int(norm_x * w) if norm_x <= 1.0 else int(norm_x)
                py = int(norm_y * h) if norm_y <= 1.0 else int(norm_y)
                if self.native_engine.send_client_cursor(px, py, seq):
                    return
            else:
                payload_bytes = json.dumps(input_dict).encode("utf-8")
                enc_payload = self.security.encrypt_packet(payload_bytes, self.state.session_key)
                ctrl_packet = ProtocolMessage.serialize("INPUT", {
                    "target_id": self.state.target_id,
                    "channel": "control"
                }, binary_data=enc_payload)
                if self.native_engine.send_client_control(ctrl_packet):
                    return

        # Fallback to WebSocket signaling transport when WebRTC is not active
        if not self.websocket:
            return

        if input_dict.get("type") == "mouse_move":
            self._cursor_seq = getattr(self, "_cursor_seq", 0) + 1
            input_dict["seq"] = self._cursor_seq
            input_dict["t"] = time.perf_counter_ns()
            channel_priority = "cursor"
        else:
            channel_priority = "control"

        payload_bytes = json.dumps(input_dict).encode("utf-8")
        encrypted = self.security.encrypt_packet(payload_bytes, self.state.session_key)
        msg = ProtocolMessage.serialize("INPUT", {
            "target_id": self.state.target_id,
            "channel": channel_priority
        }, binary_data=encrypted)
        try:
            await self.websocket.send(msg)
        except Exception as e:
            logger.error(f"Failed to send input: {e}")

    async def check_ota_update(self):
        """Ask host for available OTA updates."""
        if not self.state.is_connected or not self.websocket:
            return
        msg = ProtocolMessage.serialize("OTA_CHECK", {
            "target_id": self.state.target_id,
            "client_version": get_app_version()
        })
        try:
            await self.websocket.send(msg)
        except Exception as e:
            logger.warning(f"Failed to send OTA_CHECK: {e}")

    async def request_ota_update(self):
        """Request host to stream OTA update executable."""
        if not self.state.is_connected or not self.websocket:
            return
        msg = ProtocolMessage.serialize("OTA_REQUEST", {"target_id": self.state.target_id})
        try:
            await self.websocket.send(msg)
        except Exception as e:
            logger.warning(f"Failed to send OTA_REQUEST: {e}")

    async def disconnect(self):
        """
        Safely and idempotently terminate the client connection, teardown WebRTC/native
        pipelines, cancel background receiver loops and aux tasks without self-cancellation.
        Serialized through _disconnect_lock to prevent race conditions from concurrent disconnect triggers.
        """
        if self._disconnect_lock is None:
            self._disconnect_lock = asyncio.Lock()

        cur_task = asyncio.current_task()
        if cur_task is not None and cur_task is self._receive_task and self._disconnect_lock.locked():
            # Disconnect is already in progress by another task (e.g. GUI); prevent circular deadlock
            self._is_manual_disconnect = True
            self.auto_reconnect = False
            self.state.set_connected(False)
            self.state.set_reconnecting(False)
            self.state.set_connecting(False)
            self._webrtc_active = False
            return

        async with self._disconnect_lock:
            self._is_manual_disconnect = True
            self.auto_reconnect = False
            self.state.set_connected(False)
            self.state.set_reconnecting(False)
            self.state.set_connecting(False)
            self._webrtc_active = False

            if hasattr(self, "native_engine") and self.native_engine and self.native_engine.is_native_loaded:
                try:
                    self.native_engine.destroy_client_session()
                except Exception as e:
                    logger.debug(f"Error destroying client native session: {e}")

            cur_task = asyncio.current_task()

            # Cancel reconnect task if active and not current task
            if self._reconnect_task and not self._reconnect_task.done() and self._reconnect_task is not cur_task:
                self._reconnect_task.cancel()
                try:
                    await self._reconnect_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._reconnect_task = None

            # Cancel receive loop if active and not current task
            if self._receive_task and not self._receive_task.done() and self._receive_task is not cur_task:
                self._receive_task.cancel()
                try:
                    await self._receive_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._receive_task = None

            # Cancel auxiliary tasks (excluding current task to avoid self-cancellation during teardown!)
            other_tasks = [t for t in list(self._aux_tasks) if t is not cur_task and not t.done()]
            for task in other_tasks:
                task.cancel()
            if other_tasks:
                try:
                    await asyncio.gather(*other_tasks, return_exceptions=True)
                except Exception:
                    pass

            self._aux_tasks.clear()

            if self.websocket:
                try:
                    await self.websocket.close()
                except Exception:
                    pass
                self.websocket = None
            self._set_status("Disconnesso.")
