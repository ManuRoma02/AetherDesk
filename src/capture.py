import ctypes
from ctypes import wintypes
import cv2
import numpy as np

# Win32 Constants
DESKTOP_READOBJECTS = 0x0001
DESKTOP_WRITEOBJECTS = 0x0080
DESKTOP_SWITCHDESKTOP = 0x0100
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
SRCCOPY = 0x00CC0020
COLORONCOLOR = 3

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

    def __init__(self, target_width: int = None, target_height: int = None, quality: int = 75):
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
        self.switch_desktop()

        self._src_dc = None
        self._mem_dc = None
        self._bitmap = None
        self._last_dims = (0, 0, 0, 0)
        self._buffer = None

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

    def get_screen_resolution(self) -> tuple[int, int]:
        w = self.user32.GetSystemMetrics(0)
        h = self.user32.GetSystemMetrics(1)
        return (w if w > 0 else 1920, h if h > 0 else 1080)

    def get_cursor_position(self) -> tuple[int, int]:
        pt = POINT()
        if self.user32.GetCursorPos(ctypes.byref(pt)):
            return (pt.x, pt.y)
        return (0, 0)

    def capture_frame(self) -> tuple[bytes, tuple[int, int], tuple[int, int]]:
        """Capture the current frame, encode as JPEG, and return (jpeg_bytes, (screen_w, screen_h), (cur_x, cur_y))."""
        screen_w, screen_h = self.get_screen_resolution()
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
            self._src_dc, 0, 0, screen_w, screen_h,
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
                self._src_dc, 0, 0, screen_w, screen_h,
                SRCCOPY
            )

        # Draw cursor into memory DC
        try:
            ci = CURSORINFO()
            ci.cbSize = ctypes.sizeof(CURSORINFO)
            if self.user32.GetCursorInfo(ctypes.byref(ci)) and (ci.flags & 1):
                # Scale cursor position to output dimensions
                scaled_cur_x = int(ci.ptScreenPos.x * (out_w / screen_w))
                scaled_cur_y = int(ci.ptScreenPos.y * (out_h / screen_h))
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

        self.gdi32.GetDIBits(self._mem_dc, self._bitmap, 0, out_h, self._buffer, ctypes.byref(bmi), 0)

        # Encode to JPEG
        arr = np.frombuffer(self._buffer, dtype=np.uint8).reshape((out_h, out_w, 4))
        # Keep BGR channels
        bgr = arr[:, :, :3]
        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), self.quality]
        success, enc = cv2.imencode('.jpg', bgr, encode_params)

        jpeg_bytes = enc.tobytes() if success else b""
        return (jpeg_bytes, (screen_w, screen_h), (cur_x, cur_y))

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
