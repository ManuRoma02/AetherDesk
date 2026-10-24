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

VK_MAP = {
    "ctrl": 0x11, "control": 0x11, "lctrl": 0xA2, "rctrl": 0xA3,
    "alt": 0x12, "menu": 0x12, "lalt": 0xA4, "ralt": 0xA5,
    "shift": 0x10, "lshift": 0xA0, "rshift": 0xA1,
    "win": 0x5B, "windows": 0x5B, "super": 0x5B, "cmd": 0x5B,
    "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "space": 0x20, "spacebar": 0x20,
    "up": 0x26, "arrowup": 0x26, "arrow_up": 0x26,
    "down": 0x28, "arrowdown": 0x28, "arrow_down": 0x28,
    "left": 0x25, "arrowleft": 0x25, "arrow_left": 0x25,
    "right": 0x27, "arrowright": 0x27, "arrow_right": 0x27,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "insert": 0x2D,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "a": 0x41, "b": 0x42, "c": 0x43, "d": 0x44, "e": 0x45, "f": 0x46, "g": 0x47,
    "h": 0x48, "i": 0x49, "j": 0x4A, "k": 0x4B, "l": 0x4C, "m": 0x4D, "n": 0x4E,
    "o": 0x4F, "p": 0x50, "q": 0x51, "r": 0x52, "s": 0x53, "t": 0x54, "u": 0x55,
    "v": 0x56, "w": 0x57, "x": 0x58, "y": 0x59, "z": 0x5A,
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
}

