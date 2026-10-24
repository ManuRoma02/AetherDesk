import ctypes
from ctypes import wintypes
import time

# Win32 Constants
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_KEYDOWN = 0x0000
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

# Virtual Desktop Metrics
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

# Virtual Key Codes
VK_LWIN = 0x5B
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_SHIFT = 0x10
VK_TAB = 0x09
VK_ESCAPE = 0x1B
VK_DELETE = 0x2E

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_ulonglong)
    ]

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_ulonglong)
    ]

class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD)
    ]

class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT)
    ]

class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("union", _INPUT_UNION)
    ]

class InputHandler:
    """
    TeamViewer-grade input injection engine:
    - Auto-switches thread to active input desktop (UAC/lockscreen/worker thread support).
    - Uses SetCursorPos for pixel accuracy + SendInput with mouse_event/keybd_event fallback.
    - Full multi-monitor and virtual desktop coordinate transformation.
    """

    def __init__(self):
        self.user32 = ctypes.windll.user32
        # Enable Per-Monitor V2 DPI awareness so metrics match screen capture
        try:
            self.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
        self.last_pixel_pos = (0, 0)
        self._winsta_initialized = False

    def _ensure_input_desktop(self) -> bool:
        """Attach caller thread to active Windows input desktop (handles UAC/lockscreen/worker threads)."""
        if not self._winsta_initialized:
            try:
                hwinsta = self.user32.OpenWindowStationW("WinSta0", False, 0x037F)
                if hwinsta:
                    self.user32.SetProcessWindowStation(hwinsta)
                    self._winsta_initialized = True
            except Exception:
                pass

        try:
            # Safe explicit rights: READ + WRITE + SWITCH, fallback to READ + SWITCH
            flags = 0x0001 | 0x0040 | 0x0100
            hdesk = self.user32.OpenInputDesktop(0, False, flags)
            if not hdesk:
                hdesk = self.user32.OpenInputDesktop(0, False, 0x0001 | 0x0100)
            if hdesk:
                res = self.user32.SetThreadDesktop(hdesk)
                self.user32.CloseDesktop(hdesk)
                return bool(res)
        except Exception:
            pass
        return False

    def get_screen_bounds(self) -> tuple[int, int]:
        w = self.user32.GetSystemMetrics(0)
        h = self.user32.GetSystemMetrics(1)
        return (w if w > 0 else 1920, h if h > 0 else 1080)

    def normalize_to_screen(self, norm_x: float, norm_y: float,
                            monitor_bounds: tuple[int, int, int, int] = None) -> tuple[int, int]:
        """Convert normalized (0.0 to 1.0) coordinates to screen pixels."""
        if monitor_bounds:
            src_x, src_y, src_w, src_h = monitor_bounds
            x = int(src_x + norm_x * src_w)
            y = int(src_y + norm_y * src_h)
            return (x, y)
        w, h = self.get_screen_bounds()
        x = int(norm_x * w)
        y = int(norm_y * h)
        return (max(0, min(w - 1, x)), max(0, min(h - 1, y)))

    def _send_input(self, inp: INPUT) -> bool:
        self._ensure_input_desktop()
        res = self.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        return res > 0

    def _calc_coords(self, norm_x: float, norm_y: float,
                     monitor_bounds: tuple[int, int, int, int] = None,
                     virtual_bounds: tuple[int, int, int, int] = None) -> tuple[int, int, int, int]:
        """Convert normalized (0.0 to 1.0) coordinates to screen pixels and virtual desktop absolute units."""
        if monitor_bounds:
            src_x, src_y, src_w, src_h = monitor_bounds
            pixel_x = int(src_x + norm_x * src_w)
            pixel_y = int(src_y + norm_y * src_h)
        else:
            w, h = self.get_screen_bounds()
            pixel_x = int(norm_x * w)
            pixel_y = int(norm_y * h)

        if virtual_bounds:
            vx, vy, vw, vh = virtual_bounds
        else:
            vx = self.user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
            vy = self.user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
            vw = self.user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
            vh = self.user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)

        if vw > 0 and vh > 0:
            norm_vx = (pixel_x - vx) / vw
            norm_vy = (pixel_y - vy) / vh
            abs_x = int(max(0.0, min(1.0, norm_vx)) * 65535)
            abs_y = int(max(0.0, min(1.0, norm_vy)) * 65535)
        else:
            abs_x = int(max(0.0, min(1.0, norm_x)) * 65535)
            abs_y = int(max(0.0, min(1.0, norm_y)) * 65535)

        return (pixel_x, pixel_y, abs_x, abs_y)

    def move_mouse(self, norm_x: float, norm_y: float,
                   monitor_bounds: tuple[int, int, int, int] = None,
                   virtual_bounds: tuple[int, int, int, int] = None) -> tuple[int, int]:
        """Move cursor using physical SetCursorPos and SendInput virtual desk coordinates."""
        self._ensure_input_desktop()
        pixel_x, pixel_y, abs_x, abs_y = self._calc_coords(norm_x, norm_y, monitor_bounds, virtual_bounds)
        self.last_pixel_pos = (pixel_x, pixel_y)

        # 1. Direct hardware/OS pixel positioning
        self.user32.SetCursorPos(pixel_x, pixel_y)

        # 2. Virtual desktop coordinate injection
        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.union.mi.dx = abs_x
        inp.union.mi.dy = abs_y
        inp.union.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK

        if not self._send_input(inp):
            self.user32.mouse_event(
                MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
                abs_x, abs_y, 0, 0
            )
        return (pixel_x, pixel_y)

    def mouse_down(self, button: str = "left", norm_x: float = None, norm_y: float = None,
                   monitor_bounds=None, virtual_bounds=None):
        self._ensure_input_desktop()
        abs_x, abs_y = 0, 0
        use_abs = False
        if norm_x is not None and norm_y is not None:
            pixel_x, pixel_y, abs_x, abs_y = self._calc_coords(norm_x, norm_y, monitor_bounds, virtual_bounds)
            self.last_pixel_pos = (pixel_x, pixel_y)
            self.user32.SetCursorPos(pixel_x, pixel_y)
            use_abs = True

        flag = 0
        if button == "left":
            flag = MOUSEEVENTF_LEFTDOWN
        elif button == "right":
            flag = MOUSEEVENTF_RIGHTDOWN
        elif button == "middle":
            flag = MOUSEEVENTF_MIDDLEDOWN
        else:
            return

        inp = INPUT()
        inp.type = INPUT_MOUSE
        if use_abs:
            inp.union.mi.dx = abs_x
            inp.union.mi.dy = abs_y
            inp.union.mi.dwFlags = flag | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
        else:
            inp.union.mi.dwFlags = flag

        if not self._send_input(inp):
            if use_abs:
                self.user32.mouse_event(flag | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, abs_x, abs_y, 0, 0)
            else:
                self.user32.mouse_event(flag, 0, 0, 0, 0)

    def mouse_up(self, button: str = "left", norm_x: float = None, norm_y: float = None,
                 monitor_bounds=None, virtual_bounds=None):
        self._ensure_input_desktop()
        abs_x, abs_y = 0, 0
        use_abs = False
        if norm_x is not None and norm_y is not None:
            pixel_x, pixel_y, abs_x, abs_y = self._calc_coords(norm_x, norm_y, monitor_bounds, virtual_bounds)
            self.last_pixel_pos = (pixel_x, pixel_y)
            self.user32.SetCursorPos(pixel_x, pixel_y)
            use_abs = True

        flag = 0
        if button == "left":
            flag = MOUSEEVENTF_LEFTUP
        elif button == "right":
            flag = MOUSEEVENTF_RIGHTUP
        elif button == "middle":
            flag = MOUSEEVENTF_MIDDLEUP
        else:
            return

        inp = INPUT()
        inp.type = INPUT_MOUSE
        if use_abs:
            inp.union.mi.dx = abs_x
            inp.union.mi.dy = abs_y
            inp.union.mi.dwFlags = flag | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
        else:
            inp.union.mi.dwFlags = flag

        if not self._send_input(inp):
            if use_abs:
                self.user32.mouse_event(flag | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, abs_x, abs_y, 0, 0)
            else:
                self.user32.mouse_event(flag, 0, 0, 0, 0)

    def mouse_wheel(self, delta: int, norm_x: float = None, norm_y: float = None,
                    monitor_bounds=None, virtual_bounds=None):
        if norm_x is not None and norm_y is not None:
            self.move_mouse(norm_x, norm_y, monitor_bounds, virtual_bounds)

        self._ensure_input_desktop()
        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.union.mi.dwFlags = MOUSEEVENTF_WHEEL
        inp.union.mi.mouseData = delta
        if not self._send_input(inp):
            self.user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, delta, 0)

    def key_down(self, vk_code: int):
        self._ensure_input_desktop()
        flags = KEYEVENTF_KEYDOWN
        if vk_code in (0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C):
            flags |= KEYEVENTF_EXTENDEDKEY

        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.union.ki.wVk = vk_code
        inp.union.ki.dwFlags = flags
        if not self._send_input(inp):
            self.user32.keybd_event(vk_code, 0, flags, 0)

    def key_up(self, vk_code: int):
        self._ensure_input_desktop()
        flags = KEYEVENTF_KEYUP
        if vk_code in (0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C):
            flags |= KEYEVENTF_EXTENDEDKEY

        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.union.ki.wVk = vk_code
        inp.union.ki.dwFlags = flags
        if not self._send_input(inp):
            self.user32.keybd_event(vk_code, 0, flags, 0)

    def send_unicode_char(self, char: str):
        """Send unicode character directly (immune to layout differences)."""
        self._ensure_input_desktop()
        code = ord(char)
        inp_down = INPUT()
        inp_down.type = INPUT_KEYBOARD
        inp_down.union.ki.wScan = code
        inp_down.union.ki.dwFlags = KEYEVENTF_UNICODE
        self._send_input(inp_down)

        inp_up = INPUT()
        inp_up.type = INPUT_KEYBOARD
        inp_up.union.ki.wScan = code
        inp_up.union.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
        self._send_input(inp_up)

    def send_key_combination(self, vk_list: list[int]):
        for vk in vk_list:
            self.key_down(vk)
        time.sleep(0.02)
        for vk in reversed(vk_list):
            self.key_up(vk)

    def send_special(self, command: str) -> bool:
        """Execute special system key combinations."""
        cmd = command.lower()
        self._ensure_input_desktop()
        if cmd == "win_d":
            self.send_key_combination([VK_LWIN, 0x44]) # Win + D
            return True
        elif cmd == "win_e":
            self.send_key_combination([VK_LWIN, 0x45]) # Win + E (Explorer)
            return True
        elif cmd == "win_r":
            self.send_key_combination([VK_LWIN, 0x52]) # Win + R (Run)
            return True
        elif cmd == "alt_tab":
            self.send_key_combination([VK_MENU, VK_TAB]) # Alt + Tab
            return True
        elif cmd in ("task_mgr", "taskmgr"):
            self.send_key_combination([VK_CONTROL, VK_SHIFT, VK_ESCAPE]) # Ctrl+Shift+Esc
            return True
        elif cmd in ("lock", "lock_screen", "win_l"):
            try:
                self.user32.LockWorkStation()
                return True
            except Exception:
                self.send_key_combination([VK_LWIN, 0x4C]) # Win + L
                return True
        elif cmd in ("ctrl_alt_del", "sas"):
            try:
                sas_func = getattr(ctypes.windll.sas, "SendSAS", None)
                if sas_func:
                    sas_func(0)
                    return True
            except Exception:
                pass
            self.send_key_combination([VK_CONTROL, VK_MENU, VK_DELETE])
            return True
        return False
