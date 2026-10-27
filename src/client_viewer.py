"""
AetherDesk Client Viewer GUI Layer.

Architectural Boundary:
- GUI Layer: Tkinter event loop runs exclusively on the main UI thread.
- Asyncio Runtime Layer: Dedicated network background thread runs an asyncio event loop for client network operations.
  UI dispatches async calls via asyncio.run_coroutine_threadsafe.
- Host Session Layer: HostDaemon manages its own thread, embedded signaling broker, capture, and input worker.
  GUI interacts with HostDaemon only via thread-safe lifecycle methods (start, stop).
"""

import io
import os
import sys
import re
import time
import socket
import asyncio
import threading
import ctypes
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

def is_test_environment() -> bool:
    if "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST") or "PYTEST_VERSION" in os.environ:
        return True
    if any("pytest" in arg.lower() for arg in sys.argv):
        return True
    return False

if sys.platform == "win32" and not is_test_environment():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from PIL import Image, ImageTk

from src.client import RemoteClient, ClientState, ClientSettings
from src.security import SecurityManager
from src.host_daemon import HostDaemon
from src.signaling_broker import SignalingBroker
from src.ota import (
    CURRENT_VERSION,
    get_app_version,
    is_newer_version,
    validate_executable_file,
    apply_in_place_update,
    confirm_boot,
    check_boot_state,
    cleanup_old_versions,
    OTAUpdateState,
    check_github_update,
    download_github_binary
)

# ==============================================================================
# DESIGN SYSTEM TOKENS - Apple HIG Dark & macOS Studio
# ==============================================================================
THEME = {
    "bg": "#12141a",              # Apple macOS Dark Canvas (refined slate-obsidian)
    "card": "#1a1d26",            # Elevated Grouped Bento surface
    "card_border": "#282d3b",     # Precision 1px Apple border
    "card_hover": "#222735",      # Subtle tactile hover
    "input_bg": "#101217",        # Inset field background
    "input_border": "#2d3343",    # Clean input boundary
    "input_focus": "#0a84ff",     # Apple System Blue Focus Ring
    "accent": "#0a84ff",          # Apple System Blue (vibrant, accessible)
    "accent_hover": "#0071e3",    # Apple Blue pressed/hover
    "cyan": "#38bdf8",            # Brand cyan accent
    "text_main": "#f5f5f7",       # Primary text (SF crisp off-white)
    "text_muted": "#a1a1a6",      # Secondary text (Apple HIG secondary)
    "text_dim": "#6e6e73",        # Caption / hint text (Apple tertiary)
    "success": "#30d158",         # Apple System Green
    "success_bg": "#0c2817",      # Emerald badge container
    "warning": "#ff9f0a",         # Apple System Orange / Amber
    "danger": "#ff453a",          # Apple System Red
    "danger_hover": "#d73a31",    # Apple Red pressed
    "danger_bg": "#331618",       # Red badge container
    "tab_active_bg": "#2b3242",   # Elevated active segmented pill
    "tab_active_fg": "#ffffff",
    "tab_inactive_bg": "#13161e", # Segmented pill track
    "tab_inactive_fg": "#8e8e93",
    "toolbar_bg": "#181a24",      # Apple floating frosted toolbar
}

def bind_hover(widget, normal_bg, hover_bg, normal_fg=None, hover_fg=None):
    """Instant, tactile micro-interaction hover feedback (Emil Kowalski rule)."""
    def on_enter(e):
        try:
            if widget.cget("state") != "disabled":
                widget.config(bg=hover_bg)
                if hover_fg:
                    widget.config(fg=hover_fg)
        except Exception:
            pass
    def on_leave(e):
        try:
            if widget.cget("state") != "disabled":
                widget.config(bg=normal_bg)
                if normal_fg:
                    widget.config(fg=normal_fg)
        except Exception:
            pass
    widget.bind("<Enter>", on_enter)
    widget.bind("<Leave>", on_leave)

def bind_focus_ring(entry_widget, container_frame, normal_border=THEME["input_border"], focus_border=THEME["input_focus"]):
    """Illuminates 1px border on focus, matching Apple HIG & Linear input fields."""
    entry_widget.bind("<FocusIn>", lambda e: container_frame.config(highlightbackground=focus_border))
    entry_widget.bind("<FocusOut>", lambda e: container_frame.config(highlightbackground=normal_border))


