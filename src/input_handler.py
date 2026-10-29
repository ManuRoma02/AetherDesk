import ctypes
from ctypes import wintypes
import time
import sys
import os
import threading
import logging
from typing import Optional

logger = logging.getLogger("InputHandler")

def is_test_environment() -> bool:
    """Check if Python process is running within pytest or test discovery."""
    if "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST") or "PYTEST_VERSION" in os.environ:
        return True
    if any("pytest" in arg.lower() for arg in sys.argv):
        return True
    return False

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

class TOKEN_ELEVATION(ctypes.Structure):
    _fields_ = [("TokenIsElevated", wintypes.DWORD)]

class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Sid", ctypes.c_void_p),
        ("Attributes", wintypes.DWORD),
    ]

class TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [
        ("Label", SID_AND_ATTRIBUTES),
    ]

def get_windows_process_context() -> dict:
    """
    Query current process session ID, token elevation, and integrity level.
    Provides diagnostic clarity on UIPI constraints and secure desktop access.
    """
    ctx = {
        "session_id": None,
        "is_elevated": False,
        "integrity_level": "Unknown",
        "integrity_level_code": 0,
        "can_uipi_bypass": False,
    }
    if sys.platform != "win32":
        return ctx

    try:
        kernel32 = ctypes.windll.kernel32
        advapi32 = ctypes.windll.advapi32

        # Explicit 64-bit signatures for kernel32 process context APIs
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetCurrentProcessId.argtypes = []
        kernel32.GetCurrentProcessId.restype = wintypes.DWORD
        kernel32.GetCurrentThreadId.argtypes = []
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        # Explicit 64-bit signatures for advapi32 security token APIs
        advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD)
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
        advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)

        # 1. Session ID
        session_id = wintypes.DWORD()
        if kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(session_id)):
            ctx["session_id"] = session_id.value

        # 2. Open process token
        TOKEN_QUERY = 0x0008
        token_handle = wintypes.HANDLE()
        if advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token_handle)):
            try:
                # TokenElevation = 20
                elevation = TOKEN_ELEVATION()
                ret_len = wintypes.DWORD()
                if advapi32.GetTokenInformation(token_handle, 20, ctypes.byref(elevation), ctypes.sizeof(elevation), ctypes.byref(ret_len)):
                    ctx["is_elevated"] = bool(elevation.TokenIsElevated)

                # TokenIntegrityLevel = 25
                buf = ctypes.create_string_buffer(128)
                if advapi32.GetTokenInformation(token_handle, 25, buf, ctypes.sizeof(buf), ctypes.byref(ret_len)):
                    tml = ctypes.cast(buf, ctypes.POINTER(TOKEN_MANDATORY_LABEL)).contents
                    sid_ptr = tml.Label.Sid
                    if sid_ptr:
                        count_ptr = advapi32.GetSidSubAuthorityCount(sid_ptr)
                        if count_ptr and count_ptr.contents.value > 0:
                            sub_auth_ptr = advapi32.GetSidSubAuthority(sid_ptr, count_ptr.contents.value - 1)
                            if sub_auth_ptr:
                                rid = sub_auth_ptr.contents.value
                                ctx["integrity_level_code"] = rid
                                if rid >= 0x4000:
                                    ctx["integrity_level"] = "System"
                                elif rid >= 0x3000:
                                    ctx["integrity_level"] = "High (Administrator)"
                                elif rid >= 0x2000:
                                    ctx["integrity_level"] = "Medium (Standard User)"
                                elif rid >= 0x1000:
                                    ctx["integrity_level"] = "Low"
                                else:
                                    ctx["integrity_level"] = "Untrusted"
            finally:
                kernel32.CloseHandle(token_handle)
    except Exception:
        pass

    ctx["can_uipi_bypass"] = ctx["is_elevated"] or (ctx["integrity_level_code"] >= 0x3000)
    return ctx

