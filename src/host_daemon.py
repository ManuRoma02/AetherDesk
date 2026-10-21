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
from src.transfer import ClipboardManager, FileTransferManager

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
        self.clipboard = ClipboardManager()
        self.file_transfer = FileTransferManager()
        self.capture: Optional[DesktopCapture] = None
        self.device_id = self.security.get_or_create_device_id()

        self.running = False
        self.connected_clients: Dict[str, Any] = {}
        self._sleep_prevented = False
        self.websocket = None
        self._listener_task: Optional[asyncio.Task] = None

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

    def set_privacy_mode(self, enable: bool) -> bool:
        """Turn physical monitor off (privacy mode) or on."""
        try:
            user32 = ctypes.windll.user32
            # WM_SYSCOMMAND = 0x0112, SC_MONITORPOWER = 0xF170
            # lParam: 2 = power off, -1 = power on
            val = 2 if enable else -1
            # Use SendNotifyMessageW or PostMessageW to avoid blocking on unresponsive top-level windows
            user32.SendNotifyMessageW(0xFFFF, 0x0112, 0xF170, val)
            return True
        except Exception as e:
            logger.warning(f"Failed to toggle monitor power: {e}")
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
            master_hex = self.security.config.get("master_key", "")
            if master_hex:
                derived_key = self.security.derive_session_key(bytes.fromhex(master_hex), salt)
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

        elif msg_type == "CLIPBOARD":
            key = session_state.get("session_key")
            if key and binary:
                try:
                    decrypted = self.security.decrypt_packet(binary, key)
                    text = decrypted.decode("utf-8")
                    self.clipboard.set_text(text, update_last=True)
                    return ProtocolMessage.serialize("ACK", {"status": "clipboard_set"})
                except Exception as e:
                    logger.error(f"Clipboard sync error: {e}")

        elif msg_type == "SWITCH_MONITOR":
            mon_id = payload.get("monitor_id", 0)
            if not self.capture:
                self.capture = DesktopCapture(quality=70)
            self.capture.set_monitor(mon_id)
            return ProtocolMessage.serialize("ACK", {"status": "monitor_switched", "monitor_id": mon_id})

        elif msg_type == "GET_MONITORS":
            if not self.capture:
                self.capture = DesktopCapture(quality=70)
            mons = self.capture.get_monitors_list()
            return ProtocolMessage.serialize("MONITORS_LIST", {"monitors": mons})

        elif msg_type == "PRIVACY_MODE":
            enabled = payload.get("enabled", False)
            res = self.set_privacy_mode(enabled)
            return ProtocolMessage.serialize("ACK", {"status": "privacy_mode", "enabled": enabled, "result": res})

        elif msg_type == "FILE_START":
            file_id = payload.get("file_id")
            filename = payload.get("filename")
            filesize = payload.get("filesize", 0)
            self.file_transfer.start_receiving(file_id, filename, filesize)
            return ProtocolMessage.serialize("ACK", {"status": "file_started", "file_id": file_id})

        elif msg_type == "FILE_CHUNK":
            key = session_state.get("session_key")
            file_id = payload.get("file_id")
            offset = payload.get("offset", 0)
            if key and binary:
                chunk = self.security.decrypt_packet(binary, key)
                self.file_transfer.receive_chunk(file_id, offset, chunk)
                return ProtocolMessage.serialize("ACK", {"status": "chunk_received", "offset": offset})

        elif msg_type == "FILE_END":
            file_id = payload.get("file_id")
            saved = self.file_transfer.finish_receiving(file_id)
            return ProtocolMessage.serialize("FILE_SAVED", {"status": "ok", "saved_path": saved})

        return None

    def _execute_input(self, cmd: dict):
        action = cmd.get("type", "")
        mon_bounds = self.capture.get_active_monitor_bounds() if self.capture else None
        virt_bounds = self.capture.get_virtual_desktop_bounds() if self.capture else None

        if action == "mouse_move":
            self.input_handler.move_mouse(cmd.get("x", 0.0), cmd.get("y", 0.0),
                                          monitor_bounds=mon_bounds, virtual_bounds=virt_bounds)
        elif action == "mouse_down":
            if "x" in cmd and "y" in cmd:
                self.input_handler.move_mouse(cmd.get("x", 0.0), cmd.get("y", 0.0),
                                              monitor_bounds=mon_bounds, virtual_bounds=virt_bounds)
            self.input_handler.mouse_down(cmd.get("button", "left"))
        elif action == "mouse_up":
            if "x" in cmd and "y" in cmd:
                self.input_handler.move_mouse(cmd.get("x", 0.0), cmd.get("y", 0.0),
                                              monitor_bounds=mon_bounds, virtual_bounds=virt_bounds)
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
        """Streaming loop: captures screen and sends encrypted JPEG frames with change detection."""
        if not self.capture:
            self.capture = DesktopCapture(quality=70)

        target_interval = 1.0 / 30.0 # 30 FPS
        frame_counter = 0
        while self.running:
            # Check connection state across modern websockets versions
            ws_state = getattr(websocket, "state", None)
            if ws_state is not None and getattr(ws_state, "name", "") != "OPEN":
                break
            if ws_state is None and not getattr(websocket, "open", True):
                break

            start_t = time.time()
            try:
                # Check clipboard change
                if self.clipboard.has_changed():
                    clip_text = self.clipboard.get_text()
                    if clip_text:
                        enc_clip = self.security.encrypt_packet(clip_text.encode("utf-8"), session_key)
                        clip_msg = ProtocolMessage.serialize("CLIPBOARD", {}, binary_data=enc_clip)
                        await websocket.send(clip_msg)

                frame_counter += 1
                force_keyframe = (frame_counter % 30 == 0)
                jpeg_bytes, size, cur = self.capture.capture_frame(only_if_changed=(not force_keyframe))
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

    async def _broker_listener(self):
        """Persistent connection to signaling broker with auto-reconnect."""
        clean_id = self.device_id.replace(" ", "")
        stream_task = None
        while self.running:
            try:
                async with websockets.connect(self.broker_url) as ws:
                    self.websocket = ws
                    # Register this host with broker
                    reg_msg = ProtocolMessage.serialize("REGISTER_HOST", {
                        "device_id": clean_id,
                        "host_salt": self.security.config.get("pwd_salt", "")
                    })
                    await ws.send(reg_msg)
                    logger.info(f"Registered with signaling broker as {clean_id}")

                    session_state = {}

                    async for raw in ws:
                        if isinstance(raw, str):
                            raw = raw.encode("utf-8")
                        try:
                            msg_type, payload, binary = ProtocolMessage.deserialize(raw)
                        except Exception as e:
                            logger.warning(f"Failed to deserialize message: {e}")
                            continue

                        if msg_type == "CLIENT_ARRIVED":
                            salt_hex = payload.get("salt", "")
                            client_salt = bytes.fromhex(salt_hex) if salt_hex else b"salt"
                            master_hex = self.security.config.get("master_key", "")
                            if master_hex:
                                master_key = bytes.fromhex(master_hex)
                            else:
                                master_key = self.security.derive_master_key("default_password", b"default_salt")

                            session_key = self.security.derive_session_key(master_key, client_salt)
                            session_state["session_key"] = session_key
                            session_state["authenticated"] = True

                            if stream_task and not stream_task.done():
                                stream_task.cancel()
                            stream_task = asyncio.create_task(self.run_capture_stream(ws, session_key))

                        else:
                            resp_bytes = self.handle_client_message(msg_type, payload, binary, session_state)
                            if resp_bytes:
                                await ws.send(resp_bytes)

            except (websockets.exceptions.ConnectionClosed, OSError, Exception) as e:
                logger.warning(f"Broker connection error: {e}. Retrying in 3s...")
                if stream_task and not stream_task.done():
                    stream_task.cancel()
                if self.running:
                    await asyncio.sleep(3.0)

    async def start(self):
        """Start daemon loop, prevent sleep, and connect to signaling broker."""
        self.running = True
        self.prevent_sleep(True)
        logger.info(f"Host Daemon started for Device ID: {self.device_id}")
        self._listener_task = asyncio.create_task(self._broker_listener())
        try:
            await self._listener_task
        except asyncio.CancelledError:
            pass

    def stop(self):
        self.running = False
        self.prevent_sleep(False)
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
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
