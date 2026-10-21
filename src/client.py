import re
import json
import secrets
import asyncio
import logging
from typing import Optional, Callable
import websockets

from src.security import SecurityManager
from src.protocol import ProtocolMessage

logger = logging.getLogger("RemoteClient")

class ClientState:
    """Tracks state and validation for the client connection."""

    def __init__(self):
        self.is_connected = False
        self.is_connecting = False
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

    def set_connected(self, connected: bool, session_key: Optional[bytes] = None):
        self.is_connected = connected
        self.is_connecting = False
        self.session_key = session_key

class RemoteClient:
    """Network client for connecting to a remote host via ID & password."""

    def __init__(self, broker_url: str = "ws://127.0.0.1:8765"):
        self.broker_url = broker_url
        self.state = ClientState()
        self.security = SecurityManager()
        self.websocket = None
        self.on_frame_callback: Optional[Callable[[bytes, tuple[int, int], tuple[int, int]], None]] = None
        self.on_status_callback: Optional[Callable[[str], None]] = None

    def _set_status(self, text: str):
        if self.on_status_callback:
            self.on_status_callback(text)

    async def connect(self, device_id: str, password: str) -> bool:
        """Perform handshake and authenticate with remote host."""
        if not ClientState.validate_id(device_id):
            self._set_status("ID non valido. Inserire 9 cifre.")
            return False

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

        except websockets.exceptions.ConnectionClosed:
            self._set_status("Connessione interrotta dal computer remoto.")
        finally:
            self.state.set_connected(False)

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
