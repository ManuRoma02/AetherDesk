import re
import json
import secrets
import asyncio
import logging
from typing import Optional, Callable
import websockets

import os
from pathlib import Path
from src.security import SecurityManager
from src.protocol import ProtocolMessage
from src.transfer import ClipboardManager, FileTransferManager

logger = logging.getLogger("RemoteClient")

class ClientSettings:
    """Manages persistent client settings like saved device ID and password."""

    def __init__(self, config_dir: Optional[str] = None):
        if config_dir:
            self.config_dir = Path(config_dir)
        else:
            appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
            self.config_dir = Path(appdata) / "RemoteDesktopClient"

        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_file = self.config_dir / "client_settings.json"

    def save(self, device_id: str, password: str, remember: bool = True):
        data = {
            "remember": remember,
            "device_id": device_id if remember else "",
            "password": password if remember else ""
        }
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def load(self) -> tuple[str, str, bool]:
        if not self.config_file.exists():
            return ("", "", False)
        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return (
                    data.get("device_id", ""),
                    data.get("password", ""),
                    data.get("remember", False)
                )
        except Exception:
            return ("", "", False)

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
    def validate_id(device_id: str) -> bool:
        cleaned = ClientState.clean_id(device_id)
        return len(cleaned) == 9 and cleaned.isdigit()

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

    def __init__(self, broker_url: str = "ws://127.0.0.1:8765"):
        self.broker_url = broker_url
        self.state = ClientState()
        self.security = SecurityManager()
        self.clipboard = ClipboardManager()
        self.file_transfer = FileTransferManager()
        self.websocket = None
        self.on_frame_callback: Optional[Callable[[bytes, tuple[int, int], tuple[int, int]], None]] = None
        self.on_status_callback: Optional[Callable[[str], None]] = None
        self.last_dev_id = ""
        self.last_password = ""
        self.auto_reconnect = True
        self.max_reconnect_attempts = 10

    def _set_status(self, text: str):
        if self.on_status_callback:
            self.on_status_callback(text)

    async def connect(self, device_id: str, password: str) -> bool:
        """Perform handshake and authenticate with remote host."""
        if not ClientState.validate_id(device_id):
            self._set_status("ID non valido. Inserire 9 cifre.")
            return False

        self.last_dev_id = device_id
        self.last_password = password
        clean_id = ClientState.clean_id(device_id)
        self.state.set_connecting(True)
        self._set_status("Connessione al server di segnalazione...")

        try:
            self.websocket = await websockets.connect(self.broker_url)
            # 1. Ask broker to connect to host ID
            salt = secrets.token_bytes(16)
            conn_msg = ProtocolMessage.serialize("CONNECT_CLIENT", {
                "device_id": clean_id,
                "salt": salt.hex()
            })
            await self.websocket.send(conn_msg)

            # Wait for response
            resp_raw = await asyncio.wait_for(self.websocket.recv(), timeout=5.0)
            msg_type, payload, _ = ProtocolMessage.deserialize(resp_raw)

            if msg_type == "HOST_NOT_FOUND":
                self.state.set_connecting(False)
                self._set_status("Computer remoto non trovato o offline.")
                await self.websocket.close()
                return False

            if msg_type == "HOST_FOUND":
                self._set_status("Computer trovato. Autenticazione...")
                # Derive session key with entered password
                session_key = self.security.derive_session_key(password, salt)
                self.state.set_connected(True, session_key)
                self.state.target_id = clean_id
                self._set_status("Connesso con successo!")

                # Start background receiver loop
                asyncio.create_task(self._receive_loop())
                return True

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
                        jpeg_bytes = self.security.decrypt_packet(binary, self.state.session_key)
                        size = (payload.get("w", 1920), payload.get("h", 1080))
                        cur = (payload.get("cx", 0), payload.get("cy", 0))
                        self.state.host_dims = size
                        if self.on_frame_callback:
                            self.on_frame_callback(jpeg_bytes, size, cur)
                    except Exception as e:
                        logger.error(f"Frame decrypt error: {e}")

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

    async def send_file(self, file_path: str, progress_callback=None):
        """Stream a file chunk by chunk to the host."""
        if not self.state.is_connected or not self.websocket or not self.state.session_key:
            return False

        meta, chunks = self.file_transfer.prepare_file_for_sending(file_path)
        meta["target_id"] = self.state.target_id

        # Send start
        start_msg = ProtocolMessage.serialize("FILE_START", meta)
        await self.websocket.send(start_msg)

        total_bytes = meta["filesize"]
        sent_bytes = 0

        # Send chunks
        for offset, chunk_data in chunks:
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
            await asyncio.sleep(0.005) # Small yield

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

    async def disconnect(self):
        self.state.set_connected(False)
        if self.websocket:
            await self.websocket.close()
            self.websocket = None
        self._set_status("Disconnesso.")
