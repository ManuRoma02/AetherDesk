import io
import logging
import ctypes
from ctypes import wintypes
from PIL import Image
import win32api

logger = logging.getLogger("DesktopCapture")

def _ensure_interactive_desktop():
    """Ensure calling thread is attached to the interactive WinSta0\\Default desktop."""
    try:
        user32 = ctypes.windll.user32
        hwinsta = user32.OpenWindowStationW("WinSta0", False, 0x037F)
        if hwinsta:
            user32.SetProcessWindowStation(hwinsta)
            user32.CloseWindowStation(hwinsta)
        hdesk = user32.OpenDesktopW("Default", 0, False, 0x01FF)
        if hdesk:
            user32.SetThreadDesktop(hdesk)
            user32.CloseDesktop(hdesk)
    except Exception:
        pass

# Attach calling thread to interactive desktop before any subsystem initialization
_ensure_interactive_desktop()

try:
    import cv2
    import numpy as np
    HAS_CV2 = True
except ImportError:
    cv2 = None
    np = None
    HAS_CV2 = False

# Lazy-loaded DXcam handle. Module-level import is intentionally omitted to prevent
# DXGI adapter enumeration from attaching DirectX hooks to the calling thread,
# which would cause subsequent Win32 SetThreadDesktop() calls to fail with ERROR_BUSY (170).
HAS_DXCAM = False
dxcam = None

# Win32 Constants
DESKTOP_READOBJECTS = 0x0001
DESKTOP_WRITEOBJECTS = 0x0080
DESKTOP_SWITCHDESKTOP = 0x0100
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
BLT_ROP = SRCCOPY | CAPTUREBLT
COLORONCOLOR = 3

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

class CURSORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hCursor", wintypes.HANDLE),
        ("ptScreenPos", POINT)
    ]

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]