class WindowsInputInjector:
    """Production Windows input injector using Win32 API via ctypes."""

    def __init__(self):
        if is_test_environment():
            raise RuntimeError(
                "WindowsInputInjector cannot be initialized in a test environment. "
                "Use MockInputInjector instead."
            )
        self.user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None
        self._lock = threading.Lock()
        self._winsta_initialized = False
        self._last_desktop_check = 0.0
        self._current_hdesk = None
        self._current_desktop_name = None
        self._setup_ctypes_signatures()
        self._privilege_ctx = get_windows_process_context()
        logger.info(
            f"[PRIVILEGE] Host context: Session={self._privilege_ctx.get('session_id')}, "
            f"Integrity={self._privilege_ctx.get('integrity_level')}, "
            f"Elevated={self._privilege_ctx.get('is_elevated')}"
        )
        if not self._privilege_ctx.get("is_elevated"):
            logger.info(
                "[PRIVILEGE] Running with standard user rights. Input injection into elevated windows "
                "on the interactive desktop is restricted by UIPI. Running Host as Administrator enables UIPI bypass "
                "for elevated windows on the interactive desktop. Note: The UAC Secure Desktop (Winlogon) is not "
                "accessible to interactive user processes."
            )
        else:
            logger.info(
                "[PRIVILEGE] Running with administrative privileges (UIPI bypass enabled on interactive desktop). "
                "Note: The UAC Secure Desktop (Winlogon) remains isolated and is not accessible."
            )

    def _setup_ctypes_signatures(self):
        """Declare explicit 64-bit argument and return types for user32 Win32 API."""
        if not self.user32:
            return
        try:
            self.user32.OpenWindowStationW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL, wintypes.DWORD]
            self.user32.OpenWindowStationW.restype = wintypes.HANDLE

            self.user32.GetProcessWindowStation.argtypes = []
            self.user32.GetProcessWindowStation.restype = wintypes.HANDLE

            self.user32.SetProcessWindowStation.argtypes = [wintypes.HANDLE]
            self.user32.SetProcessWindowStation.restype = wintypes.BOOL

            self.user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self.user32.OpenInputDesktop.restype = wintypes.HANDLE

            self.user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
            self.user32.SetThreadDesktop.restype = wintypes.BOOL

            self.user32.CloseDesktop.argtypes = [wintypes.HANDLE]
            self.user32.CloseDesktop.restype = wintypes.BOOL

            self.user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
            self.user32.GetThreadDesktop.restype = wintypes.HANDLE

            self.user32.GetUserObjectInformationW.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                wintypes.LPVOID,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
            ]
            self.user32.GetUserObjectInformationW.restype = wintypes.BOOL

            self.user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
            self.user32.SendInput.restype = wintypes.UINT

            self.user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
            self.user32.SetCursorPos.restype = wintypes.BOOL

            self.user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
            self.user32.GetCursorPos.restype = wintypes.BOOL

            self.user32.GetSystemMetrics.argtypes = [ctypes.c_int]
            self.user32.GetSystemMetrics.restype = ctypes.c_int

            self.user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_ulonglong]
            self.user32.mouse_event.restype = None

            self.user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_ulonglong]
            self.user32.keybd_event.restype = None

            if hasattr(self.user32, "MapVirtualKeyW"):
                self.user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
                self.user32.MapVirtualKeyW.restype = wintypes.UINT
        except Exception as e:
            logger.debug(f"Failed to bind ctypes signatures: {e}")

    def _get_qualified_desktop_name(self, hdesk) -> str:
        """
        Query fully qualified desktop name (WindowStation\\Desktop) via GetUserObjectInformationW.
        Differentiates homonymous desktops across distinct window stations.
        """
        if not hdesk or not self.user32 or not hasattr(self.user32, "GetUserObjectInformationW"):
            return ""
        desk_name = ""
        buf = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD()
        UOI_NAME = 2
        try:
            if self.user32.GetUserObjectInformationW(hdesk, UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed)):
                desk_name = buf.value
        except Exception:
            return ""

        winsta_name = ""
        if hasattr(self.user32, "GetProcessWindowStation"):
            try:
                hwinsta = self.user32.GetProcessWindowStation()
                if hwinsta and self.user32.GetUserObjectInformationW(hwinsta, UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed)):
                    winsta_name = buf.value
            except Exception:
                pass

        if winsta_name and desk_name:
            return f"{winsta_name}\\{desk_name}".lower()
        return desk_name.lower()

    def ensure_input_desktop(self, force: bool = False) -> bool:
        now = time.time()
        if not force and (now - self._last_desktop_check < 2.0):
            return True
        self._last_desktop_check = now

        with self._lock:
            if not self._winsta_initialized and self.user32:
                try:
                    hwinsta = self.user32.OpenWindowStationW("WinSta0", False, 0x037F)
                    if hwinsta:
                        self.user32.SetProcessWindowStation(hwinsta)
                        self._winsta_initialized = True
                except Exception as e:
                    logger.debug(f"[DESKTOP] OpenWindowStationW exception: {e}")

            if not self.user32:
                return False

            try:
                flags = 0x0001 | 0x0040 | 0x0100  # DESKTOP_READOBJECTS | DESKTOP_WRITEOBJECTS | DESKTOP_SWITCHDESKTOP
                hdesk = self.user32.OpenInputDesktop(0, False, flags)
                if not hdesk:
                    hdesk = self.user32.OpenInputDesktop(0, False, 0x0001 | 0x0100)

                if not hdesk:
                    err = ctypes.GetLastError()
                    logger.debug(
                        f"[DESKTOP] OpenInputDesktop returned NULL (Win32 err={err}). "
                        "Active desktop may be isolated (e.g. Secure Desktop or locked workstation)."
                    )
                    return False

                # Query fully-qualified input desktop name
                input_desk_name = self._get_qualified_desktop_name(hdesk)

                # Query calling thread's current desktop name
                kernel32 = getattr(ctypes.windll, "kernel32", None)
                thread_id = kernel32.GetCurrentThreadId() if kernel32 else 0
                h_thread = self.user32.GetThreadDesktop(thread_id) if hasattr(self.user32, "GetThreadDesktop") else None
                thread_desk_name = self._get_qualified_desktop_name(h_thread) if h_thread else (self._current_desktop_name or "")

                # Note on Win32 semantics: handles returned by OpenInputDesktop must be closed with CloseDesktop.
                # Handles returned by GetThreadDesktop do NOT need to be closed with CloseDesktop.
                # Comparing raw handle integers (hdesk != _current_hdesk) is invalid because separate
                # OpenInputDesktop calls return distinct handle values for the same underlying desktop object.
                if thread_desk_name and input_desk_name and thread_desk_name == input_desk_name:
                    # Thread is already attached to this desktop: close temporary handle to avoid leak
                    # and avoid redundant SetThreadDesktop which fails if windows or hooks exist.
                    self.user32.CloseDesktop(hdesk)
                    self._current_desktop_name = input_desk_name
                    return True

                # Desktop has transitioned (e.g. Default -> Winlogon or vice-versa): switch thread desktop
                assigned = self.user32.SetThreadDesktop(hdesk)
                if assigned:
                    # Close previously held desktop handle only after successfully switching
                    if self._current_hdesk:
                        try:
                            self.user32.CloseDesktop(self._current_hdesk)
                        except Exception:
                            pass
                    self._current_hdesk = hdesk
                    self._current_desktop_name = input_desk_name
                    logger.info(f"[DESKTOP] InputWorkerThread assigned to active input desktop: {input_desk_name}")
                    return True
                else:
                    err = ctypes.GetLastError()
                    logger.warning(f"[DESKTOP] SetThreadDesktop failed with Win32 err={err} for desktop '{input_desk_name}'")
                    self.user32.CloseDesktop(hdesk)
                    return False
            except Exception as e:
                logger.debug(f"[DESKTOP] ensure_input_desktop error: {e}")
                return False

    def close(self):
        """Release assigned desktop handle on shutdown (thread-safe, idempotent)."""
        with self._lock:
            if self._current_hdesk and self.user32:
                try:
                    self.user32.CloseDesktop(self._current_hdesk)
                except Exception:
                    pass
                self._current_hdesk = None
                self._current_desktop_name = None

    def get_system_metrics(self, index: int) -> int:
        return self.user32.GetSystemMetrics(index)

    def set_cursor_pos(self, x: int, y: int) -> bool:
        return bool(self.user32.SetCursorPos(x, y))

    def send_input(self, inp: INPUT) -> bool:
        res = self.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        if res == 0:
            err = ctypes.GetLastError()
            logger.debug(f"[INPUT_INJECT] SendInput returned 0 (Win32 err={err}). Re-syncing desktop...")
            self.ensure_input_desktop(force=True)
            res = self.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
            if res == 0:
                err2 = ctypes.GetLastError()
                logger.warning(f"[INPUT_INJECT] SendInput retry failed (Win32 err={err2})")
        return res > 0

    def mouse_event(self, flags: int, dx: int, dy: int, data: int, extra_info: int) -> None:
        self.user32.mouse_event(flags, dx, dy, data, extra_info)

    def keybd_event(self, vk: int, scan: int, flags: int, extra_info: int) -> None:
        self.user32.keybd_event(vk, scan, flags, extra_info)


