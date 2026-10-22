import os
import sys
from pathlib import Path

# Ensure repository root is in sys.path
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

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
from src.ota import get_app_version, compute_file_sha256, get_host_dist_executable

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
        self._frame_ack_event = asyncio.Event()
        self._frame_ack_event.set()
        self._in_flight_frame = False

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

        elif msg_type == "FRAME_ACK":
            self._in_flight_frame = False
            self._frame_ack_event.set()
            return None

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

        elif msg_type == "SET_QUALITY":
            quality = int(payload.get("quality", 70))
            if not self.capture:
                self.capture = DesktopCapture(quality=quality)
            else:
                self.capture.quality = quality
            return ProtocolMessage.serialize("ACK", {"status": "quality_set", "quality": quality})

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

        elif msg_type == "OTA_CHECK":
            exe_path = get_host_dist_executable()
            if exe_path and exe_path.exists():
                sha = compute_file_sha256(exe_path)
                return ProtocolMessage.serialize("OTA_INFO", {
                    "available": True,
                    "version": get_app_version(),
                    "filesize": exe_path.stat().st_size,
                    "sha256": sha
                })
            return ProtocolMessage.serialize("OTA_INFO", {
                "available": False,
                "version": get_app_version()
            })

        return None

    def _execute_input(self, cmd: dict):
        action = cmd.get("type", "")
        mon_bounds = self.capture.get_active_monitor_bounds() if self.capture else None
        virt_bounds = self.capture.get_virtual_desktop_bounds() if self.capture else None

        nx = cmd.get("x")
        ny = cmd.get("y")
        btn = cmd.get("button", "left")

        if action == "mouse_move":
            if nx is not None and ny is not None:
                self.input_handler.move_mouse(nx, ny, monitor_bounds=mon_bounds, virtual_bounds=virt_bounds)
        elif action == "mouse_down":
            self.input_handler.mouse_down(btn, norm_x=nx, norm_y=ny, monitor_bounds=mon_bounds, virtual_bounds=virt_bounds)
        elif action == "mouse_up":
            self.input_handler.mouse_up(btn, norm_x=nx, norm_y=ny, monitor_bounds=mon_bounds, virtual_bounds=virt_bounds)
        elif action == "mouse_click":
            if nx is not None and ny is not None:
                self.input_handler.move_mouse(nx, ny, monitor_bounds=mon_bounds, virtual_bounds=virt_bounds)
            self.input_handler.mouse_down(btn)
            self.input_handler.mouse_up(btn)
        elif action == "mouse_double_click":
            if nx is not None and ny is not None:
                self.input_handler.move_mouse(nx, ny, monitor_bounds=mon_bounds, virtual_bounds=virt_bounds)
            self.input_handler.mouse_down(btn)
            self.input_handler.mouse_up(btn)
            self.input_handler.mouse_down(btn)
            self.input_handler.mouse_up(btn)
        elif action == "mouse_wheel":
            self.input_handler.mouse_wheel(cmd.get("delta", 0), norm_x=nx, norm_y=ny,
                                          monitor_bounds=mon_bounds, virtual_bounds=virt_bounds)
        elif action == "key_down":
            self.input_handler.key_down(cmd.get("vk", 0))
        elif action == "key_up":
            self.input_handler.key_up(cmd.get("vk", 0))
        elif action == "unicode_char":
            self.input_handler.send_unicode_char(cmd.get("char", ""))
        elif action == "special":
            self.input_handler.send_special(cmd.get("command", ""))

    async def run_capture_stream(self, websocket, session_key: bytes):
        """Streaming loop: captures screen and sends encrypted JPEG frames with change detection and flow control."""
        if not self.capture:
            self.capture = DesktopCapture(quality=55)
        else:
            self.capture._last_thumb = None

        target_interval = 1.0 / 30.0 # 30 FPS target
        frame_counter = 0
        self._in_flight_frame = False
        self._frame_ack_event.set()

        while self.running:
            # Check connection state across modern websockets versions
            ws_state = getattr(websocket, "state", None)
            if ws_state is not None and getattr(ws_state, "name", "") != "OPEN":
                break
            if ws_state is None and not getattr(websocket, "open", True):
                break

            # Backpressure / Flow control: wait for client to finish processing previous frame
            # This completely eliminates WebSocket bufferbloat and multi-second cursor lag!
            if self._in_flight_frame:
                try:
                    await asyncio.wait_for(self._frame_ack_event.wait(), timeout=0.06)
                except asyncio.TimeoutError:
                    pass
                self._in_flight_frame = False
                self._frame_ack_event.clear()

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
                force_keyframe = (frame_counter == 1 or frame_counter % 30 == 0)
                jpeg_bytes, size, cur = self.capture.capture_frame(only_if_changed=(not force_keyframe))
                if jpeg_bytes:
                    encrypted = self.security.encrypt_packet(jpeg_bytes, session_key)
                    meta = {"w": size[0], "h": size[1], "cx": cur[0], "cy": cur[1], "fid": frame_counter}
                    msg = ProtocolMessage.serialize("FRAME", meta, binary_data=encrypted)
                    self._in_flight_frame = True
                    self._frame_ack_event.clear()
                    await websocket.send(msg)
            except Exception as e:
                logger.error(f"Capture error: {e}")
                break

            elapsed = time.time() - start_t
            sleep_t = max(0.001, target_interval - elapsed)
            await asyncio.sleep(sleep_t)

    async def _stream_ota_update(self, websocket, session_key: Optional[bytes]):
        """Stream latest compiled executable to client over websocket."""
        exe_path = get_host_dist_executable()
        if not exe_path or not exe_path.exists():
            await websocket.send(ProtocolMessage.serialize("OTA_ERROR", {"error": "Nessun eseguibile trovato su host"}))
            return

        total_size = exe_path.stat().st_size
        sha = compute_file_sha256(exe_path)
        await websocket.send(ProtocolMessage.serialize("OTA_START", {"filesize": total_size, "sha256": sha}))

        sent = 0
        with open(exe_path, "rb") as f:
            while chunk := f.read(65536):
                enc = self.security.encrypt_packet(chunk, session_key) if session_key else chunk
                msg = ProtocolMessage.serialize("OTA_CHUNK", {"offset": sent, "length": len(chunk)}, binary_data=enc)
                await websocket.send(msg)
                sent += len(chunk)
                await asyncio.sleep(0.001)

        await websocket.send(ProtocolMessage.serialize("OTA_FINISH", {"filesize": sent, "sha256": sha}))

    async def _broker_listener(self):
        """Persistent connection to signaling broker with auto-reconnect."""
        clean_id = self.device_id.replace(" ", "")
        stream_task = None
        while self.running:
            try:
                async with websockets.connect(self.broker_url, max_size=16 * 1024 * 1024) as ws:
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
                            self.security._load_config()
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

                        elif msg_type == "OTA_REQUEST":
                            asyncio.create_task(self._stream_ota_update(ws, session_state.get("session_key")))

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

        # Start LAN UDP discovery beacon so clients on same Wi-Fi/LAN find this host automatically
        try:
            from src.discovery import LANBeaconBroadcaster
            self._broadcaster = LANBeaconBroadcaster(self.device_id)
            self._broadcaster.start()
        except Exception as e:
            logger.debug(f"Discovery broadcaster skipped: {e}")

        self._listener_task = asyncio.create_task(self._broker_listener())
        try:
            await self._listener_task
        except asyncio.CancelledError:
            pass

    def stop(self):
        self.running = False
        self.prevent_sleep(False)
        if hasattr(self, "_broadcaster") and self._broadcaster:
            try:
                self._broadcaster.stop()
            except Exception:
                pass
            self._broadcaster = None
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
        if self.capture:
            self.capture.close()
            self.capture = None
        logger.info("Host Daemon stopped")

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="AetherDesk Host Daemon - Unattended Remote Desktop Service",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python src/host_daemon.py
  python src/host_daemon.py --broker ws://127.0.0.1:8765
  python src/host_daemon.py --status
        """
    )
    parser.add_argument("-b", "--broker", default="ws://127.0.0.1:8765", help="Signaling broker WebSocket URL (default: ws://127.0.0.1:8765)")
    parser.add_argument("-c", "--config-dir", default=None, help="Directory for persistent credentials and settings")
    parser.add_argument("--headless", action="store_true", help="Run without UI hooks")
    parser.add_argument("-s", "--status", action="store_true", help="Display host status, device ID, and exit")
    parser.add_argument("-v", "--version", action="version", version=f"AetherDesk Host Daemon v{get_app_version()}")
    args = parser.parse_args()

    if args.status:
        sec = SecurityManager(config_dir=args.config_dir)
        d_id = sec.get_or_create_device_id()
        has_pwd = bool(sec.config.get("pwd_hash"))
        print(f"AetherDesk Host Status:")
        print(f"  Device ID: {d_id}")
        print(f"  Password configured: {'Yes' if has_pwd else 'No (Default: default_password)'}")
        print(f"  Broker target: {args.broker}")
        sys.exit(0)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    daemon = HostDaemon(config_dir=args.config_dir, headless=args.headless, broker_url=args.broker)
    try:
        asyncio.run(daemon.start())
    except KeyboardInterrupt:
        daemon.stop()
