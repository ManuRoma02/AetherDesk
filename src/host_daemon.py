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

from src.capture import DesktopCapture, _ensure_interactive_desktop
_ensure_interactive_desktop()
from src.input_handler import InputHandler
from src.security import SecurityManager
from src.protocol import ProtocolMessage
from src.transfer import ClipboardManager, FileTransferManager
from src.ota import get_app_version, compute_file_sha256, get_host_dist_executable, is_newer_version

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
        self._streaming_active = False

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
        client_ip = session_state.get("client_ip", "peer")

        if msg_type == "AUTH_REQUEST":
            if self.security.is_rate_limited(client_ip):
                return ProtocolMessage.serialize("AUTH_FAILED", {"error": "Troppi tentativi falliti. Riprova tra 60 secondi."})

            self.security.reset_replay_window()
            salt_hex = payload.get("salt", "")
            salt = bytes.fromhex(salt_hex) if salt_hex else b"salt"
            stored_master = self.security.get_master_key()
            if stored_master:
                derived_key = self.security.derive_session_key(stored_master, salt)
            else:
                default_pwd = self.security.get_password_display()
                derived_key = self.security.derive_session_key(default_pwd, salt)

            if not binary:
                self.security.record_failed_attempt(client_ip)
                session_state["authenticated"] = False
                return ProtocolMessage.serialize("AUTH_FAILED", {"error": "Token di autenticazione mancante"})

            try:
                dec = self.security.decrypt_packet(binary, derived_key)
                if dec != b"AETHER_AUTH_V1":
                    self.security.record_failed_attempt(client_ip)
                    session_state["authenticated"] = False
                    return ProtocolMessage.serialize("AUTH_FAILED", {"error": "Credenziali non valide"})
            except Exception:
                self.security.record_failed_attempt(client_ip)
                session_state["authenticated"] = False
                return ProtocolMessage.serialize("AUTH_FAILED", {"error": "Password errata"})

            self.security.reset_attempts(client_ip)
            self.security.reset_replay_window()
            session_state["session_key"] = derived_key
            session_state["authenticated"] = True

            resp = ProtocolMessage.serialize("AUTH_OK", {"status": "authenticated", "device_id": self.device_id})
            return resp

        elif msg_type == "FRAME_ACK":
            self._in_flight_frame = False
            self._frame_ack_event.set()
            return None

        # Guard all state-altering or information-disclosing commands behind authentication
        msg_upper = (msg_type or "").upper()
        if msg_upper in ("INPUT", "MOUSE", "TOUCH", "POINTER", "KEYBOARD", "CLIPBOARD", "SWITCH_MONITOR", "GET_MONITORS", "PRIVACY_MODE", "SET_QUALITY", "FILE_START", "FILE_CHUNK", "FILE_END"):
            if not session_state.get("authenticated"):
                return ProtocolMessage.serialize("AUTH_FAILED", {"error": "Autenticazione richiesta"})

        if msg_upper in ("INPUT", "MOUSE", "TOUCH", "POINTER", "KEYBOARD"):
            key = session_state.get("session_key")
            cmd = None
            if binary:
                if key:
                    try:
                        decrypted = self.security.decrypt_packet(binary, key)
                        cmd = json.loads(decrypted.decode("utf-8"))
                    except Exception:
                        pass
                if cmd is None:
                    try:
                        cmd = json.loads(binary.decode("utf-8"))
                    except Exception as e:
                        logger.error(f"Failed to parse input binary packet: {e}")
            if cmd is None and payload:
                if isinstance(payload, dict):
                    if "cmd" in payload and isinstance(payload["cmd"], dict):
                        cmd = payload["cmd"]
                    else:
                        cmd = payload

            if cmd:
                self._execute_input(cmd)
                return ProtocolMessage.serialize("ACK", {"status": "ok"})
            return None

        elif msg_type == "CLIPBOARD":
            key = session_state.get("session_key")
            text = payload.get("text") or payload.get("data")
            if not text and key and binary:
                try:
                    decrypted = self.security.decrypt_packet(binary, key)
                    text = decrypted.decode("utf-8")
                except Exception as e:
                    logger.error(f"Clipboard decrypt error: {e}")
            if text:
                self.clipboard.set_text(text, update_last=True)
                return ProtocolMessage.serialize("ACK", {"status": "clipboard_set"})
            return None

        elif msg_type == "SWITCH_MONITOR":
            mon_id = payload.get("monitor_id", 0)
            if not self.capture:
                self.capture = DesktopCapture(quality=55)
            self.capture.set_monitor(mon_id)
            return ProtocolMessage.serialize("ACK", {"status": "monitor_switched", "monitor_id": mon_id})

        elif msg_type == "GET_MONITORS":
            if not self.capture:
                self.capture = DesktopCapture(quality=55)
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
            client_ver = payload.get("client_version", "")
            exe_path = get_host_dist_executable()
            current_ver = get_app_version()
            has_newer = is_newer_version(current_ver, client_ver) if client_ver else False
            if exe_path and exe_path.exists() and has_newer:
                sha = compute_file_sha256(exe_path)
                return ProtocolMessage.serialize("OTA_INFO", {
                    "available": True,
                    "version": current_ver,
                    "filesize": exe_path.stat().st_size,
                    "sha256": sha
                })
            return ProtocolMessage.serialize("OTA_INFO", {
                "available": False,
                "version": current_ver
            })

        return None

    def _execute_input(self, cmd: dict):
        if not isinstance(cmd, dict):
            return

        action = str(
            cmd.get("type", "")
            or cmd.get("action", "")
            or cmd.get("event", "")
            or cmd.get("name", "")
            or ""
        ).lower().strip()
        mon_bounds = self.capture.get_active_monitor_bounds() if self.capture else None
        virt_bounds = self.capture.get_virtual_desktop_bounds() if self.capture else None

        nx = cmd.get("x") if cmd.get("x") is not None else cmd.get("norm_x")
        if nx is None:
            nx = cmd.get("clientX") or cmd.get("touchX") or cmd.get("px")
        ny = cmd.get("y") if cmd.get("y") is not None else cmd.get("norm_y")
        if ny is None:
            ny = cmd.get("clientY") or cmd.get("touchY") or cmd.get("py")

        dx = cmd.get("dx")
        dy = cmd.get("dy")
        btn = cmd.get("button", "left")

        # Pointer down/up flag support (e.g. from mobile gesture recognizers)
        if "down" in cmd and not action:
            action = "mouse_down" if cmd["down"] else "mouse_up"

        if not action and nx is not None and ny is not None:
            action = "click" if ("button" in cmd or "click" in cmd) else "mouse_move"

        # 1. Mouse Move / Drag / Trackpad
        if action in ("mouse_move", "move", "touch_move", "cursor_move"):
            if dx is not None and dy is not None and (nx is None or ny is None):
                self.input_handler.move_mouse_relative(float(dx), float(dy))
            elif nx is not None and ny is not None:
                self.input_handler.move_mouse(float(nx), float(ny), monitor_bounds=mon_bounds, virtual_bounds=virt_bounds)

        # 2. Mouse / Touch Down
        elif action in ("mouse_down", "touch_down", "down", "pointer_down"):
            self.input_handler.mouse_down(btn, norm_x=nx, norm_y=ny, monitor_bounds=mon_bounds, virtual_bounds=virt_bounds)

        # 3. Mouse / Touch Up
        elif action in ("mouse_up", "touch_up", "up", "pointer_up"):
            self.input_handler.mouse_up(btn, norm_x=nx, norm_y=ny, monitor_bounds=mon_bounds, virtual_bounds=virt_bounds)

        # 4. Click / Tap (with physical down->up delay for Windows GUI)
        elif action in ("mouse_click", "click", "tap", "touch_tap", "pointer_click"):
            self.input_handler.mouse_click(btn, norm_x=nx, norm_y=ny, monitor_bounds=mon_bounds, virtual_bounds=virt_bounds)

        # 5. Right Click / Secondary / Context Menu
        elif action in ("right_click", "context_menu", "secondary_click"):
            self.input_handler.mouse_click("right", norm_x=nx, norm_y=ny, monitor_bounds=mon_bounds, virtual_bounds=virt_bounds)

        # 6. Double Click / Double Tap
        elif action in ("mouse_double_click", "double_click", "double_tap"):
            self.input_handler.mouse_double_click(btn, norm_x=nx, norm_y=ny, monitor_bounds=mon_bounds, virtual_bounds=virt_bounds)

        # 7. Mouse Wheel / Scroll
        elif action in ("mouse_wheel", "wheel", "scroll"):
            delta = cmd.get("delta") or cmd.get("dy", 0)
            self.input_handler.mouse_wheel(delta, norm_x=nx, norm_y=ny, monitor_bounds=mon_bounds, virtual_bounds=virt_bounds)

        # 8. Keyboard Key Down
        elif action in ("key_down", "keydown"):
            vk = cmd.get("vk", 0)
            char = cmd.get("char") or cmd.get("text")
            if (not vk or vk == 0) and char:
                self.input_handler.send_unicode_char(char)
            elif vk:
                self.input_handler.key_down(int(vk))
            elif cmd.get("key"):
                k = str(cmd.get("key")).lower()
                from src.input_handler import VK_MAP
                if k in VK_MAP:
                    self.input_handler.key_down(VK_MAP[k])
                else:
                    self.input_handler.send_unicode_char(cmd.get("key"))

        # 9. Keyboard Key Up
        elif action in ("key_up", "keyup"):
            vk = cmd.get("vk", 0)
            if vk:
                self.input_handler.key_up(int(vk))
            elif cmd.get("key"):
                k = str(cmd.get("key")).lower()
                from src.input_handler import VK_MAP
                if k in VK_MAP:
                    self.input_handler.key_up(VK_MAP[k])

        # 10. Unicode Text / Typing (Soft keyboard / Word input)
        elif action in ("unicode_char", "char", "text", "type_text", "string", "input_text"):
            text_val = cmd.get("text") or cmd.get("char") or cmd.get("string") or ""
            if text_val:
                self.input_handler.send_unicode_char(text_val)

        # 11. Special Shortcuts / Combos / Hotkeys (Ctrl+C, Ctrl+V, Win+D, etc.)
        elif action in ("special", "command", "shortcut", "hotkey", "key_combination", "combo"):
            cmd_str = cmd.get("command") or cmd.get("shortcut") or cmd.get("key") or ""
            keys_list = cmd.get("keys") or cmd.get("vk_list")
            if keys_list:
                if isinstance(keys_list[0], int):
                    self.input_handler.send_key_combination(keys_list)
                else:
                    self.input_handler.send_key_combination_by_names(keys_list)
            elif cmd_str:
                self.input_handler.send_special(cmd_str)

        # 12. Direct Clipboard Set via Input
        elif action in ("clipboard", "set_clipboard", "copy_text", "paste_text"):
            text_val = cmd.get("text") or cmd.get("data") or ""
            if text_val:
                self.clipboard.set_text(text_val, update_last=True)

    async def run_capture_stream(self, websocket, session_key: bytes):
        """Streaming loop: captures screen and sends encrypted JPEG frames with zero-blocking threadpool & flow control."""
        if not self.capture:
            self.capture = DesktopCapture(quality=50)
        else:
            self.capture.switch_desktop()
            self.capture._last_thumb = None

        target_interval = 1.0 / 30.0 # 30 FPS target
        frame_counter = 0
        self._in_flight_frame = False
        self._streaming_active = True
        self._frame_ack_event.set()

        while self.running and self._streaming_active:
            # Check connection state across modern websockets versions
            ws_state = getattr(websocket, "state", None)
            if ws_state is not None and getattr(ws_state, "name", "") != "OPEN":
                break
            if ws_state is None and not getattr(websocket, "open", True):
                break

            # Adaptive Backpressure: if previous frame hasn't been ACKed, wait briefly or skip frame
            if self._in_flight_frame:
                try:
                    await asyncio.wait_for(self._frame_ack_event.wait(), timeout=0.12)
                except asyncio.TimeoutError:
                    pass
                finally:
                    self._in_flight_frame = False
                    self._frame_ack_event.clear()

            start_t = time.time()
            t_cap_start = time.perf_counter_ns()
            try:
                # Fast sequence-number check for clipboard changes (zero-lock O(1))
                if frame_counter % 10 == 0:
                    try:
                        if self.clipboard.has_changed():
                            clip_text = self.clipboard.get_text()
                            if clip_text:
                                enc_clip = self.security.encrypt_packet(clip_text.encode("utf-8"), session_key)
                                clip_msg = ProtocolMessage.serialize("CLIPBOARD", {}, binary_data=enc_clip)
                                await websocket.send(clip_msg)
                    except Exception:
                        pass

                frame_counter += 1
                force_keyframe = (frame_counter == 1 or frame_counter % 90 == 0)

                # Offload heavy screen capture and image compression to background thread (keeps event loop unblocked)
                cap_res = await asyncio.to_thread(self.capture.capture_frame, only_if_changed=(not force_keyframe))
                jpeg_bytes, size, cur = cap_res
                t_cap_done = time.perf_counter_ns()

                if jpeg_bytes:
                    t_enc_start = time.perf_counter_ns()
                    encrypted = await asyncio.to_thread(self.security.encrypt_packet, jpeg_bytes, session_key)
                    t_enc_done = time.perf_counter_ns()

                    meta = {
                        "w": size[0],
                        "h": size[1],
                        "cx": cur[0],
                        "cy": cur[1],
                        "fid": frame_counter,
                        "t_send": time.time(),
                        "cap_ms": round((t_cap_done - t_cap_start) / 1e6, 2),
                        "enc_ms": round((t_enc_done - t_enc_start) / 1e6, 2)
                    }
                    msg = ProtocolMessage.serialize("FRAME", meta, binary_data=encrypted)
                    self._in_flight_frame = True
                    self._frame_ack_event.clear()
                    await websocket.send(msg)
            except Exception as e:
                logger.error(f"Capture error in run_capture_stream: {e}")
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
                            if msg_type != "INPUT":
                                logger.info(f"Host received message: {msg_type}")
                            else:
                                logger.debug(f"Host received message: {msg_type}")
                        except Exception as e:
                            logger.warning(f"Failed to deserialize message: {e}")
                            continue

                        if msg_type == "CLIENT_ARRIVED":
                            self._streaming_active = False
                            session_state.clear()
                            salt_hex = payload.get("salt", "")
                            session_state["client_salt"] = bytes.fromhex(salt_hex) if salt_hex else b"salt"
                            session_state["client_ip"] = payload.get("client_id", "peer")
                            logger.info(f"Client connected to broker, awaiting authentication...")

                        elif msg_type == "CLIENT_DISCONNECTED":
                            self._streaming_active = False
                            self.file_transfer.cancel_all_transfers()
                            self.input_handler.release_all_keys()
                            session_state.clear()
                            logger.info("Client disconnected. Streaming halted.")

                        elif msg_type == "OTA_REQUEST":
                            if not session_state.get("authenticated"):
                                logger.warning("Richiesta OTA_REQUEST respinta: client non autenticato.")
                                err_msg = ProtocolMessage.serialize("OTA_ERROR", {"error": "Autenticazione richiesta per scaricare l'aggiornamento."})
                                await ws.send(err_msg)
                                continue
                            asyncio.create_task(self._stream_ota_update(ws, session_state.get("session_key")))

                        else:
                            resp_bytes = self.handle_client_message(msg_type, payload, binary, session_state)
                            if resp_bytes:
                                await ws.send(resp_bytes)
                            if msg_type == "AUTH_REQUEST" and session_state.get("authenticated"):
                                self._streaming_active = False
                                if stream_task and not stream_task.done():
                                    try:
                                        await asyncio.wait_for(stream_task, timeout=0.1)
                                    except Exception:
                                        pass
                                self._streaming_active = True
                                stream_task = asyncio.create_task(self.run_capture_stream(ws, session_state.get("session_key")))
                            elif not session_state.get("authenticated", False):
                                self._streaming_active = False

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

        # If broker target is default loopback and port 8765 is not yet open, run embedded broker
        if "127.0.0.1:8765" in self.broker_url or "localhost:8765" in self.broker_url:
            try:
                from src.signaling_broker import SignalingBroker
                self._embedded_broker = SignalingBroker(host="0.0.0.0", port=8765)
                await self._embedded_broker.start()
                logger.info("Embedded Signaling Broker started on port 8765")
            except Exception:
                self._embedded_broker = None

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
        if hasattr(self, "_embedded_broker") and self._embedded_broker:
            try:
                asyncio.run(self._embedded_broker.stop())
            except Exception:
                pass
            self._embedded_broker = None
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
        def_pwd = sec.get_password_display()
        print(f"AetherDesk Host Status:")
        print(f"  Device ID: {d_id}")
        print(f"  Password configured: {'Yes' if has_pwd else f'No (Default: {def_pwd})'}")
        print(f"  Broker target: {args.broker}")
        sys.exit(0)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    daemon = HostDaemon(config_dir=args.config_dir, headless=args.headless, broker_url=args.broker)
    try:
        asyncio.run(daemon.start())
    except KeyboardInterrupt:
        daemon.stop()