class MockInputInjector:
    """Mock injector for deterministic unit testing without requiring real desktop."""

    def __init__(self, screen_w: int = 1920, screen_h: int = 1080):
        self.screen_w = screen_w
        self.screen_h = screen_h
        self.cursor_pos = (0, 0)
        self.sent_inputs = []
        self.mouse_events = []
        self.keybd_events = []
        self.desktop_ensured_count = 0
        self.closed = False

    def ensure_input_desktop(self, force: bool = False) -> bool:
        self.desktop_ensured_count += 1
        return True

    def close(self):
        self.closed = True

    def get_system_metrics(self, index: int) -> int:
        if index in (0, 78):
            return self.screen_w
        if index in (1, 79):
            return self.screen_h
        if index in (76, 77):
            return 0
        return 0

    def set_cursor_pos(self, x: int, y: int) -> bool:
        self.cursor_pos = (x, y)
        return True

    def send_input(self, inp: INPUT) -> bool:
        self.sent_inputs.append(inp)
        return True

    def mouse_event(self, flags: int, dx: int, dy: int, data: int, extra_info: int) -> None:
        self.mouse_events.append((flags, dx, dy, data, extra_info))

    def keybd_event(self, vk: int, scan: int, flags: int, extra_info: int) -> None:
        self.keybd_events.append((vk, scan, flags, extra_info))


