import io
import ctypes
from ctypes import wintypes
from PIL import Image
import win32api

try:
    import cv2
    import numpy as np
    HAS_CV2 = True
except ImportError:
    cv2 = None
    np = None
    HAS_CV2 = False

# Win32 Constants
DESKTOP_READOBJECTS = 0x0001
DESKTOP_WRITEOBJECTS = 0x0080
DESKTOP_SWITCHDESKTOP = 0x0100
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
SRCCOPY = 0x00CC0020
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
        self.current_monitor_id = 0
        self.switch_desktop()

        self._src_dc = None
        self._mem_dc = None
        self._bitmap = None
        self._last_dims = (0, 0, 0, 0)
        self._buffer = None
        self._last_thumb = None
        self._last_cursor = (0, 0)
        self._cached_mon_bounds = None
        self._cached_virt_bounds = None
        self._bounds_cache_time = 0.0

    def switch_desktop(self) -> bool:
        """Switch current thread desktop to the active input desktop (handles UAC/lockscreen)."""
        flags = DESKTOP_READOBJECTS | DESKTOP_WRITEOBJECTS | DESKTOP_SWITCHDESKTOP
        hdesk = self.user32.OpenInputDesktop(0, False, flags)
        if hdesk:
            res = self.user32.SetThreadDesktop(hdesk)
            if res:
                if self.current_desktop and self.current_desktop != hdesk:
                    try:
                        self.user32.CloseDesktop(self.current_desktop)
                    except Exception:
                        pass
                self.current_desktop = hdesk
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
        src_x, src_y, screen_w, screen_h = self.get_active_monitor_bounds()
        cur_x, cur_y = self.get_cursor_position()

        out_w = self.target_width if self.target_width else screen_w
        out_h = self.target_height if self.target_height else screen_h

        # Recreate DCs/Bitmaps if dimensions change
        dims = (screen_w, screen_h, out_w, out_h)
        if dims != self._last_dims:
            self._cleanup_gdi()
            self._src_dc = self.user32.GetDC(0)
            self._mem_dc = self.gdi32.CreateCompatibleDC(self._src_dc)
            self._bitmap = self.gdi32.CreateCompatibleBitmap(self._src_dc, out_w, out_h)
            self.gdi32.SelectObject(self._mem_dc, self._bitmap)
            self.gdi32.SetStretchBltMode(self._mem_dc, COLORONCOLOR)
            self._buffer = (ctypes.c_char * (out_w * out_h * 4))()
            self._last_dims = dims

        # StretchBlt from screen to memory DC
        blt_res = self.gdi32.StretchBlt(
            self._mem_dc, 0, 0, out_w, out_h,
            self._src_dc, src_x, src_y, screen_w, screen_h,
            SRCCOPY
        )

        if not blt_res:
            # Try switching desktop and retry
            self.switch_desktop()
            self._cleanup_gdi()
            self._src_dc = self.user32.GetDC(0)
            self._mem_dc = self.gdi32.CreateCompatibleDC(self._src_dc)
            self._bitmap = self.gdi32.CreateCompatibleBitmap(self._src_dc, out_w, out_h)
            self.gdi32.SelectObject(self._mem_dc, self._bitmap)
            self.gdi32.SetStretchBltMode(self._mem_dc, COLORONCOLOR)
            self.gdi32.StretchBlt(
                self._mem_dc, 0, 0, out_w, out_h,
                self._src_dc, src_x, src_y, screen_w, screen_h,
                SRCCOPY
            )

        # Draw cursor into memory DC
        try:
            ci = CURSORINFO()
            ci.cbSize = ctypes.sizeof(CURSORINFO)
            if self.user32.GetCursorInfo(ctypes.byref(ci)) and (ci.flags & 1):
                # Scale cursor position relative to current monitor bounds
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

            thumb = cv2.resize(bgr, (32, 18), interpolation=cv2.INTER_NEAREST)
            if only_if_changed and self._last_thumb is not None:
                is_same_thumb = False
                if isinstance(self._last_thumb, np.ndarray):
                    is_same_thumb = np.array_equal(thumb, self._last_thumb)
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
            img = Image.frombuffer("RGBA", (out_w, out_h), self._buffer, "raw", "BGRA", 0, 1)
            thumb = img.resize((32, 18), Image.Resampling.NEAREST)
            thumb_bytes = thumb.tobytes()

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
            img.convert("RGB").save(bio, format="JPEG", quality=self.quality, subsampling=2, optimize=False)
            jpeg_bytes = bio.getvalue()

        return (jpeg_bytes, (screen_w, screen_h), cur_pos)

    def _cleanup_gdi(self):
        if self._bitmap:
            self.gdi32.DeleteObject(self._bitmap)
            self._bitmap = None
        if self._mem_dc:
            self.gdi32.DeleteDC(self._mem_dc)
            self._mem_dc = None
        if self._src_dc:
            self.user32.ReleaseDC(0, self._src_dc)
            self._src_dc = None

    def close(self):
        self._cleanup_gdi()
        if self.current_desktop:
            try:
                self.user32.CloseDesktop(self.current_desktop)
            except Exception:
                pass
            self.current_desktop = None