def normalize_button(button) -> str:
    """Normalize any button representation (int, str, mobile/web) to 'left'|'right'|'middle'."""
    if button in (0, "0", "left", "primary", "btn0", None):
        return "left"
    if button in (2, "2", "right", "secondary", "context", "btn2"):
        return "right"
    if button in (1, "1", "middle", "btn1"):
        return "middle"
    return "left"

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
        self._last_desktop_check = 0.0
        self._pressed_keys = set()

    def _ensure_input_desktop(self, force: bool = False) -> bool:
        """Attach caller thread to active Windows input desktop (handles UAC/lockscreen/worker threads)."""
        now = time.time()
        if not force and (now - self._last_desktop_check < 2.0):
            return True
        self._last_desktop_check = now

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
        res = self.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        if res == 0:
            # SendInput failed, try re-attaching thread to active input desktop (e.g. UAC or lock screen)
            self._ensure_input_desktop(force=True)
            res = self.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        return res > 0

    def _calc_coords(self, norm_x: float, norm_y: float,
                     monitor_bounds: tuple[int, int, int, int] = None,
                     virtual_bounds: tuple[int, int, int, int] = None) -> tuple[int, int, int, int]:
        """Convert normalized (0.0 to 1.0) coordinates to screen pixels and virtual desktop absolute units."""
        # Auto-detect if coordinates were sent in raw pixels (>1.0) instead of normalized 0..1
        if norm_x is not None and norm_x > 1.0:
            ref_w = monitor_bounds[2] if monitor_bounds else self.get_screen_bounds()[0]
            norm_x = norm_x / ref_w if ref_w > 0 else norm_x
        if norm_y is not None and norm_y > 1.0:
            ref_h = monitor_bounds[3] if monitor_bounds else self.get_screen_bounds()[1]
            norm_y = norm_y / ref_h if ref_h > 0 else norm_y

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

    def move_mouse_relative(self, dx: float, dy: float) -> tuple[int, int]:
        """Move cursor relatively (for virtual trackpad / relative mouse mode)."""
        pos = ctypes.wintypes.POINT()
        if not self.user32.GetCursorPos(ctypes.byref(pos)):
            self._ensure_input_desktop(force=True)
            self.user32.GetCursorPos(ctypes.byref(pos))
        new_x = pos.x + int(dx)
        new_y = pos.y + int(dy)
        if not self.user32.SetCursorPos(new_x, new_y):
            self._ensure_input_desktop(force=True)
            self.user32.SetCursorPos(new_x, new_y)
        self.last_pixel_pos = (new_x, new_y)
        return (new_x, new_y)

    def move_mouse(self, norm_x: float, norm_y: float,
                   monitor_bounds: tuple[int, int, int, int] = None,
                   virtual_bounds: tuple[int, int, int, int] = None) -> tuple[int, int]:
        """Move cursor using physical SetCursorPos and SendInput virtual desk coordinates."""
        pixel_x, pixel_y, abs_x, abs_y = self._calc_coords(norm_x, norm_y, monitor_bounds, virtual_bounds)
        self.last_pixel_pos = (pixel_x, pixel_y)

        # 1. Direct hardware/OS pixel positioning
        if not self.user32.SetCursorPos(pixel_x, pixel_y):
            self._ensure_input_desktop(force=True)
            self.user32.SetCursorPos(pixel_x, pixel_y)

        # 2. Virtual desktop coordinate injection
        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.union.mi.dx = abs_x
        inp.union.mi.dy = abs_y
        inp.union.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
        self._send_input(inp)
        return (pixel_x, pixel_y)

        if not self._send_input(inp):
            self.user32.mouse_event(
                MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
                abs_x, abs_y, 0, 0
            )
        return (pixel_x, pixel_y)

    def mouse_down(self, button = "left", norm_x: float = None, norm_y: float = None,
                   monitor_bounds=None, virtual_bounds=None):
        button = normalize_button(button)
        if norm_x is not None and norm_y is not None:
            self.move_mouse(norm_x, norm_y, monitor_bounds, virtual_bounds)

        flag = MOUSEEVENTF_LEFTDOWN
        if button == "right":
            flag = MOUSEEVENTF_RIGHTDOWN
        elif button == "middle":
            flag = MOUSEEVENTF_MIDDLEDOWN

        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.union.mi.dwFlags = flag
        inp.union.mi.dx = 0
        inp.union.mi.dy = 0
        inp.union.mi.dwExtraInfo = 100

        if not self._send_input(inp):
            self.user32.mouse_event(flag, 0, 0, 0, 0)
        self.user32.mouse_event(flag, 0, 0, 0, 0)

    def mouse_up(self, button = "left", norm_x: float = None, norm_y: float = None,
                 monitor_bounds=None, virtual_bounds=None):
        button = normalize_button(button)
        if norm_x is not None and norm_y is not None:
            self.move_mouse(norm_x, norm_y, monitor_bounds, virtual_bounds)

        flag = MOUSEEVENTF_LEFTUP
        if button == "right":
            flag = MOUSEEVENTF_RIGHTUP
        elif button == "middle":
            flag = MOUSEEVENTF_MIDDLEUP

        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.union.mi.dwFlags = flag
        inp.union.mi.dx = 0
        inp.union.mi.dy = 0
        inp.union.mi.dwExtraInfo = 100

        if not self._send_input(inp):
            self.user32.mouse_event(flag, 0, 0, 0, 0)
        self.user32.mouse_event(flag, 0, 0, 0, 0)

    def mouse_click(self, button = "left", norm_x: float = None, norm_y: float = None,
                    monitor_bounds=None, virtual_bounds=None):
        """Execute full click with mandatory hardware delay so Windows UI registers the event."""
        button = normalize_button(button)
        if norm_x is not None and norm_y is not None:
            self.move_mouse(norm_x, norm_y, monitor_bounds=monitor_bounds, virtual_bounds=virtual_bounds)
        self.mouse_down(button)
        time.sleep(0.025)
        self.mouse_up(button)

    def mouse_double_click(self, button = "left", norm_x: float = None, norm_y: float = None,
                           monitor_bounds=None, virtual_bounds=None):
        self.mouse_click(button, norm_x=norm_x, norm_y=norm_y, monitor_bounds=monitor_bounds, virtual_bounds=virtual_bounds)
        time.sleep(0.05)
        self.mouse_click(button, norm_x=norm_x, norm_y=norm_y, monitor_bounds=monitor_bounds, virtual_bounds=virtual_bounds)

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
        self._pressed_keys.add(vk_code)
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
        self._pressed_keys.discard(vk_code)
        flags = KEYEVENTF_KEYUP
        if vk_code in (0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C):
            flags |= KEYEVENTF_EXTENDEDKEY

        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.union.ki.wVk = vk_code
        inp.union.ki.dwFlags = flags
        if not self._send_input(inp):
            self.user32.keybd_event(vk_code, 0, flags, 0)

    def release_all_keys(self):
        """Release all currently pressed keys to avoid stuck modifiers or letters."""
        for vk in list(self._pressed_keys):
            self.key_up(vk)
        self._pressed_keys.clear()
        # Explicitly ensure common modifiers are released
        for mod in (0x11, 0x12, 0x10, 0x5B, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5):
            flags = KEYEVENTF_KEYUP
            if mod in (0x5B, 0xA2, 0xA3, 0xA4, 0xA5):
                flags |= KEYEVENTF_EXTENDEDKEY
            inp = INPUT()
            inp.type = INPUT_KEYBOARD
            inp.union.ki.wVk = mod
            inp.union.ki.dwFlags = flags
            if not self._send_input(inp):
                self.user32.keybd_event(mod, 0, flags, 0)

    def send_unicode_char(self, char_or_text: str):
        """Send unicode character or full text string directly (immune to layout differences)."""
        if not char_or_text:
            return
        self._ensure_input_desktop()
        for char in str(char_or_text):
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

    def send_key_combination_by_names(self, key_names: list[str]) -> bool:
        vks = []
        for name in key_names:
            clean = str(name).lower().strip()
            if clean in VK_MAP:
                vks.append(VK_MAP[clean])
            elif len(clean) == 1 and clean.isalnum():
                vks.append(VK_MAP.get(clean, ord(clean.upper())))
        if vks:
            self.send_key_combination(vks)
            return True
        return False

    def send_shortcut(self, shortcut: str) -> bool:
        """Parse shortcut string like 'ctrl+c', 'ctrl+alt+del', 'shift+tab'."""
        parts = [p.strip().lower() for p in shortcut.replace("-", "+").split("+")]
        return self.send_key_combination_by_names(parts)

    def send_special(self, command: str) -> bool:
        """Execute special system key combinations and clipboard shortcuts."""
        cmd = command.lower().strip().replace(" ", "_").replace("-", "_")
        self._ensure_input_desktop()

        # Clipboard shortcuts (Ctrl+C, Ctrl+V, etc.)
        if cmd in ("copy", "ctrl_c", "ctrl+c"):
            self.send_key_combination([VK_CONTROL, 0x43]) # Ctrl+C
            return True
        elif cmd in ("paste", "ctrl_v", "ctrl+v"):
            self.send_key_combination([VK_CONTROL, 0x56]) # Ctrl+V
            return True
        elif cmd in ("cut", "ctrl_x", "ctrl+x"):
            self.send_key_combination([VK_CONTROL, 0x58]) # Ctrl+X
            return True
        elif cmd in ("select_all", "selectall", "ctrl_a", "ctrl+a"):
            self.send_key_combination([VK_CONTROL, 0x41]) # Ctrl+A
            return True
        elif cmd in ("undo", "ctrl_z", "ctrl+z"):
            self.send_key_combination([VK_CONTROL, 0x5A]) # Ctrl+Z
            return True
        elif cmd in ("redo", "ctrl_y", "ctrl+y"):
            self.send_key_combination([VK_CONTROL, 0x59]) # Ctrl+Y
            return True
        elif cmd in ("save", "ctrl_s", "ctrl+s"):
            self.send_key_combination([VK_CONTROL, 0x53]) # Ctrl+S
            return True
        elif cmd in ("enter", "return"):
            self.send_key_combination([0x0D])
            return True
        elif cmd in ("backspace", "back"):
            self.send_key_combination([0x08])
            return True
        elif cmd in ("tab",):
            self.send_key_combination([VK_TAB])
            return True
        elif cmd in ("esc", "escape"):
            self.send_key_combination([VK_ESCAPE])
            return True
        elif cmd in ("delete", "del"):
            self.send_key_combination([VK_DELETE])
            return True
        elif cmd in ("arrow_up", "up"):
            self.send_key_combination([0x26])
            return True
        elif cmd in ("arrow_down", "down"):
            self.send_key_combination([0x28])
            return True
        elif cmd in ("arrow_left", "left"):
            self.send_key_combination([0x25])
            return True
        elif cmd in ("arrow_right", "right"):
            self.send_key_combination([0x27])
            return True
        elif cmd in ("win_d", "wind"):
            self.send_key_combination([VK_LWIN, 0x44]) # Win + D
            return True
        elif cmd in ("win_e", "wine"):
            self.send_key_combination([VK_LWIN, 0x45]) # Win + E (Explorer)
            return True
        elif cmd in ("win_r", "winr"):
            self.send_key_combination([VK_LWIN, 0x52]) # Win + R (Run)
            return True
        elif cmd in ("alt_tab", "alttab"):
            self.send_key_combination([VK_MENU, VK_TAB]) # Alt + Tab
            return True
        elif cmd in ("alt_f4", "altf4"):
            self.send_key_combination([VK_MENU, 0x73]) # Alt + F4
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

        # Fallback to shortcut string
        return self.send_shortcut(command)