class InputHandler:
    """
    Remote input injection engine:
    - Attaches thread to active input desktop when accessible (reports access denial on Secure Desktop).
    - Uses SetCursorPos for pixel accuracy + SendInput with mouse_event/keybd_event fallback.
    - Full multi-monitor and virtual desktop coordinate transformation.
    """

    def __init__(self, injector=None):
        if injector is not None:
            self.injector = injector
        elif is_test_environment() or not hasattr(ctypes, "windll"):
            self.injector = MockInputInjector()
        else:
            self.injector = WindowsInputInjector()

        if isinstance(self.injector, WindowsInputInjector):
            self.user32 = getattr(self.injector, "user32", None)
            if self.user32 is None and hasattr(ctypes, "windll"):
                self.user32 = ctypes.windll.user32
            # Enable Per-Monitor V2 DPI awareness only on real Windows injector
            try:
                ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            except Exception:
                try:
                    ctypes.windll.shcore.SetProcessDpiAwareness(1)
                except Exception:
                    pass
        else:
            self.user32 = None
        self.last_pixel_pos = (0, 0)
        self._pressed_keys = set()
        self._pressed_mouse_buttons = set()

    def close(self):
        """Release assigned desktop handle and resources held by the injector."""
        if hasattr(self.injector, "close"):
            try:
                self.injector.close()
            except Exception as e:
                logger.debug(f"InputHandler close error: {e}")

    def _ensure_input_desktop(self, force: bool = False) -> bool:
        """Attach caller thread to active Windows input desktop when accessible."""
        return self.injector.ensure_input_desktop(force=force)

    def get_screen_bounds(self) -> tuple[int, int]:
        w = self.injector.get_system_metrics(0)
        h = self.injector.get_system_metrics(1)
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
        return self.injector.send_input(inp)

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
        elif self.user32 and hasattr(self.user32, "GetSystemMetrics"):
            vx = self.user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
            vy = self.user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
            vw = self.user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
            vh = self.user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        else:
            vx, vy = 0, 0
            vw, vh = self.get_screen_bounds()

        if vw > 0 and vh > 0:
            norm_vx = (pixel_x - vx) / vw
            norm_vy = (pixel_y - vy) / vh
            abs_x = int(max(0.0, min(1.0, norm_vx)) * 65535)
            abs_y = int(max(0.0, min(1.0, norm_vy)) * 65535)
        else:
            abs_x = int(max(0.0, min(1.0, norm_x)) * 65535)
            abs_y = int(max(0.0, min(1.0, norm_y)) * 65535)

        return (pixel_x, pixel_y, abs_x, abs_y)

    def move_mouse_relative(self, dx: float, dy: float, seq: Optional[int] = None) -> tuple[int, int]:
        """Move cursor relatively (for virtual trackpad / relative mouse mode)."""
        if self.user32 and hasattr(self.user32, "GetCursorPos"):
            pos = ctypes.wintypes.POINT()
            if not self.user32.GetCursorPos(ctypes.byref(pos)):
                self._ensure_input_desktop(force=True)
                self.user32.GetCursorPos(ctypes.byref(pos))
            cur_x, cur_y = pos.x, pos.y
        else:
            cur_x, cur_y = self.last_pixel_pos

        new_x = cur_x + int(dx)
        new_y = cur_y + int(dy)

        ok = True
        if self.user32 and hasattr(self.user32, "SetCursorPos"):
            if not self.user32.SetCursorPos(new_x, new_y):
                self._ensure_input_desktop(force=True)
                ok = bool(self.user32.SetCursorPos(new_x, new_y))
        elif hasattr(self.injector, "set_cursor_pos"):
            ok = bool(self.injector.set_cursor_pos(new_x, new_y))

        self.last_pixel_pos = (new_x, new_y)
        cnt = getattr(self, "_rel_move_count", 0) + 1
        self._rel_move_count = cnt
        if cnt % 100 == 0:
            logger.debug(f"[INPUT_INJECT] seq={seq} action=move_relative move_count={cnt} set_cursor_pos={ok}")
        return (new_x, new_y)

    def move_mouse(self, norm_x: float, norm_y: float,
                   monitor_bounds: tuple[int, int, int, int] = None,
                   virtual_bounds: tuple[int, int, int, int] = None,
                   seq: Optional[int] = None) -> tuple[int, int]:
        """Move cursor using physical SetCursorPos and SendInput virtual desk coordinates."""
        pixel_x, pixel_y, abs_x, abs_y = self._calc_coords(norm_x, norm_y, monitor_bounds, virtual_bounds)
        self.last_pixel_pos = (pixel_x, pixel_y)

        # Send one absolute virtual-desktop event. SetCursorPos followed by
        # SendInput injected the same move twice and made pointer motion jittery.
        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.union.mi.dx = abs_x
        inp.union.mi.dy = abs_y
        inp.union.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
        ok = self._send_input(inp)
        fallback = False
        if not ok:
            fallback = True
            # Fallback for desktops where absolute SendInput is unavailable.
            self.injector.mouse_event(
                MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
                abs_x, abs_y, 0, 0
            )
        cnt = getattr(self, "_injected_move_count", 0) + 1
        self._injected_move_count = cnt
        if cnt % 100 == 0:
            logger.debug(f"[INPUT_INJECT] seq={seq} action=mouse_move move_count={cnt} send_input_inserted={ok} fallback_attempted={fallback}")
        return (pixel_x, pixel_y)

    def mouse_down(self, button = "left", norm_x: float = None, norm_y: float = None,
                   monitor_bounds=None, virtual_bounds=None, seq: Optional[int] = None):
        button = normalize_button(button)
        self._pressed_mouse_buttons.add(button)
        if norm_x is not None and norm_y is not None:
            self.move_mouse(norm_x, norm_y, monitor_bounds, virtual_bounds, seq=seq)

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

        ok = self._send_input(inp)
        fallback = False
        if not ok:
            fallback = True
            self.injector.mouse_event(flag, 0, 0, 0, 0)
        logger.info(f"[INPUT_INJECT] seq={seq} action=mouse_down button={button} send_input_inserted={ok} fallback_attempted={fallback}")

    def mouse_up(self, button = "left", norm_x: float = None, norm_y: float = None,
                 monitor_bounds=None, virtual_bounds=None, seq: Optional[int] = None):
        button = normalize_button(button)
        self._pressed_mouse_buttons.discard(button)
        if norm_x is not None and norm_y is not None:
            self.move_mouse(norm_x, norm_y, monitor_bounds, virtual_bounds, seq=seq)

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

        ok = self._send_input(inp)
        fallback = False
        if not ok:
            fallback = True
            self.injector.mouse_event(flag, 0, 0, 0, 0)
        logger.info(f"[INPUT_INJECT] seq={seq} action=mouse_up button={button} send_input_inserted={ok} fallback_attempted={fallback}")

    def mouse_click(self, button = "left", norm_x: float = None, norm_y: float = None,
                    monitor_bounds=None, virtual_bounds=None, seq: Optional[int] = None):
        """Execute full click with mandatory hardware delay so Windows UI registers the event."""
        button = normalize_button(button)
        if norm_x is not None and norm_y is not None:
            self.move_mouse(norm_x, norm_y, monitor_bounds=monitor_bounds, virtual_bounds=virtual_bounds, seq=seq)
        self.mouse_down(button, seq=seq)
        time.sleep(0.025)
        self.mouse_up(button, seq=seq)

    def mouse_double_click(self, button = "left", norm_x: float = None, norm_y: float = None,
                           monitor_bounds=None, virtual_bounds=None, seq: Optional[int] = None):
        self.mouse_click(button, norm_x=norm_x, norm_y=norm_y, monitor_bounds=monitor_bounds, virtual_bounds=virtual_bounds, seq=seq)
        time.sleep(0.05)
        self.mouse_click(button, norm_x=norm_x, norm_y=norm_y, monitor_bounds=monitor_bounds, virtual_bounds=virtual_bounds, seq=seq)

    def mouse_wheel(self, delta: int, norm_x: float = None, norm_y: float = None,
                    monitor_bounds=None, virtual_bounds=None, seq: Optional[int] = None):
        if norm_x is not None and norm_y is not None:
            self.move_mouse(norm_x, norm_y, monitor_bounds, virtual_bounds, seq=seq)

        self._ensure_input_desktop()
        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.union.mi.dwFlags = MOUSEEVENTF_WHEEL
        inp.union.mi.mouseData = delta
        ok = self._send_input(inp)
        fallback = False
        if not ok:
            fallback = True
            self.injector.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, delta, 0)
        logger.info(f"[INPUT_INJECT] seq={seq} action=mouse_wheel send_input_inserted={ok} fallback_attempted={fallback}")

    def key_down(self, vk_code: int, seq: Optional[int] = None):
        self._pressed_keys.add(vk_code)
        flags = KEYEVENTF_KEYDOWN
        if vk_code in (0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C):
            flags |= KEYEVENTF_EXTENDEDKEY

        wScan = 0
        if self.user32 and hasattr(self.user32, "MapVirtualKeyW"):
            try:
                wScan = self.user32.MapVirtualKeyW(vk_code, 0)
            except Exception:
                wScan = 0

        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.union.ki.wVk = vk_code
        inp.union.ki.wScan = wScan
        inp.union.ki.dwFlags = flags
        ok = self._send_input(inp)
        fallback = False
        if not ok:
            fallback = True
            self.injector.keybd_event(vk_code, wScan, flags, 0)
        logger.info(f"[INPUT_INJECT] seq={seq} action=key_down send_input_inserted={ok} fallback_attempted={fallback}")

    def key_up(self, vk_code: int, seq: Optional[int] = None):
        self._pressed_keys.discard(vk_code)
        flags = KEYEVENTF_KEYUP
        if vk_code in (0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C):
            flags |= KEYEVENTF_EXTENDEDKEY

        wScan = 0
        if self.user32 and hasattr(self.user32, "MapVirtualKeyW"):
            try:
                wScan = self.user32.MapVirtualKeyW(vk_code, 0)
            except Exception:
                wScan = 0

        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.union.ki.wVk = vk_code
        inp.union.ki.wScan = wScan
        inp.union.ki.dwFlags = flags
        ok = self._send_input(inp)
        fallback = False
        if not ok:
            fallback = True
            self.injector.keybd_event(vk_code, wScan, flags, 0)
        logger.info(f"[INPUT_INJECT] seq={seq} action=key_up send_input_inserted={ok} fallback_attempted={fallback}")

    def release_all_mouse_buttons(self):
        """Release all currently pressed mouse buttons to avoid stuck drag states."""
        for btn in list(self._pressed_mouse_buttons):
            self.mouse_up(btn)
        self._pressed_mouse_buttons.clear()
        # Safety fallback: ensure left, right, middle physical state is cleared
        for btn in ("left", "right", "middle"):
            flag = MOUSEEVENTF_LEFTUP if btn == "left" else (MOUSEEVENTF_RIGHTUP if btn == "right" else MOUSEEVENTF_MIDDLEUP)
            inp = INPUT()
            inp.type = INPUT_MOUSE
            inp.union.mi.dwFlags = flag
            if not self._send_input(inp):
                self.injector.mouse_event(flag, 0, 0, 0, 0)

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
                self.injector.keybd_event(mod, 0, flags, 0)
        # Also release any stuck mouse buttons
        self.release_all_mouse_buttons()

    def release_all_inputs(self):
        """Release both keyboard keys and mouse buttons."""
        self.release_all_keys()

    def send_unicode_char(self, char_or_text: str, seq: Optional[int] = None):
        """Send unicode character or full text string directly (immune to layout differences)."""
        if not char_or_text:
            return
        self._ensure_input_desktop()
        all_inserted = True
        for char in str(char_or_text):
            code = ord(char)
            inp_down = INPUT()
            inp_down.type = INPUT_KEYBOARD
            inp_down.union.ki.wScan = code
            inp_down.union.ki.dwFlags = KEYEVENTF_UNICODE
            ok_down = self._send_input(inp_down)

            inp_up = INPUT()
            inp_up.type = INPUT_KEYBOARD
            inp_up.union.ki.wScan = code
            inp_up.union.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
            ok_up = self._send_input(inp_up)
            if not ok_down or not ok_up:
                all_inserted = False
        logger.info(f"[INPUT_INJECT] seq={seq} action=unicode_char send_input_inserted={all_inserted} chars_count={len(str(char_or_text))}")

    def send_key_combination(self, vk_list: list[int], seq: Optional[int] = None):
        for vk in vk_list:
            self.key_down(vk, seq=seq)
        time.sleep(0.02)
        for vk in reversed(vk_list):
            self.key_up(vk, seq=seq)

    def send_key_combination_by_names(self, key_names: list[str], seq: Optional[int] = None) -> bool:
        vks = []
        for name in key_names:
            clean = str(name).lower().strip()
            if clean in VK_MAP:
                vks.append(VK_MAP[clean])
            elif len(clean) == 1 and clean.isalnum():
                vks.append(VK_MAP.get(clean, ord(clean.upper())))
        if vks:
            self.send_key_combination(vks, seq=seq)
            return True
        return False

    def send_shortcut(self, shortcut: str, seq: Optional[int] = None) -> bool:
        """Parse shortcut string like 'ctrl+c', 'ctrl+alt+del', 'shift+tab'."""
        parts = [p.strip().lower() for p in shortcut.replace("-", "+").split("+")]
        return self.send_key_combination_by_names(parts, seq=seq)

    def send_special(self, command: str, seq: Optional[int] = None) -> bool:
        """Execute special system key combinations and clipboard shortcuts."""
        cmd = command.lower().strip().replace(" ", "_").replace("-", "_")
        self._ensure_input_desktop()

        # Clipboard shortcuts (Ctrl+C, Ctrl+V, etc.)
        if cmd in ("copy", "ctrl_c", "ctrl+c"):
            self.send_key_combination([VK_CONTROL, 0x43], seq=seq) # Ctrl+C
            return True
        elif cmd in ("paste", "ctrl_v", "ctrl+v"):
            self.send_key_combination([VK_CONTROL, 0x56], seq=seq) # Ctrl+V
            return True
        elif cmd in ("cut", "ctrl_x", "ctrl+x"):
            self.send_key_combination([VK_CONTROL, 0x58], seq=seq) # Ctrl+X
            return True
        elif cmd in ("select_all", "selectall", "ctrl_a", "ctrl+a"):
            self.send_key_combination([VK_CONTROL, 0x41], seq=seq) # Ctrl+A
            return True
        elif cmd in ("undo", "ctrl_z", "ctrl+z"):
            self.send_key_combination([VK_CONTROL, 0x5A], seq=seq) # Ctrl+Z
            return True
        elif cmd in ("redo", "ctrl_y", "ctrl+y"):
            self.send_key_combination([VK_CONTROL, 0x59], seq=seq) # Ctrl+Y
            return True
        elif cmd in ("save", "ctrl_s", "ctrl+s"):
            self.send_key_combination([VK_CONTROL, 0x53], seq=seq) # Ctrl+S
            return True
        elif cmd in ("enter", "return"):
            self.send_key_combination([0x0D], seq=seq)
            return True
        elif cmd in ("backspace", "back"):
            self.send_key_combination([0x08], seq=seq)
            return True
        elif cmd in ("tab",):
            self.send_key_combination([VK_TAB], seq=seq)
            return True
        elif cmd in ("esc", "escape"):
            self.send_key_combination([VK_ESCAPE], seq=seq)
            return True
        elif cmd in ("delete", "del"):
            self.send_key_combination([VK_DELETE], seq=seq)
            return True
        elif cmd in ("arrow_up", "up"):
            self.send_key_combination([0x26], seq=seq)
            return True
        elif cmd in ("arrow_down", "down"):
            self.send_key_combination([0x28], seq=seq)
            return True
        elif cmd in ("arrow_left", "left"):
            self.send_key_combination([0x25], seq=seq)
            return True
        elif cmd in ("arrow_right", "right"):
            self.send_key_combination([0x27], seq=seq)
            return True
        elif cmd in ("win_d", "wind"):
            self.send_key_combination([VK_LWIN, 0x44], seq=seq) # Win + D
            return True
        elif cmd in ("win_e", "wine"):
            self.send_key_combination([VK_LWIN, 0x45], seq=seq) # Win + E (Explorer)
            return True
        elif cmd in ("win_r", "winr"):
            self.send_key_combination([VK_LWIN, 0x52], seq=seq) # Win + R (Run)
            return True
        elif cmd in ("alt_tab", "alttab"):
            self.send_key_combination([VK_MENU, VK_TAB], seq=seq) # Alt + Tab
            return True
        elif cmd in ("alt_f4", "altf4"):
            self.send_key_combination([VK_MENU, 0x73], seq=seq) # Alt + F4
            return True
        elif cmd in ("task_mgr", "taskmgr"):
            self.send_key_combination([VK_CONTROL, VK_SHIFT, VK_ESCAPE], seq=seq) # Ctrl+Shift+Esc
            return True
        elif cmd in ("lock", "lock_screen", "win_l"):
            if not is_test_environment() and self.user32 and hasattr(self.user32, "LockWorkStation"):
                try:
                    self.user32.LockWorkStation()
                    return True
                except Exception:
                    pass
            self.send_key_combination([VK_LWIN, 0x4C], seq=seq) # Win + L
            return True
        elif cmd in ("ctrl_alt_del", "sas"):
            if not is_test_environment():
                try:
                    sas_func = getattr(ctypes.windll.sas, "SendSAS", None)
                    if sas_func:
                        sas_func(0)
                        return True
                except Exception:
                    pass
            self.send_key_combination([VK_CONTROL, VK_MENU, VK_DELETE], seq=seq)
            return True

        # Fallback to shortcut string
        return self.send_shortcut(command, seq=seq)