class DesktopCapture:
    """High-performance Windows screen capture engine with secure UAC desktop switching."""

    def __init__(self, target_width: int = None, target_height: int = None, quality: int = 55):
        self.quality = quality
        self.target_width = target_width
        self.target_height = target_height

        self.user32 = ctypes.windll.user32
        self.gdi32 = ctypes.windll.gdi32

        # Enable DPI awareness
        try:
            self.user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        except Exception:
            try:
                self.user32.SetProcessDPIAware()
            except Exception:
                pass

        self.current_desktop = None
        self._current_desktop_name = ""
        self.current_monitor_id = 0
        self._frame_count = 0
        self._src_dc = None
        self._mem_dc = None
        self._bitmap = None
        self._last_dims = (0, 0, 0, 0)
        self._buffer = None
        self._last_thumb = None
        self._last_cursor = (0, 0)
        self._cached_mon_bounds = None
        self._cached_virt_bounds = None
        self._dxcam = None

        self.switch_desktop()
        self._bounds_cache_time = 0.0

        self._dxcam = None
        self._init_dxcam()

    def _init_dxcam(self):
        """Try initializing DXGI Desktop Duplication engine via DXcam if explicitly enabled and supported."""
        import os
        if os.environ.get("AETHER_ENABLE_DXCAM", "0") != "1":
            # Disabled by default: GDI StretchBlt with CAPTUREBLT runs at 60+ FPS,
            # fully supports desktop switching (UAC/lockscreen), and avoids
            # DXGI_ERROR_ACCESS_DENIED (0x80070005) on hybrid GPU laptops.
            self._dxcam = None
            return

        global dxcam, HAS_DXCAM
        try:
            if dxcam is None:
                import dxcam as _dxcam
                dxcam = _dxcam
                HAS_DXCAM = True
            out_idx = max(0, self.current_monitor_id)
            self._dxcam = dxcam.create(output_idx=out_idx)
            # Verify grab actually works on active GPU (rejects hybrid GPU Access Denied)
            test_f = self._dxcam.grab()
            if test_f is None:
                self._dxcam.release()
                self._dxcam = None
        except Exception:
            self._dxcam = None

    def _get_desktop_name(self, hdesk) -> str:
        """Query Unicode name of desktop object via GetUserObjectInformationW."""
        if not hdesk:
            return ""
        buf = ctypes.create_unicode_buffer(256)
        if self.user32.GetUserObjectInformationW(hdesk, 2, buf, ctypes.sizeof(buf), None):
            return buf.value
        return ""

    def switch_desktop(self) -> bool:
        """Switch current thread desktop to the active input desktop (handles UAC/lockscreen/password screen)."""
        try:
            hwinsta = self.user32.OpenWindowStationW("WinSta0", False, 0x037F)
            if hwinsta:
                self.user32.SetProcessWindowStation(hwinsta)
                self.user32.CloseWindowStation(hwinsta)
        except Exception:
            pass

        flags = DESKTOP_READOBJECTS | DESKTOP_WRITEOBJECTS | DESKTOP_SWITCHDESKTOP
        hdesk = self.user32.OpenInputDesktop(0, False, flags)
        if not hdesk:
            hdesk = self.user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS | DESKTOP_SWITCHDESKTOP)

        desk_name = self._get_desktop_name(hdesk) if hdesk else ""
        if not hdesk or "exebox" in desk_name.lower():
            if hdesk:
                try:
                    self.user32.CloseDesktop(hdesk)
                except Exception:
                    pass
            hdesk = self.user32.OpenDesktopW("Default", 0, False, 0x01FF)

        if hdesk:
            res = self.user32.SetThreadDesktop(hdesk)
            if res:
                if self.current_desktop and self.current_desktop != hdesk:
                    try:
                        self.user32.CloseDesktop(self.current_desktop)
                    except Exception:
                        pass
                self.current_desktop = hdesk
                self._current_desktop_name = self._get_desktop_name(hdesk)
                self._cleanup_gdi()
                return True
            else:
                self.user32.CloseDesktop(hdesk)
        return False

    def get_monitors_list(self) -> list[dict]:
        """Returns list of connected display monitors and virtual desktop option."""
        try:
            mons = win32api.EnumDisplayMonitors()
        except Exception:
            mons = []

        results = []
        if not mons:
            w, h = self.get_screen_resolution()
            results.append({"id": 0, "name": "Monitor Principale", "rect": (0, 0, w, h), "width": w, "height": h})
            return results

        for i, m in enumerate(mons):
            rect = m[2]
            w = rect[2] - rect[0]
            h = rect[3] - rect[1]
            results.append({
                "id": i,
                "name": f"Monitor {i + 1} ({w}x{h})",
                "rect": rect,
                "width": w,
                "height": h
            })

        if len(mons) > 1:
            vx = self.user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
            vy = self.user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
            vw = self.user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
            vh = self.user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
            results.append({
                "id": -1,
                "name": "Tutti i Monitor",
                "rect": (vx, vy, vx + vw, vy + vh),
                "width": vw,
                "height": vh
            })

        return results

    def set_monitor(self, monitor_id: int):
        self.current_monitor_id = monitor_id
        self._last_dims = (0, 0, 0, 0)
        self._last_thumb = None
        self._cached_mon_bounds = None
        self._cached_virt_bounds = None
        self._bounds_cache_time = 0.0
        if self._dxcam:
            try:
                self._dxcam.release()
            except Exception:
                pass
            self._dxcam = None
        self._init_dxcam()

    def get_virtual_desktop_bounds(self) -> tuple[int, int, int, int]:
        """Returns (vx, vy, vw, vh) for virtual desktop spanning all monitors (cached 3s)."""
        import time
        now = time.time()
        if self._cached_virt_bounds and (now - self._bounds_cache_time < 3.0):
            return self._cached_virt_bounds
        vx = self.user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = self.user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw = self.user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        vh = self.user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        if vw <= 0 or vh <= 0:
            w, h = self.get_screen_resolution()
            res = (0, 0, w, h)
        else:
            res = (vx, vy, vw, vh)
        self._cached_virt_bounds = res
        self._bounds_cache_time = now
        return res

    def get_active_monitor_bounds(self) -> tuple[int, int, int, int]:
        """Returns (src_x, src_y, src_w, src_h) for currently active monitor (cached 3s)."""
        import time
        now = time.time()
        if self._cached_mon_bounds and (now - self._bounds_cache_time < 3.0):
            return self._cached_mon_bounds
        self._bounds_cache_time = now
        mons = self.get_monitors_list()
        for m in mons:
            if m["id"] == self.current_monitor_id:
                rect = m["rect"]
                res = (rect[0], rect[1], m["width"], m["height"])
                self._cached_mon_bounds = res
                return res

        # Fallback to primary monitor
        w, h = self.get_screen_resolution()
        res = (0, 0, w, h)
        self._cached_mon_bounds = res
        return res

    def get_screen_resolution(self) -> tuple[int, int]:
        w = self.user32.GetSystemMetrics(0)
        h = self.user32.GetSystemMetrics(1)
        return (w if w > 0 else 1920, h if h > 0 else 1080)

    def get_cursor_position(self) -> tuple[int, int]:
        pt = POINT()
        if self.user32.GetCursorPos(ctypes.byref(pt)):
            return (pt.x, pt.y)
        return (0, 0)

    def capture_frame(self, only_if_changed: bool = False) -> tuple[bytes | None, tuple[int, int], tuple[int, int]]:
        """Capture the current frame, encode as JPEG, and return (jpeg_bytes, (screen_w, screen_h), (cur_x, cur_y))."""
        self._frame_count += 1
        if self._frame_count % 30 == 0:
            try:
                h_in = self.user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS | DESKTOP_SWITCHDESKTOP)
                if h_in:
                    in_name = self._get_desktop_name(h_in)
                    if in_name and in_name.lower() != self._current_desktop_name.lower():
                        self.switch_desktop()
                    self.user32.CloseDesktop(h_in)
            except Exception:
                pass

        src_x, src_y, screen_w, screen_h = self.get_active_monitor_bounds()
        cur_x, cur_y = self.get_cursor_position()
        cur_pos = (cur_x, cur_y)

        out_w = self.target_width if self.target_width else screen_w
        out_h = self.target_height if self.target_height else screen_h

        # 1. High-Performance DXcam (Desktop Duplication) path
        if self._dxcam is not None and HAS_CV2 and cv2 is not None and np is not None:
            try:
                frame = self._dxcam.grab()
                # If frame is None and only_if_changed requested, DXGI indicates compositor has no dirty rects
                if frame is None and only_if_changed and cur_pos == self._last_cursor and self._last_thumb is not None:
                    return (None, (screen_w, screen_h), cur_pos)

                if frame is None:
                    self._dxcam_none_count = getattr(self, "_dxcam_none_count", 0) + 1
                    if self._dxcam_none_count > 3:
                        try:
                            self._dxcam.release()
                        except Exception:
                            pass
                        self._dxcam = None
                else:
                    self._dxcam_none_count = 0

                if frame is not None:
                    if frame.shape[2] == 4:
                        bgr = frame[:, :, :3]
                    else:
                        bgr = frame

                    if (out_w, out_h) != (bgr.shape[1], bgr.shape[0]):
                        bgr = cv2.resize(bgr, (out_w, out_h), interpolation=cv2.INTER_LINEAR)

                    # High-fidelity change detection (160x90 INTER_NEAREST)
                    thumb = cv2.resize(bgr, (160, 90), interpolation=cv2.INTER_NEAREST)
                    if only_if_changed and self._last_thumb is not None:
                        is_same = False
                        if isinstance(self._last_thumb, np.ndarray):
                            diff = cv2.absdiff(thumb, self._last_thumb)
                            is_same = bool(np.max(diff) < 2)
                        if cur_pos == self._last_cursor and is_same:
                            return (None, (screen_w, screen_h), cur_pos)

                    self._last_thumb = thumb
                    self._last_cursor = cur_pos

                    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), self.quality]
                    success, enc = cv2.imencode('.jpg', bgr, encode_params)
                    jpeg_bytes = enc.tobytes() if success else b""
                    return (jpeg_bytes, (screen_w, screen_h), cur_pos)
            except Exception:
                # Graceful fallback to GDI if DXGI surface becomes invalid
                self._dxcam = None

        # 2. GDI Fallback Path
        dims = (screen_w, screen_h, out_w, out_h)
        if dims != self._last_dims or not self._src_dc or not self._mem_dc:
            self._cleanup_gdi()
            self._src_dc = self.user32.GetDC(0)
            self._mem_dc = self.gdi32.CreateCompatibleDC(self._src_dc)
            self._bitmap = self.gdi32.CreateCompatibleBitmap(self._src_dc, out_w, out_h)
            self.gdi32.SelectObject(self._mem_dc, self._bitmap)
            self.gdi32.SetStretchBltMode(self._mem_dc, COLORONCOLOR)
            self._buffer = (ctypes.c_char * (out_w * out_h * 4))()
            self._last_dims = dims

        # StretchBlt from screen to memory DC (with CAPTUREBLT for DWM hardware accelerated surfaces)
        blt_res = self.gdi32.StretchBlt(
            self._mem_dc, 0, 0, out_w, out_h,
            self._src_dc, src_x, src_y, screen_w, screen_h,
            BLT_ROP
        )

        if not blt_res:
            self.switch_desktop()
            self._cleanup_gdi()
            self._src_dc = self.user32.GetDC(0)
            self._mem_dc = self.gdi32.CreateCompatibleDC(self._src_dc)
            self._bitmap = self.gdi32.CreateCompatibleBitmap(self._src_dc, out_w, out_h)
            self.gdi32.SelectObject(self._mem_dc, self._bitmap)
            self.gdi32.SetStretchBltMode(self._mem_dc, COLORONCOLOR)
            self._buffer = (ctypes.c_char * (out_w * out_h * 4))()
            self._last_dims = dims
            self.gdi32.StretchBlt(
                self._mem_dc, 0, 0, out_w, out_h,
                self._src_dc, src_x, src_y, screen_w, screen_h,
                BLT_ROP
            )

        # Draw cursor into memory DC
        try:
            ci = CURSORINFO()
            ci.cbSize = ctypes.sizeof(CURSORINFO)
            if self.user32.GetCursorInfo(ctypes.byref(ci)) and (ci.flags & 1):
                rel_cur_x = ci.ptScreenPos.x - src_x
                rel_cur_y = ci.ptScreenPos.y - src_y
                scaled_cur_x = int(rel_cur_x * (out_w / screen_w))
                scaled_cur_y = int(rel_cur_y * (out_h / screen_h))
                self.user32.DrawIcon(self._mem_dc, scaled_cur_x, scaled_cur_y, ci.hCursor)
        except Exception:
            pass

        # Extract bits
        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = out_w
        bmi.biHeight = -out_h  # Top-down DIB
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0

        if not self._buffer:
            return (b"", (screen_w, screen_h), (cur_x, cur_y))

        self.gdi32.GetDIBits(self._mem_dc, self._bitmap, 0, out_h, self._buffer, ctypes.byref(bmi), 0)

        # Encode to JPEG
        cur_pos = (cur_x, cur_y)

        if HAS_CV2 and cv2 is not None and np is not None:
            # High-speed OpenCV path
            arr = np.frombuffer(self._buffer, dtype=np.uint8).reshape((out_h, out_w, 4))
            bgr = arr[:, :, :3]

            thumb = cv2.resize(bgr, (160, 90), interpolation=cv2.INTER_NEAREST)

            # Active black screen self-healing: if frame is completely black, re-sync desktop handle
            if thumb.max() == 0 and not getattr(self, "_recovering_black", False):
                self._recovering_black = True
                self.switch_desktop()
                self._cleanup_gdi()
                recov_res = self.capture_frame(only_if_changed=False)
                self._recovering_black = False
                return recov_res

            if only_if_changed and self._last_thumb is not None:
                is_same_thumb = False
                if isinstance(self._last_thumb, np.ndarray):
                    diff = cv2.absdiff(thumb, self._last_thumb)
                    is_same_thumb = bool(np.max(diff) < 2)
                elif isinstance(self._last_thumb, bytes):
                    is_same_thumb = (thumb.tobytes() == self._last_thumb)

                if cur_pos == self._last_cursor and is_same_thumb:
                    return (None, (screen_w, screen_h), cur_pos)

            self._last_thumb = thumb
            self._last_cursor = cur_pos

            encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), self.quality]
            success, enc = cv2.imencode('.jpg', bgr, encode_params)
            jpeg_bytes = enc.tobytes() if success else b""
        else:
            # Native Pillow fallback (zero C-extension dependency, bundled in standalone binary)
            img = Image.frombuffer("RGB", (out_w, out_h), self._buffer, "raw", "BGRX", 0, 1)
            thumb = img.resize((160, 90), Image.Resampling.NEAREST)
            thumb_bytes = thumb.tobytes()

            if max(thumb_bytes) == 0 and not getattr(self, "_recovering_black", False):
                self._recovering_black = True
                self.switch_desktop()
                self._cleanup_gdi()
                recov_res = self.capture_frame(only_if_changed=False)
                self._recovering_black = False
                return recov_res

            if only_if_changed and self._last_thumb is not None:
                is_same_thumb = False
                if isinstance(self._last_thumb, bytes):
                    is_same_thumb = (thumb_bytes == self._last_thumb)
                elif hasattr(self._last_thumb, "tobytes"):
                    is_same_thumb = (thumb_bytes == self._last_thumb.tobytes())

                if cur_pos == self._last_cursor and is_same_thumb:
                    return (None, (screen_w, screen_h), cur_pos)

            self._last_thumb = thumb_bytes
            self._last_cursor = cur_pos

            bio = io.BytesIO()
            img.save(bio, format="JPEG", quality=self.quality, subsampling=2, optimize=False)
            jpeg_bytes = bio.getvalue()

        return (jpeg_bytes, (screen_w, screen_h), cur_pos)

    def _cleanup_gdi(self):
        if getattr(self, "_bitmap", None):
            try:
                self.gdi32.DeleteObject(self._bitmap)
            except Exception:
                pass
            self._bitmap = None
        if getattr(self, "_mem_dc", None):
            try:
                self.gdi32.DeleteDC(self._mem_dc)
            except Exception:
                pass
            self._mem_dc = None
        if getattr(self, "_src_dc", None):
            try:
                self.user32.ReleaseDC(0, self._src_dc)
            except Exception:
                pass
            self._src_dc = None
        self._last_dims = (0, 0, 0, 0)
        self._buffer = None

    def close(self):
        self._cleanup_gdi()
        if self._dxcam:
            try:
                self._dxcam.release()
            except Exception:
                pass
            self._dxcam = None
        if self.current_desktop:
            try:
                self.user32.CloseDesktop(self.current_desktop)
            except Exception:
                pass
            self.current_desktop = None
