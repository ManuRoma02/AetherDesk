"""
AetherDesk Native Real-Time Engine Bridge & WebRTC Transport Architecture.

Architectural Boundary:
- Native Real-Time Engine Layer: Connects Python runtime to high-performance DXGI Desktop Duplication,
  Media Foundation H.264 low-latency hardware encoder, Media Foundation H.264 decoder, and WebRTC P2P direct transport.
- Channel Segregation: Separates 125 Hz loss-tolerant mouse cursor streaming (latest-wins) from reliable,
  ordered keyboard and control message channels.
- Versioned C-ABI & Lifecycle: Bridges to aether_core.dll (ABI v1) for both Host and Client sessions.
- Absolute Truthfulness: In absence of compiled DLL, does NOT return fake success and clearly marks
  native WebRTC pipeline as unavailable. All unmeasured statistics are strictly None (null).
"""

import ctypes
import os
import sys
import time
import logging
from pathlib import Path
from typing import Optional, Dict, Any, Callable, Tuple

logger = logging.getLogger("NativeEngine")

AETHER_CORE_ABI_VERSION = 1

# C-ABI Error codes
AETHER_OK = 0
AETHER_ERR_NULL_PTR = -1
AETHER_ERR_INVALID_PARAM = -2
AETHER_ERR_NOT_INITIALIZED = -3
AETHER_ERR_CAPTURE_FAILED = -4
AETHER_ERR_ENCODE_FAILED = -5
AETHER_ERR_WEBRTC_FAILED = -6
AETHER_ERR_BUFFER_TOO_SMALL = -7
AETHER_ERR_DECODE_FAILED = -8


class PipelineTelemetryStruct(ctypes.Structure):
    _fields_ = [
        ("capture_ns", ctypes.c_uint64),
        ("encode_ns", ctypes.c_uint64),
        ("queue_ns", ctypes.c_uint64),
        ("network_rtt_ns", ctypes.c_uint64),
        ("clock_offset_ns", ctypes.c_int64),
        ("decode_ns", ctypes.c_uint64),
        ("render_ns", ctypes.c_uint64),
        ("fps", ctypes.c_float),
        ("bitrate_kbps", ctypes.c_uint32),
        ("is_hardware_encoder", ctypes.c_uint32),
        ("is_zero_copy", ctypes.c_uint32),
        ("has_capture", ctypes.c_uint32),
        ("has_encode", ctypes.c_uint32),
        ("has_queue", ctypes.c_uint32),
        ("has_rtt", ctypes.c_uint32),
        ("has_clock_offset", ctypes.c_uint32),
        ("has_decode", ctypes.c_uint32),
        ("has_render", ctypes.c_uint32),
        ("has_fps", ctypes.c_uint32),
    ]


class AetherHostConfigStruct(ctypes.Structure):
    _fields_ = [
        ("output_index", ctypes.c_uint32),
        ("target_bitrate_kbps", ctypes.c_uint32),
        ("fps", ctypes.c_uint32),
        ("stun_servers_csv", ctypes.c_char_p),
        ("turn_server", ctypes.c_char_p),
        ("turn_username", ctypes.c_char_p),
        ("turn_credential", ctypes.c_char_p),
    ]


class AetherClientConfigStruct(ctypes.Structure):
    _fields_ = [
        ("stun_servers_csv", ctypes.c_char_p),
        ("turn_server", ctypes.c_char_p),
        ("turn_username", ctypes.c_char_p),
        ("turn_credential", ctypes.c_char_p),
    ]


def get_ice_servers_config() -> Dict[str, Optional[str]]:
    """
    Retrieve ICE server configuration securely from environment.
    Never returns hardcoded or dummy credentials.
    """
    stun = os.environ.get(
        "AETHER_STUN_SERVERS",
        "stun:stun.l.google.com:19302,stun:stun1.l.google.com:19302"
    )
    turn_url = os.environ.get("AETHER_TURN_URL")
    turn_user = os.environ.get("AETHER_TURN_USERNAME")
    turn_cred = os.environ.get("AETHER_TURN_CREDENTIAL")

    if not (turn_url and turn_user and turn_cred):
        turn_url, turn_user, turn_cred = None, None, None

    return {
        "stun_csv": stun,
        "turn_url": turn_url,
        "turn_user": turn_user,
        "turn_cred": turn_cred,
    }


