import re
import time
import json
import secrets
import asyncio
import logging
from typing import Optional, Callable
import websockets

import os
import tempfile
from pathlib import Path
from src.security import SecurityManager
from src.protocol import ProtocolMessage
from src.transfer import ClipboardManager, FileTransferManager
from src.ota import get_app_version, compute_file_sha256, apply_in_place_update

logger = logging.getLogger("RemoteClient")

class ClientSettings:
    """Manages persistent client settings like saved device ID, password, address book, and broker URL."""

    def __init__(self, config_dir: Optional[str] = None):
        if config_dir:
            self.config_dir = Path(config_dir)
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

    def __init__(self, broker_url: str = "ws://127.0.0.1:8765", auto_lan_discovery: bool = False):
        self.broker_url = broker_url
        self.auto_lan_discovery = auto_lan_discovery
        self.state = ClientState()
        self.security = SecurityManager()
        self.clipboard = ClipboardManager()
        self.file_transfer = FileTransferManager()
        self.websocket = None
        self.on_frame_callback: Optional[Callable[[bytes, tuple[int, int], tuple[int, int]], None]] = None
        self.on_status_callback: Optional[Callable[[str], None]] = None
        self.on_clipboard_callback: Optional[Callable[[str], None]] = None
        self.on_file_saved_callback: Optional[Callable[[str], None]] = None
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
        self.state.set_connecting(True)
        self._set_status(f"Connessione a {self.broker_url}...")

        try:
            # Enforce 4.0s connection timeout so the client never hangs indefinitely
            self.websocket = await asyncio.wait_for(
                websockets.connect(self.broker_url, open_timeout=4.0, max_size=16 * 1024 * 1024),
                timeout=5.0
            )
            # 1. Ask broker to connect to host ID
            salt = secrets.token_bytes(16)
            conn_msg = ProtocolMessage.serialize("CONNECT_CLIENT", {
                "device_id": clean_id,
                "salt": salt.hex()
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

            if msg_type == "HOST_FOUND":
                self.security.reset_replay_window()
                self._set_status("Computer trovato. Autenticazione...")
                # Derive session key with entered password and host salt
                host_salt_hex = payload.get("host_salt", "")
                host_salt = bytes.fromhex(host_salt_hex) if host_salt_hex else b""
                master_key = self.security.derive_master_key(password, host_salt) if host_salt else password.encode("utf-8")
                session_key = self.security.derive_session_key(master_key, salt)

                # Send authentication challenge token
                auth_token = self.security.encrypt_packet(b"AETHER_AUTH_V1", session_key)
                auth_req = ProtocolMessage.serialize("AUTH_REQUEST", {"salt": salt.hex(), "target_id": clean_id}, binary_data=auth_token)
                await self.websocket.send(auth_req)

                # Validate host response (drain any stale in-flight messages until AUTH_OK or AUTH_FAILED)
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
                            break
                        elif auth_type == "FRAME" and auth_bin:
                            try:
                                first_jpeg = self.security.decrypt_packet(auth_bin, session_key, check_replay=False)
                                break
                            except Exception:
                                pass
                except asyncio.TimeoutError:
                    pass

                self.state.set_connected(True, session_key)
                self.state.target_id = clean_id
                self._set_status("Connesso con successo!")

                # Start background receiver loop
                asyncio.create_task(self._receive_loop())
                # Query host for OTA update availability
                asyncio.create_task(self.check_ota_update())
                # Request list of available physical monitors on remote host
                asyncio.create_task(self.request_monitors_list())
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
        """Listen for incoming encrypted video frames from the host."""
        try:
            while self.state.is_connected and self.websocket:
                raw = await self.websocket.recv()
                msg_type, payload, binary = ProtocolMessage.deserialize(raw)

                if msg_type == "FRAME" and binary and self.state.session_key:
                    try:
                        t_recv = time.time()
                        # Backpressure / Flow control: send FRAME_ACK immediately to prevent bufferbloat
                        fid = payload.get("fid", 0)
                        ack_msg = ProtocolMessage.serialize("FRAME_ACK", {"fid": fid, "target_id": self.state.target_id})
                        asyncio.create_task(self.websocket.send(ack_msg))

                        t_dec_start = time.perf_counter_ns()
                        jpeg_bytes = self.security.decrypt_packet(binary, self.state.session_key, check_replay=False)
                        t_dec_ms = round((time.perf_counter_ns() - t_dec_start) / 1e6, 2)

                        size = (payload.get("w", 1920), payload.get("h", 1080))
                        cur = (payload.get("cx", 0), payload.get("cy", 0))
                        self.state.host_dims = size

                        telemetry = {
                            "fid": fid,
                            "t_send": payload.get("t_send", 0.0),
                            "t_recv": t_recv,
                            "cap_ms": payload.get("cap_ms", 0.0),
                            "enc_ms": payload.get("enc_ms", 0.0),
                            "dec_ms": t_dec_ms,
                            "size_bytes": len(binary)
                        }

                        if self.on_frame_callback:
                            try:
                                self.on_frame_callback(jpeg_bytes, size, cur, telemetry)
                            except TypeError:
                                self.on_frame_callback(jpeg_bytes, size, cur)
                        self._decrypt_failures = 0
                    except Exception as e:
                        logger.error(f"Frame decrypt error: {e}")
                        self._decrypt_failures = getattr(self, "_decrypt_failures", 0) + 1
                        if self._decrypt_failures >= 3:
                            self._set_status("Password errata o chiave di sessione non valida.")
                            if self.on_status_callback:
                                self.on_status_callback("Password errata. Impossibile decifrare il video.")
                            asyncio.create_task(self.disconnect())

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
                    expected_sha = payload.get("sha256", self._ota_sha256)
                    if self._ota_temp_file and Path(self._ota_temp_file).exists():
                        actual_sha = compute_file_sha256(self._ota_temp_file)
                        if expected_sha and actual_sha.lower() != expected_sha.lower():
                            if self.on_ota_complete_callback:
                                self.on_ota_complete_callback(False, f"Checksum SHA-256 non valido (Atteso: {expected_sha[:8]}..., Calcolato: {actual_sha[:8]}...).")
                        else:
                            ok, msg = apply_in_place_update(self._ota_temp_file, target_version=payload.get("version"))
                            if self.on_ota_complete_callback:
                                self.on_ota_complete_callback(ok, msg)
                    else:
                        if self.on_ota_complete_callback:
                            self.on_ota_complete_callback(False, "File OTA non trovato.")

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
                    self.state.set_connected(False)
                    self._set_status("Il computer remoto si e' disconnesso.")
                    if self.on_status_callback:
                        self.on_status_callback("Il computer remoto si e' disconnesso.")
                    break

        except (websockets.exceptions.ConnectionClosed, Exception) as e:
            logger.warning(f"Connection lost: {e}")
            if self.auto_reconnect and self.last_dev_id and not self.state.is_reconnecting:
                asyncio.create_task(self._attempt_reconnection())
            else:
                self.state.set_connected(False)
                self._set_status("Connessione interrotta dal computer remoto.")

    async def _attempt_reconnection(self):
        self.state.set_reconnecting(True)
        for attempt in range(1, self.max_reconnect_attempts + 1):
            self._set_status(f"Connessione persa. Riconnessione in corso... ({attempt}/{self.max_reconnect_attempts})")
            await asyncio.sleep(2.0)
            try:
                ok = await self.connect(self.last_dev_id, self.last_password)
                if ok:
                    self._set_status("Riconnesso con successo!")
                    return
            except Exception:
                pass
        self.state.set_reconnecting(False)
        self.state.set_connected(False)
        self._set_status("Impossibile riconnettersi al computer remoto.")

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
        """Encrypt and send mouse/keyboard input to host."""
        if not self.state.is_connected or not self.websocket or not self.state.session_key:
            return

        payload_bytes = json.dumps(input_dict).encode("utf-8")
        encrypted = self.security.encrypt_packet(payload_bytes, self.state.session_key)
        msg = ProtocolMessage.serialize("INPUT", {"target_id": self.state.target_id}, binary_data=encrypted)
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
        self.auto_reconnect = False
        self.state.set_connected(False)
        if self.websocket:
            await self.websocket.close()
            self.websocket = None
        self._set_status("Disconnesso.")
