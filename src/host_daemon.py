import os
import json
import time
import ctypes
import asyncio
import logging
from typing import Dict, Any, Optional
import websockets

from src.capture import DesktopCapture
from src.input_handler import InputHandler
from src.security import SecurityManager
from src.protocol import ProtocolMessage

# Win32 Power Flags
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

logger = logging.getLogger("HostDaemon")

class HostDaemon:
    """Core daemon managing screen capture, input injection, sleep prevention and sessions."""

    def __init__(self, config_dir: str = None, headless: bool = False, broker_url: str = "ws://127.0.0.1:8765"):
        self.headless = headless
        self.broker_url = broker_url
        self.security = SecurityManager(config_dir=config_dir)
        self.input_handler = InputHandler()
        self.capture: Optional[DesktopCapture] = None
        self.device_id = self.security.get_or_create_device_id()

        self.running = False
        self.connected_clients: Dict[str, Any] = {}
        self._sleep_prevented = False

    def prevent_sleep(self, enable: bool = True) -> bool:
        """Prevent Windows from sleeping or turning off the screen while unattended."""
        try:
            kernel32 = ctypes.windll.kernel32
            if enable:
                res = kernel32.SetThreadExecutionState(
                    ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
                )
                self._sleep_prevented = (res != 0)
                return self._sleep_prevented
            else:
                kernel32.SetThreadExecutionState(ES_CONTINUOUS)
                self._sleep_prevented = False
                return True
        except Exception as e:
            logger.warning(f"Failed to set sleep state: {e}")
            return False

    def get_status(self) -> dict:
        return {
            "device_id": self.device_id,
            "running": self.running,
            "connected_clients": len(self.connected_clients),
            "sleep_prevention": self._sleep_prevented,
        }

    def handle_client_message(self, msg_type: str, payload: dict, binary: Optional[bytes], session_state: dict) -> Optional[bytes]:
        """Process incoming client message, authenticate or execute input."""
        if msg_type == "AUTH_REQUEST":
            salt_hex = payload.get("salt", "")
            salt = bytes.fromhex(salt_hex) if salt_hex else b"salt"
            # In E2EE mode, host computes the session key using its configured password
            password = self.security.config.get("password_plain") # if set in session, or derived
            # We derive session key from the host password hash salt
            pwd_salt = self.security.config.get("pwd_salt", "")
            if pwd_salt:
                derived_key = self.security.derive_session_key(pwd_salt, salt)
            else:
                derived_key = self.security.derive_session_key("default_password", salt)
            session_state["session_key"] = derived_key
            session_state["authenticated"] = True

            resp = ProtocolMessage.serialize("AUTH_OK", {"status": "authenticated", "device_id": self.device_id})
            return resp

        elif msg_type == "INPUT":
            key = session_state.get("session_key")
            if not key or not binary:
                return None
            try:
                decrypted = self.security.decrypt_packet(binary, key)
                cmd = json.loads(decrypted.decode("utf-8"))
                self._execute_input(cmd)
                return ProtocolMessage.serialize("ACK", {"status": "ok"})
            except Exception as e:
                logger.error(f"Failed to decrypt/execute input: {e}")
                return None

        return None

    def _execute_input(self, cmd: dict):
        action = cmd.get("type", "")
        if action == "mouse_move":
            self.input_handler.move_mouse(cmd.get("x", 0.0), cmd.get("y", 0.0))
        elif action == "mouse_down":
            self.input_handler.mouse_down(cmd.get("button", "left"))
        elif action == "mouse_up":
            self.input_handler.mouse_up(cmd.get("button", "left"))
        elif action == "mouse_wheel":
            self.input_handler.mouse_wheel(cmd.get("delta", 0))
        elif action == "key_down":
            self.input_handler.key_down(cmd.get("vk", 0))
        elif action == "key_up":
            self.input_handler.key_up(cmd.get("vk", 0))
        elif action == "special":
            self.input_handler.send_special(cmd.get("command", ""))

    async def run_capture_stream(self, websocket, session_key: bytes):
        """Streaming loop: captures screen and sends encrypted JPEG frames."""
        if not self.capture:
            self.capture = DesktopCapture(quality=70)

        target_interval = 1.0 / 30.0 # 30 FPS
        while self.running and websocket.open:
            start_t = time.time()
            try:
                jpeg_bytes, size, cur = self.capture.capture_frame()
                if jpeg_bytes:
                    encrypted = self.security.encrypt_packet(jpeg_bytes, session_key)
                    meta = {"w": size[0], "h": size[1], "cx": cur[0], "cy": cur[1]}
                    msg = ProtocolMessage.serialize("FRAME", meta, binary_data=encrypted)
                    await websocket.send(msg)
            except Exception as e:
                logger.error(f"Capture error: {e}")
                break

            elapsed = time.time() - start_t
            sleep_t = max(0.001, target_interval - elapsed)
            await asyncio.sleep(sleep_t)

    async def start(self):
        """Start daemon loop, prevent sleep, and connect to signaling broker."""
        self.running = True
        self.prevent_sleep(True)
        logger.info(f"Host Daemon started for Device ID: {self.device_id}")

    def stop(self):
        self.running = False
        self.prevent_sleep(False)
        if self.capture:
            self.capture.close()
            self.capture = None
        logger.info("Host Daemon stopped")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    daemon = HostDaemon()
    try:
        asyncio.run(daemon.start())
    except KeyboardInterrupt:
        daemon.stop()
