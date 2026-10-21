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
    """Elevated mouse and keyboard injection engine via SendInput."""

    def __init__(self):
        self.user32 = ctypes.windll.user32

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

    def _send_input(self, inp: INPUT):
        self.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    def move_mouse(self, norm_x: float, norm_y: float,
                   monitor_bounds: tuple[int, int, int, int] = None,
                   virtual_bounds: tuple[int, int, int, int] = None):
        """Move cursor using absolute coordinate space (0..65535), properly mapped to the active monitor."""
        if monitor_bounds and virtual_bounds:
            src_x, src_y, src_w, src_h = monitor_bounds
            vx, vy, vw, vh = virtual_bounds
            if vw > 0 and vh > 0:
                pixel_x = src_x + norm_x * src_w
                pixel_y = src_y + norm_y * src_h
                norm_vx = (pixel_x - vx) / vw
                norm_vy = (pixel_y - vy) / vh
                abs_x = int(max(0.0, min(1.0, norm_vx)) * 65535)
                abs_y = int(max(0.0, min(1.0, norm_vy)) * 65535)
            else:
                abs_x = int(max(0.0, min(1.0, norm_x)) * 65535)
                abs_y = int(max(0.0, min(1.0, norm_y)) * 65535)
        else:
            abs_x = int(max(0.0, min(1.0, norm_x)) * 65535)
            abs_y = int(max(0.0, min(1.0, norm_y)) * 65535)

        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.union.mi.dx = abs_x
        inp.union.mi.dy = abs_y
        inp.union.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
        self._send_input(inp)

    def mouse_down(self, button: str = "left"):
        inp = INPUT()
        inp.type = INPUT_MOUSE
        if button == "left":
            inp.union.mi.dwFlags = MOUSEEVENTF_LEFTDOWN
        elif button == "right":
            inp.union.mi.dwFlags = MOUSEEVENTF_RIGHTDOWN
        elif button == "middle":
            inp.union.mi.dwFlags = MOUSEEVENTF_MIDDLEDOWN
        else:
            return
        self._send_input(inp)

    def mouse_up(self, button: str = "left"):
        inp = INPUT()
        inp.type = INPUT_MOUSE
        if button == "left":
            inp.union.mi.dwFlags = MOUSEEVENTF_LEFTUP
        elif button == "right":
            inp.union.mi.dwFlags = MOUSEEVENTF_RIGHTUP
        elif button == "middle":
            inp.union.mi.dwFlags = MOUSEEVENTF_MIDDLEUP
        else:
            return
        self._send_input(inp)

    def mouse_wheel(self, delta: int):
        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.union.mi.dwFlags = MOUSEEVENTF_WHEEL
        inp.union.mi.mouseData = delta
        self._send_input(inp)

    def key_down(self, vk_code: int):
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.union.ki.wVk = vk_code
        inp.union.ki.dwFlags = KEYEVENTF_KEYDOWN
        self._send_input(inp)

    def key_up(self, vk_code: int):
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.union.ki.wVk = vk_code
        inp.union.ki.dwFlags = KEYEVENTF_KEYUP
        self._send_input(inp)

    def send_key_combination(self, vk_list: list[int]):
        for vk in vk_list:
            self.key_down(vk)
        time.sleep(0.02)
        for vk in reversed(vk_list):
            self.key_up(vk)

    def send_special(self, command: str) -> bool:
        """Execute special system key combinations."""
        cmd = command.lower()
        if cmd == "win_d":
            self.send_key_combination([VK_LWIN, 0x44]) # Win + D
            return True
        elif cmd == "alt_tab":
            self.send_key_combination([VK_MENU, VK_TAB]) # Alt + Tab
            return True
        elif cmd in ("task_mgr", "taskmgr"):
            self.send_key_combination([VK_CONTROL, VK_SHIFT, VK_ESCAPE]) # Ctrl+Shift+Esc
            return True
        elif cmd in ("ctrl_alt_del", "sas"):
            # Try SendSAS if available (in service mode), fallback to Ctrl+Alt+Del simulation
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
