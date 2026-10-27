"""
AetherDesk Host Session Layer.

Architectural Boundary:
- Host Session Layer: Manages authenticated host session lifecycle, video capture and streaming pipeline,
  synchronized input worker coordination, file transfer accounting, and background tasks.
- Security Boundary: Validates broker tokens on broker events, enforces authentication before handling
  session commands or ACKs, bounds file transfer resources, and strictly isolates thread concurrency.
"""

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
from typing import Dict, Any, Optional, Tuple
import websockets
try:
    from websockets.exceptions import ConnectionClosed
except ImportError:
    class ConnectionClosed(Exception):
        pass

from src.capture import DesktopCapture, MockDesktopCapture, _ensure_interactive_desktop, is_test_environment
from src.input_handler import InputHandler, MockInputInjector
from src.input_worker import InputWorker, validate_input_command
from src.security import SecurityManager
from src.protocol import ProtocolMessage
from src.transfer import ClipboardManager, FileTransferManager
from src.ota import get_app_version, compute_file_sha256, get_host_dist_executable, is_newer_version
from src.native_engine import get_native_engine, NativeRealtimeEngine

# Win32 Power Flags
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

logger = logging.getLogger("HostDaemon")

class HostDaemon:
    """Core daemon managing screen capture, input injection, sleep prevention and sessions."""

    def __init__(
        self,
        config_dir: str = None,
        headless: bool = False,
        broker_url: str = "ws://127.0.0.1:8765",
        dpapi_engine: Optional[Any] = None,
        security: Optional[SecurityManager] = None,
        input_handler: Optional[InputHandler] = None,
        capture: Optional[Any] = None,
        start_worker: bool = True,
        ws_connector: Optional[Any] = None,
    ):
        # Auto-activate headless mode during pytest executions as an absolute safety net
        if is_test_environment():
            headless = True

        self.headless = headless
        self.broker_url = broker_url
        self.ws_connector = ws_connector
        self.security = security or SecurityManager(config_dir=config_dir, dpapi_engine=dpapi_engine)
        self.clipboard = ClipboardManager()
        self.file_transfer = FileTransferManager()

        if capture is not None:
            self.capture = capture
        elif self.headless:
            self.capture = MockDesktopCapture(quality=55)
        else:
            self.capture = None

        if input_handler is not None:
            self.input_handler = input_handler
        elif self.headless:
            self.input_handler = InputHandler(injector=MockInputInjector())
        else:
            self.input_handler = InputHandler()

        self.input_worker = InputWorker(self.input_handler, get_bounds_cb=self._get_input_bounds, clipboard=self.clipboard)
        if start_worker:
            self.input_worker.start()
        self.device_id = self.security.get_or_create_device_id()

        self.running = False
        self.connected_clients: Dict[str, Any] = {}
        self._sleep_prevented = False
        self.websocket = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._broker_token: str = ""
        self._is_shutting_down: bool = False
        self._shutdown_done: bool = False
        self.native_engine: NativeRealtimeEngine = get_native_engine()
        self.production_mode: str = "webrtc_native" if self.native_engine.is_native_loaded else "standard_fallback"
        self._webrtc_active: bool = False
        self._listener_task: Optional[asyncio.Task] = None
        self._stream_task: Optional[asyncio.Task] = None
        self._child_tasks: set[asyncio.Task] = set()
        self._frame_ack_event = asyncio.Event()
        self._frame_ack_event.set()
        self._in_flight_frame = False
        self._in_flight_fids: set[int] = set()
        self._frame_send_times: dict[int, float] = {}
        self._max_in_flight = 2
        self.telemetry: dict[str, Any] = {}
        self._streaming_active = False
        self._stream_generation: int = 0
        self._control_saturated: bool = False
        self._current_session_state: Optional[dict] = None

    def _create_child_task(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._child_tasks.add(task)
        task.add_done_callback(self._child_tasks.discard)
        return task

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def __del__(self):
        try:
            if hasattr(self, "input_worker") and self.input_worker and self.input_worker._thread and self.input_worker._thread.is_alive():
                self.stop(timeout=0.2)
        except Exception:
            pass

    def _get_input_bounds(self):
        mon_bounds = self.capture.get_active_monitor_bounds() if self.capture else None
        virt_bounds = self.capture.get_virtual_desktop_bounds() if self.capture else None
        return mon_bounds, virt_bounds

    def _safe_release_inputs(self, timeout: float = 1.0) -> bool:
        """
        Single synchronized entry point for releasing host inputs.
        Routes solely through input_worker when available; never calls input_handler
        concurrently if the worker lock is held by an active command.
        """
        if hasattr(self, "input_worker") and self.input_worker:
            self.input_worker.clear()
            ok = self.input_worker.release_all(timeout=timeout)
            if not ok:
                logger.error(
                    "InputWorker timed out during release_all in _safe_release_inputs. "
                    "Skipping parallel input_handler call to prevent concurrent execution on injector."
                )
            return ok
        elif hasattr(self, "input_handler") and self.input_handler:
            try:
                self.input_handler.release_all_inputs()
                return True
            except Exception as e:
                logger.error(f"Error releasing inputs on handler fallback: {e}")
                return False
        return True

    def reset_session_saturation(self, release_inputs: bool = True) -> bool:
        """
        Session-scoped saturation latch reset.
        Can only be invoked upon receiving an authentic CLIENT_ARRIVED broker event
        after previous session teardown. Verifies that all pressed keys/buttons have
        been released and no emergency release is still pending in InputWorker.
        If release failed or is still pending, remains in fail-closed state (latch remains True).
        Returns True if latch was successfully reset, False otherwise.
        """
        # Ensure previous native host session is fully destroyed
        if hasattr(self, "native_engine") and self.native_engine and self.native_engine.host_handle:
            try:
                self.native_engine.destroy_host_session()
            except Exception as e:
                logger.warning(f"Error destroying native session during saturation reset: {e}")

        # Ensure all inputs are released cleanly if not already released off-thread
        release_ok = True
        if release_inputs:
            release_ok = self._safe_release_inputs(timeout=2.0)

        # Check if worker has pending emergency release
        is_pending = False
        if hasattr(self, "input_worker") and self.input_worker:
            is_pending = bool(getattr(self.input_worker, "_release_pending", False))

        if release_ok and not is_pending:
            self._control_saturated = False
            logger.info("Host control channel saturation latch atomically reset for new incoming session.")
            return True
        else:
            self._control_saturated = True
            logger.error("Failed to reset saturation latch: input recovery failed or pending. Remaining fail-closed.")
            return False

    def prevent_sleep(self, enable: bool = True) -> bool:
        """Prevent Windows from sleeping or turning off the screen while unattended."""
        if self.headless or is_test_environment():
            self._sleep_prevented = enable
            return True
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
        if self.headless or is_test_environment():
            return True
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
        if msg_type == "CLIENT_DISCONNECTED":
            token = payload.get("broker_token", "") if isinstance(payload, dict) else ""
            is_broker_event = (bool(self._broker_token) and token == self._broker_token)
            is_auth_client = bool(session_state.get("authenticated"))
            if not is_broker_event and not is_auth_client:
                logger.warning("Rejected unauthenticated CLIENT_DISCONNECTED message")
                return None
            self._streaming_active = False
            self._webrtc_active = False
            if hasattr(self, "native_engine") and self.native_engine:
                self.native_engine.destroy_host_session()
            self.prevent_sleep(False)
            self.file_transfer.cancel_all_transfers()
            self._safe_release_inputs(timeout=1.0)
            session_state.clear()
            logger.info("Client disconnected. Streaming halted.")
            return None

        if getattr(self, "_control_saturated", False):
            logger.warning("Rejected client message: host control channel is saturated")
            return ProtocolMessage.serialize("ERROR", {
                "error": "CONTROL_CHANNEL_SATURATED",
                "message": "Host control channel saturated; session terminated"
            })

        client_ip = session_state.get("client_ip", "peer")

        if msg_type == "AUTH_REQUEST":
            if self.security.is_rate_limited(client_ip):
                return ProtocolMessage.serialize("AUTH_FAILED", {"error": "Troppi tentativi falliti. Riprova tra 60 secondi."})

            self.security.reset_replay_window()
            salt_hex = payload.get("salt", "")
            salt = bytes.fromhex(salt_hex) if salt_hex else b"salt"

            # Check if modern Forward-Secure X25519 ECDH handshake was requested
            client_eph_hex = payload.get("client_eph")
            if client_eph_hex:
                try:
                    client_eph = bytes.fromhex(client_eph_hex)
                    host_eph_priv, host_eph_pub = self.security.create_ephemeral_ecdh()
                    session_key = self.security.derive_ecdh_shared_key(
                        host_eph_priv, client_eph, salt, context=b"AetherDesk-v2-ForwardSecure"
                    )
                    # Verify client proof if provided
                    client_proof_hex = payload.get("client_proof")
                    pwd_hash = self.security.config.get("pwd_hash", "")
                    if client_proof_hex:
                        expected_proof = self.security.compute_auth_proof(
                            session_key, b"CLIENT_AUTH", pwd_hash, salt, client_eph, host_eph_pub
                        )
                        import secrets
                        if not secrets.compare_digest(bytes.fromhex(client_proof_hex), expected_proof):
                            self.security.record_failed_attempt(client_ip)
                            session_state["authenticated"] = False
                            return ProtocolMessage.serialize("AUTH_FAILED", {"error": "Credenziali non valide"})

                    host_proof = self.security.compute_auth_proof(
                        session_key, b"HOST_AUTH", pwd_hash, salt, host_eph_pub, client_eph
                    )
                    sig = self.security.sign_data(host_eph_pub + client_eph + salt)

                    self.security.reset_attempts(client_ip)
                    self.security.reset_replay_window()
                    session_state["session_key"] = session_key
                    session_state["authenticated"] = True
                    self.prevent_sleep(True)

                    # Initialize native host session and generate WebRTC offer
                    self.native_engine.create_host_session()
                    offer_sdp = self.native_engine.create_host_offer()

                    return ProtocolMessage.serialize("AUTH_OK", {
                        "status": "authenticated",
                        "device_id": self.device_id,
                        "host_eph": host_eph_pub.hex(),
                        "host_sig": sig.hex(),
                        "host_proof": host_proof.hex(),
                        "engine_mode": self.production_mode,
                        "webrtc_offer": offer_sdp,
                    })
                except Exception as e:
                    logger.error(f"Forward-secret auth failed: {e}")
                    self.security.record_failed_attempt(client_ip)
                    session_state["authenticated"] = False
                    return ProtocolMessage.serialize("AUTH_FAILED", {"error": "Handshake crittografico fallito"})

            # Fallback to standard derived master key
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
            # Remote session active: prevent host display from suspending while streaming
            self.prevent_sleep(True)

            resp = ProtocolMessage.serialize("AUTH_OK", {
                "status": "authenticated",
                "device_id": self.device_id,
                "engine_mode": self.production_mode
            })
            return resp

        elif msg_type == "WEBRTC_ANSWER":
            if not session_state.get("authenticated"):
                return ProtocolMessage.serialize("ERROR", {"error": "UNAUTHENTICATED"})
            sdp = payload.get("sdp", "")
            if sdp:
                self.native_engine.set_host_answer(sdp)
                logger.info("WebRTC client Answer received, initiating ICE connection...")
            return None

        elif msg_type == "KEYFRAME_REQ":
            self._keyframe_requested = True
            logger.info("Host received client KEYFRAME_REQ, forcing IDR keyframe.")
            return None

        elif msg_type == "ICE_CANDIDATE":
            if not session_state.get("authenticated"):
                return ProtocolMessage.serialize("ERROR", {"error": "UNAUTHENTICATED"})
            cand = payload.get("candidate", "")
            if cand:
                self.native_engine.add_host_ice_candidate(cand)
            return None

        elif msg_type == "FRAME_ACK":
            if not session_state.get("authenticated"):
                return None
            fid = payload.get("fid") if payload else None
            now = time.time()
            if fid is not None:
                self._in_flight_fids.discard(fid)
                # Purge older superseded frame IDs
                for old_f in list(self._in_flight_fids):
                    if old_f <= fid:
                        self._in_flight_fids.discard(old_f)
                        self._frame_send_times.pop(old_f, None)

                t_send = self._frame_send_times.pop(fid, None)
                if t_send is not None:
                    rtt_ms = round((now - t_send) * 1000, 2)
                    self.telemetry["last_ack_rtt_ms"] = rtt_ms
                    self.telemetry["total_acks"] = self.telemetry.get("total_acks", 0) + 1
            else:
                self._in_flight_fids.clear()

            self._in_flight_frame = (len(self._in_flight_fids) > 0)
            self._frame_ack_event.set()
            return None

        # Guard all state-altering or information-disclosing commands behind authentication
        msg_upper = (msg_type or "").upper()
        if msg_upper in ("INPUT", "MOUSE", "TOUCH", "POINTER", "KEYBOARD", "CLIPBOARD", "SWITCH_MONITOR", "GET_MONITORS", "PRIVACY_MODE", "SET_QUALITY", "FILE_START", "FILE_CHUNK", "FILE_END"):
            if not session_state.get("authenticated"):
                return ProtocolMessage.serialize("AUTH_FAILED", {"error": "Autenticazione richiesta"})

        if msg_upper in ("INPUT", "MOUSE", "TOUCH", "POINTER", "KEYBOARD"):
            key = session_state.get("session_key")
            if not key or not binary:
                # Require encrypted payload when session key is established (no insecure unencrypted downgrade)
                if session_state.get("allow_legacy_unencrypted") and payload:
                    cmd_raw = payload.get("cmd") if isinstance(payload.get("cmd"), dict) else payload
                else:
                    logger.warning("Rejected unencrypted or missing binary input payload")
                    return None
            else:
                try:
                    decrypted = self.security.decrypt_packet(binary, key)
                    cmd_raw = json.loads(decrypted.decode("utf-8"))
                except Exception as e:
                    logger.warning(f"Failed to decrypt input binary packet: {e}")
                    return None

            is_valid, err_msg, clean_cmd = validate_input_command(cmd_raw)
            if not is_valid:
                logger.warning(f"Rejected invalid input command: {err_msg}")
                return None

            enqueued = self.input_worker.enqueue(clean_cmd)
            if not enqueued:
                logger.error(f"Input queue saturated or event rejected: {clean_cmd.get('action')}")
                return ProtocolMessage.serialize("ERROR", {"error": "input_queue_saturated", "action": clean_cmd.get("action")})

            return ProtocolMessage.serialize("ACK", {"status": "ok"})

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
                self.capture = MockDesktopCapture(quality=55) if self.headless else DesktopCapture(quality=55)
            self.capture.set_monitor(mon_id)
            return ProtocolMessage.serialize("ACK", {"status": "monitor_switched", "monitor_id": mon_id})

        elif msg_type == "GET_MONITORS":
            if not self.capture:
                self.capture = MockDesktopCapture(quality=55) if self.headless else DesktopCapture(quality=55)
            mons = self.capture.get_monitors_list()
            return ProtocolMessage.serialize("MONITORS_LIST", {"monitors": mons})

        elif msg_type == "PRIVACY_MODE":
            enabled = payload.get("enabled", False)
            res = self.set_privacy_mode(enabled)
            return ProtocolMessage.serialize("ACK", {"status": "privacy_mode", "enabled": enabled, "result": res})

        elif msg_type == "SET_QUALITY":
            quality = int(payload.get("quality", 70))
            if not self.capture:
                self.capture = MockDesktopCapture(quality=quality) if self.headless else DesktopCapture(quality=quality)
            else:
                self.capture.quality = quality
            return ProtocolMessage.serialize("ACK", {"status": "quality_set", "quality": quality})

        elif msg_type == "FILE_START":
            file_id = payload.get("file_id")
            filename = payload.get("filename")
            filesize = payload.get("filesize", 0)
            try:
                self.file_transfer.start_receiving(file_id, filename, filesize)
                return ProtocolMessage.serialize("ACK", {"status": "file_started", "file_id": file_id})
            except Exception as e:
                logger.error(f"FILE_START failed for {file_id}: {e}")
                return ProtocolMessage.serialize("FILE_ERROR", {"file_id": file_id, "error": str(e)})

        elif msg_type == "FILE_CHUNK":
            key = session_state.get("session_key")
            file_id = payload.get("file_id")
            offset = payload.get("offset", 0)
            if key and binary:
                try:
                    chunk = self.security.decrypt_packet(binary, key)
                    self.file_transfer.receive_chunk(file_id, offset, chunk)
                    return ProtocolMessage.serialize("ACK", {"status": "chunk_received", "offset": offset})
                except Exception as e:
                    logger.error(f"FILE_CHUNK failed for {file_id}: {e}")
                    self.file_transfer.cancel_transfer(file_id)
                    return ProtocolMessage.serialize("FILE_ERROR", {"file_id": file_id, "error": str(e)})
            else:
                self.file_transfer.cancel_transfer(file_id)
                return ProtocolMessage.serialize("FILE_ERROR", {"file_id": file_id, "error": "Missing key or binary payload"})

        elif msg_type == "FILE_END":
            file_id = payload.get("file_id")
            try:
                saved = self.file_transfer.finish_receiving(file_id)
                return ProtocolMessage.serialize("FILE_SAVED", {"status": "ok", "saved_path": saved})
            except Exception as e:
                logger.error(f"FILE_END failed for {file_id}: {e}")
                self.file_transfer.cancel_transfer(file_id)
                return ProtocolMessage.serialize("FILE_ERROR", {"file_id": file_id, "error": str(e)})

        elif msg_type == "OTA_CHECK":
            logger.info("OTA_CHECK ricevuto: P2P OTA disabilitato fail-closed (firma fornitore non disponibile sul nodo host).")
            return ProtocolMessage.serialize("OTA_INFO", {
                "available": False,
                "version": get_app_version(),
                "error": "P2P OTA disabilitato fail-closed: autenticazione crittografica della firma fornitore non disponibile nel trasporto locale."
            })

        return None

    def _execute_input(self, cmd: dict):
        """Execute input event through InputWorker (preserves order, validates bounds, prevents race conditions)."""
        if not isinstance(cmd, dict):
            return
        is_valid, err_msg, clean_cmd = validate_input_command(cmd)
        if is_valid and clean_cmd:
            self.input_worker.execute_now(clean_cmd)
        elif err_msg:
            logger.warning(f"Invalid input in _execute_input: {err_msg}")

    def _poll_and_enqueue_webrtc_cursor(self) -> Optional[Tuple[float, float, int]]:
        """
        Poll latest mouse coordinates from the native WebRTC loss-tolerant cursor channel
        and enqueue normalized coordinates into InputWorker.
        Routing through InputWorker ensures serialization with discrete clicks/keyboard
        events and coalescing of high-frequency moves without direct injection race conditions.
        Returns (norm_x, norm_y, cseq) if an update was polled and enqueued, else None.
        """
        if not hasattr(self, "native_engine") or not self.native_engine:
            return None
        cursor_upd = self.native_engine.poll_host_cursor()
        if not cursor_upd:
            return None

        cx, cy, cseq = cursor_upd
        ref_w, ref_h = self.input_handler.get_screen_bounds() if hasattr(self, "input_handler") and self.input_handler else (1920, 1080)
        norm_x = (cx / ref_w) if (cx > 1.0 and ref_w > 0) else float(cx)
        norm_y = (cy / ref_h) if (cy > 1.0 and ref_h > 0) else float(cy)

        cursor_cmd = {
            "action": "mouse_move",
            "norm_x": norm_x,
            "norm_y": norm_y,
            "seq": cseq,
        }
        if hasattr(self, "input_worker") and self.input_worker:
            self.input_worker.enqueue(cursor_cmd)
        elif hasattr(self, "input_handler") and self.input_handler:
            self.input_handler.move_mouse(norm_x, norm_y)

        return (norm_x, norm_y, cseq)

    async def run_capture_stream(
        self,
        websocket,
        session_key: bytes,
        session_state: Optional[dict] = None,
        stream_generation: Optional[int] = None
    ):
        """Streaming loop: captures screen and sends encrypted JPEG frames with bounded 2-frame pipeline & telemetry."""
        cur_task = asyncio.current_task()

        # STRICT OWNERSHIP CHECK BEFORE ANY SHARED MUTATION:
        # If another task is already registered as the active stream task, this coroutine is stale: abort immediately!
        if self._stream_task is not None and self._stream_task is not cur_task:
            logger.debug("Bypassing run_capture_stream startup: another stream task is already active.")
            return

        # Deterministic generation assignment:
        # If stream_generation was not passed explicitly by the caller:
        # - If self._stream_task is cur_task: adopt existing generation.
        # - Only if self._stream_task is None (no active stream): allocate a new generation.
        if stream_generation is None:
            if self._stream_task is cur_task:
                stream_generation = getattr(self, "_stream_generation", 0)
            elif self._stream_task is None:
                self._stream_generation = getattr(self, "_stream_generation", 0) + 1
                stream_generation = self._stream_generation
            else:
                return

        if getattr(self, "_stream_generation", 0) != stream_generation:
            logger.debug("Bypassing run_capture_stream startup: stream generation mismatch.")
            return

        was_cancelled = False
        orig_exc: Optional[BaseException] = None
        try:
            if not self.capture:
                self.capture = MockDesktopCapture(quality=50) if self.headless else DesktopCapture(quality=50)
            else:
                self.capture.switch_desktop()
                self.capture._last_thumb = None

            target_interval = 1.0 / 30.0 # 30 FPS target
            frame_counter = 0
            self._streaming_active = True
            self._in_flight_fids.clear()
            self._frame_send_times.clear()
            self._in_flight_frame = False
            self._frame_ack_event.set()

            while self.running and self._streaming_active:
                # Early loop exit if session generation has been superseded
                if getattr(self, "_stream_generation", 0) != stream_generation or (self._stream_task is not None and self._stream_task is not cur_task):
                    logger.debug("Terminating run_capture_stream loop: superseded by newer stream generation.")
                    break

                # Check connection state across modern websockets versions
                ws_state = getattr(websocket, "state", None)
                if ws_state is not None and getattr(ws_state, "name", "") != "OPEN":
                    break
                if ws_state is None and not getattr(websocket, "open", True):
                    break

                # 0. Check for control channel saturation BEFORE any ICE state check or fallback branching!
                is_saturated = False
                if hasattr(self, "native_engine") and self.native_engine and self.native_engine.host_handle:
                    try:
                        if self.native_engine.is_host_control_saturated():
                            is_saturated = True
                    except Exception:
                        pass
                if getattr(self, "_control_saturated", False):
                    is_saturated = True

                if is_saturated:
                    self._control_saturated = True
                    logger.error("Native host control channel saturated! Emergency input recovery triggered.")
                    self._safe_release_inputs(timeout=2.0)
                    self._streaming_active = False
                    self._webrtc_active = False
                    if hasattr(self, "native_engine") and self.native_engine and self.native_engine.host_handle:
                        try:
                            self.native_engine.destroy_host_session()
                        except Exception:
                            pass
                    try:
                        err_msg = ProtocolMessage.serialize("ERROR", {
                            "error": "CONTROL_CHANNEL_SATURATED",
                            "message": "Host control channel saturated; session terminated"
                        })
                        await websocket.send(err_msg)
                    except Exception:
                        pass
                    target_state = session_state if session_state is not None else getattr(self, "_current_session_state", None)
                    if target_state is not None:
                        target_state.clear()
                    break

                # Check and advance native WebRTC session if engine is loaded
                if self.native_engine.is_native_loaded and self.native_engine.host_handle:
                    # 1. Forward outgoing local ICE candidates to client via signaling rendezvous
                    cand = self.native_engine.poll_host_ice_candidate()
                    if cand:
                        cand_msg = ProtocolMessage.serialize("ICE_CANDIDATE", {"candidate": cand})
                        await websocket.send(cand_msg)

                    # 2. Gate active state on real ICE connection state
                    ice_state = self.native_engine.get_host_ice_state()
                    if ice_state in (2, 3):  # Connected or Completed
                        if not self._webrtc_active:
                            self._webrtc_active = True
                            logger.info("Host WebRTC P2P direct transport activated (ICE connected).")
                    elif ice_state in (4, 5, 6):  # Failed, Disconnected, or Closed
                        if self._webrtc_active:
                            self._webrtc_active = False
                            logger.warning(f"Host WebRTC ICE state {ice_state}, falling back to WebSocket stream.")

                    # 3. Direct low-latency P2P loop when active
                    if self._webrtc_active:
                        frame_counter += 1
                        need_kf = getattr(self, "_keyframe_requested", False)
                        self._keyframe_requested = False
                        force_keyframe = (frame_counter == 1 or frame_counter % 90 == 0 or need_kf)

                        # Step native DXGI -> MF MFT -> WebRTC video track pipeline
                        self.native_engine.step_host_pipeline(force_keyframe=force_keyframe)

                        # Poll latest-wins mouse movement from loss-tolerant cursor channel
                        # Route through InputWorker to serialize with clicks/keys and prevent concurrent injection!
                        self._poll_and_enqueue_webrtc_cursor()

                        # Drain discrete control messages from reliable control channel (burst handling)
                        for _ in range(64):
                            ctrl_data = self.native_engine.poll_host_control()
                            if not ctrl_data:
                                break
                            try:
                                if b"KEYFRAME_REQ" in ctrl_data:
                                    self._keyframe_requested = True
                                    logger.info("WebRTC client requested IDR keyframe (PLI/recovery).")
                                else:
                                    c_type, c_payload, c_bin = ProtocolMessage.deserialize(ctrl_data)
                                    target_state = session_state if session_state is not None else getattr(self, "_current_session_state", None)
                                    s_state = target_state if target_state is not None else {"authenticated": True, "session_key": session_key}
                                    self.handle_client_message(c_type, c_payload, c_bin, s_state)
                            except Exception as err:
                                logger.warning(f"Error handling WebRTC control packet: {err}")

                        await asyncio.sleep(1.0 / 60.0)
                        continue

                # Adaptive Backpressure: small bounded in-flight pipeline (max 2 frames)
                if len(self._in_flight_fids) >= self._max_in_flight:
                    try:
                        await asyncio.wait_for(self._frame_ack_event.wait(), timeout=0.035)
                    except asyncio.TimeoutError:
                        # Stalled ACK: discard oldest un-ACKed frame ID to prevent blocking capture pipeline
                        if self._in_flight_fids:
                            oldest_fid = min(self._in_flight_fids)
                            self._in_flight_fids.discard(oldest_fid)
                            self._frame_send_times.pop(oldest_fid, None)
                        self.telemetry["late_or_dropped_acks"] = self.telemetry.get("late_or_dropped_acks", 0) + 1
                    finally:
                        self._in_flight_frame = (len(self._in_flight_fids) > 0)
                        if len(self._in_flight_fids) < self._max_in_flight:
                            self._frame_ack_event.set()

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

                        t_now = time.time()
                        self._frame_send_times[frame_counter] = t_now
                        self._in_flight_fids.add(frame_counter)
                        self._in_flight_frame = True
                        if len(self._in_flight_fids) >= self._max_in_flight:
                            self._frame_ack_event.clear()

                        cap_ms = round((t_cap_done - t_cap_start) / 1e6, 2)
                        enc_ms = round((t_enc_done - t_enc_start) / 1e6, 2)
                        self.telemetry["last_cap_ms"] = cap_ms
                        self.telemetry["last_enc_ms"] = enc_ms
                        self.telemetry["frames_sent"] = frame_counter
                        self.native_engine.record_fallback_metrics(t_cap_done - t_cap_start, t_enc_done - t_enc_start)

                        meta = {
                            "w": size[0],
                            "h": size[1],
                            "cx": cur[0] if cur else 0,
                            "cy": cur[1] if cur else 0,
                            "fid": frame_counter,
                            "t_send": t_now,
                            "cap_ms": cap_ms,
                            "enc_ms": enc_ms,
                            "rtt_ms": self.telemetry.get("last_ack_rtt_ms", 0.0)
                        }
                        msg = ProtocolMessage.serialize("FRAME", meta, binary_data=encrypted)
                        await websocket.send(msg)
                except Exception as e:
                    logger.error(f"Capture error in run_capture_stream: {e}")
                    orig_exc = e
                    try:
                        err_msg = ProtocolMessage.serialize("ERROR", {
                            "error": "CAPTURE_FAILED",
                            "message": f"Errore di cattura schermo host: {e}"
                        })
                        await websocket.send(err_msg)
                    except Exception as send_err:
                        logger.debug(f"Failed to send CAPTURE_FAILED to client: {send_err}")
                    break

                elapsed = time.time() - start_t
                sleep_t = max(0.001, target_interval - elapsed)
                await asyncio.sleep(sleep_t)
        except asyncio.CancelledError:
            was_cancelled = True
            raise
        except BaseException as exc:
            orig_exc = exc
            try:
                err_msg = ProtocolMessage.serialize("ERROR", {
                    "error": "STREAM_ERROR",
                    "message": f"Errore critico streaming host: {exc}"
                })
                await websocket.send(err_msg)
            except Exception:
                pass
            raise
        finally:
            # Coordinated, idempotent cleanup of the streaming session.
            # Only perform teardown if this coroutine represents the current active stream generation
            # and is either the registered stream task or the stream task has not been re-assigned to a new session.
            is_our_session = (
                getattr(self, "_stream_generation", 0) == stream_generation
                and (self._stream_task is cur_task or self._stream_task is None)
            )
            if is_our_session:
                self._streaming_active = False
                self._webrtc_active = False
                if self._stream_task is cur_task:
                    self._stream_task = None
                self._in_flight_fids.clear()
                self._frame_send_times.clear()
                self._in_flight_frame = False
                self._frame_ack_event.set()
                if hasattr(self, "native_engine") and self.native_engine and self.native_engine.host_handle:
                    try:
                        self.native_engine.destroy_host_session()
                    except Exception as err:
                        logger.debug(f"Error destroying host session during cleanup: {err}")

                # If this session suffered an unrecoverable streaming failure (not intentional cancellation),
                # close the host websocket connection so the broker detects disconnect and notifies the peer.
                # Do NOT send CLIENT_DISCONNECTED directly from the host: that type is reserved for broker-internal routing.
                if orig_exc is not None and not was_cancelled:
                    try:
                        if hasattr(websocket, "close") and callable(websocket.close):
                            await asyncio.wait_for(websocket.close(), timeout=0.5)
                    except Exception as close_err:
                        logger.debug(f"Error closing host websocket after streaming failure: {close_err}")

                # Offload input release outside the event loop to prevent event loop stalls.
                # Must await completion deterministically so executor is not left running in background,
                # while preserving CancelledError propagation without blocking the event loop.
                try:
                    loop = asyncio.get_running_loop()
                    rel_fut = loop.run_in_executor(None, self._safe_release_inputs, 1.0)
                    while not rel_fut.done():
                        try:
                            await asyncio.shield(rel_fut)
                        except asyncio.CancelledError:
                            was_cancelled = True
                            try:
                                await asyncio.sleep(0.005)
                            except asyncio.CancelledError:
                                was_cancelled = True
                except Exception as rel_err:
                    logger.debug(f"Error offloading input release in cleanup: {rel_err}")

                target_state = session_state if session_state is not None else getattr(self, "_current_session_state", None)
                if target_state is not None:
                    target_state.clear()
                if getattr(self, "_current_session_state", None) is session_state:
                    self._current_session_state = None
            else:
                logger.debug("Bypassing cleanup in run_capture_stream: stream task was superseded by another session.")
                if session_state is not None and session_state is not getattr(self, "_current_session_state", None):
                    session_state.clear()

            if was_cancelled and orig_exc is None:
                raise asyncio.CancelledError()

    async def _stream_ota_update(self, websocket, session_key: Optional[bytes]):
        """Stream executable update over websocket (disabled fail-closed without vendor signed manifest)."""
        logger.error("Tentativo di esecuzione _stream_ota_update bloccato: P2P OTA disabilitato fail-closed.")
        await websocket.send(ProtocolMessage.serialize("OTA_ERROR", {
            "error": "P2P OTA disabilitato fail-closed: autenticazione crittografica della firma fornitore non disponibile."
        }))

    async def handle_client_arrived(self, payload: dict, session_state: dict, ws: Optional[Any] = None) -> bool:
        """
        Process broker CLIENT_ARRIVED event:
        1. Validate broker_token against authenticated host registration token.
        2. Cancel any running stream task from previous session.
        3. If control channel was saturated, attempt atomic reset_session_saturation().
           If recovery fails or is incomplete, keep latch fail-closed, notify broker/client,
           clear session_state, and return False (refuse connection).
        4. Clear old session state, initialize fresh unauthenticated client credentials/salt,
           and return True.
        """
        token = payload.get("broker_token", "") if isinstance(payload, dict) else ""
        if not self._broker_token or token != self._broker_token:
            logger.warning("Rejected unauthenticated CLIENT_ARRIVED: invalid broker token")
            return False

        self._streaming_active = False
        self._stream_generation = getattr(self, "_stream_generation", 0) + 1
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
            try:
                await asyncio.wait_for(self._stream_task, timeout=0.2)
            except Exception:
                pass
        self._stream_task = None

        # Cleanly release inputs from prior session in a strictly serialized manner through InputWorker
        release_ok = await asyncio.to_thread(self._safe_release_inputs, 2.0)
        is_pending = False
        if hasattr(self, "input_worker") and self.input_worker:
            is_pending = bool(getattr(self.input_worker, "_release_pending", False))

        if not release_ok or is_pending:
            logger.error("Rejecting CLIENT_ARRIVED: failed to safely release inputs from prior session (fail-closed)")
            if ws is not None:
                err_msg = ProtocolMessage.serialize("ERROR", {
                    "error": "HOST_INPUT_CLEANUP_FAILED",
                    "message": "Host failed to safely release inputs from prior session; connection refused"
                })
                try:
                    await ws.send(err_msg)
                except Exception:
                    pass
            session_state.clear()
            return False

        if getattr(self, "_control_saturated", False):
            if not self.reset_session_saturation(release_inputs=False):
                logger.error("Rejecting CLIENT_ARRIVED: saturation recovery incomplete (fail-closed)")
                if ws is not None:
                    err_msg = ProtocolMessage.serialize("ERROR", {
                        "error": "HOST_RECOVERY_INCOMPLETE",
                        "message": "Host recovery incomplete after saturation; connection refused"
                    })
                    try:
                        await ws.send(err_msg)
                    except Exception:
                        pass
                session_state.clear()
                return False

        session_state.clear()
        salt_hex = payload.get("salt", "") if isinstance(payload, dict) else ""
        session_state["client_salt"] = bytes.fromhex(salt_hex) if salt_hex else b"salt"
        session_state["client_ip"] = payload.get("client_id", "peer") if isinstance(payload, dict) else "peer"
        logger.info(f"Client connected to broker, awaiting authentication...")
        return True

    async def _broker_listener(self):
        """Persistent connection to signaling broker with auto-reconnect."""
        clean_id = self.device_id.replace(" ", "")
        while self.running and not (self._stop_event and self._stop_event.is_set()):
            try:
                conn_target = self.ws_connector() if self.ws_connector else websockets.connect(self.broker_url, max_size=16 * 1024 * 1024)
                if asyncio.iscoroutine(conn_target):
                    conn_target = await conn_target
                async with conn_target as ws:
                    self.websocket = ws
                    # Register this host with broker using persistent cryptographic identity
                    pubkey_hex = self.security.get_identity_public_key_hex()
                    reg_msg = ProtocolMessage.serialize("REGISTER_HOST", {
                        "device_id": clean_id,
                        "host_salt": self.security.config.get("pwd_salt", ""),
                        "pubkey": pubkey_hex
                    })
                    await ws.send(reg_msg)
                    logger.info(f"Registered with signaling broker as {clean_id} (Pubkey: {pubkey_hex[:8]}...)")

                    session_state = {}
                    self._current_session_state = session_state

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

                        if msg_type == "HOST_REGISTERED":
                            self._broker_token = payload.get("broker_token", "") if isinstance(payload, dict) else ""
                            logger.info(f"Host registered with broker. Token acquired.")

                        elif msg_type == "HOST_CHALLENGE":
                            nonce_hex = payload.get("nonce", "") if isinstance(payload, dict) else ""
                            if nonce_hex:
                                nonce_bytes = bytes.fromhex(nonce_hex)
                                signature = self.security.sign_data(nonce_bytes)
                                challenge_ans = ProtocolMessage.serialize("HOST_CHALLENGE_RESPONSE", {
                                    "device_id": clean_id,
                                    "signature": signature.hex()
                                })
                                await ws.send(challenge_ans)
                                logger.info("Responded to broker cryptographic identity challenge with Ed25519 signature.")

                        elif msg_type == "ERROR":
                            err_code = payload.get("error", "") if isinstance(payload, dict) else ""
                            logger.error(f"Broker error: {err_code} - {payload.get('message', '') if isinstance(payload, dict) else ''}")
                            if err_code in ("HOST_ALREADY_REGISTERED", "HOST_IMPERSONATION_REJECTED", "HOST_CHALLENGE_FAILED"):
                                await asyncio.sleep(2.0)
                                break

                        elif msg_type == "CLIENT_ARRIVED":
                            accepted = await self.handle_client_arrived(payload, session_state, ws=ws)
                            if not accepted:
                                continue

                        elif msg_type == "CLIENT_DISCONNECTED":
                            token = payload.get("broker_token", "") if isinstance(payload, dict) else ""
                            is_broker_event = (bool(self._broker_token) and token == self._broker_token)
                            is_auth_client = bool(session_state.get("authenticated"))
                            if not is_broker_event and not is_auth_client:
                                logger.warning("Rejected unauthenticated CLIENT_DISCONNECTED message")
                                continue
                            self.handle_client_message(msg_type, payload, binary, session_state)

                        elif msg_type == "OTA_REQUEST":
                            logger.warning("Richiesta OTA_REQUEST respinta: P2P OTA disabilitato fail-closed.")
                            err_msg = ProtocolMessage.serialize("OTA_ERROR", {
                                "error": "P2P OTA disabilitato fail-closed: impossibile autenticare la firma digitale del fornitore sul binario dell'host."
                            })
                            await ws.send(err_msg)
                            continue

                        else:
                            resp_bytes = self.handle_client_message(msg_type, payload, binary, session_state)
                            msg_upper = (msg_type or "").upper()
                            # Avoid flooding WebSocket with unneeded ACKs for 125 Hz mouse moves,
                            # but ALWAYS send errors, rejections, or explicit client-requested ACKs
                            is_routine_input_ack = (
                                msg_upper in ("INPUT", "MOUSE", "TOUCH", "POINTER", "KEYBOARD")
                                and resp_bytes is not None
                                and b'"ACK"' in resp_bytes
                                and not (payload and payload.get("ack"))
                            )
                            should_send = resp_bytes is not None and not is_routine_input_ack
                            if should_send:
                                await ws.send(resp_bytes)
                            if msg_type == "AUTH_REQUEST" and session_state.get("authenticated"):
                                self._streaming_active = False
                                self._stream_generation = getattr(self, "_stream_generation", 0) + 1
                                gen = self._stream_generation
                                if self._stream_task and not self._stream_task.done():
                                    self._stream_task.cancel()
                                    try:
                                        await asyncio.wait_for(self._stream_task, timeout=0.1)
                                    except Exception:
                                        pass
                                self._streaming_active = True
                                self._stream_task = asyncio.create_task(
                                    self.run_capture_stream(
                                        ws,
                                        session_state.get("session_key"),
                                        session_state=session_state,
                                        stream_generation=gen
                                    )
                                )
                            elif not session_state.get("authenticated", False):
                                self._streaming_active = False

            except (ConnectionClosed, OSError, Exception) as e:
                logger.warning(f"Broker connection error: {e}. Retrying in 3s...")
                self._streaming_active = False
                self.prevent_sleep(False)
                self._safe_release_inputs(timeout=1.0)
                session_state.clear()
                self._current_session_state = None
                if self._stream_task and not self._stream_task.done():
                    self._stream_task.cancel()
                    try:
                        await self._stream_task
                    except Exception:
                        pass
                self._stream_task = None
                if self.running and not (self._stop_event and self._stop_event.is_set()):
                    try:
                        await asyncio.sleep(3.0)
                    except asyncio.CancelledError:
                        break

    async def start(self):
        """Start daemon loop and connect to signaling broker (sleep prevention deferred until active session)."""
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        if not self.headless and not is_test_environment():
            _ensure_interactive_desktop()
        if hasattr(self, "input_worker") and self.input_worker and not self.input_worker._running:
            self.input_worker.start()
        self.running = True
        logger.info(f"Host Daemon started for Device ID: {self.device_id}")

        # If broker target is default loopback and port 8765 is not yet open, run embedded broker (production only)
        if not self.headless and not is_test_environment() and not self.ws_connector and ("127.0.0.1:8765" in self.broker_url or "localhost:8765" in self.broker_url):
            try:
                from src.signaling_broker import SignalingBroker
                self._embedded_broker = SignalingBroker(host="0.0.0.0", port=8765)
                await self._embedded_broker.start()
                logger.info("Embedded Signaling Broker started on port 8765")
            except Exception as e:
                logger.debug(f"Embedded broker not started (port likely already in use): {e}")
                self._embedded_broker = None

        # Start LAN UDP discovery beacon so clients on same Wi-Fi/LAN find this host automatically (production only)
        if not self.headless and not is_test_environment() and not self.ws_connector:
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
        finally:
            await self.stop_async()

    async def stop_async(self, timeout: float = 2.0):
        """Asynchronously and deterministically shut down all host resources on the owning event loop."""
        self.running = False
        self._streaming_active = False
        self._webrtc_active = False
        if hasattr(self, "native_engine") and self.native_engine:
            self.native_engine.destroy_host_session()
        self.prevent_sleep(False)
        if self._stop_event:
            self._stop_event.set()

        # 1. Stop streaming task
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
            try:
                await self._stream_task
            except (asyncio.CancelledError, Exception):
                pass
        self._stream_task = None

        # 2. Cancel and await listener task
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            try:
                await self._listener_task
            except (asyncio.CancelledError, Exception):
                pass
        self._listener_task = None

        # 3. Cancel and await child tasks (e.g. OTA streams)
        for task in list(self._child_tasks):
            if not task.done():
                task.cancel()
        if self._child_tasks:
            await asyncio.gather(*self._child_tasks, return_exceptions=True)
        self._child_tasks.clear()

        # 4. Stop embedded broker on its owning loop
        if hasattr(self, "_embedded_broker") and self._embedded_broker:
            try:
                await self._embedded_broker.stop()
            except Exception as e:
                logger.debug(f"Error stopping embedded broker: {e}")
            self._embedded_broker = None

        # 5. Stop UDP discovery broadcaster
        if hasattr(self, "_broadcaster") and self._broadcaster:
            try:
                self._broadcaster.stop()
            except Exception:
                pass
            self._broadcaster = None

        # 6. Stop input worker safely offloaded to background executor (non-blocking to asyncio loop)
        if hasattr(self, "input_worker") and self.input_worker:
            try:
                self.input_worker.clear()
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self.input_worker.stop, timeout)
            except Exception as e:
                logger.error(f"Error stopping input_worker in executor: {e}")
        elif hasattr(self, "input_handler") and self.input_handler:
            try:
                self.input_handler.release_all_inputs()
            except Exception:
                pass

        # 7. Cancel and remove partial file transfers
        if hasattr(self, "file_transfer") and self.file_transfer:
            self.file_transfer.cancel_all_transfers()

        # 8. Close capture
        if self.capture:
            try:
                self.capture.close()
            except Exception:
                pass
            self.capture = None
        self._shutdown_done = True
        logger.info("Host Daemon stopped cleanly (stop_async)")

    def _sync_cleanup(self, timeout: float = 2.0):
        """Synchronous cleanup fallback if the owning loop is not running."""
        self.running = False
        self._webrtc_active = False
        if hasattr(self, "native_engine") and self.native_engine:
            self.native_engine.destroy_host_session()
        self.prevent_sleep(False)
        if hasattr(self, "input_worker") and self.input_worker:
            try:
                self.input_worker.clear()
                self.input_worker.stop(timeout=timeout)
            except Exception as e:
                logger.error(f"Error stopping input_worker: {e}")
        elif hasattr(self, "input_handler") and self.input_handler:
            try:
                self.input_handler.release_all_inputs()
            except Exception:
                pass
        if hasattr(self, "_broadcaster") and self._broadcaster:
            try:
                self._broadcaster.stop()
            except Exception:
                pass
            self._broadcaster = None
        if hasattr(self, "file_transfer") and self.file_transfer:
            self.file_transfer.cancel_all_transfers()
        if self.capture:
            try:
                self.capture.close()
            except Exception:
                pass
            self.capture = None

    def stop(self, timeout: float = 3.5):
        """Thread-safe synchronous stop method. Single authority, no concurrent duplicate cleanups."""
        self.running = False
        self._streaming_active = False
        if self._shutdown_done:
            return

        loop = getattr(self, "_loop", None)
        if loop and loop.is_running():
            try:
                current_loop = None
                try:
                    current_loop = asyncio.get_running_loop()
                except RuntimeError:
                    pass
                if current_loop is loop:
                    if not self._is_shutting_down:
                        self._is_shutting_down = True
                        loop.create_task(self.stop_async(timeout=timeout))
                else:
                    fut = asyncio.run_coroutine_threadsafe(self.stop_async(timeout=timeout), loop)
                    try:
                        fut.result(timeout=timeout)
                    except Exception as e:
                        logger.warning(f"HostDaemon stop_async timed out ({timeout}s) on owning loop: {e}")
                        # DO NOT trigger concurrent _sync_cleanup while stop_async is running!
                        return
                return
            except Exception as e:
                logger.warning(f"Threadsafe stop dispatch error: {e}")

        # Fallback only when loop is completely inactive
        if not self._shutdown_done:
            self._sync_cleanup(timeout=timeout)
            self._shutdown_done = True

def build_parser():
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
    return parser

if __name__ == "__main__":
    parser = build_parser()
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