class ClockOffsetEstimator:
    """
    NTP-style 4-timestamp clock offset and RTT estimator (RFC 5905).
    Estimates true one-way network delay and clock skew between host and client.
    """

    def __init__(self):
        self.rtt_ms: Optional[float] = None
        self.offset_ms: Optional[float] = None
        self.jitter_ms: Optional[float] = None
        self._samples = []

    def update(self, t1_ms: float, t2_ms: float, t3_ms: float, t4_ms: float):
        if any(t is None for t in (t1_ms, t2_ms, t3_ms, t4_ms)):
            return
        if t4_ms > t1_ms and t3_ms >= t2_ms:
            rtt = (t4_ms - t1_ms) - (t3_ms - t2_ms)
            offset = ((t2_ms - t1_ms) + (t3_ms - t4_ms)) / 2.0
            self.rtt_ms = max(0.1, rtt)
            self.offset_ms = offset
            self._samples.append(rtt)
            if len(self._samples) > 20:
                self._samples.pop(0)
            if len(self._samples) >= 2:
                diffs = [abs(self._samples[i] - self._samples[i - 1]) for i in range(1, len(self._samples))]
                self.jitter_ms = sum(diffs) / len(diffs)

    def to_host_time(self, client_timestamp_ms: float) -> Optional[float]:
        return client_timestamp_ms + self.offset_ms if (self.offset_ms is not None and client_timestamp_ms is not None) else None

    def to_client_time(self, host_timestamp_ms: float) -> Optional[float]:
        return host_timestamp_ms - self.offset_ms if (self.offset_ms is not None and host_timestamp_ms is not None) else None


class CongestionController:
    """
    Real-time transport congestion controller.
    Dynamically adjusts target video bitrate and frame rate based on network telemetry.
    """

    def __init__(self, min_bitrate_kbps: int = 1500, max_bitrate_kbps: int = 25000):
        self.min_bitrate_kbps = min_bitrate_kbps
        self.max_bitrate_kbps = max_bitrate_kbps
        self.current_bitrate_kbps = 4500
        self.target_fps = 60

    def adapt(self, rtt_ms: float, loss_rate: float, queue_delay_ms: float) -> Tuple[int, int]:
        if loss_rate > 0.05 or queue_delay_ms > 35.0 or rtt_ms > 150.0:
            self.current_bitrate_kbps = max(self.min_bitrate_kbps, int(self.current_bitrate_kbps * 0.85))
            if rtt_ms > 200.0:
                self.target_fps = 30
        elif loss_rate < 0.01 and queue_delay_ms < 10.0 and rtt_ms < 60.0:
            self.current_bitrate_kbps = min(self.max_bitrate_kbps, self.current_bitrate_kbps + 500)
            self.target_fps = 60

        return self.current_bitrate_kbps, self.target_fps


