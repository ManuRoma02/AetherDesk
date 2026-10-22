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

if sys.platform == "win32":
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
    get_app_version,
    is_newer_version,
    validate_executable_file,
    apply_in_place_update,
    confirm_boot,
    check_boot_state,
    OTAUpdateState,
    check_github_update,
    download_github_binary
)

# ==============================================================================
# DESIGN SYSTEM TOKENS - Apple Dark & High-End Studio
# ==============================================================================
THEME = {
    "bg": "#090b11",              # Deep space obsidian
    "card": "#131622",            # Elevated Bento surface
    "card_border": "#212638",     # Refined 1px card outline
    "card_hover": "#1a1f30",      # Hover on card items
    "input_bg": "#0e1018",        # Inset field background
    "input_border": "#252b3e",    # Subtle input border
    "input_focus": "#38bdf8",     # Electric cyan focus ring
    "accent": "#0284c7",          # Apple System Blue
    "accent_hover": "#0ea5e9",    # Vibrant blue hover
    "cyan": "#38bdf8",            # Brand cyan
    "text_main": "#f8fafc",       # Primary crisp text
    "text_muted": "#94a3b8",      # Secondary clean text
    "text_dim": "#64748b",        # Caption / hint text
    "success": "#10b981",         # Emerald success / connected
    "success_bg": "#052e20",      # Emerald badge background
    "warning": "#f59e0b",         # Amber
    "danger": "#ef4444",          # Crimson disconnect
    "tab_active_bg": "#23293e",
    "tab_active_fg": "#f8fafc",
    "tab_inactive_bg": "#121520",
    "tab_inactive_fg": "#94a3b8",
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

        self.settings = ClientSettings()
        initial_broker = self.settings.get_broker_url() if hasattr(self.settings, "get_broker_url") else broker_url
        self.client = RemoteClient(broker_url=initial_broker)
        self.client.on_frame_callback = self._on_frame_received
        self.client.on_status_callback = self._on_status_update
        self.client.on_ota_info_callback = self._on_ota_info
        self.client.on_ota_progress_callback = self._on_ota_progress
        self.client.on_ota_complete_callback = self._on_ota_complete

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
        self._pending_frame = None
        self._render_scheduled = False

        # OTA state
        self.ota_update_available = False
        self.ota_remote_version = ""
        self.ota_downloading = False
        self.btn_ota = None
        self.git_update_info = None
        self.btn_git_update = None

        # FPS metrics
        self._frame_count = 0
        self._last_fps_time = time.time()
        self._current_fps = 0
        self.lbl_fps = None

        self.active_tab = start_tab

        self._build_login_ui()

        # Check for Git OTA updates automatically in background
        threading.Thread(target=self._check_git_updates_background, daemon=True).start()

        # Failsafe Boot Confirmation
        if check_boot_state() == OTAUpdateState.TESTING:
            self.root.after(2500, self._verify_and_confirm_ota_boot)

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

        # Brand Identity (Apple Typography & Status Pill)
        brand_frame = tk.Frame(header_bar, bg=THEME["bg"])
        brand_frame.pack(side=tk.LEFT)

        title_row = tk.Frame(brand_frame, bg=THEME["bg"])
        title_row.pack(anchor="w")

        lbl_dot = tk.Label(title_row, text="●", bg=THEME["bg"], fg=THEME["cyan"], font=("Segoe UI", 12, "bold"))
        lbl_dot.pack(side=tk.LEFT, padx=(0, 6))

        lbl_brand = tk.Label(title_row, text="AETHERDESK", bg=THEME["bg"], fg=THEME["text_main"],
                             font=("Segoe UI", 16, "bold"))
        lbl_brand.pack(side=tk.LEFT)

        lbl_ver_badge = tk.Label(title_row, text=f"v{get_app_version()}", bg="#161b2b", fg=THEME["cyan"],
                                 font=("Consolas", 8, "bold"), padx=6, pady=1)
        lbl_ver_badge.pack(side=tk.LEFT, padx=(8, 0))

        lbl_sub = tk.Label(brand_frame, text="Controllo Remoto Ultra-Leggero • Crittografia E2EE",
                           bg=THEME["bg"], fg=THEME["text_dim"], font=("Segoe UI", 9))
        lbl_sub.pack(anchor="w", pady=(1, 0))

        # Segmented Tab Pill [ 💻 Connetti | 🖥 Host ]
        tab_capsule = tk.Frame(header_bar, bg="#121520", padx=3, pady=3,
                               highlightthickness=1, highlightbackground=THEME["card_border"])
        tab_capsule.pack(side=tk.RIGHT)

        self.btn_tab_client = tk.Button(
            tab_capsule, text="💻 Connetti", font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2",
            padx=12, pady=3, highlightthickness=0,
            bg=THEME["tab_active_bg"] if self.active_tab == "client" else "#121520",
            fg=THEME["tab_active_fg"] if self.active_tab == "client" else THEME["text_muted"],
            command=lambda: self._switch_tab("client")
        )
        self.btn_tab_client.pack(side=tk.LEFT)

        self.btn_tab_host = tk.Button(
            tab_capsule, text="🖥 Questo PC", font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2",
            padx=12, pady=3, highlightthickness=0,
            bg=THEME["tab_active_bg"] if self.active_tab == "host" else "#121520",
            fg=THEME["tab_active_fg"] if self.active_tab == "host" else THEME["text_muted"],
            command=lambda: self._switch_tab("host")
        )
        self.btn_tab_host.pack(side=tk.LEFT, padx=(2, 0))

        bind_hover(self.btn_tab_client, "#121520", "#1c2235", THEME["text_muted"], THEME["text_main"])
        bind_hover(self.btn_tab_host, "#121520", "#1c2235", THEME["text_muted"], THEME["text_main"])

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

        lbl_foot_sec = tk.Label(footer, text="🔒 AES-256-GCM Zero-Knowledge • 60 FPS Engine",
                                bg=THEME["bg"], fg=THEME["text_dim"], font=("Segoe UI", 8))
        lbl_foot_sec.pack(side=tk.LEFT)

        self.btn_git_update = tk.Button(
            footer, text="🔄 Git: Controllo...", bg="#141824", fg=THEME["text_muted"],
            activebackground="#20263a", activeforeground="#ffffff", font=("Segoe UI", 8, "bold"),
            relief="flat", cursor="hand2", padx=10, pady=3, highlightthickness=1,
            highlightbackground=THEME["card_border"], command=self._on_git_update_click
        )
        self.btn_git_update.pack(side=tk.RIGHT)
        bind_hover(self.btn_git_update, "#141824", "#1d2335", THEME["text_muted"], THEME["cyan"])

    def _switch_tab(self, tab: str):
        self.active_tab = tab
        if tab == "client":
            self.btn_tab_client.config(bg=THEME["tab_active_bg"], fg=THEME["tab_active_fg"])
            self.btn_tab_host.config(bg="#121520", fg=THEME["text_muted"])
        else:
            self.btn_tab_host.config(bg=THEME["tab_active_bg"], fg=THEME["tab_active_fg"])
            self.btn_tab_client.config(bg="#121520", fg=THEME["text_muted"])

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

        lbl_card_title = tk.Label(conn_card, text="●  CONTROLLA UN COMPUTER REMOTO", bg=THEME["card"],
                                  fg=THEME["cyan"], font=("Segoe UI", 8, "bold"))
        lbl_card_title.pack(anchor="w", pady=(0, 10))

        # 1. Device ID Field
        lbl_id = tk.Label(conn_card, text="ID Computer (9 cifre):", bg=THEME["card"],
                          fg=THEME["text_muted"], font=("Segoe UI", 9, "bold"))
        lbl_id.pack(anchor="w", pady=(0, 4))

        id_box = tk.Frame(conn_card, bg=THEME["input_bg"], highlightthickness=1,
                          highlightbackground=THEME["input_border"], padx=8, pady=4)
        id_box.pack(fill=tk.X, pady=(0, 10))

        device_options = [f"{d['id']} ({d.get('name', 'PC')})" for d in devices] if devices else []
        self.combo_id = ttk.Combobox(id_box, values=device_options, font=("Consolas", 13, "bold"), justify="left")
        self.combo_id.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3)
        self.combo_id.set(saved_id if saved_id else "123 456 789")
        self.combo_id.bind("<<ComboboxSelected>>", self._on_device_selected)
        self.combo_id.bind("<Return>", lambda e: self._start_connect())
        bind_focus_ring(self.combo_id, id_box)

        btn_paste_id = tk.Button(id_box, text="Incolla", bg="#1b2032", fg=THEME["text_muted"], relief="flat",
                                 font=("Segoe UI", 8, "bold"), cursor="hand2", padx=8, pady=2, command=self._paste_id)
        btn_paste_id.pack(side=tk.RIGHT, padx=(4, 0))
        bind_hover(btn_paste_id, "#1b2032", "#262d44", THEME["text_muted"], THEME["text_main"])

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
        bind_focus_ring(self.entry_pwd, pwd_box)

        self.btn_pwd_toggle = tk.Button(pwd_box, text="👁", bg="#1b2032", fg=THEME["text_muted"], relief="flat",
                                        font=("Segoe UI", 9), cursor="hand2", padx=6, pady=2, command=self._toggle_pwd_view)
        self.btn_pwd_toggle.pack(side=tk.RIGHT)
        bind_hover(self.btn_pwd_toggle, "#1b2032", "#262d44", THEME["text_muted"], THEME["text_main"])

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

        btn_paste_brk = tk.Button(brk_box, text="Incolla", bg="#1b2032", fg=THEME["text_muted"], relief="flat",
                                  font=("Segoe UI", 8, "bold"), cursor="hand2", padx=8, pady=2, command=self._paste_broker)
        btn_paste_brk.pack(side=tk.RIGHT)
        bind_hover(btn_paste_brk, "#1b2032", "#262d44", THEME["text_muted"], THEME["text_main"])

        # Quick Broker Preset Chips
        chips_frame = tk.Frame(conn_card, bg=THEME["card"])
        chips_frame.pack(fill=tk.X, pady=(0, 10))

        def _set_brk(url):
            self.entry_broker.delete(0, tk.END)
            self.entry_broker.insert(0, url)

        btn_chip_local = tk.Button(chips_frame, text="🏠 Locale (127.0.0.1)", bg="#161a29", fg=THEME["text_dim"],
                                   font=("Segoe UI", 8), relief="flat", cursor="hand2", padx=6, pady=1,
                                   command=lambda: _set_brk("ws://127.0.0.1:8765"))
        btn_chip_local.pack(side=tk.LEFT, padx=(0, 4))
        bind_hover(btn_chip_local, "#161a29", "#20263c", THEME["text_dim"], THEME["cyan"])

        # Remember Checkbox
        self.var_remember = tk.BooleanVar(value=saved_rem)
        self.chk_remember = tk.Checkbutton(
            conn_card, text="Ricorda questo computer e impostazioni", variable=self.var_remember,
            bg=THEME["card"], fg=THEME["text_muted"], selectcolor=THEME["input_bg"],
            activebackground=THEME["card"], activeforeground=THEME["text_main"],
            font=("Segoe UI", 9)
        )
        self.chk_remember.pack(anchor="w", pady=(0, 12))

        # Primary Action Button (Apple / High-End CTA)
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

            lbl_q_title = tk.Label(quick_frame, text="●  CONNESSIONE RAPIDA SALVATI", bg=THEME["card"],
                                  fg=THEME["text_muted"], font=("Segoe UI", 8, "bold"))
            lbl_q_title.pack(anchor="w", pady=(0, 8))

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

                btn_q = tk.Button(row, text="⚡ Connetti", bg="#064e3b", fg="#6ee7b7",
                                  activebackground="#059669", activeforeground="#ffffff",
                                  font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2", padx=10, pady=3,
                                  command=lambda d=dev: self._quick_connect_to(d))
                btn_q.pack(side=tk.RIGHT)
                bind_hover(btn_q, "#064e3b", "#059669", "#6ee7b7", "#ffffff")

    def _render_host_tab(self, parent):
        # Card 1: This Machine ID & Status
        host_card = tk.Frame(parent, bg=THEME["card"], padx=18, pady=16,
                             highlightthickness=1, highlightbackground=THEME["card_border"])
        host_card.pack(fill=tk.X, pady=(0, 12))

        lbl_h_title = tk.Label(host_card, text="●  I TUOI DATI PER RICEVERE ASSISTENZA", bg=THEME["card"],
                               fg=THEME["cyan"], font=("Segoe UI", 8, "bold"))
        lbl_h_title.pack(anchor="w", pady=(0, 10))

        # Big Monospace ID Display
        id_display_box = tk.Frame(host_card, bg="#0d101a", padx=14, pady=10,
                                  highlightthickness=1, highlightbackground=THEME["input_border"])
        id_display_box.pack(fill=tk.X, pady=(0, 12))

        lbl_id_hint = tk.Label(id_display_box, text="ID DISPOSITIVO (DA FORNIRE A CHI SI CONNETTE):",
                               bg="#0d101a", fg=THEME["text_dim"], font=("Segoe UI", 8, "bold"))
        lbl_id_hint.pack(anchor="w")

        id_val_row = tk.Frame(id_display_box, bg="#0d101a")
        id_val_row.pack(fill=tk.X, pady=(4, 0))

        dev_id = self.security.get_or_create_device_id()
        lbl_id_val = tk.Label(id_val_row, text=dev_id, bg="#0d101a", fg="#fbbf24",
                              font=("Consolas", 22, "bold"))
        lbl_id_val.pack(side=tk.LEFT)

        btn_copy_id = tk.Button(id_val_row, text="📋 Copia ID", bg="#1b2032", fg=THEME["text_main"],
                                font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2", padx=10, pady=4,
                                command=lambda: self._copy_text(dev_id, "ID Dispositivo"))
        btn_copy_id.pack(side=tk.RIGHT)
        bind_hover(btn_copy_id, "#1b2032", "#262d44", THEME["text_main"], THEME["cyan"])

        # Password Config
        lbl_pwd_h = tk.Label(host_card, text="Password di Accesso:", bg=THEME["card"],
                             fg=THEME["text_muted"], font=("Segoe UI", 9, "bold"))
        lbl_pwd_h.pack(anchor="w", pady=(0, 4))

        pwd_row = tk.Frame(host_card, bg=THEME["input_bg"], highlightthickness=1,
                           highlightbackground=THEME["input_border"], padx=8, pady=3)
        pwd_row.pack(fill=tk.X, pady=(0, 12))

        self.entry_host_pwd = tk.Entry(pwd_row, show="*", font=("Segoe UI", 11), bg=THEME["input_bg"],
                                       fg=THEME["text_main"], insertbackground="white", relief="flat")
        self.entry_host_pwd.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3)
        bind_focus_ring(self.entry_host_pwd, pwd_row)

        btn_save_host_pwd = tk.Button(pwd_row, text="Salva Password", bg="#10b981", fg="#ffffff",
                                      font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2", padx=8, pady=2,
                                      command=self._save_host_password)
        btn_save_host_pwd.pack(side=tk.RIGHT)
        bind_hover(btn_save_host_pwd, "#10b981", "#059669", "#ffffff", "#ffffff")

        # Daemon State Control & Status
        daemon_row = tk.Frame(host_card, bg=THEME["card"])
        daemon_row.pack(fill=tk.X, pady=(2, 0))

        is_running = bool(self.host_daemon and self.host_daemon.running)
        self.lbl_host_status = tk.Label(
            daemon_row,
            text="● Servizio Host Attivo (In ascolto su porta 8765)" if is_running else "● Servizio Host non attivo",
            bg=THEME["card"], fg=THEME["success"] if is_running else THEME["text_dim"],
            font=("Segoe UI", 9, "bold")
        )
        self.lbl_host_status.pack(side=tk.LEFT)

        self.btn_host_toggle = tk.Button(
            daemon_row,
            text="Arresta" if is_running else "Avvia Demone",
            bg="#ef4444" if is_running else THEME["accent"], fg="#ffffff",
            font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2", padx=12, pady=4,
            command=self._toggle_host_daemon
        )
        self.btn_host_toggle.pack(side=tk.RIGHT)

        # Card 2: Detected LAN / Wi-Fi Endpoints
        net_card = tk.Frame(parent, bg=THEME["card"], padx=18, pady=12,
                            highlightthickness=1, highlightbackground=THEME["card_border"])
        net_card.pack(fill=tk.X, pady=(0, 10))

        lbl_net_title = tk.Label(net_card, text="●  INDIRIZZI BROKER RILEVATI PER IL CLIENT", bg=THEME["card"],
                                 fg=THEME["text_muted"], font=("Segoe UI", 8, "bold"))
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

            btn_cp = tk.Button(row, text="Copia", bg="#1b2032", fg=THEME["text_muted"], relief="flat",
                               font=("Segoe UI", 8, "bold"), cursor="hand2", padx=8, pady=1,
                               command=lambda u=net_url: self._copy_text(u, "URL"))
            btn_cp.pack(side=tk.RIGHT)
            bind_hover(btn_cp, "#1b2032", "#262d44", THEME["text_muted"], THEME["text_main"])

        # Quick System Tools
        tools_row = tk.Frame(parent, bg=THEME["bg"])
        tools_row.pack(fill=tk.X, pady=(2, 0))

        btn_fw = tk.Button(tools_row, text="🛡 Sblocca Firewall Windows", bg="#161b29", fg=THEME["warning"],
                           font=("Segoe UI", 8, "bold"), relief="flat", cursor="hand2", padx=10, pady=4,
                           command=self._open_firewall_unblock)
        btn_fw.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        bind_hover(btn_fw, "#161b29", "#242018", THEME["warning"], "#fde047")

        btn_svc = tk.Button(tools_row, text="⚙ Installa come Servizio Windows", bg="#161b29", fg=THEME["text_muted"],
                            font=("Segoe UI", 8, "bold"), relief="flat", cursor="hand2", padx=10, pady=4,
                            command=self._open_installer)
        btn_svc.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(4, 0))
        bind_hover(btn_svc, "#161b29", "#20263c", THEME["text_muted"], THEME["text_main"])

    def _copy_text(self, text: str, label: str = "Testo"):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        messagebox.showinfo("Copiato", f"{label} copiato negli appunti:\n{text}")

    def _save_host_password(self):
        new_pwd = self.entry_host_pwd.get().strip() if hasattr(self, "entry_host_pwd") else ""
        if not new_pwd:
            messagebox.showwarning("Attenzione", "Inserisci una password valida non vuota.")
            return
        self.security.set_password(new_pwd)
        self.entry_host_pwd.delete(0, tk.END)
        messagebox.showinfo("Successo", "Password di accesso remoto salvata con successo!")

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
            self.host_daemon.stop()
            if hasattr(self, "btn_host_toggle"):
                self.btn_host_toggle.config(text="Avvia Demone", bg=THEME["accent"])
            if hasattr(self, "lbl_host_status"):
                self.lbl_host_status.config(text="● Servizio Host Arrestato", fg=THEME["danger"])

    def _run_host_daemon_worker(self):
        async def runner():
            try:
                self.host_broker = SignalingBroker(host="0.0.0.0", port=8765)
                await self.host_broker.start()
            except Exception:
                pass
            await self.host_daemon.start()
        asyncio.run(runner())

    def _open_firewall_unblock(self):
        bat_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts", "allow_firewall.bat"))
        if os.path.exists(bat_path):
            os.startfile(bat_path)
        else:
            messagebox.showerror("Errore", f"File non trovato: {bat_path}")

    def _open_installer(self):
        bat_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts", "install_service.bat"))
        if os.path.exists(bat_path):
            os.startfile(bat_path)
        else:
            messagebox.showerror("Errore", f"File non trovato: {bat_path}")

    def _quick_connect_to(self, dev: dict):
        self.combo_id.set(dev.get("id", ""))
        self.entry_pwd.delete(0, tk.END)
        self.entry_pwd.insert(0, dev.get("password", ""))
        if dev.get("broker_url"):
            self.entry_broker.delete(0, tk.END)
            self.entry_broker.insert(0, dev.get("broker_url"))
        self._start_connect()

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

    def _start_connect(self):
        raw_val = self.combo_id.get().strip()
        dev_id = raw_val.split("(")[0].strip() if "(" in raw_val else raw_val
        pwd = self.entry_pwd.get().strip()
        broker_url = self.entry_broker.get().strip()
        if not broker_url:
            broker_url = "ws://127.0.0.1:8765"

        if not ClientState.validate_id(dev_id):
            messagebox.showerror("Errore ID", "L'ID deve essere composto esattamente da 9 cifre (es. 482 910 234).")
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

    def _switch_to_host_panel(self):
        self._switch_tab("host")

    def _open_viewer_window(self):
        if self.viewer_window and tk.Toplevel.winfo_exists(self.viewer_window):
            return

        self.viewer_window = tk.Toplevel(self.root)
        self.viewer_window.title(f"AetherDesk - [{self.client.state.target_id}]")
        self._center_window(self.viewer_window, 1280, 720)
        self.viewer_window.configure(bg="#080a10")
        self.viewer_window.protocol("WM_DELETE_WINDOW", self._close_viewer)

        self.scale_mode = "fit"
        self.toolbar_visible = True
        self._active_mon = -1
        self._active_quality = 65

        # Floating notch to reveal toolbar when collapsed
        self.notch_frame = tk.Frame(self.viewer_window, bg="#111422", height=26,
                                    highlightthickness=1, highlightbackground=THEME["card_border"])
        lbl_show = tk.Label(self.notch_frame, text="▼ Controlli Sessione (F11 / Ctrl+Alt+H)", bg="#111422", fg=THEME["cyan"],
                            font=("Segoe UI", 9, "bold"), cursor="hand2", padx=16, pady=3)
        lbl_show.pack(side=tk.TOP)
        lbl_show.bind("<Button-1>", lambda e: self._toggle_toolbar())

        # Main Modern Floating Toolbar
        self.toolbar = tk.Frame(self.viewer_window, bg="#111422", height=44, padx=12, pady=5)
        self.toolbar.pack(fill=tk.X, side=tk.TOP)

        # Subtle bottom border line
        self.tb_border = tk.Frame(self.viewer_window, bg=THEME["card_border"], height=1)
        self.tb_border.pack(fill=tk.X, side=tk.TOP)

        def _make_btn(parent, text, cmd, bg="#1b2032", fg=THEME["text_muted"], h_bg="#262d44", h_fg="#ffffff",
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
        badge_frame = tk.Frame(self.toolbar, bg="#161a29", padx=10, pady=3,
                               highlightbackground=THEME["card_border"], highlightthickness=1)
        badge_frame.pack(side=tk.LEFT, padx=(0, 6))

        lbl_dot = tk.Label(badge_frame, text="●", bg="#161a29", fg=THEME["success"], font=("Segoe UI", 10, "bold"))
        lbl_dot.pack(side=tk.LEFT, padx=(0, 5))
        lbl_brand = tk.Label(badge_frame, text="LIVE", bg="#161a29", fg=THEME["cyan"], font=("Segoe UI", 9, "bold"))
        lbl_brand.pack(side=tk.LEFT, padx=(0, 6))
        lbl_tid = tk.Label(badge_frame, text=f"[{self.client.state.target_id}]", bg="#161a29",
                           fg=THEME["text_main"], font=("Consolas", 9, "bold"))
        lbl_tid.pack(side=tk.LEFT)

        _make_sep(self.toolbar)

        # 2. System Commands Capsule (TeamViewer-grade Quick Actions)
        actions_capsule = tk.Frame(self.toolbar, bg="#161a29", padx=3, pady=2,
                                   highlightthickness=1, highlightbackground=THEME["card_border"])
        actions_capsule.pack(side=tk.LEFT, padx=3)

        _make_btn(actions_capsule, "⊞ Desktop", lambda: self._send_special("win_d"), padx=6)
        _make_btn(actions_capsule, "🗔 Alt+Tab", lambda: self._send_special("alt_tab"), padx=6)
        _make_btn(actions_capsule, "⚙ TaskMgr", lambda: self._send_special("task_mgr"), padx=6)
        _make_btn(actions_capsule, "🔒 Blocca", lambda: self._send_special("lock"), padx=6)
        _make_btn(actions_capsule, "⚡ Ctrl+Alt+Del", lambda: self._send_special("ctrl_alt_del"), padx=6)
        _make_btn(actions_capsule, "📁 Invia File", self._pick_and_send_file,
                  bg="#0c3a5c", fg=THEME["cyan"], h_bg="#0284c7", h_fg="#ffffff", padx=8)

        _make_sep(self.toolbar)

        # 3. Stream Controls Capsule (Monitor & Quality)
        stream_capsule = tk.Frame(self.toolbar, bg="#161a29", padx=4, pady=2,
                                  highlightthickness=1, highlightbackground=THEME["card_border"])
        stream_capsule.pack(side=tk.LEFT, padx=3)

        lbl_mon = tk.Label(stream_capsule, text="🖥", bg="#161a29", fg=THEME["text_dim"], font=("Segoe UI", 9))
        lbl_mon.pack(side=tk.LEFT, padx=(2, 2))

        self.btn_m1 = _make_btn(stream_capsule, "1", lambda: self._on_switch_mon(0), padx=5, font=("Segoe UI", 8, "bold"))
        self.btn_m2 = _make_btn(stream_capsule, "2", lambda: self._on_switch_mon(1), padx=5, font=("Segoe UI", 8, "bold"))
        self.btn_mall = _make_btn(stream_capsule, "Tutti", lambda: self._on_switch_mon(-1), padx=5, font=("Segoe UI", 8, "bold"))
        self.btn_mall.config(bg=THEME["accent"], fg="#ffffff")

        lbl_sep_in = tk.Label(stream_capsule, text="•", bg="#161a29", fg=THEME["card_border"])
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

        # 6. OTA In-Place Update
        self.btn_ota = _make_btn(self.toolbar, "🔄 OTA", self._request_ota_update,
                                 bg="#064e3b", fg="#6ee7b7", h_bg="#047857", h_fg="#ffffff", padx=6)
        if self.ota_update_available:
            self.btn_ota.config(text=f"⚡ Aggiorna v{self.ota_remote_version}", bg="#059669", fg="#ffffff")

        # Right side items
        _make_btn(self.toolbar, "✕ Disconnetti", self._close_viewer, bg="#3b171f", fg="#f87171",
                  h_bg=THEME["danger"], h_fg="#ffffff", side=tk.RIGHT, padx=10, font=("Segoe UI", 9, "bold"))

        _make_btn(self.toolbar, "▲ Nascondi", self._toggle_toolbar, side=tk.RIGHT, padx=6, font=("Segoe UI", 8))

        self.btn_fs = _make_btn(self.toolbar, "⛶ Schermo Intero", self._toggle_fullscreen, side=tk.RIGHT, padx=8)

        # Telemetry Pill (FPS + Encryption)
        tele_frame = tk.Frame(self.toolbar, bg="#161a29", padx=8, pady=3,
                              highlightbackground=THEME["card_border"], highlightthickness=1)
        tele_frame.pack(side=tk.RIGHT, padx=6)

        self.lbl_fps = tk.Label(tele_frame, text="FPS: 0", bg="#161a29", fg=THEME["cyan"], font=("Consolas", 8, "bold"))
        self.lbl_fps.pack(side=tk.LEFT, padx=(0, 6))

        lbl_sec = tk.Label(tele_frame, text="🔒 AES-256", bg="#161a29", fg=THEME["text_dim"], font=("Segoe UI", 8))
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
        self.btn_scale.config(text=txt, bg=THEME["accent"] if self.scale_mode == "1:1" else "#1b2032",
                              fg="#ffffff" if self.scale_mode == "1:1" else THEME["text_muted"])

    def _on_switch_mon(self, mon_id: int):
        self._active_mon = mon_id
        for m, btn in [(0, self.btn_m1), (1, self.btn_m2), (-1, self.btn_mall)]:
            btn.config(bg=THEME["accent"] if m == mon_id else "#1b2032",
                       fg="#ffffff" if m == mon_id else THEME["text_muted"])
        self._switch_monitor(mon_id)

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

    def _check_git_updates_background(self, notify_if_none: bool = False):
        try:
            is_newer, info = check_github_update()
            def update_ui():
                if not self.btn_git_update:
                    return
                if is_newer and info.get("download_url"):
                    self.git_update_info = info
                    self.btn_git_update.config(
                        text=f"⚡ Aggiorna Git (v{info['tag']})",
                        bg="#059669",
                        fg="#ffffff"
                    )
                    self._on_status_update(f"Aggiornamento Git disponibile: v{info['tag']}!")
                else:
                    tag = info.get("tag", get_app_version())
                    self.btn_git_update.config(
                        text=f"✓ Git v{tag} (Aggiornato)",
                        bg="#141824",
                        fg="#34d399"
                    )
                    if notify_if_none:
                        messagebox.showinfo("Aggiornamento Git", f"AetherDesk e' gia' aggiornato all'ultima versione disponibile su Git (v{tag}).")
            self.root.after(0, update_ui)
        except Exception:
            pass

    def _on_git_update_click(self):
        if getattr(self, "ota_downloading", False):
            return
        if self.git_update_info and self.git_update_info.get("download_url"):
            info = self.git_update_info
            size_mb = info.get("size", 0) / (1024 * 1024)
            confirm = messagebox.askyesno(
                "Aggiornamento Automatico da Git",
                f"Trovata nuova versione su GitHub:\n\n"
                f"• Versione: v{info['tag']}\n"
                f"• Dimensione: {size_mb:.1f} MB\n\n"
                f"Vuoi scaricare l'aggiornamento direttamente da Git ed eseguire il riavvio in-place automatico?"
            )
            if not confirm:
                return
            self._start_git_download(info["download_url"], info["tag"])
        else:
            self.btn_git_update.config(text="🔄 Controllo...")
            threading.Thread(target=lambda: self._check_git_updates_background(notify_if_none=True), daemon=True).start()

    def _start_git_download(self, download_url: str, target_version: str):
        self.ota_downloading = True
        self.btn_git_update.config(text="⏳ Scaricamento...", state=tk.DISABLED)

        def worker():
            def on_progress(rec, tot):
                pct = int((rec / tot) * 100) if tot > 0 else 0
                mb_rec = rec / (1024 * 1024)
                mb_tot = tot / (1024 * 1024)
                def up():
                    if self.btn_git_update:
                        self.btn_git_update.config(text=f"⬇ Git: {pct}% ({mb_rec:.1f}/{mb_tot:.1f}MB)")
                    self._on_status_update(f"Download da Git: {pct}%...")
                self.root.after(0, up)

            ok, temp_path_or_err = download_github_binary(download_url, progress_callback=on_progress)
            if not ok:
                self.ota_downloading = False
                def err():
                    messagebox.showerror("Errore Download Git", temp_path_or_err)
                    if self.btn_git_update:
                        self.btn_git_update.config(text="🔄 Riprova Git", state=tk.NORMAL, bg="#141824", fg="#f87171")
                self.root.after(0, err)
                return

            applied, app_msg = apply_in_place_update(temp_path_or_err, target_version=target_version)
            self.ota_downloading = False
            def finish():
                if applied:
                    messagebox.showinfo("Aggiornamento Riuscito", f"{app_msg}\nAetherDesk verra' riavviato con la nuova versione.")
                    self.root.destroy()
                    sys.exit(0)
                else:
                    messagebox.showerror("Errore Aggiornamento", app_msg)
                    if self.btn_git_update:
                        self.btn_git_update.config(text="🔄 Riprova Git", state=tk.NORMAL)
            self.root.after(0, finish)

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
        available = payload.get("available", False)
        remote_ver = payload.get("version", "")
        self.ota_remote_version = remote_ver
        self.ota_update_available = available
        def update_ui():
            if available:
                if self.btn_ota:
                    self.btn_ota.config(text=f"⚡ Aggiorna v{remote_ver}", bg="#059669", fg="#ffffff", state=tk.NORMAL)
                self._on_status_update(f"Aggiornamento OTA disponibile: v{remote_ver}")
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
                messagebox.showinfo("Aggiornamento Riuscito", f"{msg}\nL'applicazione si riavviera' adesso.")
                if self.viewer_window:
                    try:
                        self.viewer_window.destroy()
                    except Exception:
                        pass
                self.root.destroy()
                sys.exit(0)
            else:
                messagebox.showerror("Errore Aggiornamento OTA", f"Impossibile completare l'aggiornamento:\n{msg}")
                if self.btn_ota:
                    self.btn_ota.config(text="🔄 Aggiorna OTA", state=tk.NORMAL, bg="#064e3b", fg="#6ee7b7")
        self.root.after(0, notify)

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

    def _close_viewer(self):
        asyncio.run_coroutine_threadsafe(self.client.disconnect(), self.loop)
        self._canvas_img_id = None
        if self.viewer_window:
            self.viewer_window.destroy()
            self.viewer_window = None

    def _set_quality(self, quality: int):
        asyncio.run_coroutine_threadsafe(self.client.set_quality(quality), self.loop)

    def _on_frame_received(self, jpeg_bytes: bytes, dims: tuple[int, int], cur: tuple[int, int]):
        if not self.canvas:
            return
        self._pending_frame = (jpeg_bytes, dims, cur)
        if not self._render_scheduled:
            self._render_scheduled = True
            self.root.after(0, self._render_latest_frame)

    def _render_latest_frame(self):
        self._render_scheduled = False
        if not self.canvas or not getattr(self, "_pending_frame", None):
            return

        jpeg_bytes, dims, cur = self._pending_frame

        # Frame rate calculation
        self._frame_count += 1
        now = time.time()
        elapsed = now - self._last_fps_time
        if elapsed >= 1.0:
            self._current_fps = int(self._frame_count / elapsed)
            self._frame_count = 0
            self._last_fps_time = now
            if self.lbl_fps and self.viewer_window and tk.Toplevel.winfo_exists(self.viewer_window):
                fps_val = self._current_fps
                self.lbl_fps.config(text=f"FPS: {fps_val}")

        try:
            c_w = self.canvas.winfo_width()
            c_h = self.canvas.winfo_height()
            if c_w <= 10 or c_h <= 10:
                c_w = max(self.viewer_window.winfo_width() if self.viewer_window else 1280, 1280)
                c_h = max((self.viewer_window.winfo_height() - 44) if self.viewer_window else 720, 720)

            img = Image.open(io.BytesIO(jpeg_bytes))
            if getattr(self, "scale_mode", "fit") == "fit":
                img.thumbnail((c_w, c_h), Image.Resampling.LANCZOS)
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
        except Exception as e:
            pass

    def _calc_norm_coords(self, event) -> tuple[float, float]:
        if not self.canvas or self.current_img_dims == (0, 0):
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

    def _on_mouse_move(self, event):
        nx, ny = self._calc_norm_coords(event)
        asyncio.run_coroutine_threadsafe(
            self.client.send_input({"type": "mouse_move", "x": nx, "y": ny}),
            self.loop
        )

    def _on_mouse_btn(self, event, button: str, action: str):
        if self.viewer_window:
            try:
                self.viewer_window.focus_force()
            except Exception:
                pass
        nx, ny = self._calc_norm_coords(event)
        cmd = {
            "type": f"mouse_{action}",
            "button": button,
            "x": nx,
            "y": ny
        }
        asyncio.run_coroutine_threadsafe(self.client.send_input(cmd), self.loop)

    def _on_double_click(self, event, button: str):
        pass

    def _on_mouse_wheel(self, event):
        nx, ny = self._calc_norm_coords(event)
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

if __name__ == "__main__":
    import argparse
    from src.ota import get_app_version

    if sys.platform == "win32" and len(sys.argv) > 1:
        try:
            ctypes.windll.kernel32.AttachConsole(-1)
        except Exception:
            pass

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
    parser.add_argument("-v", "--version", action="version", version=f"AetherDesk Client v{get_app_version()}")
    args = parser.parse_args()

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