class ClientViewerGUI:
    """Portable GUI Application for connecting to remote desktop host."""

    @staticmethod
    def _center_window(win: tk.Tk | tk.Toplevel, width: int, height: int):
        """Center a window dynamically on the user's primary display."""
        try:
            win.update_idletasks()
            sw = win.winfo_screenwidth()
            sh = win.winfo_screenheight()
            x = max(0, (sw - width) // 2)
            y = max(0, (sh - height) // 2)
            win.geometry(f"{width}x{height}+{x}+{y}")
        except Exception:
            win.geometry(f"{width}x{height}")

    def __init__(self, broker_url: str = "ws://127.0.0.1:8765", start_tab: str = "client"):
        self.root = tk.Tk()
        self.root.title("AetherDesk")
        self._center_window(self.root, 520, 680)
        self.root.configure(bg=THEME["bg"])
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_main_window_close)

        # Set official window icon
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            base_dir = Path(sys._MEIPASS)
        else:
            base_dir = Path(__file__).resolve().parent.parent

        icon_path = base_dir / "assets" / "icon.png"
        if not icon_path.exists():
            icon_path = Path(__file__).resolve().parent / "assets" / "icon.png"
        if icon_path.exists():
            try:
                self.icon_photo = tk.PhotoImage(file=str(icon_path))
                self.root.iconphoto(True, self.icon_photo)
            except Exception:
                pass

        # Apple HIG modern dark ttk theme initialization
        try:
            style = ttk.Style()
            if "clam" in style.theme_names():
                style.theme_use("clam")
            style.configure(
                "TCombobox",
                fieldbackground=THEME["input_bg"],
                background=THEME["card"],
                foreground=THEME["text_main"],
                arrowcolor=THEME["accent"],
                bordercolor=THEME["input_border"],
                lightcolor=THEME["input_border"],
                darkcolor=THEME["input_border"]
            )
            style.map(
                "TCombobox",
                fieldbackground=[("readonly", THEME["input_bg"]), ("focus", THEME["input_bg"])],
                foreground=[("readonly", THEME["text_main"]), ("focus", THEME["text_main"])]
            )
            self.root.option_add("*TCombobox*Listbox.background", THEME["card"])
            self.root.option_add("*TCombobox*Listbox.foreground", THEME["text_main"])
            self.root.option_add("*TCombobox*Listbox.selectBackground", THEME["accent"])
            self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        except Exception:
            pass

        # Purge any leftover previous version backup files or staging scripts
        try:
            cleanup_old_versions()
        except Exception:
            pass

        self.settings = ClientSettings()
        initial_broker = self.settings.get_broker_url() if hasattr(self.settings, "get_broker_url") else broker_url
        self.client = RemoteClient(broker_url=initial_broker, auto_lan_discovery=True)
        self.client.on_frame_callback = self._on_frame_received
        self.client.on_status_callback = self._on_status_update
        self.client.on_ota_info_callback = self._on_ota_info
        self.client.on_ota_progress_callback = self._on_ota_progress
        self.client.on_ota_complete_callback = self._on_ota_complete
        self.client.on_monitors_callback = self._on_monitors_received
        self.client.on_verify_fingerprint_callback = self._on_verify_host_fingerprint
        self.client.on_disconnect_callback = self._on_remote_disconnect

        # Host daemon integrated management
        self.security = SecurityManager()
        self.host_daemon = None
        self.host_thread = None
        self.host_broker = None

        self.loop = asyncio.new_event_loop()
        self.network_thread = threading.Thread(target=self._run_async_loop, daemon=True)
        self.network_thread.start()

        self.viewer_window = None
        self.canvas = None
        self.photo_img = None
        self.current_img_dims = (0, 0)
        self._canvas_img_id = None
        self.pwd_visible = False

        # Dual-Window Multi-Monitor State
        self.dual_window_mode = False
        self.viewer_window_m2 = None
        self.canvas_m2 = None
        self.photo_img_m2 = None
        self.current_img_dims_m2 = (0, 0)
        self._canvas_img_id_m2 = None
        self.remote_monitors = []
        self.btn_mdual = None
        self._crop_m1 = (0, 0, 1920, 1080)
        self._crop_m2 = (1920, 0, 3840, 1080)
        self._last_full_dims = (1920, 1080)
        self._pending_frame = None
        self._render_scheduled = False

        # OTA state
        self.ota_update_available = False
        self.ota_remote_version = ""
        self.ota_downloading = False
        self.btn_ota = None
        self.git_update_info = None
        self.btn_git_update = None
        self.btn_header_update = None

        # FPS & Pipeline Telemetry metrics
        self._frame_count = 0
        self._last_fps_time = time.time()
        self._current_fps = 0
        self._latencies = []
        self._render_latencies = []
        self._current_latency = 0.0
        self._current_render_ms = 0.0
        self.lbl_fps = None

        self.active_tab = start_tab

        self._build_login_ui()

        # Check for Git OTA updates automatically in background on startup
        threading.Thread(target=lambda: self._check_git_updates_background(notify_if_none=False, prompt_on_found=True), daemon=True).start()

        # Auto-start Host daemon in background so this PC is immediately reachable
        self.root.after(200, self._auto_start_host_daemon)

        # Start polling for discovered LAN peers
        self.root.after(1500, self._poll_discovered_lan_peers)

        # Failsafe Boot Confirmation
        if check_boot_state() == OTAUpdateState.TESTING:
            self.root.after(2500, self._verify_and_confirm_ota_boot)

    def _auto_start_host_daemon(self):
        try:
            if not self.host_daemon or not self.host_daemon.running:
                self._toggle_host_daemon()
        except Exception as e:
            pass

    def _poll_discovered_lan_peers(self):
        try:
            from src.discovery import get_discovery_listener
            listener = get_discovery_listener()
            peers = listener.get_discovered_peers()
            if peers and hasattr(self, "combo_id"):
                saved_devices = self.settings.get_devices() if hasattr(self, "settings") else []
                current_val = self.combo_id.get()
                options = []
                for p in peers:
                    options.append(f"{p['device_id']} (🌐 {p['name']} - {p['ip']})")
                for d in saved_devices:
                    opt = f"{d['id']} ({d.get('name', 'PC')})"
                    if opt not in options:
                        options.append(opt)
                if options:
                    self.combo_id["values"] = options
        except Exception:
            pass
        if self.root:
            self.root.after(3000, self._poll_discovered_lan_peers)

    def _verify_and_confirm_ota_boot(self):
        try:
            if self.root and self.network_thread.is_alive():
                confirmed = confirm_boot()
                if confirmed:
                    self._on_status_update(f"AetherDesk v{get_app_version()} confermato con successo!")
        except Exception:
            pass

    def _run_async_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _get_network_endpoints(self) -> list[tuple[str, str]]:
        endpoints = []
        found_ips = set()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                primary_ip = s.getsockname()[0]
                if primary_ip and not primary_ip.startswith("127."):
                    found_ips.add(primary_ip)
                    endpoints.append(("Stesso Wi-Fi / LAN", f"ws://{primary_ip}:8765"))
        except Exception:
            pass
        try:
            for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
                if ip not in found_ips and not ip.startswith("127.") and not ip.startswith("169.254."):
                    found_ips.add(ip)
                    label = "Tailscale VPN" if ip.startswith("100.") else "Rete Locale (LAN)"
                    endpoints.append((label, f"ws://{ip}:8765"))
        except Exception:
            pass
        if not endpoints:
            endpoints.append(("Locale (Test)", "ws://127.0.0.1:8765"))
        return endpoints

    def _build_login_ui(self):
        for w in self.root.winfo_children():
            w.destroy()

        container = tk.Frame(self.root, bg=THEME["bg"], padx=26, pady=20)
        container.pack(fill=tk.BOTH, expand=True)

        # 1. Top Header & Segmented Pill Switcher
        header_bar = tk.Frame(container, bg=THEME["bg"])
        header_bar.pack(fill=tk.X, pady=(0, 16))

        # Brand Identity (Apple HIG Typography & Status Pill)
        brand_frame = tk.Frame(header_bar, bg=THEME["bg"])
        brand_frame.pack(side=tk.LEFT)

        title_row = tk.Frame(brand_frame, bg=THEME["bg"])
        title_row.pack(anchor="w")

        lbl_dot = tk.Label(title_row, text="●", bg=THEME["bg"], fg=THEME["success"], font=("Segoe UI", 11, "bold"))
        lbl_dot.pack(side=tk.LEFT, padx=(0, 6))

        lbl_brand = tk.Label(title_row, text="AETHERDESK", bg=THEME["bg"], fg=THEME["text_main"],
                             font=("Segoe UI", 15, "bold"))
        lbl_brand.pack(side=tk.LEFT)

        self.btn_header_update = tk.Button(
            title_row, text="🔄 Aggiornamenti", bg=THEME["card"], fg=THEME["cyan"],
            activebackground=THEME["card_hover"], activeforeground="#ffffff", font=("Segoe UI", 8, "bold"),
            relief="flat", cursor="hand2", padx=8, pady=2, highlightthickness=1,
            highlightbackground=THEME["card_border"], command=self._on_git_update_click
        )
        self.btn_header_update.pack(side=tk.LEFT, padx=(8, 0))
        bind_hover(self.btn_header_update, THEME["card"], THEME["card_hover"], THEME["cyan"], "#ffffff")

        lbl_sub = tk.Label(brand_frame, text="Controllo Remoto Ultra-Leggero • Crittografia E2EE",
                           bg=THEME["bg"], fg=THEME["text_dim"], font=("Segoe UI", 9))
        lbl_sub.pack(anchor="w", pady=(2, 0))

        # Segmented Control Pill [ 💻 Client | 🖥 Host ] (Apple macOS HIG style)
        tab_capsule = tk.Frame(header_bar, bg=THEME["tab_inactive_bg"], padx=3, pady=3,
                               highlightthickness=1, highlightbackground=THEME["card_border"])
        tab_capsule.pack(side=tk.RIGHT)

        self.btn_tab_client = tk.Button(
            tab_capsule, text="💻 Client", font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2",
            padx=14, pady=4, highlightthickness=0,
            bg=THEME["tab_active_bg"] if self.active_tab == "client" else THEME["tab_inactive_bg"],
            fg=THEME["tab_active_fg"] if self.active_tab == "client" else THEME["tab_inactive_fg"],
            command=lambda: self._switch_tab("client")
        )
        self.btn_tab_client.pack(side=tk.LEFT)

        self.btn_tab_host = tk.Button(
            tab_capsule, text="🖥 Host", font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2",
            padx=14, pady=4, highlightthickness=0,
            bg=THEME["tab_active_bg"] if self.active_tab == "host" else THEME["tab_inactive_bg"],
            fg=THEME["tab_active_fg"] if self.active_tab == "host" else THEME["tab_inactive_fg"],
            command=lambda: self._switch_tab("host")
        )
        self.btn_tab_host.pack(side=tk.LEFT, padx=(2, 0))

        bind_hover(self.btn_tab_client, THEME["tab_inactive_bg"], THEME["card_hover"], THEME["tab_inactive_fg"], THEME["text_main"])
        bind_hover(self.btn_tab_host, THEME["tab_inactive_bg"], THEME["card_hover"], THEME["tab_inactive_fg"], THEME["text_main"])

        # Main Body Frame (Swapped by tabs)
        self.body_frame = tk.Frame(container, bg=THEME["bg"])
        self.body_frame.pack(fill=tk.BOTH, expand=True)

        if self.active_tab == "client":
            self._render_client_tab(self.body_frame)
        else:
            self._render_host_tab(self.body_frame)

        # 3. Footer Bar with Version & Live Git OTA Status
        footer = tk.Frame(container, bg=THEME["bg"])
        footer.pack(fill=tk.X, pady=(12, 0))

        lbl_foot_sec = tk.Label(footer, text=f"AetherDesk v{get_app_version()} • 🔒 AES-256-GCM Zero-Knowledge",
                                bg=THEME["bg"], fg=THEME["text_dim"], font=("Segoe UI", 8))
        lbl_foot_sec.pack(side=tk.LEFT)

        self.btn_git_update = tk.Button(
            footer, text="🔄 Controlla Aggiornamenti", bg=THEME["card"], fg=THEME["text_muted"],
            activebackground=THEME["card_hover"], activeforeground="#ffffff", font=("Segoe UI", 8, "bold"),
            relief="flat", cursor="hand2", padx=10, pady=3, highlightthickness=1,
            highlightbackground=THEME["card_border"], command=self._on_git_update_click
        )
        self.btn_git_update.pack(side=tk.RIGHT)
        bind_hover(self.btn_git_update, THEME["card"], THEME["card_hover"], THEME["text_muted"], THEME["cyan"])

    def _switch_tab(self, tab: str):
        self.active_tab = tab
        if tab == "client":
            self.btn_tab_client.config(bg=THEME["tab_active_bg"], fg=THEME["tab_active_fg"])
            self.btn_tab_host.config(bg=THEME["tab_inactive_bg"], fg=THEME["tab_inactive_fg"])
        else:
            self.btn_tab_host.config(bg=THEME["tab_active_bg"], fg=THEME["tab_active_fg"])
            self.btn_tab_client.config(bg=THEME["tab_inactive_bg"], fg=THEME["tab_inactive_fg"])

        for w in self.body_frame.winfo_children():
            w.destroy()

        if tab == "client":
            self._render_client_tab(self.body_frame)
        else:
            self._render_host_tab(self.body_frame)

    def _render_client_tab(self, parent):
        saved_id, saved_pwd, saved_rem = self.settings.load()
        saved_broker = self.settings.get_broker_url()
        devices = self.settings.get_devices()

        # Bento Card: Remote Connection
        conn_card = tk.Frame(parent, bg=THEME["card"], padx=18, pady=16,
                             highlightthickness=1, highlightbackground=THEME["card_border"])
        conn_card.pack(fill=tk.X, pady=(0, 12))

        lbl_card_title = tk.Label(conn_card, text="●  Connessione Remota", bg=THEME["card"],
                                  fg=THEME["accent"], font=("Segoe UI", 9, "bold"))
        lbl_card_title.pack(anchor="w", pady=(0, 10))

        # 1. Device ID Field
        lbl_id = tk.Label(conn_card, text="ID Computer Remoto (9 cifre):", bg=THEME["card"],
                          fg=THEME["text_muted"], font=("Segoe UI", 9, "bold"))
        lbl_id.pack(anchor="w", pady=(0, 4))

        id_box = tk.Frame(conn_card, bg=THEME["input_bg"], highlightthickness=1,
                          highlightbackground=THEME["input_border"], padx=8, pady=4)
        id_box.pack(fill=tk.X, pady=(0, 10))

        device_options = [f"{d['id']} ({d.get('name', 'PC')})" for d in devices] if devices else []
        self.combo_id = ttk.Combobox(id_box, values=device_options, font=("Consolas", 13, "bold"), justify="left")
        self.combo_id.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3)
        default_id = saved_id if saved_id else self.security.get_or_create_device_id()
        self.combo_id.set(default_id)
        self.combo_id.bind("<<ComboboxSelected>>", self._on_device_selected)
        self.combo_id.bind("<Return>", lambda e: self._start_connect())
        bind_focus_ring(self.combo_id, id_box)

        btn_paste_id = tk.Button(id_box, text="Incolla", bg=THEME["card_hover"], fg=THEME["text_muted"], relief="flat",
                                 font=("Segoe UI", 8, "bold"), cursor="hand2", padx=8, pady=2, command=self._paste_id)
        btn_paste_id.pack(side=tk.RIGHT, padx=(4, 0))
        bind_hover(btn_paste_id, THEME["card_hover"], THEME["card_border"], THEME["text_muted"], THEME["text_main"])

        # 2. Password Field
        lbl_pwd = tk.Label(conn_card, text="Password di Accesso:", bg=THEME["card"],
                          fg=THEME["text_muted"], font=("Segoe UI", 9, "bold"))
        lbl_pwd.pack(anchor="w", pady=(0, 4))

        pwd_box = tk.Frame(conn_card, bg=THEME["input_bg"], highlightthickness=1,
                          highlightbackground=THEME["input_border"], padx=8, pady=4)
        pwd_box.pack(fill=tk.X, pady=(0, 10))

        self.entry_pwd = tk.Entry(pwd_box, font=("Segoe UI", 11), show="*", bg=THEME["input_bg"],
                                  fg=THEME["text_main"], insertbackground="white", relief="flat")
        self.entry_pwd.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3)
        self.entry_pwd.bind("<Return>", lambda e: self._start_connect())
        if saved_pwd:
            self.entry_pwd.insert(0, saved_pwd)
        else:
            self.entry_pwd.insert(0, self.security.get_password_display())
        bind_focus_ring(self.entry_pwd, pwd_box)

        self.btn_pwd_toggle = tk.Button(pwd_box, text="👁", bg=THEME["card_hover"], fg=THEME["text_muted"], relief="flat",
                                        font=("Segoe UI", 9), cursor="hand2", padx=6, pady=2, command=self._toggle_pwd_view)
        self.btn_pwd_toggle.pack(side=tk.RIGHT)
        bind_hover(self.btn_pwd_toggle, THEME["card_hover"], THEME["card_border"], THEME["text_muted"], THEME["text_main"])

        # 3. Server Broker URL
        lbl_broker = tk.Label(conn_card, text="Server Broker (IP o Dominio):", bg=THEME["card"],
                              fg=THEME["text_muted"], font=("Segoe UI", 9, "bold"))
        lbl_broker.pack(anchor="w", pady=(0, 4))

        brk_box = tk.Frame(conn_card, bg=THEME["input_bg"], highlightthickness=1,
                          highlightbackground=THEME["input_border"], padx=8, pady=4)
        brk_box.pack(fill=tk.X, pady=(0, 6))

        self.entry_broker = tk.Entry(brk_box, font=("Consolas", 10), bg=THEME["input_bg"],
                                     fg=THEME["text_main"], insertbackground="white", relief="flat")
        self.entry_broker.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3)
        self.entry_broker.bind("<Return>", lambda e: self._start_connect())
        self.entry_broker.insert(0, saved_broker if saved_broker else "ws://127.0.0.1:8765")
        bind_focus_ring(self.entry_broker, brk_box)

        btn_paste_brk = tk.Button(brk_box, text="Incolla", bg=THEME["card_hover"], fg=THEME["text_muted"], relief="flat",
                                  font=("Segoe UI", 8, "bold"), cursor="hand2", padx=8, pady=2, command=self._paste_broker)
        btn_paste_brk.pack(side=tk.RIGHT)
        bind_hover(btn_paste_brk, THEME["card_hover"], THEME["card_border"], THEME["text_muted"], THEME["text_main"])

        # Quick Broker Preset Chips
        chips_frame = tk.Frame(conn_card, bg=THEME["card"])
        chips_frame.pack(fill=tk.X, pady=(0, 10))

        def _set_brk(url):
            self.entry_broker.delete(0, tk.END)
            self.entry_broker.insert(0, url)

        btn_chip_local = tk.Button(chips_frame, text="🏠 Locale (127.0.0.1)", bg=THEME["card_hover"], fg=THEME["text_dim"],
                                   font=("Segoe UI", 8), relief="flat", cursor="hand2", padx=8, pady=2,
                                   highlightthickness=1, highlightbackground=THEME["card_border"],
                                   command=lambda: _set_brk("ws://127.0.0.1:8765"))
        btn_chip_local.pack(side=tk.LEFT, padx=(0, 4))
        bind_hover(btn_chip_local, THEME["card_hover"], THEME["card_border"], THEME["text_dim"], THEME["cyan"])

        # Remember Checkbox
        self.var_remember = tk.BooleanVar(value=saved_rem)
        self.chk_remember = tk.Checkbutton(
            conn_card, text="Ricorda questo computer e impostazioni", variable=self.var_remember,
            bg=THEME["card"], fg=THEME["text_muted"], selectcolor=THEME["input_bg"],
            activebackground=THEME["card"], activeforeground=THEME["text_main"],
            font=("Segoe UI", 9)
        )
        self.chk_remember.pack(anchor="w", pady=(0, 12))

        # Primary Action Button (Apple System Blue CTA)
        self.btn_connect = tk.Button(
            conn_card, text="🚀  Connetti al Computer", bg=THEME["accent"], fg="#ffffff",
            activebackground=THEME["accent_hover"], activeforeground="#ffffff",
            font=("Segoe UI", 11, "bold"), relief="flat", cursor="hand2", command=self._start_connect
        )
        self.btn_connect.pack(fill=tk.X, ipady=8)
        bind_hover(self.btn_connect, THEME["accent"], THEME["accent_hover"], "#ffffff", "#ffffff")

        # Status Bar
        self.lbl_status = tk.Label(conn_card, text="● Pronto alla connessione.", bg=THEME["card"],
                                   fg=THEME["success"], font=("Segoe UI", 9))
        self.lbl_status.pack(pady=(10, 0))

        # Bento Card: Saved Devices
        if devices or saved_id:
            quick_frame = tk.Frame(parent, bg=THEME["card"], padx=18, pady=12,
                                   highlightthickness=1, highlightbackground=THEME["card_border"])
            quick_frame.pack(fill=tk.X)

            lbl_q_title = tk.Label(quick_frame, text="●  Dispositivi Recenti & Salvati", bg=THEME["card"],
                                   fg=THEME["text_muted"], font=("Segoe UI", 9, "bold"))
            lbl_q_title.pack(anchor="w", pady=(0, 3))

            lbl_q_hint = tk.Label(quick_frame, text="Seleziona un dispositivo per compilare automaticamente i campi:",
                                  bg=THEME["card"], fg=THEME["text_dim"], font=("Segoe UI", 8))
            lbl_q_hint.pack(anchor="w", pady=(0, 8))

            displayed_devices = devices if devices else [{"id": saved_id, "name": "PC Recente", "password": saved_pwd, "broker_url": saved_broker}]
            for dev in displayed_devices[:2]:
                d_id = dev.get("id", "")
                d_name = dev.get("name", "Computer")
                d_brk = dev.get("broker_url", "ws://127.0.0.1:8765")

                row = tk.Frame(quick_frame, bg=THEME["card"], pady=4)
                row.pack(fill=tk.X)

                lbl_ico = tk.Label(row, text="💻", bg=THEME["card"], fg=THEME["cyan"], font=("Segoe UI", 12))
                lbl_ico.pack(side=tk.LEFT, padx=(0, 8))

                info_frame = tk.Frame(row, bg=THEME["card"])
                info_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)

                lbl_n = tk.Label(info_frame, text=f"{d_name}  [{d_id}]", bg=THEME["card"],
                                 fg=THEME["text_main"], font=("Segoe UI", 9, "bold"), anchor="w")
                lbl_n.pack(anchor="w")

                lbl_b = tk.Label(info_frame, text=d_brk, bg=THEME["card"], fg=THEME["text_dim"],
                                 font=("Consolas", 8), anchor="w")
                lbl_b.pack(anchor="w")

                btn_sel = tk.Button(row, text="Seleziona", bg=THEME["card_hover"], fg=THEME["cyan"],
                                    activebackground=THEME["card_border"], activeforeground="#ffffff",
                                    font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2", padx=12, pady=3,
                                    highlightthickness=1, highlightbackground=THEME["card_border"],
                                    command=lambda d=dev: self._select_saved_device(d))
                btn_sel.pack(side=tk.RIGHT)
                bind_hover(btn_sel, THEME["card_hover"], THEME["card_border"], THEME["cyan"], "#ffffff")

    def _render_host_tab(self, parent):
        # Card 1: This Machine ID & Status
        host_card = tk.Frame(parent, bg=THEME["card"], padx=18, pady=16,
                             highlightthickness=1, highlightbackground=THEME["card_border"])
        host_card.pack(fill=tk.X, pady=(0, 12))

        lbl_h_title = tk.Label(host_card, text="●  Assistenza e Condivisione", bg=THEME["card"],
                               fg=THEME["accent"], font=("Segoe UI", 9, "bold"))
        lbl_h_title.pack(anchor="w", pady=(0, 10))

        # Big Monospace ID Display (Apple HIG Grouped Display)
        id_display_box = tk.Frame(host_card, bg=THEME["input_bg"], padx=14, pady=10,
                                  highlightthickness=1, highlightbackground=THEME["input_border"])
        id_display_box.pack(fill=tk.X, pady=(0, 12))

        lbl_id_hint = tk.Label(id_display_box, text="ID DISPOSITIVO (DA FORNIRE A CHI SI CONNETTE):",
                               bg=THEME["input_bg"], fg=THEME["text_dim"], font=("Segoe UI", 8, "bold"))
        lbl_id_hint.pack(anchor="w")

        id_val_row = tk.Frame(id_display_box, bg=THEME["input_bg"])
        id_val_row.pack(fill=tk.X, pady=(4, 0))

        dev_id = self.security.get_or_create_device_id()
        lbl_id_val = tk.Label(id_val_row, text=dev_id, bg=THEME["input_bg"], fg=THEME["warning"],
                              font=("Consolas", 22, "bold"))
        lbl_id_val.pack(side=tk.LEFT)

        btn_copy_id = tk.Button(id_val_row, text="📋 Copia ID", bg=THEME["card_hover"], fg=THEME["text_main"],
                                font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2", padx=12, pady=4,
                                highlightthickness=1, highlightbackground=THEME["card_border"])
        btn_copy_id.config(command=lambda: self._copy_text(dev_id, "ID Dispositivo", btn_widget=btn_copy_id))
        btn_copy_id.pack(side=tk.RIGHT)
        bind_hover(btn_copy_id, THEME["card_hover"], THEME["card_border"], THEME["text_main"], THEME["cyan"])

        # Password Config
        lbl_pwd_h = tk.Label(host_card, text="Password di Accesso Remoto:", bg=THEME["card"],
                             fg=THEME["text_muted"], font=("Segoe UI", 9, "bold"))
        lbl_pwd_h.pack(anchor="w", pady=(0, 4))

        pwd_row = tk.Frame(host_card, bg=THEME["input_bg"], highlightthickness=1,
                           highlightbackground=THEME["input_border"], padx=8, pady=3)
        pwd_row.pack(fill=tk.X, pady=(0, 12))

        self.entry_host_pwd = tk.Entry(pwd_row, show="*", font=("Segoe UI", 11), bg=THEME["input_bg"],
                                       fg=THEME["text_main"], insertbackground="white", relief="flat")
        self.entry_host_pwd.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3)
        self.entry_host_pwd.insert(0, self.security.get_password_display())
        bind_focus_ring(self.entry_host_pwd, pwd_row)

        self.host_pwd_visible = False
        def _toggle_h_pwd():
            self.host_pwd_visible = not self.host_pwd_visible
            self.entry_host_pwd.config(show="" if self.host_pwd_visible else "*")
            btn_h_eye.config(text="🙈" if self.host_pwd_visible else "👁")

        btn_h_eye = tk.Button(pwd_row, text="👁", bg=THEME["card_hover"], fg=THEME["text_muted"], relief="flat",
                              font=("Segoe UI", 9), cursor="hand2", padx=6, command=_toggle_h_pwd)
        btn_h_eye.pack(side=tk.LEFT, padx=(0, 4))
        bind_hover(btn_h_eye, THEME["card_hover"], THEME["card_border"], THEME["text_muted"], THEME["text_main"])

        btn_cp_h_pwd = tk.Button(pwd_row, text="📋 Copia", bg=THEME["card_hover"], fg=THEME["text_muted"],
                                 font=("Segoe UI", 9), relief="flat", cursor="hand2", padx=6,
                                 command=lambda: self._copy_text(self.entry_host_pwd.get(), "Password", btn_widget=btn_cp_h_pwd))
        btn_cp_h_pwd.pack(side=tk.LEFT, padx=(0, 6))
        bind_hover(btn_cp_h_pwd, THEME["card_hover"], THEME["card_border"], THEME["text_muted"], THEME["text_main"])

        self.btn_save_host_pwd = tk.Button(pwd_row, text="Salva Password", bg=THEME["accent"], fg="#ffffff",
                                           font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2", padx=10, pady=3,
                                           command=self._save_host_password)
        self.btn_save_host_pwd.pack(side=tk.RIGHT)
        bind_hover(self.btn_save_host_pwd, THEME["accent"], THEME["accent_hover"], "#ffffff", "#ffffff")

        # Daemon State Control & Status
        daemon_row = tk.Frame(host_card, bg=THEME["card"])
        daemon_row.pack(fill=tk.X, pady=(2, 0))

        is_running = bool(self.host_daemon and self.host_daemon.running)
        self.lbl_host_status = tk.Label(
            daemon_row,
            text="● Servizio Host Attivo (Porta 8765)" if is_running else "● Servizio Host non attivo",
            bg=THEME["card"], fg=THEME["success"] if is_running else THEME["text_dim"],
            font=("Segoe UI", 9, "bold")
        )
        self.lbl_host_status.pack(side=tk.LEFT)

        self.btn_host_toggle = tk.Button(
            daemon_row,
            text="Arresta Servizio" if is_running else "Avvia Servizio",
            bg=THEME["danger"] if is_running else THEME["accent"], fg="#ffffff",
            font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2", padx=12, pady=4,
            command=self._toggle_host_daemon
        )
        self.btn_host_toggle.pack(side=tk.RIGHT)
        bind_hover(self.btn_host_toggle,
                   THEME["danger"] if is_running else THEME["accent"],
                   THEME["danger_hover"] if is_running else THEME["accent_hover"],
                   "#ffffff", "#ffffff")

        # Card 2: Detected LAN / Wi-Fi Endpoints
        net_card = tk.Frame(parent, bg=THEME["card"], padx=18, pady=12,
                            highlightthickness=1, highlightbackground=THEME["card_border"])
        net_card.pack(fill=tk.X, pady=(0, 10))

        lbl_net_title = tk.Label(net_card, text="●  Indirizzi Broker Rilevati per il Client", bg=THEME["card"],
                                 fg=THEME["text_muted"], font=("Segoe UI", 9, "bold"))
        lbl_net_title.pack(anchor="w", pady=(0, 6))

        for net_label, net_url in self._get_network_endpoints():
            row = tk.Frame(net_card, bg=THEME["card"], pady=2)
            row.pack(fill=tk.X)

            lbl_l = tk.Label(row, text=f"{net_label}:", bg=THEME["card"], fg=THEME["text_muted"],
                             font=("Segoe UI", 8, "bold"), width=16, anchor="w")
            lbl_l.pack(side=tk.LEFT)

            lbl_u = tk.Label(row, text=net_url, bg=THEME["input_bg"], fg=THEME["success"],
                             font=("Consolas", 9), padx=6, pady=2)
            lbl_u.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))

            btn_cp = tk.Button(row, text="Copia", bg=THEME["card_hover"], fg=THEME["text_muted"], relief="flat",
                               font=("Segoe UI", 8, "bold"), cursor="hand2", padx=8, pady=2,
                               highlightthickness=1, highlightbackground=THEME["card_border"])
            btn_cp.config(command=lambda u=net_url, b=btn_cp: self._copy_text(u, "URL", btn_widget=b))
            btn_cp.pack(side=tk.RIGHT)
            bind_hover(btn_cp, THEME["card_hover"], THEME["card_border"], THEME["text_muted"], THEME["text_main"])

        # Quick System Tools
        tools_row = tk.Frame(parent, bg=THEME["bg"])
        tools_row.pack(fill=tk.X, pady=(2, 0))

        btn_fw = tk.Button(tools_row, text="🛡 Sblocca Firewall Windows", bg=THEME["card"], fg=THEME["warning"],
                           font=("Segoe UI", 8, "bold"), relief="flat", cursor="hand2", padx=10, pady=5,
                           highlightthickness=1, highlightbackground=THEME["card_border"],
                           command=self._open_firewall_unblock)
        btn_fw.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        bind_hover(btn_fw, THEME["card"], THEME["card_hover"], THEME["warning"], "#fde047")

        btn_svc = tk.Button(tools_row, text="⚙ Installa come Servizio Windows", bg=THEME["card"], fg=THEME["text_muted"],
                            font=("Segoe UI", 8, "bold"), relief="flat", cursor="hand2", padx=10, pady=5,
                            highlightthickness=1, highlightbackground=THEME["card_border"],
                            command=self._open_installer)
        btn_svc.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(4, 0))
        bind_hover(btn_svc, THEME["card"], THEME["card_hover"], THEME["text_muted"], THEME["text_main"])

    def _copy_text(self, text: str, label: str = "Testo", btn_widget: tk.Button = None):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        except Exception:
            pass
        if btn_widget:
            orig_text = btn_widget.cget("text")
            orig_fg = btn_widget.cget("fg")
            btn_widget.config(text="✓ Copiato!", fg=THEME["success"])
            def _revert():
                try:
                    btn_widget.config(text=orig_text, fg=orig_fg)
                except Exception:
                    pass
            self.root.after(1400, _revert)
        if hasattr(self, "lbl_status") and self.lbl_status and self.lbl_status.winfo_exists():
            orig_status = self.lbl_status.cget("text")
            orig_status_fg = self.lbl_status.cget("fg")
            self.lbl_status.config(text=f"✓ {label} copiato negli appunti", fg=THEME["success"])
            def _revert_status():
                try:
                    if self.lbl_status and self.lbl_status.winfo_exists():
                        self.lbl_status.config(text=orig_status, fg=orig_status_fg)
                except Exception:
                    pass
            self.root.after(2000, _revert_status)

    def _save_host_password(self):
        new_pwd = self.entry_host_pwd.get().strip() if hasattr(self, "entry_host_pwd") else ""
        if not new_pwd:
            messagebox.showwarning("Attenzione", "Inserisci una password valida non vuota.")
            return
        self.security.set_password(new_pwd)
        self.entry_host_pwd.delete(0, tk.END)
        if hasattr(self, "btn_save_host_pwd") and self.btn_save_host_pwd:
            orig_text = self.btn_save_host_pwd.cget("text")
            self.btn_save_host_pwd.config(text="✓ Salvata!", bg=THEME["success"])
            def _revert_pwd_btn():
                try:
                    self.btn_save_host_pwd.config(text=orig_text, bg=THEME["accent"])
                except Exception:
                    pass
            self.root.after(1500, _revert_pwd_btn)

    def _on_main_window_close(self):
        """Coordinated, deterministic shutdown of GUI, client, host daemon, and network threads."""
        logger.info("Main window closing, initiating clean shutdown sequence...")

        # 1. Stop host daemon if running
        if self.host_daemon:
            try:
                self.host_daemon.stop(timeout=2.0)
            except Exception as e:
                logger.warning(f"Host daemon stop error: {e}")

        # 2. Join host thread if alive (do not drop reference if still alive)
        if self.host_thread and self.host_thread.is_alive():
            try:
                self.host_thread.join(timeout=2.0)
                if not self.host_thread.is_alive():
                    self.host_thread = None
                else:
                    logger.warning("Host thread still running after timeout")
            except Exception as e:
                logger.warning(f"Host thread join error: {e}")

        # 3. Disconnect client on its owning loop and await completion
        if self.client and self.loop and self.loop.is_running():
            try:
                fut = asyncio.run_coroutine_threadsafe(self.client.disconnect(), self.loop)
                fut.result(timeout=2.0)
            except Exception as e:
                logger.warning(f"Client disconnect error during shutdown: {e}")

        # 4. Stop client network loop and join network thread
        if self.loop and self.loop.is_running():
            try:
                self.loop.call_soon_threadsafe(self.loop.stop)
            except Exception:
                pass
        if self.network_thread and self.network_thread.is_alive():
            try:
                self.network_thread.join(timeout=2.0)
                if not self.network_thread.is_alive():
                    self.network_thread = None
                else:
                    logger.warning("Network thread still running after timeout")
            except Exception as e:
                logger.warning(f"Network thread join error: {e}")

        # 5. Cleanly destroy Tkinter root
        try:
            self.root.destroy()
        except Exception:
            pass

    def _toggle_host_daemon(self):
        if not self.host_daemon or not self.host_daemon.running:
            self.host_daemon = HostDaemon(broker_url="ws://127.0.0.1:8765")
            self.host_thread = threading.Thread(target=self._run_host_daemon_worker, daemon=True)
            self.host_thread.start()
            if hasattr(self, "btn_host_toggle"):
                self.btn_host_toggle.config(text="Arresta", bg="#ef4444")
            if hasattr(self, "lbl_host_status"):
                self.lbl_host_status.config(text="● Servizio Host Attivo (In ascolto su porta 8765)", fg=THEME["success"])
        else:
            if self.host_daemon:
                self.host_daemon.stop(timeout=2.0)
                self.host_daemon = None
            if self.host_thread and self.host_thread.is_alive():
                self.host_thread.join(timeout=2.0)
                self.host_thread = None
            self.host_broker = None
            if hasattr(self, "btn_host_toggle"):
                self.btn_host_toggle.config(text="Avvia Demone", bg=THEME["accent"])
            if hasattr(self, "lbl_host_status"):
                self.lbl_host_status.config(text="● Servizio Host Arrestato", fg=THEME["danger"])

    def _run_host_daemon_worker(self):
        try:
            asyncio.run(self.host_daemon.start())
        except Exception as e:
            logger.debug(f"Host daemon worker exited: {e}")

    def _open_firewall_unblock(self):
        base_dir = getattr(sys, "_MEIPASS", os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
        bat_path = os.path.join(base_dir, "scripts", "allow_firewall.bat")
        if not os.path.exists(bat_path):
            bat_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts", "allow_firewall.bat"))
        if os.path.exists(bat_path):
            os.startfile(bat_path)
        else:
            messagebox.showerror("Errore", f"File non trovato: {bat_path}")

    def _open_installer(self):
        base_dir = getattr(sys, "_MEIPASS", os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
        bat_path = os.path.join(base_dir, "scripts", "install_service.bat")
        if not os.path.exists(bat_path):
            bat_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts", "install_service.bat"))
        if os.path.exists(bat_path):
            os.startfile(bat_path)
        else:
            messagebox.showerror("Errore", f"File non trovato: {bat_path}")

    def _select_saved_device(self, dev: dict):
        d_id = dev.get("id", "")
        self.combo_id.set(d_id)
        self.entry_pwd.delete(0, tk.END)
        self.entry_pwd.insert(0, dev.get("password", ""))
        if dev.get("broker_url"):
            self.entry_broker.delete(0, tk.END)
            self.entry_broker.insert(0, dev.get("broker_url"))
        name = dev.get("name", "Computer")
        self._on_status_update(f"Selezionato: {name} [{d_id}]. Clicca 'Connetti al Computer' per avviare.")

    def _quick_connect_to(self, dev: dict):
        self._select_saved_device(dev)

    def _toggle_pwd_view(self):
        self.pwd_visible = not self.pwd_visible
        self.entry_pwd.config(show="" if self.pwd_visible else "*")
        self.btn_pwd_toggle.config(text="🙈" if self.pwd_visible else "👁")

    def _paste_id(self):
        try:
            txt = self.root.clipboard_get().strip()
            self.combo_id.set(txt)
        except Exception:
            pass

    def _paste_broker(self):
        try:
            txt = self.root.clipboard_get().strip()
            self.entry_broker.delete(0, tk.END)
            self.entry_broker.insert(0, txt)
        except Exception:
            pass

    def _on_device_selected(self, event=None):
        selected = self.combo_id.get().strip()
        clean_id = re.sub(r"\s+", "", selected.split("(")[0]) if "(" in selected else re.sub(r"\s+", "", selected)

        # Auto-extract IP if chosen from LAN discovery (e.g. "... - 192.168.1.50)")
        if "(" in selected and "-" in selected:
            ip_match = re.search(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})", selected)
            if ip_match:
                lan_ip = ip_match.group(1)
                self.entry_broker.delete(0, tk.END)
                self.entry_broker.insert(0, f"ws://{lan_ip}:8765")

        devices = self.settings.get_devices()
        for d in devices:
            if re.sub(r"\s+", "", d.get("id", "")) == clean_id:
                self.combo_id.set(d.get("id", ""))
                self.entry_pwd.delete(0, tk.END)
                self.entry_pwd.insert(0, d.get("password", ""))
                if d.get("broker_url"):
                    self.entry_broker.delete(0, tk.END)
                    self.entry_broker.insert(0, d.get("broker_url"))
                break

    def _on_status_update(self, text: str):
        if hasattr(self, "lbl_status") and self.lbl_status:
            self.root.after(0, lambda: self.lbl_status.config(text=f"● {text}"))
        if self.viewer_window and tk.Toplevel.winfo_exists(self.viewer_window):
            self.root.after(0, lambda: self.viewer_window.title(f"AetherDesk - [{self.client.state.target_id}] - {text}"))
            if "Password errata" in text:
                self.root.after(0, lambda: messagebox.showerror("Disconnesso", text))
                self.root.after(0, self._close_viewer)

    def _start_connect(self):
        raw_val = self.combo_id.get().strip()
        dev_id = raw_val.split("(")[0].strip() if "(" in raw_val else raw_val
        pwd = self.entry_pwd.get().strip()
        broker_url = self.entry_broker.get().strip()

        # Direct IP support: if user entered an IP address in the ID field
        if ClientState.is_ip_or_host(dev_id):
            if not broker_url or "127.0.0.1" in broker_url or "localhost" in broker_url:
                target_host = dev_id if dev_id.startswith("ws://") else f"ws://{dev_id}"
                if ":" not in target_host.split("://")[1]:
                    target_host = f"{target_host}:8765"
                broker_url = target_host
                self.entry_broker.delete(0, tk.END)
                self.entry_broker.insert(0, broker_url)

        if not broker_url:
            broker_url = "ws://127.0.0.1:8765"

        if not ClientState.validate_id(dev_id):
            messagebox.showerror("Errore ID", "Inserisci un ID di 9 cifre (es. 482 910 234) oppure l'indirizzo IP del computer.")
            return

        if not pwd:
            local_pwd = self.security.get_password_display()
            clean_own_id = self.security.get_or_create_device_id().replace(" ", "")
            if dev_id.replace(" ", "") == clean_own_id or "127.0.0.1" in broker_url:
                pwd = local_pwd
                self.entry_pwd.delete(0, tk.END)
                self.entry_pwd.insert(0, pwd)
            else:
                messagebox.showwarning("Password Richiesta", "Inserisci la password di accesso remoto del computer da controllare.")
                return

        self.btn_connect.config(state="disabled", text="⏳  Connessione in corso...", bg="#0369a1")
        asyncio.run_coroutine_threadsafe(self._async_connect_task(dev_id, pwd, broker_url), self.loop)
        self.root.after(7000, self._ensure_connect_btn_reset)

    def _ensure_connect_btn_reset(self):
        if not self.client.state.is_connected:
            self.btn_connect.config(state="normal", text="🚀  Connetti al Computer", bg=THEME["accent"])

    async def _async_connect_task(self, dev_id: str, pwd: str, broker_url: str):
        ok = await self.client.connect(dev_id, pwd, broker_url=broker_url)
        self.root.after(0, lambda: self._handle_connect_result(ok))

    def _handle_connect_result(self, success: bool):
        self.btn_connect.config(state="normal", text="🚀  Connetti al Computer", bg=THEME["accent"])
        if success:
            raw_val = self.combo_id.get().strip()
            dev_id = raw_val.split("(")[0].strip() if "(" in raw_val else raw_val
            pwd = self.entry_pwd.get().strip()
            broker_url = self.entry_broker.get().strip()
            self.settings.save(dev_id, pwd, self.var_remember.get(), broker_url=broker_url)
            self._open_viewer_window()
        else:
            status_text = self.lbl_status.cget("text") if hasattr(self, "lbl_status") else ""
            if "Password errata" in status_text or "Flusso non decifrabile" in status_text or "non valida" in status_text:
                messagebox.showerror(
                    "Autenticazione Fallita",
                    "Password errata.\n\nVerifica la password configurata nel pannello Host del computer remoto (default: 123456) e riprova."
                )

    def _switch_to_host_panel(self):
        self._switch_tab("host")

    def _open_viewer_window(self):
        if self.viewer_window and tk.Toplevel.winfo_exists(self.viewer_window):
            return

        self.viewer_window = tk.Toplevel(self.root)
        self.viewer_window.title(f"AetherDesk - [{self.client.state.target_id}]")
        self._center_window(self.viewer_window, 1280, 720)
        self.viewer_window.configure(bg="#0c0d12")
        self.viewer_window.protocol("WM_DELETE_WINDOW", self._close_viewer)

        self.scale_mode = "fit"
        self.toolbar_visible = True
        self._active_mon = -1
        self._active_quality = 65

        # Floating notch to reveal toolbar when collapsed (Apple Dynamic Island style)
        self.notch_frame = tk.Frame(self.viewer_window, bg=THEME["toolbar_bg"], height=26,
                                    highlightthickness=1, highlightbackground=THEME["card_border"])
        lbl_show = tk.Label(self.notch_frame, text="▼ Controlli Sessione (F11 / Ctrl+Alt+H)", bg=THEME["toolbar_bg"], fg=THEME["cyan"],
                            font=("Segoe UI", 9, "bold"), cursor="hand2", padx=16, pady=3)
        lbl_show.pack(side=tk.TOP)
        lbl_show.bind("<Button-1>", lambda e: self._toggle_toolbar())

        # Main Modern Floating Toolbar (macOS Frosted Bar style)
        self.toolbar = tk.Frame(self.viewer_window, bg=THEME["toolbar_bg"], height=44, padx=12, pady=5)
        self.toolbar.pack(fill=tk.X, side=tk.TOP)

        # Subtle bottom border line
        self.tb_border = tk.Frame(self.viewer_window, bg=THEME["card_border"], height=1)
        self.tb_border.pack(fill=tk.X, side=tk.TOP)

        def _make_btn(parent, text, cmd, bg=THEME["card"], fg=THEME["text_muted"], h_bg=THEME["card_hover"], h_fg=THEME["text_main"],
                      font=("Segoe UI", 9), padx=8, pady=3, side=tk.LEFT):
            b = tk.Button(parent, text=text, bg=bg, fg=fg, relief="flat", activebackground=h_bg,
                          activeforeground=h_fg, font=font, cursor="hand2", padx=padx, pady=pady,
                          command=cmd, highlightthickness=0)
            b.pack(side=side, padx=2, pady=2)
            bind_hover(b, bg, h_bg, fg, h_fg)
            return b

        def _make_sep(parent, side=tk.LEFT):
            s = tk.Frame(parent, bg=THEME["card_border"], width=1, height=22)
            s.pack(side=side, padx=6, pady=3)
            return s

        # 1. Left Badge: LIVE Indicator + Target ID
        badge_frame = tk.Frame(self.toolbar, bg=THEME["card"], padx=10, pady=3,
                               highlightbackground=THEME["card_border"], highlightthickness=1)
        badge_frame.pack(side=tk.LEFT, padx=(0, 6))

        lbl_dot = tk.Label(badge_frame, text="●", bg=THEME["card"], fg=THEME["success"], font=("Segoe UI", 10, "bold"))
        lbl_dot.pack(side=tk.LEFT, padx=(0, 5))
        lbl_brand = tk.Label(badge_frame, text="LIVE", bg=THEME["card"], fg=THEME["cyan"], font=("Segoe UI", 9, "bold"))
        lbl_brand.pack(side=tk.LEFT, padx=(0, 6))
        lbl_tid = tk.Label(badge_frame, text=f"[{self.client.state.target_id}]", bg=THEME["card"],
                           fg=THEME["text_main"], font=("Consolas", 9, "bold"))
        lbl_tid.pack(side=tk.LEFT)

        _make_sep(self.toolbar)

        # 2. System Commands Capsule (Apple macOS Control Center Quick Actions)
        actions_capsule = tk.Frame(self.toolbar, bg=THEME["card"], padx=3, pady=2,
                                   highlightthickness=1, highlightbackground=THEME["card_border"])
        actions_capsule.pack(side=tk.LEFT, padx=3)

        _make_btn(actions_capsule, "⊞ Desktop", lambda: self._send_special("win_d"), padx=6)
        _make_btn(actions_capsule, "🗔 Alt+Tab", lambda: self._send_special("alt_tab"), padx=6)
        _make_btn(actions_capsule, "⚙ TaskMgr", lambda: self._send_special("task_mgr"), padx=6)
        _make_btn(actions_capsule, "🔒 Blocca", lambda: self._send_special("lock"), padx=6)
        _make_btn(actions_capsule, "⚡ Ctrl+Alt+Del", lambda: self._send_special("ctrl_alt_del"), padx=6)
        _make_btn(actions_capsule, "📁 Invia File", self._pick_and_send_file,
                  bg=THEME["card_hover"], fg=THEME["cyan"], h_bg=THEME["accent"], h_fg="#ffffff", padx=8)

        _make_sep(self.toolbar)

        # 3. Stream Controls Capsule (Monitor & Quality)
        stream_capsule = tk.Frame(self.toolbar, bg=THEME["card"], padx=4, pady=2,
                                  highlightthickness=1, highlightbackground=THEME["card_border"])
        stream_capsule.pack(side=tk.LEFT, padx=3)

        lbl_mon = tk.Label(stream_capsule, text="🖥", bg=THEME["card"], fg=THEME["text_dim"], font=("Segoe UI", 9))
        lbl_mon.pack(side=tk.LEFT, padx=(2, 2))

        self.btn_m1 = _make_btn(stream_capsule, "1", lambda: self._on_switch_mon(0), padx=5, font=("Segoe UI", 8, "bold"))
        self.btn_m2 = _make_btn(stream_capsule, "2", lambda: self._on_switch_mon(1), padx=5, font=("Segoe UI", 8, "bold"))
        self.btn_mall = _make_btn(stream_capsule, "Tutti", lambda: self._on_switch_mon(-1), padx=5, font=("Segoe UI", 8, "bold"))
        self.btn_mall.config(bg=THEME["accent"], fg="#ffffff")
        self.btn_mdual = _make_btn(stream_capsule, "🗔 2 Finestre", self._toggle_dual_window, padx=6, font=("Segoe UI", 8, "bold"))

        lbl_sep_in = tk.Label(stream_capsule, text="•", bg=THEME["card"], fg=THEME["card_border"])
        lbl_sep_in.pack(side=tk.LEFT, padx=3)

        self.btn_q_eco = _make_btn(stream_capsule, "Eco", lambda: self._on_select_quality(35), padx=5, font=("Segoe UI", 8))
        self.btn_q_med = _make_btn(stream_capsule, "Media", lambda: self._on_select_quality(65), padx=5, font=("Segoe UI", 8))
        self.btn_q_high = _make_btn(stream_capsule, "HD", lambda: self._on_select_quality(85), padx=5, font=("Segoe UI", 8))
        self.btn_q_med.config(bg=THEME["accent"], fg="#ffffff")

        # 4. View Scale Mode (Adatta / 1:1)
        self.btn_scale = _make_btn(self.toolbar, "🔍 Adatta", self._toggle_scale_mode, padx=7)

        # 5. Privacy Mode
        self.privacy_on = False
        self.btn_privacy = _make_btn(self.toolbar, "🔒 Privacy", self._toggle_privacy, padx=7)

        # 6. OTA In-Place Update (only visible if a newer version actually exists)
        self.btn_ota = _make_btn(self.toolbar, f"✓ v{CURRENT_VERSION}", self._request_ota_update,
                                 bg=THEME["card"], fg=THEME["text_muted"], h_bg=THEME["card_border"], h_fg="#ffffff", padx=6)
        has_ota = self.ota_update_available and self.ota_remote_version and is_newer_version(self.ota_remote_version, CURRENT_VERSION)
        if has_ota:
            self.btn_ota.config(text=f"⚡ Aggiorna v{self.ota_remote_version}", bg=THEME["success"], fg="#ffffff", state=tk.NORMAL)
        else:
            self.btn_ota.config(text=f"✓ v{CURRENT_VERSION}", state=tk.DISABLED)

        # Right side items
        _make_btn(self.toolbar, "✕ Disconnetti", self._close_viewer, bg=THEME["danger_bg"], fg=THEME["danger"],
                  h_bg=THEME["danger"], h_fg="#ffffff", side=tk.RIGHT, padx=10, font=("Segoe UI", 9, "bold"))

        _make_btn(self.toolbar, "▲ Nascondi", self._toggle_toolbar, side=tk.RIGHT, padx=6, font=("Segoe UI", 8))

        self.btn_fs = _make_btn(self.toolbar, "⛶ Schermo Intero", self._toggle_fullscreen, side=tk.RIGHT, padx=8)

        # Telemetry Pill (FPS + Encryption)
        tele_frame = tk.Frame(self.toolbar, bg=THEME["card"], padx=8, pady=3,
                              highlightbackground=THEME["card_border"], highlightthickness=1)
        tele_frame.pack(side=tk.RIGHT, padx=6)

        self.lbl_fps = tk.Label(tele_frame, text="FPS: 0", bg=THEME["card"], fg=THEME["cyan"], font=("Consolas", 8, "bold"))
        self.lbl_fps.pack(side=tk.LEFT, padx=(0, 6))

        lbl_sec = tk.Label(tele_frame, text="🔒 AES-256", bg=THEME["card"], fg=THEME["text_dim"], font=("Segoe UI", 8))
        lbl_sec.pack(side=tk.LEFT)

        # Video Canvas
        self.canvas = tk.Canvas(self.viewer_window, bg="#080a10", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self._render_placeholder()
        self.canvas.bind("<Configure>", lambda e: self._on_canvas_resize())

        # Input Bindings: Motion & Dragging across all mouse buttons
        self.canvas.bind("<Motion>", self._on_mouse_move)
        self.canvas.bind("<B1-Motion>", self._on_mouse_move)
        self.canvas.bind("<B2-Motion>", self._on_mouse_move)
        self.canvas.bind("<B3-Motion>", self._on_mouse_move)

        # Left Click & Dragging
        self.canvas.bind("<Button-1>", lambda e: self._on_mouse_btn(e, "left", "down"))
        self.canvas.bind("<ButtonRelease-1>", lambda e: self._on_mouse_btn(e, "left", "up"))

        # Middle Click
        self.canvas.bind("<Button-2>", lambda e: self._on_mouse_btn(e, "middle", "down"))
        self.canvas.bind("<ButtonRelease-2>", lambda e: self._on_mouse_btn(e, "middle", "up"))

        # Right Click
        self.canvas.bind("<Button-3>", lambda e: self._on_mouse_btn(e, "right", "down"))
        self.canvas.bind("<ButtonRelease-3>", lambda e: self._on_mouse_btn(e, "right", "up"))

        # Scroll Wheel
        self.canvas.bind("<MouseWheel>", self._on_mouse_wheel)

        self.viewer_window.bind("<KeyPress>", self._on_key_press)
        self.viewer_window.bind("<KeyRelease>", self._on_key_release)
        self.viewer_window.bind("<Escape>", self._on_escape_press)
        self.viewer_window.bind("<F11>", lambda e: self._toggle_fullscreen())
        self.viewer_window.bind("<Control-Alt-h>", lambda e: self._toggle_toolbar())
        self.viewer_window.bind("<Control-Alt-H>", lambda e: self._toggle_toolbar())

        # Start clipboard sync loop
        self._sync_clipboard_loop()

    def _render_placeholder(self):
        if not self.canvas or self._canvas_img_id is not None:
            return
        self.canvas.delete("loading")
        c_w = max(self.canvas.winfo_width(), 1280)
        c_h = max(self.canvas.winfo_height(), 720)
        cx, cy = c_w // 2, c_h // 2
        self.canvas.create_text(cx, cy - 35, text="AETHERDESK", fill=THEME["cyan"], font=("Segoe UI", 26, "bold"), tags="loading")
        self.canvas.create_text(cx, cy + 5, text=f"Sessione E2EE Connessa • Host: {self.client.state.target_id}", fill=THEME["text_main"], font=("Segoe UI", 11), tags="loading")
        self.canvas.create_text(cx, cy + 32, text="In attesa del primo fotogramma dal desktop remoto...", fill=THEME["text_dim"], font=("Segoe UI", 9), tags="loading")

    def _on_canvas_resize(self):
        if self._canvas_img_id is None:
            self._render_placeholder()

    def _toggle_toolbar(self):
        self.toolbar_visible = not self.toolbar_visible
        if self.toolbar_visible:
            self.notch_frame.pack_forget()
            self.toolbar.pack(fill=tk.X, side=tk.TOP, before=self.canvas)
            self.tb_border.pack(fill=tk.X, side=tk.TOP, before=self.canvas)
        else:
            self.toolbar.pack_forget()
            self.tb_border.pack_forget()
            self.notch_frame.pack(fill=tk.X, side=tk.TOP, before=self.canvas)

    def _toggle_scale_mode(self):
        self.scale_mode = "1:1" if self.scale_mode == "fit" else "fit"
        txt = "🔍 1:1 Pixel" if self.scale_mode == "1:1" else "🔍 Adatta"
        self.btn_scale.config(text=txt, bg=THEME["accent"] if self.scale_mode == "1:1" else THEME["card"],
                              fg="#ffffff" if self.scale_mode == "1:1" else THEME["text_muted"])

    def _on_switch_mon(self, mon_id: int):
        if self.dual_window_mode and mon_id != -1:
            self._close_dual_window()
        self._active_mon = mon_id
        for m, btn in [(0, self.btn_m1), (1, self.btn_m2), (-1, self.btn_mall)]:
            btn.config(bg=THEME["accent"] if m == mon_id else THEME["card"],
                       fg="#ffffff" if m == mon_id else THEME["text_muted"])
        self._switch_monitor(mon_id)

    def _on_monitors_received(self, mons: list):
        """Handle incoming list of active physical display monitors from host."""
        self.remote_monitors = [m for m in mons if m.get("id", -1) >= 0]
        def update_ui():
            if hasattr(self, "btn_mdual") and self.btn_mdual:
                if len(self.remote_monitors) >= 2:
                    self.btn_mdual.config(text=f"🗔 2 Finestre ({len(self.remote_monitors)} Mon)", state=tk.NORMAL)
                    self._on_status_update(f"Rilevati {len(self.remote_monitors)} monitor remoti! Puoi sdoppiarli in 2 finestre indipendenti.")
        self.root.after(0, update_ui)

    def _toggle_dual_window(self):
        if not self.dual_window_mode:
            self._open_dual_window()
        else:
            self._close_dual_window()

    def _open_dual_window(self):
        if self.dual_window_mode and self.viewer_window_m2:
            return
        self.dual_window_mode = True
        if hasattr(self, "btn_mdual") and self.btn_mdual:
            self.btn_mdual.config(bg=THEME["success"], fg="#ffffff")

        # Stream all monitors combined from host
        self._on_switch_mon(-1)

        # Create TopLevel window for Screen 2
        target_id = getattr(self.client.state, "target_id", "Remoto")
        self.viewer_window_m2 = tk.Toplevel(self.root)
        self.viewer_window_m2.title(f"AetherDesk - Schermo 2 [{target_id}]")
        self.viewer_window_m2.geometry("1280x720")
        self.viewer_window_m2.configure(bg=THEME["bg"])

        # Secondary Window Toolbar
        tb_m2 = tk.Frame(self.viewer_window_m2, bg=THEME["toolbar_bg"], height=36, padx=8, pady=4)
        tb_m2.pack(fill=tk.X, side=tk.TOP)

        lbl_b = tk.Label(tb_m2, text="🖥 SCHERMO 2", bg=THEME["card"], fg=THEME["cyan"],
                         font=("Segoe UI", 9, "bold"), padx=8, pady=2)
        lbl_b.pack(side=tk.LEFT, padx=(0, 6))

        btn_fs_m2 = tk.Button(tb_m2, text="⛶ Schermo Intero (F11)", bg=THEME["card"], fg=THEME["text_muted"],
                              activebackground=THEME["card_hover"], activeforeground="#ffffff", font=("Segoe UI", 8, "bold"),
                              relief="flat", cursor="hand2", padx=8, pady=2,
                              command=self._toggle_fullscreen_m2)
        btn_fs_m2.pack(side=tk.LEFT, padx=4)

        btn_close_m2 = tk.Button(tb_m2, text="✕ Chiudi Schermo 2", bg=THEME["danger_bg"], fg=THEME["danger"],
                                 activebackground=THEME["danger"], activeforeground="#ffffff",
                                 font=("Segoe UI", 8, "bold"), relief="flat", cursor="hand2", padx=8, pady=2,
                                 command=self._close_dual_window)
        btn_close_m2.pack(side=tk.RIGHT, padx=4)

        # Canvas for Screen 2
        self.canvas_m2 = tk.Canvas(self.viewer_window_m2, bg="#080a10", highlightthickness=0)
        self.canvas_m2.pack(fill=tk.BOTH, expand=True)

        # Mouse & keyboard bindings for Screen 2
        self.canvas_m2.bind("<Motion>", lambda e: self._on_mouse_move(e, mon_idx=1))
        self.canvas_m2.bind("<B1-Motion>", lambda e: self._on_mouse_move(e, mon_idx=1))
        self.canvas_m2.bind("<B2-Motion>", lambda e: self._on_mouse_move(e, mon_idx=1))
        self.canvas_m2.bind("<B3-Motion>", lambda e: self._on_mouse_move(e, mon_idx=1))

        self.canvas_m2.bind("<Button-1>", lambda e: self._on_mouse_btn(e, "left", "down", mon_idx=1))
        self.canvas_m2.bind("<ButtonRelease-1>", lambda e: self._on_mouse_btn(e, "left", "up", mon_idx=1))
        self.canvas_m2.bind("<Button-2>", lambda e: self._on_mouse_btn(e, "middle", "down", mon_idx=1))
        self.canvas_m2.bind("<ButtonRelease-2>", lambda e: self._on_mouse_btn(e, "middle", "up", mon_idx=1))
        self.canvas_m2.bind("<Button-3>", lambda e: self._on_mouse_btn(e, "right", "down", mon_idx=1))
        self.canvas_m2.bind("<ButtonRelease-3>", lambda e: self._on_mouse_btn(e, "right", "up", mon_idx=1))
        self.canvas_m2.bind("<MouseWheel>", lambda e: self._on_mouse_wheel(e, mon_idx=1))

        self.viewer_window_m2.bind("<KeyPress>", self._on_key_press)
        self.viewer_window_m2.bind("<KeyRelease>", self._on_key_release)
        self.viewer_window_m2.bind("<Escape>", lambda e: self._on_escape_press_m2())
        self.viewer_window_m2.bind("<F11>", lambda e: self._toggle_fullscreen_m2())
        self.viewer_window_m2.protocol("WM_DELETE_WINDOW", self._close_dual_window)

    def _toggle_fullscreen_m2(self):
        if not self.viewer_window_m2:
            return
        is_fs = self.viewer_window_m2.attributes("-fullscreen")
        self.viewer_window_m2.attributes("-fullscreen", not is_fs)

    def _on_escape_press_m2(self):
        if self.viewer_window_m2 and self.viewer_window_m2.attributes("-fullscreen"):
            self.viewer_window_m2.attributes("-fullscreen", False)
            return "break"

    def _close_dual_window(self):
        self.dual_window_mode = False
        if hasattr(self, "btn_mdual") and self.btn_mdual:
            self.btn_mdual.config(bg="#1b2032", fg=THEME["text_muted"])
        if self.viewer_window_m2:
            try:
                self.viewer_window_m2.destroy()
            except Exception:
                pass
            self.viewer_window_m2 = None
            self.canvas_m2 = None
            self._canvas_img_id_m2 = None
            self.photo_img_m2 = None

    def _on_select_quality(self, quality: int):
        self._active_quality = quality
        for q, btn in [(35, self.btn_q_eco), (65, self.btn_q_med), (85, self.btn_q_high)]:
            btn.config(bg=THEME["accent"] if q == quality else "#1b2032",
                       fg="#ffffff" if q == quality else THEME["text_muted"])
        self._set_quality(quality)

    def _toggle_fullscreen(self):
        is_fs = self.viewer_window.attributes("-fullscreen")
        self.viewer_window.attributes("-fullscreen", not is_fs)
        if hasattr(self, "btn_fs") and self.btn_fs:
            self.btn_fs.config(text="⤓ Finestra" if not is_fs else "⛶ Schermo Intero")

    def _on_escape_press(self, event):
        """Escape exits fullscreen mode if active, otherwise relays keycode to remote host."""
        if self.viewer_window and self.viewer_window.attributes("-fullscreen"):
            self._toggle_fullscreen()
            return "break"
        self._on_key_press(event)

    def _toggle_privacy(self):
        self.privacy_on = not self.privacy_on
        txt = "🔒 Privacy: ON" if self.privacy_on else "🔒 Privacy"
        bg = THEME["success"] if self.privacy_on else "#1b2032"
        fg = "#ffffff" if self.privacy_on else THEME["text_muted"]
        self.btn_privacy.config(text=txt, bg=bg, fg=fg)
        asyncio.run_coroutine_threadsafe(self.client.toggle_privacy_mode(self.privacy_on), self.loop)

    def _switch_monitor(self, mon_id: int):
        asyncio.run_coroutine_threadsafe(self.client.switch_monitor(mon_id), self.loop)

    def _pick_and_send_file(self):
        file_path = filedialog.askopenfilename(title="Seleziona file da inviare al PC remoto")
        if not file_path:
            return
        threading.Thread(target=self._send_file_worker, args=(file_path,), daemon=True).start()

    def _send_file_worker(self, file_path: str):
        fn = os.path.basename(file_path)
        self._on_status_update(f"Invio file '{fn}'...")

        def on_progress(sent, total):
            pct = int((sent / total) * 100) if total > 0 else 100
            mb_sent = sent / (1024 * 1024)
            mb_total = total / (1024 * 1024)
            self._on_status_update(f"Invio '{fn}': {pct}% ({mb_sent:.1f}/{mb_total:.1f} MB)")

        future = asyncio.run_coroutine_threadsafe(
            self.client.send_file(file_path, progress_callback=on_progress),
            self.loop
        )
        try:
            future.result()
            self._on_status_update(f"File '{fn}' inviato con successo!")
            self.root.after(0, lambda: messagebox.showinfo("File Inviato", f"Il file '{fn}' e' stato trasferito sul PC remoto nella cartella Download!"))
        except Exception as e:
            self._on_status_update(f"Errore invio file: {e}")

    def _safe_after(self, ms: int, func):
        try:
            if hasattr(self, "root") and self.root and self.root.winfo_exists():
                self.root.after(ms, func)
        except Exception:
            pass

    def _check_git_updates_background(self, notify_if_none: bool = False, prompt_on_found: bool = False):
        try:
            def set_checking():
                if self.btn_header_update:
                    self.btn_header_update.config(text="🔄 Controllo...", state=tk.DISABLED)
                if self.btn_git_update:
                    self.btn_git_update.config(text="🔄 Controllo...", state=tk.DISABLED)
            self._safe_after(0, set_checking)

            is_newer, info = check_github_update()

            def update_ui():
                if self.btn_header_update:
                    self.btn_header_update.config(state=tk.NORMAL)
                if self.btn_git_update:
                    self.btn_git_update.config(state=tk.NORMAL)

                if is_newer and info.get("download_url"):
                    self.git_update_info = info
                    tag = info.get("tag", "")
                    if self.btn_header_update:
                        self.btn_header_update.config(
                            text=f"⚡ Aggiorna a v{tag}",
                            bg="#059669",
                            fg="#ffffff"
                        )
                    if self.btn_git_update:
                        self.btn_git_update.config(
                            text=f"⚡ Aggiornamento Disponibile (v{tag})",
                            bg="#059669",
                            fg="#ffffff"
                        )
                    self._on_status_update(f"Aggiornamento disponibile: v{tag}!")

                    if prompt_on_found:
                        size_mb = info.get("size", 0) / (1024 * 1024)
                        confirm = messagebox.askyesno(
                            "Aggiornamento Disponibile",
                            f"E' disponibile una nuova versione di AetherDesk!\n\n"
                            f"• Versione Installata: v{get_app_version()}\n"
                            f"• Nuova Versione: v{tag}\n"
                            f"• Dimensione: {size_mb:.1f} MB\n\n"
                            f"Vuoi scaricare l'aggiornamento e riavviare adesso automaticamente?"
                        )
                        if confirm:
                            self._start_git_download(
                                info["download_url"],
                                tag,
                                expected_sha256=info.get("expected_sha256"),
                                signed_manifest=info.get("manifest")
                            )
                else:
                    tag = info.get("tag", get_app_version())
                    err = info.get("error")
                    if err:
                        if self.btn_header_update:
                            self.btn_header_update.config(text="🔄 Riprova", bg="#161b2b", fg="#f87171")
                        if self.btn_git_update:
                            self.btn_git_update.config(text="🔄 Riprova Aggiornamento", bg="#141824", fg="#f87171")
                        if notify_if_none:
                            messagebox.showwarning(
                                "Controllo Aggiornamenti",
                                f"Impossibile verificare gli aggiornamenti:\n{err}\n\nVerifica la connessione internet."
                            )
                    else:
                        if self.btn_header_update:
                            self.btn_header_update.config(
                                text=f"✓ v{get_app_version()} (Aggiornato)",
                                bg="#161b2b",
                                fg="#34d399"
                            )
                        if self.btn_git_update:
                            self.btn_git_update.config(
                                text=f"✓ v{get_app_version()} (Aggiornato)",
                                bg="#141824",
                                fg="#34d399"
                            )
                        if notify_if_none:
                            messagebox.showinfo(
                                "Aggiornamenti AetherDesk",
                                f"AetherDesk e' gia' aggiornato all'ultima versione disponibile (v{get_app_version()})."
                            )
            self._safe_after(0, update_ui)
        except Exception as e:
            def err_ui():
                if self.btn_header_update:
                    self.btn_header_update.config(text="🔄 Riprova", state=tk.NORMAL, fg="#f87171")
                if self.btn_git_update:
                    self.btn_git_update.config(text="🔄 Riprova Aggiornamento", state=tk.NORMAL, fg="#f87171")
                if notify_if_none:
                    messagebox.showwarning("Controllo Aggiornamenti", f"Errore durante la verifica:\n{e}")
            self._safe_after(0, err_ui)

    def _on_git_update_click(self):
        if getattr(self, "ota_downloading", False):
            return
        if self.git_update_info and self.git_update_info.get("download_url"):
            info = self.git_update_info
            size_mb = info.get("size", 0) / (1024 * 1024)
            confirm = messagebox.askyesno(
                "Aggiornamento Disponibile",
                f"Trovata nuova versione su GitHub:\n\n"
                f"• Versione Installata: v{get_app_version()}\n"
                f"• Nuova Versione: v{info['tag']}\n"
                f"• Dimensione: {size_mb:.1f} MB\n\n"
                f"Vuoi scaricare l'aggiornamento da GitHub ed eseguire il riavvio in-place automatico?"
            )
            if not confirm:
                return
            self._start_git_download(
                info["download_url"],
                info["tag"],
                expected_sha256=info.get("expected_sha256"),
                signed_manifest=info.get("manifest")
            )
        else:
            threading.Thread(
                target=lambda: self._check_git_updates_background(notify_if_none=True, prompt_on_found=True),
                daemon=True
            ).start()

    def _start_git_download(
        self,
        download_url: str,
        target_version: str,
        expected_sha256: Optional[str] = None,
        signed_manifest: Optional[dict] = None
    ):
        self.ota_downloading = True
        if self.btn_header_update:
            self.btn_header_update.config(text="⏳ Scaricamento...", state=tk.DISABLED)
        if self.btn_git_update:
            self.btn_git_update.config(text="⏳ Scaricamento...", state=tk.DISABLED)

        def worker():
            def on_progress(rec, tot):
                pct = int((rec / tot) * 100) if tot > 0 else 0
                mb_rec = rec / (1024 * 1024)
                mb_tot = tot / (1024 * 1024)
                def up():
                    txt = f"⬇ {pct}% ({mb_rec:.1f}/{mb_tot:.1f}MB)"
                    if self.btn_header_update:
                        self.btn_header_update.config(text=txt)
                    if self.btn_git_update:
                        self.btn_git_update.config(text=f"⬇ Download: {pct}%")
                    self._on_status_update(f"Download da Git: {pct}%...")
                self._safe_after(0, up)

            ok, temp_path_or_err = download_github_binary(
                download_url,
                progress_callback=on_progress,
                expected_sha256=expected_sha256,
                signed_manifest=signed_manifest
            )
            if not ok:
                self.ota_downloading = False
                def err():
                    messagebox.showerror("Errore Download Git", temp_path_or_err)
                    if self.btn_header_update:
                        self.btn_header_update.config(text="🔄 Riprova", state=tk.NORMAL, bg="#161b2b", fg="#f87171")
                    if self.btn_git_update:
                        self.btn_git_update.config(text="🔄 Riprova Git", state=tk.NORMAL, bg="#141824", fg="#f87171")
                self._safe_after(0, err)
                return

            applied, app_msg = apply_in_place_update(
                temp_path_or_err,
                target_version=target_version,
                expected_sha256=expected_sha256,
                signed_manifest=signed_manifest
            )
            self.ota_downloading = False
            def finish():
                if applied:
                    if self.viewer_window:
                        try:
                            self.viewer_window.destroy()
                        except Exception:
                            pass
                    try:
                        self.root.destroy()
                    except Exception:
                        pass
                    import os
                    os._exit(0)
                else:
                    try:
                        p_temp = Path(temp_path_or_err)
                        if p_temp.is_file():
                            p_temp.unlink()
                    except Exception:
                        pass
                    messagebox.showerror("Errore Aggiornamento", app_msg)
                    if self.btn_header_update:
                        self.btn_header_update.config(text="🔄 Riprova", state=tk.NORMAL)
                    if self.btn_git_update:
                        self.btn_git_update.config(text="🔄 Riprova Git", state=tk.NORMAL)
            self._safe_after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def _request_ota_update(self):
        if self.ota_downloading:
            return
        msg = (
            f"E' disponibile un aggiornamento del client (versione {self.ota_remote_version or 'nuova'}).\n\n"
            f"Vuoi scaricare l'aggiornamento direttamente dall'host remoto e riavviare AetherDesk?"
        )
        confirm = messagebox.askyesno("Aggiornamento Remoto OTA", msg)
        if not confirm:
            return
        self.ota_downloading = True
        if self.btn_ota:
            self.btn_ota.config(text="⏳ Richiesta OTA...", state=tk.DISABLED)
        asyncio.run_coroutine_threadsafe(self.client.request_ota_update(), self.loop)

    def _on_ota_info(self, payload: dict):
        remote_ver = payload.get("version", "")
        # Only mark available if remote version is strictly newer than current installed version
        available = payload.get("available", False) and bool(remote_ver and is_newer_version(remote_ver, CURRENT_VERSION))
        self.ota_remote_version = remote_ver
        self.ota_update_available = available
        def update_ui():
            if available:
                if self.btn_ota:
                    self.btn_ota.config(text=f"⚡ Aggiorna v{remote_ver}", bg="#059669", fg="#ffffff", state=tk.NORMAL)
                self._on_status_update(f"Aggiornamento OTA disponibile: v{remote_ver}")
            else:
                if self.btn_ota:
                    self.btn_ota.config(text=f"✓ v{CURRENT_VERSION}", bg="#1e293b", fg="#94a3b8", state=tk.DISABLED)
        self.root.after(0, update_ui)

    def _on_ota_progress(self, received: int, total: int):
        pct = int((received / total) * 100) if total > 0 else 0
        mb_rec = received / (1024 * 1024)
        mb_tot = total / (1024 * 1024)
        def update_ui():
            if self.btn_ota:
                self.btn_ota.config(text=f"⬇ OTA: {pct}% ({mb_rec:.1f}/{mb_tot:.1f}MB)")
            self._on_status_update(f"Download aggiornamento OTA: {pct}%...")
        self.root.after(0, update_ui)

    def _on_ota_complete(self, success: bool, msg: str):
        self.ota_downloading = False
        def notify():
            if success:
                if self.viewer_window:
                    try:
                        self.viewer_window.destroy()
                    except Exception:
                        pass
                try:
                    self.root.destroy()
                except Exception:
                    pass
                import os
                os._exit(0)
            else:
                messagebox.showerror("Errore Aggiornamento OTA", f"Impossibile completare l'aggiornamento:\n{msg}")
                if self.btn_ota:
                    self.btn_ota.config(text="🔄 Aggiorna OTA", state=tk.NORMAL, bg="#064e3b", fg="#6ee7b7")
        self._safe_after(0, notify)

    def _sync_clipboard_loop(self):
        if self.viewer_window and self.client.state.is_connected:
            try:
                if self.client.clipboard.has_changed():
                    txt = self.client.clipboard.get_text()
                    if txt:
                        asyncio.run_coroutine_threadsafe(self.client.send_clipboard(txt), self.loop)
            except Exception:
                pass
        self.root.after(1500, self._sync_clipboard_loop)

    def _on_remote_disconnect(self):
        self._safe_after(0, self._close_viewer)

    def _close_viewer(self):
        self._close_dual_window()
        asyncio.run_coroutine_threadsafe(self.client.disconnect(), self.loop)
        self._canvas_img_id = None
        if self.viewer_window:
            self.viewer_window.destroy()
            self.viewer_window = None

    def _set_quality(self, quality: int):
        asyncio.run_coroutine_threadsafe(self.client.set_quality(quality), self.loop)

    def _on_frame_received(self, jpeg_bytes: bytes, dims: tuple[int, int], cur: tuple[int, int], *args):
        if not self.canvas:
            return
        telemetry = args[0] if args else {}
        self._pending_frame = (jpeg_bytes, dims, cur, telemetry)
        if not self._render_scheduled:
            self._render_scheduled = True
            self.root.after(0, self._render_latest_frame)

    def _on_verify_host_fingerprint(self, device_id: str, fingerprint: str) -> bool:
        """Prompt user with explicit full 32-byte SHA-256 fingerprint on first pairing."""
        if not self.root or not self.root.winfo_exists():
            return False  # Never approve first pairing without an active UI window

        result = [False]
        ev = threading.Event()

        def prompt():
            try:
                # Format 32-byte fingerprint into two clean 16-byte lines for comfortable reading
                parts = fingerprint.split(":")
                fp_display = fingerprint
                if len(parts) == 32:
                    fp_display = ":".join(parts[:16]) + "\n" + ":".join(parts[16:])

                msg = (
                    f"Primo collegamento al dispositivo: {device_id}\n\n"
                    f"Impronta di sicurezza SHA-256 completa (32 byte / 64 caratteri esadecimali):\n\n"
                    f"{fp_display}\n\n"
                    f"Confermi che l'impronta corrisponde al computer a cui vuoi collegarti?\n"
                    f"(Verrà memorizzata in modo permanente per impedire attacchi man-in-the-middle)"
                )
                res = messagebox.askyesno("Verifica Impronta Digitale Host (SHA-256)", msg, parent=self.root)
                result[0] = bool(res)
            except Exception:
                result[0] = False
            finally:
                ev.set()

        self._safe_after(0, prompt)
        signaled = ev.wait(timeout=30.0)
        if not signaled:
            return False  # Timeout: abort first pairing without pinning
        return result[0]

    def _render_latest_frame(self):
        self._render_scheduled = False
        if not self.canvas or not getattr(self, "_pending_frame", None):
            return

        t_render_start = time.perf_counter_ns()
        pending = self._pending_frame
        frame_bytes, dims, cur = pending[0], pending[1], pending[2]
        telemetry = pending[3] if len(pending) > 3 else {}

        # Frame rate & latency calculation
        self._frame_count += 1
        now = time.time()
        t_send = telemetry.get("t_send") if isinstance(telemetry, dict) else None
        if t_send is not None and t_send > 0:
            e2e_ms = round((now - t_send) * 1000, 1)
            if 0 <= e2e_ms < 5000:
                self._latencies.append(e2e_ms)
                if len(self._latencies) > 30:
                    self._latencies.pop(0)

        elapsed = now - self._last_fps_time
        if elapsed >= 1.0:
            self._current_fps = int(self._frame_count / elapsed)
            self._frame_count = 0
            self._last_fps_time = now
            if self.lbl_fps and self.viewer_window and tk.Toplevel.winfo_exists(self.viewer_window):
                fps_val = self._current_fps
                avg_lat = round(sum(self._latencies) / len(self._latencies), 1) if self._latencies else 0.0
                avg_rnd = round(sum(self._render_latencies) / len(self._render_latencies), 1) if self._render_latencies else 0.0
                lat_str = f" | {avg_lat}ms" if avg_lat > 0 else ""
                rnd_str = f" (gui {avg_rnd}ms)" if avg_rnd > 0 else ""
                self.lbl_fps.config(text=f"FPS: {fps_val}{lat_str}{rnd_str}")

        try:
            if frame_bytes.startswith(b"\xff\xd8"):
                raw_img = Image.open(io.BytesIO(frame_bytes))
            elif dims[0] > 0 and dims[1] > 0 and len(frame_bytes) >= dims[0] * dims[1] * 4:
                raw_img = Image.frombytes("RGBA", dims, frame_bytes[:dims[0] * dims[1] * 4], "raw", "BGRA")
            else:
                try:
                    raw_img = Image.open(io.BytesIO(frame_bytes))
                except Exception:
                    raw_img = Image.frombytes("RGBA", dims, frame_bytes, "raw", "BGRA")
            full_w, full_h = raw_img.size
            self._last_full_dims = (full_w, full_h)

            if self.dual_window_mode and self.viewer_window_m2 and tk.Toplevel.winfo_exists(self.viewer_window_m2):
                if len(self.remote_monitors) >= 2:
                    m1 = self.remote_monitors[0]
                    m2 = self.remote_monitors[1]
                    r1 = m1.get("rect", (0, 0, full_w // 2, full_h))
                    r2 = m2.get("rect", (full_w // 2, 0, full_w, full_h))
                    vx = min(r1[0], r2[0])
                    vy = min(r1[1], r2[1])
                    crop1 = (max(0, r1[0] - vx), max(0, r1[1] - vy), min(full_w, r1[2] - vx), min(full_h, r1[3] - vy))
                    crop2 = (max(0, r2[0] - vx), max(0, r2[1] - vy), min(full_w, r2[2] - vx), min(full_h, r2[3] - vy))
                else:
                    half_w = full_w // 2
                    crop1 = (0, 0, half_w, full_h)
                    crop2 = (half_w, 0, full_w, full_h)

                self._crop_m1 = crop1
                self._crop_m2 = crop2

                # 1. Render Screen 1 on Window 1 Canvas
                c1_w = self.canvas.winfo_width()
                c1_h = self.canvas.winfo_height()
                if c1_w <= 10 or c1_h <= 10:
                    c1_w = max(self.viewer_window.winfo_width() if self.viewer_window else 1280, 1280)
                    c1_h = max((self.viewer_window.winfo_height() - 44) if self.viewer_window else 720, 720)

                img1 = raw_img.crop(crop1)
                if getattr(self, "scale_mode", "fit") == "fit":
                    img1.thumbnail((c1_w, c1_h), Image.Resampling.BILINEAR)
                self.current_img_dims = img1.size
                photo1 = ImageTk.PhotoImage(img1)
                self.photo_img = photo1
                pos1_x = (c1_w - img1.width) // 2
                pos1_y = (c1_h - img1.height) // 2
                if self._canvas_img_id is None:
                    self.canvas.delete("loading")
                    self._canvas_img_id = self.canvas.create_image(pos1_x, pos1_y, anchor="nw", image=photo1)
                else:
                    self.canvas.coords(self._canvas_img_id, pos1_x, pos1_y)
                    self.canvas.itemconfig(self._canvas_img_id, image=photo1)

                # 2. Render Screen 2 on Window 2 Canvas
                if self.canvas_m2 and self.viewer_window_m2 and tk.Toplevel.winfo_exists(self.viewer_window_m2):
                    c2_w = self.canvas_m2.winfo_width()
                    c2_h = self.canvas_m2.winfo_height()
                    if c2_w <= 10 or c2_h <= 10:
                        c2_w = max(self.viewer_window_m2.winfo_width() if self.viewer_window_m2 else 1280, 1280)
                        c2_h = max((self.viewer_window_m2.winfo_height() - 40) if self.viewer_window_m2 else 720, 720)

                    img2 = raw_img.crop(crop2)
                    if getattr(self, "scale_mode", "fit") == "fit":
                        img2.thumbnail((c2_w, c2_h), Image.Resampling.BILINEAR)
                    self.current_img_dims_m2 = img2.size
                    photo2 = ImageTk.PhotoImage(img2)
                    self.photo_img_m2 = photo2
                    pos2_x = (c2_w - img2.width) // 2
                    pos2_y = (c2_h - img2.height) // 2
                    if self._canvas_img_id_m2 is None:
                        self.canvas_m2.delete("loading")
                        self._canvas_img_id_m2 = self.canvas_m2.create_image(pos2_x, pos2_y, anchor="nw", image=photo2)
                    else:
                        self.canvas_m2.coords(self._canvas_img_id_m2, pos2_x, pos2_y)
                        self.canvas_m2.itemconfig(self._canvas_img_id_m2, image=photo2)
            else:
                c_w = self.canvas.winfo_width()
                c_h = self.canvas.winfo_height()
                if c_w <= 10 or c_h <= 10:
                    c_w = max(self.viewer_window.winfo_width() if self.viewer_window else 1280, 1280)
                    c_h = max((self.viewer_window.winfo_height() - 44) if self.viewer_window else 720, 720)

                img = raw_img
                if getattr(self, "scale_mode", "fit") == "fit":
                    img.thumbnail((c_w, c_h), Image.Resampling.BILINEAR)
                self.current_img_dims = img.size
                photo = ImageTk.PhotoImage(img)
                self.photo_img = photo
                pos_x = (c_w - img.width) // 2
                pos_y = (c_h - img.height) // 2
                if self._canvas_img_id is None:
                    self.canvas.delete("loading")
                    self._canvas_img_id = self.canvas.create_image(pos_x, pos_y, anchor="nw", image=photo)
                else:
                    self.canvas.coords(self._canvas_img_id, pos_x, pos_y)
                    self.canvas.itemconfig(self._canvas_img_id, image=photo)

            t_render_ms = round((time.perf_counter_ns() - t_render_start) / 1e6, 2)
            self._render_latencies.append(t_render_ms)
            if len(self._render_latencies) > 30:
                self._render_latencies.pop(0)
        except Exception as e:
            pass

    def _calc_norm_coords(self, event, mon_idx: int = 0) -> tuple[float, float]:
        if not self.canvas:
            return (0.0, 0.0)

        # In dual window mode:
        if self.dual_window_mode and self.viewer_window_m2:
            target_canvas = self.canvas_m2 if mon_idx == 1 else self.canvas
            target_dims = self.current_img_dims_m2 if mon_idx == 1 else self.current_img_dims
            if not target_canvas or target_dims == (0, 0):
                return (0.0, 0.0)

            c_w = target_canvas.winfo_width()
            c_h = target_canvas.winfo_height()
            img_w, img_h = target_dims
            offset_x = (c_w - img_w) / 2.0
            offset_y = (c_h - img_h) / 2.0

            rel_x = event.x - offset_x
            rel_y = event.y - offset_y
            local_nx = max(0.0, min(1.0, rel_x / img_w)) if img_w > 0 else 0.0
            local_ny = max(0.0, min(1.0, rel_y / img_h)) if img_h > 0 else 0.0

            full_w, full_h = getattr(self, "_last_full_dims", (1920, 1080))
            crop_box = getattr(self, "_crop_m2" if mon_idx == 1 else "_crop_m1", (0, 0, full_w, full_h))
            bx1, by1, bx2, by2 = crop_box
            mon_w = max(1, bx2 - bx1)
            mon_h = max(1, by2 - by1)

            virt_x = bx1 + local_nx * mon_w
            virt_y = by1 + local_ny * mon_h
            return (max(0.0, min(1.0, virt_x / full_w)), max(0.0, min(1.0, virt_y / full_h)))

        # Standard single window mode
        if self.current_img_dims == (0, 0):
            return (0.0, 0.0)
        c_w = self.canvas.winfo_width()
        c_h = self.canvas.winfo_height()
        img_w, img_h = self.current_img_dims
        offset_x = (c_w - img_w) / 2.0
        offset_y = (c_h - img_h) / 2.0

        rel_x = event.x - offset_x
        rel_y = event.y - offset_y
        norm_x = max(0.0, min(1.0, rel_x / img_w)) if img_w > 0 else 0.0
        norm_y = max(0.0, min(1.0, rel_y / img_h)) if img_h > 0 else 0.0
        return (norm_x, norm_y)

    def _on_mouse_move(self, event, mon_idx: int = 0):
        nx, ny = self._calc_norm_coords(event, mon_idx=mon_idx)
        now = time.time()
        # Coalesce moves at 125 Hz; a 20 ms interval made the host cursor lag
        # visibly behind the local pointer.
        if now - getattr(self, "_last_mouse_send_time", 0.0) < 0.008:
            self._pending_mouse_coords = (nx, ny)
            if not getattr(self, "_mouse_timer_scheduled", False):
                self._mouse_timer_scheduled = True
                self._safe_after(8, self._flush_pending_mouse)
            return

        self._last_mouse_send_time = now
        self._pending_mouse_coords = None
        asyncio.run_coroutine_threadsafe(
            self.client.send_input({"type": "mouse_move", "x": nx, "y": ny}),
            self.loop
        )

    def _flush_pending_mouse(self):
        self._mouse_timer_scheduled = False
        coords = getattr(self, "_pending_mouse_coords", None)
        if coords and self.client and self.client.state.is_connected:
            self._last_mouse_send_time = time.time()
            self._pending_mouse_coords = None
            asyncio.run_coroutine_threadsafe(
                self.client.send_input({"type": "mouse_move", "x": coords[0], "y": coords[1]}),
                self.loop
            )

    def _on_mouse_btn(self, event, button: str, action: str, mon_idx: int = 0):
        if mon_idx == 1 and self.viewer_window_m2:
            try:
                self.viewer_window_m2.focus_force()
            except Exception:
                pass
        elif self.viewer_window:
            try:
                self.viewer_window.focus_force()
            except Exception:
                pass
        self._pending_mouse_coords = None
        nx, ny = self._calc_norm_coords(event, mon_idx=mon_idx)
        cmd = {
            "type": f"mouse_{action}",
            "button": button,
            "x": nx,
            "y": ny
        }
        asyncio.run_coroutine_threadsafe(self.client.send_input(cmd), self.loop)

    def _on_double_click(self, event, button: str):
        pass

    def _on_mouse_wheel(self, event, mon_idx: int = 0):
        nx, ny = self._calc_norm_coords(event, mon_idx=mon_idx)
        delta = event.delta
        asyncio.run_coroutine_threadsafe(
            self.client.send_input({"type": "mouse_wheel", "delta": delta, "x": nx, "y": ny}),
            self.loop
        )

    def _on_key_press(self, event):
        vk = event.keycode
        payload = {"type": "key_down", "vk": vk}
        if event.char and len(event.char) == 1 and ord(event.char) >= 32:
            payload["char"] = event.char
        asyncio.run_coroutine_threadsafe(
            self.client.send_input(payload),
            self.loop
        )

    def _on_key_release(self, event):
        vk = event.keycode
        asyncio.run_coroutine_threadsafe(
            self.client.send_input({"type": "key_up", "vk": vk}),
            self.loop
        )

    def _send_special(self, cmd: str):
        asyncio.run_coroutine_threadsafe(
            self.client.send_input({"type": "special", "command": cmd}),
            self.loop
        )

    def run(self):
        self.root.mainloop()

def build_parser():
    import argparse
    from src.ota import get_app_version
    parser = argparse.ArgumentParser(
        description="AetherDesk Client - Ultra-Lightweight E2EE Remote Desktop Viewer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  AetherDesk.exe
  AetherDesk.exe --connect 315747031
  AetherDesk.exe --connect 315747031 --password 051002 --broker ws://192.168.1.100:8765
  AetherDesk.exe --host
        """
    )
    parser.add_argument("-c", "--connect", dest="target_id", default=None, help="Target computer ID to connect to (e.g., 315747031)")
    parser.add_argument("-p", "--password", default=None, help="Password for remote authentication")
    parser.add_argument("-b", "--broker", default=None, help="Signaling broker WebSocket URL")
    parser.add_argument("--host", action="store_true", help="Launch directly into AetherDesk Host mode")
    parser.add_argument("--service", action="store_true", help="Launch in background headless host service mode (Windows Service / Session 0)")
    parser.add_argument("-v", "--version", action="version", version=f"AetherDesk Client v{get_app_version()}")
    return parser

if __name__ == "__main__":
    if sys.platform == "win32" and len(sys.argv) > 1:
        try:
            ctypes.windll.kernel32.AttachConsole(-1)
        except Exception:
            pass

    parser = build_parser()
    args = parser.parse_args()

    if args.service:
        from src.host_service import WindowsServiceManager
        svc = WindowsServiceManager()
        svc.run()
        sys.exit(0)

    start_tab = "host" if args.host else "client"
    gui = ClientViewerGUI(broker_url=args.broker if args.broker else "ws://127.0.0.1:8765", start_tab=start_tab)
    if args.target_id:
        gui.combo_id.set(args.target_id)
        if args.password:
            gui.entry_pwd.delete(0, tk.END)
            gui.entry_pwd.insert(0, args.password)
        if args.broker:
            gui.entry_broker.delete(0, tk.END)
            gui.entry_broker.insert(0, args.broker)
        if args.password:
            gui.root.after(200, gui._start_connect)
    gui.run()