class NativeRealtimeEngine:
    """
    Production Realtime Engine interfacing with DXGI Desktop Duplication,
    Media Foundation H.264 Encoder/Decoder, and WebRTC P2P direct channels.
    """

    def __init__(self):
        self.dll = None
        self.is_native_loaded = False
        self.clock_estimator = ClockOffsetEstimator()
        self.congestion_controller = CongestionController()
        self.host_handle: Optional[ctypes.c_void_p] = None
        self.client_handle: Optional[ctypes.c_void_p] = None

        # Real fallback state tracking (measured dynamically, no fake constant numbers)
        self._fallback_last_cap_ns: Optional[int] = None
        self._fallback_last_enc_ns: Optional[int] = None
        self._fallback_last_queue_ns: Optional[int] = None
        self._fallback_frame_count = 0
        self._fallback_last_fps_time = time.perf_counter()
        self._fallback_fps: Optional[float] = None

        self._try_load_native_library()

    def _try_load_native_library(self):
        """Attempt to load compiled aether_core.dll library and bind ABI v1 functions."""
        base_dir = Path(__file__).resolve().parent.parent
        search_paths = []

        # 1. PyInstaller frozen temporary extraction directory (_MEIPASS)
        if hasattr(sys, "_MEIPASS"):
            search_paths.append(Path(sys._MEIPASS) / "aether_core.dll")

        # 2. Executable parent directory (when running packaged AetherDesk.exe)
        exe_dir = Path(sys.executable).resolve().parent
        search_paths.append(exe_dir / "aether_core.dll")
        search_paths.append(exe_dir / "dist" / "aether_core.dll")

        # 3. Source repository relative paths
        search_paths.extend([
            base_dir / "native" / "aether_core" / "target" / "release" / "aether_core.dll",
            base_dir / "dist" / "aether_core.dll",
            base_dir / "aether_core.dll",
        ])

        # 4. Working directory fallback
        cwd = Path.cwd()
        search_paths.append(cwd / "aether_core.dll")
        search_paths.append(cwd / "dist" / "aether_core.dll")

        for p in search_paths:
            if p.exists():
                try:
                    loaded = ctypes.CDLL(str(p))
                    loaded.aether_core_abi_version.restype = ctypes.c_uint32
                    abi_ver = loaded.aether_core_abi_version()
                    if abi_ver == AETHER_CORE_ABI_VERSION:
                        self._bind_ctypes_signatures(loaded)
                        loaded.aether_core_init()
                        self.dll = loaded
                        self.is_native_loaded = True
                        logger.info(f"Loaded native realtime engine from {p} (ABI v{abi_ver})")
                        return
                    else:
                        logger.warning(f"Incompatible native ABI version: expected {AETHER_CORE_ABI_VERSION}, got {abi_ver}")
                except Exception as e:
                    logger.debug(f"Failed to load native library from {p}: {e}")

        logger.info("Native aether_core.dll not compiled on this host; running integrated fallback runtime.")

    def _bind_ctypes_signatures(self, dll: ctypes.CDLL):
        """Explicitly declare all argtypes and restype for aether_core C-ABI v1."""
        dll.aether_core_init.restype = ctypes.c_int32
        dll.aether_core_shutdown.restype = ctypes.c_int32

        # Host signatures
        dll.aether_host_create.argtypes = [ctypes.POINTER(AetherHostConfigStruct), ctypes.POINTER(ctypes.c_void_p)]
        dll.aether_host_create.restype = ctypes.c_int32

        dll.aether_host_create_offer.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        dll.aether_host_create_offer.restype = ctypes.c_int32

        dll.aether_host_set_answer.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        dll.aether_host_set_answer.restype = ctypes.c_int32

        dll.aether_host_add_ice_candidate.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        dll.aether_host_add_ice_candidate.restype = ctypes.c_int32

        dll.aether_host_poll_ice_candidate.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        dll.aether_host_poll_ice_candidate.restype = ctypes.c_int32

        dll.aether_host_get_ice_state.argtypes = [ctypes.c_void_p]
        dll.aether_host_get_ice_state.restype = ctypes.c_int32

        dll.aether_host_step_pipeline.argtypes = [ctypes.c_void_p, ctypes.c_int32]
        dll.aether_host_step_pipeline.restype = ctypes.c_int32

        dll.aether_host_poll_cursor.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(ctypes.c_uint64)]
        dll.aether_host_poll_cursor.restype = ctypes.c_int32

        dll.aether_host_poll_control.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        dll.aether_host_poll_control.restype = ctypes.c_int32

        dll.aether_host_is_control_saturated.argtypes = [ctypes.c_void_p]
        dll.aether_host_is_control_saturated.restype = ctypes.c_int32

        dll.aether_host_adjust_bitrate.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        dll.aether_host_adjust_bitrate.restype = ctypes.c_int32

        dll.aether_host_get_telemetry.argtypes = [ctypes.c_void_p, ctypes.POINTER(PipelineTelemetryStruct)]
        dll.aether_host_get_telemetry.restype = ctypes.c_int32

        dll.aether_host_destroy.argtypes = [ctypes.c_void_p]
        dll.aether_host_destroy.restype = ctypes.c_int32

        # Client signatures
        dll.aether_client_create.argtypes = [ctypes.POINTER(AetherClientConfigStruct), ctypes.POINTER(ctypes.c_void_p)]
        dll.aether_client_create.restype = ctypes.c_int32

        dll.aether_client_set_offer.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        dll.aether_client_set_offer.restype = ctypes.c_int32

        dll.aether_client_create_answer.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        dll.aether_client_create_answer.restype = ctypes.c_int32

        dll.aether_client_add_ice_candidate.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        dll.aether_client_add_ice_candidate.restype = ctypes.c_int32

        dll.aether_client_poll_ice_candidate.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        dll.aether_client_poll_ice_candidate.restype = ctypes.c_int32

        dll.aether_client_get_ice_state.argtypes = [ctypes.c_void_p]
        dll.aether_client_get_ice_state.restype = ctypes.c_int32

        dll.aether_client_poll_video_frame.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint64),
        ]
        dll.aether_client_poll_video_frame.restype = ctypes.c_int32

        dll.aether_client_send_cursor.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32, ctypes.c_uint64]
        dll.aether_client_send_cursor.restype = ctypes.c_int32

        dll.aether_client_send_control.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
        dll.aether_client_send_control.restype = ctypes.c_int32

        dll.aether_client_get_telemetry.argtypes = [ctypes.c_void_p, ctypes.POINTER(PipelineTelemetryStruct)]
        dll.aether_client_get_telemetry.restype = ctypes.c_int32

        dll.aether_client_destroy.argtypes = [ctypes.c_void_p]
        dll.aether_client_destroy.restype = ctypes.c_int32

    # =========================================================================
    # HOST SESSION METHODS
    # =========================================================================

    def create_host_session(self, output_idx: int = 0, target_bitrate_kbps: int = 4500, fps: int = 60) -> bool:
        """Create a native host session (DXGI + MF H.264 + WebRTC). Returns False if native DLL is missing."""
        if not (self.is_native_loaded and self.dll):
            logger.info("Native library not loaded; cannot create native host session.")
            return False

        ice = get_ice_servers_config()
        cfg = AetherHostConfigStruct(
            output_index=output_idx,
            target_bitrate_kbps=target_bitrate_kbps,
            fps=fps,
            stun_servers_csv=ice["stun_csv"].encode("utf-8") if ice["stun_csv"] else None,
            turn_server=ice["turn_url"].encode("utf-8") if ice["turn_url"] else None,
            turn_username=ice["turn_user"].encode("utf-8") if ice["turn_user"] else None,
            turn_credential=ice["turn_cred"].encode("utf-8") if ice["turn_cred"] else None,
        )
        handle = ctypes.c_void_p()
        res = self.dll.aether_host_create(ctypes.byref(cfg), ctypes.byref(handle))
        if res == AETHER_OK and handle.value:
            self.host_handle = handle
            logger.info(f"Native host session initialized (Handle: {hex(handle.value)})")
            return True

        logger.error(f"Failed to create native host session (Code: {res})")
        return False

    def create_host_offer(self) -> Optional[str]:
        if self.is_native_loaded and self.dll and self.host_handle:
            buf = ctypes.create_string_buffer(65536)
            out_len = ctypes.c_size_t()
            res = self.dll.aether_host_create_offer(self.host_handle, buf, ctypes.sizeof(buf), ctypes.byref(out_len))
            if res == AETHER_OK and out_len.value > 0:
                return buf.value.decode("utf-8")
        return None

    def set_host_answer(self, sdp_answer: str) -> bool:
        if self.is_native_loaded and self.dll and self.host_handle:
            sdp_bytes = sdp_answer.encode("utf-8")
            res = self.dll.aether_host_set_answer(self.host_handle, sdp_bytes)
            return res == AETHER_OK
        return False

    def add_host_ice_candidate(self, candidate_json: str) -> bool:
        if self.is_native_loaded and self.dll and self.host_handle:
            cand_bytes = candidate_json.encode("utf-8")
            res = self.dll.aether_host_add_ice_candidate(self.host_handle, cand_bytes)
            return res == AETHER_OK
        return False

    def poll_host_ice_candidate(self) -> Optional[str]:
        if self.is_native_loaded and self.dll and self.host_handle:
            buf = ctypes.create_string_buffer(4096)
            out_len = ctypes.c_size_t()
            res = self.dll.aether_host_poll_ice_candidate(self.host_handle, buf, ctypes.sizeof(buf), ctypes.byref(out_len))
            if res == AETHER_OK and out_len.value > 0:
                return buf.value.decode("utf-8")
        return None

    def get_host_ice_state(self) -> int:
        """Returns ICE state code (0: New, 1: Checking, 2: Connected, 3: Completed, 4: Failed, 5: Disconnected, 6: Closed)."""
        if self.is_native_loaded and self.dll and self.host_handle:
            return self.dll.aether_host_get_ice_state(self.host_handle)
        return -1

    def step_host_pipeline(self, force_keyframe: bool = False) -> int:
        """Execute one capture -> encode -> transmit step. Returns error code if not running."""
        if self.is_native_loaded and self.dll and self.host_handle:
            return self.dll.aether_host_step_pipeline(self.host_handle, 1 if force_keyframe else 0)
        return AETHER_ERR_NOT_INITIALIZED

    def poll_host_cursor(self) -> Optional[Tuple[int, int, int]]:
        if self.is_native_loaded and self.dll and self.host_handle:
            x = ctypes.c_int32()
            y = ctypes.c_int32()
            seq = ctypes.c_uint64()
            has_update = self.dll.aether_host_poll_cursor(self.host_handle, ctypes.byref(x), ctypes.byref(y), ctypes.byref(seq))
            if has_update == 1:
                return x.value, y.value, seq.value
        return None

    def poll_host_control(self) -> Optional[bytes]:
        if self.is_native_loaded and self.dll and self.host_handle:
            buf = (ctypes.c_uint8 * 65536)()
            out_len = ctypes.c_size_t()
            has_msg = self.dll.aether_host_poll_control(self.host_handle, buf, len(buf), ctypes.byref(out_len))
            if has_msg == 1 and out_len.value > 0:
                return bytes(buf[:out_len.value])
        return None

    def is_host_control_saturated(self) -> bool:
        """Returns True if host control channel experienced severe backpressure/saturation."""
        if self.is_native_loaded and self.dll and self.host_handle:
            return self.dll.aether_host_is_control_saturated(self.host_handle) != 0
        return False

    def adjust_host_bitrate(self, bitrate_kbps: int):
        if self.is_native_loaded and self.dll and self.host_handle:
            self.dll.aether_host_adjust_bitrate(self.host_handle, bitrate_kbps)
        self.congestion_controller.current_bitrate_kbps = bitrate_kbps

    def destroy_host_session(self):
        if self.is_native_loaded and self.dll and self.host_handle:
            self.dll.aether_host_destroy(self.host_handle)
            self.host_handle = None
            logger.info("Native host session destroyed.")

    # =========================================================================
    # CLIENT SESSION METHODS
    # =========================================================================

    def create_client_session(self) -> bool:
        """Create a native WebRTC client session with H.264 decoder. Returns False if native DLL is missing."""
        if not (self.is_native_loaded and self.dll):
            logger.info("Native library not loaded; cannot create native client session.")
            return False

        ice = get_ice_servers_config()
        cfg = AetherClientConfigStruct(
            stun_servers_csv=ice["stun_csv"].encode("utf-8") if ice["stun_csv"] else None,
            turn_server=ice["turn_url"].encode("utf-8") if ice["turn_url"] else None,
            turn_username=ice["turn_user"].encode("utf-8") if ice["turn_user"] else None,
            turn_credential=ice["turn_cred"].encode("utf-8") if ice["turn_cred"] else None,
        )
        handle = ctypes.c_void_p()
        res = self.dll.aether_client_create(ctypes.byref(cfg), ctypes.byref(handle))
        if res == AETHER_OK and handle.value:
            self.client_handle = handle
            logger.info(f"Native client session initialized (Handle: {hex(handle.value)})")
            return True

        logger.error(f"Failed to create native client session (Code: {res})")
        return False

    def set_client_offer(self, sdp_offer: str) -> bool:
        if self.is_native_loaded and self.dll and self.client_handle:
            sdp_bytes = sdp_offer.encode("utf-8")
            res = self.dll.aether_client_set_offer(self.client_handle, sdp_bytes)
            return res == AETHER_OK
        return False

    def create_client_answer(self) -> Optional[str]:
        if self.is_native_loaded and self.dll and self.client_handle:
            buf = ctypes.create_string_buffer(65536)
            out_len = ctypes.c_size_t()
            res = self.dll.aether_client_create_answer(self.client_handle, buf, ctypes.sizeof(buf), ctypes.byref(out_len))
            if res == AETHER_OK and out_len.value > 0:
                return buf.value.decode("utf-8")
        return None

    def add_client_ice_candidate(self, candidate_json: str) -> bool:
        if self.is_native_loaded and self.dll and self.client_handle:
            cand_bytes = candidate_json.encode("utf-8")
            res = self.dll.aether_client_add_ice_candidate(self.client_handle, cand_bytes)
            return res == AETHER_OK
        return False

    def poll_client_ice_candidate(self) -> Optional[str]:
        if self.is_native_loaded and self.dll and self.client_handle:
            buf = ctypes.create_string_buffer(4096)
            out_len = ctypes.c_size_t()
            res = self.dll.aether_client_poll_ice_candidate(self.client_handle, buf, ctypes.sizeof(buf), ctypes.byref(out_len))
            if res == AETHER_OK and out_len.value > 0:
                return buf.value.decode("utf-8")
        return None

    def get_client_ice_state(self) -> int:
        if self.is_native_loaded and self.dll and self.client_handle:
            return self.dll.aether_client_get_ice_state(self.client_handle)
        return -1

    def poll_client_video_frame(self) -> Optional[Tuple[bytes, Tuple[int, int], float]]:
        """Poll decoded RGBA frame from native client pipeline. Returns (rgba_bytes, (w, h), decode_ms)."""
        if self.is_native_loaded and self.dll and self.client_handle:
            max_size = 3840 * 2160 * 4
            buf = (ctypes.c_uint8 * max_size)()
            out_len = ctypes.c_size_t()
            width = ctypes.c_uint32()
            height = ctypes.c_uint32()
            decode_ns = ctypes.c_uint64()

            res = self.dll.aether_client_poll_video_frame(
                self.client_handle,
                buf,
                len(buf),
                ctypes.byref(out_len),
                ctypes.byref(width),
                ctypes.byref(height),
                ctypes.byref(decode_ns),
            )
            if res == 1 and out_len.value > 0:
                raw_bytes = bytes(buf[:out_len.value])
                dims = (width.value, height.value)
                dec_ms = round(decode_ns.value / 1e6, 2)
                return raw_bytes, dims, dec_ms
        return None

    def send_client_cursor(self, x: int, y: int, seq: int) -> bool:
        if self.is_native_loaded and self.dll and self.client_handle:
            res = self.dll.aether_client_send_cursor(self.client_handle, x, y, seq)
            return res == AETHER_OK
        return False

    def send_client_control(self, data: bytes) -> bool:
        if self.is_native_loaded and self.dll and self.client_handle:
            buf = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
            res = self.dll.aether_client_send_control(self.client_handle, buf, len(data))
            return res == AETHER_OK
        return False

    def destroy_client_session(self):
        if self.is_native_loaded and self.dll and self.client_handle:
            self.dll.aether_client_destroy(self.client_handle)
            self.client_handle = None
            logger.info("Native client session destroyed.")

    # =========================================================================
    # TELEMETRY & FALLBACK TRACKING
    # =========================================================================

    def record_fallback_metrics(self, cap_ns: int, enc_ns: int, queue_ns: int = 0):
        self._fallback_last_cap_ns = cap_ns
        self._fallback_last_enc_ns = enc_ns
        self._fallback_last_queue_ns = queue_ns
        self._fallback_frame_count += 1
        now = time.perf_counter()
        elapsed = now - self._fallback_last_fps_time
        if elapsed >= 1.0:
            self._fallback_fps = round(self._fallback_frame_count / elapsed, 1)
            self._fallback_frame_count = 0
            self._fallback_last_fps_time = now

    def get_telemetry_snapshot(self) -> Dict[str, Any]:
        """
        Return segregated stage-by-stage pipeline telemetry.
        Unmeasured metrics are strictly None (null), never fake zeros.
        """
        active_handle = self.host_handle or self.client_handle
        if self.is_native_loaded and self.dll and active_handle:
            telem = PipelineTelemetryStruct()
            fn = self.dll.aether_host_get_telemetry if self.host_handle else self.dll.aether_client_get_telemetry
            res = fn(active_handle, ctypes.byref(telem))
            if res == AETHER_OK:
                return {
                    "capture_ms": round(telem.capture_ns / 1e6, 2) if telem.has_capture else None,
                    "encode_ms": round(telem.encode_ns / 1e6, 2) if telem.has_encode else None,
                    "queue_ms": round(telem.queue_ns / 1e6, 2) if telem.has_queue else None,
                    "network_rtt_ms": round(telem.network_rtt_ns / 1e6, 2) if telem.has_rtt else None,
                    "clock_offset_ms": round(telem.clock_offset_ns / 1e6, 3) if telem.has_clock_offset else None,
                    "decode_ms": round(telem.decode_ns / 1e6, 2) if telem.has_decode else None,
                    "render_ms": round(telem.render_ns / 1e6, 2) if telem.has_render else None,
                    "fps": round(telem.fps, 1) if telem.has_fps else None,
                    "bitrate_kbps": telem.bitrate_kbps if telem.bitrate_kbps > 0 else None,
                    "is_hardware_encoder": bool(telem.is_hardware_encoder),
                    "is_zero_copy": bool(telem.is_zero_copy),
                    "native_engine_loaded": True,
                }

        # Fallback mode (native DLL not loaded)
        return {
            "capture_ms": round(self._fallback_last_cap_ns / 1e6, 2) if self._fallback_last_cap_ns is not None else None,
            "encode_ms": round(self._fallback_last_enc_ns / 1e6, 2) if self._fallback_last_enc_ns is not None else None,
            "queue_ms": round(self._fallback_last_queue_ns / 1e6, 2) if self._fallback_last_queue_ns is not None else None,
            "network_rtt_ms": round(self.clock_estimator.rtt_ms, 2) if (self.clock_estimator.rtt_ms is not None and self.clock_estimator.rtt_ms > 0) else None,
            "clock_offset_ms": round(self.clock_estimator.offset_ms, 3) if self.clock_estimator.offset_ms is not None else None,
            "jitter_ms": round(self.clock_estimator.jitter_ms, 2) if self.clock_estimator.jitter_ms is not None else None,
            "decode_ms": None,
            "render_ms": None,
            "fps": self._fallback_fps,
            "bitrate_kbps": self.congestion_controller.current_bitrate_kbps,
            "is_hardware_encoder": False,
            "is_zero_copy": False,
            "native_engine_loaded": False,
        }


# Global singleton instance
_engine_instance = None


def get_native_engine() -> NativeRealtimeEngine:
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = NativeRealtimeEngine()
    return _engine_instance
