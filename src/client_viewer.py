import io
import os
import sys
import re
import time
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

    def __init__(self, broker_url: str = "ws://127.0.0.1:8765"):
        self.root = tk.Tk()
        self.root.title("AetherDesk - Client Portatile")
        self._center_window(self.root, 490, 630)
        self.root.configure(bg="#0a0c14")

        # Set official window icon (handles frozen PyInstaller package vs dev)
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

        self._build_login_ui()

        # Check for Git OTA updates automatically in background
        threading.Thread(target=self._check_git_updates_background, daemon=True).start()

        # Failsafe Boot Confirmation (adhering to OTA firmware & software patterns)
        if check_boot_state() == OTAUpdateState.TESTING:
            self.root.after(2500, self._verify_and_confirm_ota_boot)

    def _verify_and_confirm_ota_boot(self):
        """Perform self-test verification and confirm OTA boot permanently."""
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

    def _build_login_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TLabel", background="#0a0c14", foreground="#cdd6f4", font=("Segoe UI", 10))
        style.configure("Header.TLabel", background="#0a0c14", foreground="#38bdf8", font=("Segoe UI", 18, "bold"))
        style.configure("SubHeader.TLabel", background="#0a0c14", foreground="#94a3b8", font=("Segoe UI", 9))
        style.configure("Status.TLabel", background="#0a0c14", foreground="#a6adc8", font=("Segoe UI", 9))
        style.configure("TButton", font=("Segoe UI", 10, "bold"), background="#38bdf8", foreground="#0a0c14")

        container = tk.Frame(self.root, bg="#0a0c14", padx=25, pady=12)
        container.pack(fill=tk.BOTH, expand=True)

        header_row = tk.Frame(container, bg="#0a0c14")
        header_row.pack(fill=tk.X, pady=(0, 2))

        header = ttk.Label(header_row, text="AETHERDESK", style="Header.TLabel")
        header.pack(side=tk.LEFT)

        btn_host = tk.Button(header_row, text="🖥 Modalità Host", bg="#1e293b", fg="#38bdf8", relief="flat",
                             font=("Segoe UI", 9, "bold"), cursor="hand2", command=self._switch_to_host_panel)
        btn_host.pack(side=tk.RIGHT)

        subheader = ttk.Label(container, text="Controllo Remoto Ultra-Leggero (Zero-Bloat E2EE)", style="SubHeader.TLabel")
        subheader.pack(pady=(0, 10))

        saved_id, saved_pwd, saved_rem = self.settings.load()
        saved_broker = self.settings.get_broker_url()
        devices = self.settings.get_devices()

        # Device ID
        lbl_id = ttk.Label(container, text="ID Computer Remoto (9 cifre):")
        lbl_id.pack(anchor="w")

        id_box = tk.Frame(container, bg="#0a0c14")
        id_box.pack(fill=tk.X, pady=(2, 8))

        device_options = [f"{d['id']} ({d.get('name', 'PC')})" for d in devices] if devices else []
        self.combo_id = ttk.Combobox(id_box, values=device_options, font=("Consolas", 12), justify="center")
        self.combo_id.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3)
        self.combo_id.set(saved_id if saved_id else "123 456 789")
        self.combo_id.bind("<<ComboboxSelected>>", self._on_device_selected)
        self.combo_id.bind("<Return>", lambda e: self._start_connect())

        btn_paste_id = tk.Button(id_box, text="Incolla", bg="#1e293b", fg="#cdd6f4", relief="flat",
                                 font=("Segoe UI", 9), cursor="hand2", command=self._paste_id)
        btn_paste_id.pack(side=tk.RIGHT, padx=(4, 0), ipady=2)

        # Password
        lbl_pwd = ttk.Label(container, text="Password di Accesso:")
        lbl_pwd.pack(anchor="w")

        pwd_box = tk.Frame(container, bg="#0a0c14")
        pwd_box.pack(fill=tk.X, pady=(2, 8))

        self.entry_pwd = tk.Entry(pwd_box, font=("Segoe UI", 11), show="*", bg="#181c2c", fg="#f1f5f9",
                                  insertbackground="white", relief="flat")
        self.entry_pwd.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3)
        self.entry_pwd.bind("<Return>", lambda e: self._start_connect())
        if saved_pwd:
            self.entry_pwd.insert(0, saved_pwd)

        self.btn_pwd_toggle = tk.Button(pwd_box, text="👁", bg="#1e293b", fg="#cdd6f4", relief="flat",
                                        font=("Segoe UI", 9), cursor="hand2", command=self._toggle_pwd_view)
        self.btn_pwd_toggle.pack(side=tk.RIGHT, padx=(4, 0), ipady=2)

        # Broker URL (LAN / Cloud / VPN)
        lbl_broker = ttk.Label(container, text="Server Broker (IP o Dominio):")
        lbl_broker.pack(anchor="w")

        brk_box = tk.Frame(container, bg="#0a0c14")
        brk_box.pack(fill=tk.X, pady=(2, 8))

        self.entry_broker = tk.Entry(brk_box, font=("Segoe UI", 10), bg="#181c2c", fg="#f1f5f9",
                                     insertbackground="white", relief="flat")
        self.entry_broker.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3)
        self.entry_broker.bind("<Return>", lambda e: self._start_connect())
        self.entry_broker.insert(0, saved_broker if saved_broker else "ws://127.0.0.1:8765")

        btn_paste_brk = tk.Button(brk_box, text="Incolla", bg="#1e293b", fg="#cdd6f4", relief="flat",
                                  font=("Segoe UI", 9), cursor="hand2", command=self._paste_broker)
        btn_paste_brk.pack(side=tk.RIGHT, padx=(4, 0), ipady=2)

        self.var_remember = tk.BooleanVar(value=saved_rem)
        self.chk_remember = tk.Checkbutton(container, text="Ricorda questo computer e impostazioni",
                                           variable=self.var_remember, bg="#0a0c14", fg="#cdd6f4",
                                           selectcolor="#181825", activebackground="#0a0c14",
                                           activeforeground="#cdd6f4", font=("Segoe UI", 9))
        self.chk_remember.pack(anchor="w", pady=(0, 10))

        self.btn_connect = tk.Button(container, text="Connetti al Computer", bg="#89b4fa", fg="#11111b",
                                     font=("Segoe UI", 11, "bold"), relief="flat", cursor="hand2", command=self._start_connect)
        self.btn_connect.pack(fill=tk.X, ipady=6)

        self.lbl_status = ttk.Label(container, text="Pronto alla connessione.", style="Status.TLabel")
        self.lbl_status.pack(pady=(8, 0))

        # Quick-connect saved devices section
        if devices or saved_id:
            quick_frame = tk.Frame(container, bg="#181c2c", padx=12, pady=10, highlightbackground="#272b3f", highlightthickness=1)
            quick_frame.pack(fill=tk.X, pady=(12, 0))

            lbl_q_title = tk.Label(quick_frame, text="CONNESSIONE RAPIDA DISPOSITIVI SALVATI:", bg="#181c2c", fg="#38bdf8", font=("Segoe UI", 8, "bold"))
            lbl_q_title.pack(anchor="w", pady=(0, 6))

            displayed_devices = devices if devices else [{"id": saved_id, "name": "PC Recente", "password": saved_pwd, "broker_url": saved_broker}]
            for dev in displayed_devices[:3]:
                d_id = dev.get("id", "")
                d_name = dev.get("name", "Computer")
                d_brk = dev.get("broker_url", "ws://127.0.0.1:8765")

                row = tk.Frame(quick_frame, bg="#181c2c")
                row.pack(fill=tk.X, pady=2)

                lbl_ico = tk.Label(row, text="💻", bg="#181c2c", fg="#a6adc8", font=("Segoe UI", 10))
                lbl_ico.pack(side=tk.LEFT, padx=(0, 4))

                info_frame = tk.Frame(row, bg="#181c2c")
                info_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)

                lbl_n = tk.Label(info_frame, text=f"{d_name} [{d_id}]", bg="#181c2c", fg="#cdd6f4", font=("Segoe UI", 9, "bold"), anchor="w")
                lbl_n.pack(anchor="w")

                lbl_b = tk.Label(info_frame, text=d_brk, bg="#181c2c", fg="#64748b", font=("Consolas", 8), anchor="w")
                lbl_b.pack(anchor="w")

                btn_q = tk.Button(row, text="⚡ Connetti", bg="#10b981", fg="#ffffff", font=("Segoe UI", 9, "bold"),
                                  relief="flat", cursor="hand2", padx=8, pady=2,
                                  command=lambda d=dev: self._quick_connect_to(d))
                btn_q.pack(side=tk.RIGHT, padx=2)

        # Footer with Version & Automatic Git OTA Update badge/button
        footer_frame = tk.Frame(container, bg="#0a0c14")
        footer_frame.pack(fill=tk.X, pady=(14, 0))

        lbl_ver = tk.Label(footer_frame, text=f"v{get_app_version()} • E2EE Pronto", bg="#0a0c14", fg="#64748b", font=("Segoe UI", 8))
        lbl_ver.pack(side=tk.LEFT)

        self.btn_git_update = tk.Button(footer_frame, text="🔄 Git: Controllo...", bg="#181c2c", fg="#94a3b8",
                                        activebackground="#272b3f", activeforeground="#ffffff",
                                        font=("Segoe UI", 8, "bold"), relief="flat", cursor="hand2", padx=6, pady=2,
                                        command=self._on_git_update_click)
        self.btn_git_update.pack(side=tk.RIGHT)

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
        self.root.after(0, lambda: self.lbl_status.config(text=text))
        if self.viewer_window and tk.Toplevel.winfo_exists(self.viewer_window):
            self.root.after(0, lambda: self.viewer_window.title(f"Desktop Remoto - [{self.client.state.target_id}] - {text}"))

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

        self.btn_connect.config(state="disabled", text="Connessione in corso...")
        asyncio.run_coroutine_threadsafe(self._async_connect_task(dev_id, pwd, broker_url), self.loop)
        self.root.after(7000, self._ensure_connect_btn_reset)

    def _ensure_connect_btn_reset(self):
        if not self.client.state.is_connected:
            self.btn_connect.config(state="normal", text="Connetti al Computer")

    async def _async_connect_task(self, dev_id: str, pwd: str, broker_url: str):
        ok = await self.client.connect(dev_id, pwd, broker_url=broker_url)
        self.root.after(0, lambda: self._handle_connect_result(ok))

    def _handle_connect_result(self, success: bool):
        self.btn_connect.config(state="normal", text="Connetti al Computer")
        if success:
            raw_val = self.combo_id.get().strip()
            dev_id = raw_val.split("(")[0].strip() if "(" in raw_val else raw_val
            pwd = self.entry_pwd.get().strip()
            broker_url = self.entry_broker.get().strip()
            self.settings.save(dev_id, pwd, self.var_remember.get(), broker_url=broker_url)
            self._open_viewer_window()

    def _open_viewer_window(self):
        if self.viewer_window and tk.Toplevel.winfo_exists(self.viewer_window):
            return

        self.viewer_window = tk.Toplevel(self.root)
        self.viewer_window.title(f"AetherDesk - [{self.client.state.target_id}]")
        self._center_window(self.viewer_window, 1280, 720)
        self.viewer_window.configure(bg="#0c0e14")
        self.viewer_window.protocol("WM_DELETE_WINDOW", self._close_viewer)

        self.scale_mode = "fit"
        self.toolbar_visible = True
        self._active_mon = -1
        self._active_quality = 65

        # Floating notch to reveal toolbar when collapsed
        self.notch_frame = tk.Frame(self.viewer_window, bg="#111420", height=24)
        lbl_show = tk.Label(self.notch_frame, text="▼ Mostra Controlli (F11 / Ctrl+Alt+H)", bg="#111420", fg="#38bdf8",
                            font=("Segoe UI", 9, "bold"), cursor="hand2", padx=12, pady=2)
        lbl_show.pack(side=tk.TOP)
        lbl_show.bind("<Button-1>", lambda e: self._toggle_toolbar())

        # Main Modern Toolbar
        self.toolbar = tk.Frame(self.viewer_window, bg="#111420", height=42, padx=10, pady=4)
        self.toolbar.pack(fill=tk.X, side=tk.TOP)

        # Subtle bottom border line
        self.tb_border = tk.Frame(self.viewer_window, bg="#1e2336", height=1)
        self.tb_border.pack(fill=tk.X, side=tk.TOP)

        def _make_btn(parent, text, cmd, bg="#1e2235", fg="#d1d5db", h_bg="#2c324c", h_fg="#ffffff",
                      font=("Segoe UI", 9), padx=7, pady=3, side=tk.LEFT):
            b = tk.Button(parent, text=text, bg=bg, fg=fg, relief="flat", activebackground=h_bg,
                          activeforeground=h_fg, font=font, cursor="hand2", padx=padx, pady=pady, command=cmd)
            b.pack(side=side, padx=2, pady=2)
            def on_enter(e):
                cur_bg = b.cget("bg")
                if cur_bg not in ("#3b82f6", "#10b981", "#ef4444", "#3b171f"):
                    b.config(bg=h_bg, fg=h_fg)
            def on_leave(e):
                cur_bg = b.cget("bg")
                if cur_bg not in ("#3b82f6", "#10b981", "#ef4444", "#3b171f"):
                    b.config(bg=bg, fg=fg)
            b.bind("<Enter>", on_enter)
            b.bind("<Leave>", on_leave)
            return b

        def _make_sep(parent, side=tk.LEFT):
            s = tk.Frame(parent, bg="#272b3f", width=1, height=20)
            s.pack(side=side, padx=5, pady=4)
            return s

        # 1. Left Badge: LIVE Indicator + Target ID
        badge_frame = tk.Frame(self.toolbar, bg="#181c2c", padx=8, pady=3, highlightbackground="#272b3f", highlightthickness=1)
        badge_frame.pack(side=tk.LEFT, padx=(0, 4))
        lbl_dot = tk.Label(badge_frame, text="●", bg="#181c2c", fg="#10b981", font=("Segoe UI", 10, "bold"))
        lbl_dot.pack(side=tk.LEFT, padx=(0, 4))
        lbl_brand = tk.Label(badge_frame, text="AETHERDESK", bg="#181c2c", fg="#38bdf8", font=("Segoe UI", 9, "bold"))
        lbl_brand.pack(side=tk.LEFT, padx=(0, 6))
        lbl_tid = tk.Label(badge_frame, text=f"[{self.client.state.target_id}]", bg="#181c2c", fg="#cdd6f4", font=("Consolas", 9))
        lbl_tid.pack(side=tk.LEFT)

        _make_sep(self.toolbar)

        # 2. System Commands (TeamViewer-grade Quick Actions)
        _make_btn(self.toolbar, "⊞ Desktop", lambda: self._send_special("win_d"), padx=5)
        _make_btn(self.toolbar, "🗔 Alt+Tab", lambda: self._send_special("alt_tab"), padx=5)
        _make_btn(self.toolbar, "⚙ TaskMgr", lambda: self._send_special("task_mgr"), padx=5)
        _make_btn(self.toolbar, "🔒 Blocca", lambda: self._send_special("lock"), padx=5)
        _make_btn(self.toolbar, "⚡ Ctrl+Alt+Del", lambda: self._send_special("ctrl_alt_del"), padx=6)
        _make_btn(self.toolbar, "📁 Invia File", self._pick_and_send_file, bg="#1d283a", fg="#38bdf8", h_bg="#2563eb", h_fg="#ffffff", padx=8)

        _make_sep(self.toolbar)

        # 3. Monitor Selector
        lbl_mon = tk.Label(self.toolbar, text="🖥 Monitor:", bg="#111420", fg="#94a3b8", font=("Segoe UI", 8, "bold"))
        lbl_mon.pack(side=tk.LEFT, padx=(2, 2))

        self.btn_m1 = _make_btn(self.toolbar, "1", lambda: self._on_switch_mon(0), padx=5, font=("Segoe UI", 8, "bold"))
        self.btn_m2 = _make_btn(self.toolbar, "2", lambda: self._on_switch_mon(1), padx=5, font=("Segoe UI", 8, "bold"))
        self.btn_mall = _make_btn(self.toolbar, "Tutti", lambda: self._on_switch_mon(-1), padx=5, font=("Segoe UI", 8, "bold"))
        self.btn_mall.config(bg="#3b82f6", fg="#ffffff")

        _make_sep(self.toolbar)

        # 4. View Scale Mode (Adatta / 1:1)
        self.btn_scale = _make_btn(self.toolbar, "🔍 Adatta", self._toggle_scale_mode, padx=6)

        # 5. Quality Selector
        lbl_q = tk.Label(self.toolbar, text="✦ Qualità:", bg="#111420", fg="#94a3b8", font=("Segoe UI", 8, "bold"))
        lbl_q.pack(side=tk.LEFT, padx=(2, 2))

        self.btn_q_eco = _make_btn(self.toolbar, "Eco", lambda: self._on_select_quality(35), padx=5, font=("Segoe UI", 8))
        self.btn_q_med = _make_btn(self.toolbar, "Media", lambda: self._on_select_quality(65), padx=5, font=("Segoe UI", 8))
        self.btn_q_high = _make_btn(self.toolbar, "HD", lambda: self._on_select_quality(85), padx=5, font=("Segoe UI", 8))
        self.btn_q_med.config(bg="#3b82f6", fg="#ffffff")

        _make_sep(self.toolbar)

        # 6. Privacy Mode
        self.privacy_on = False
        self.btn_privacy = _make_btn(self.toolbar, "🔒 Privacy: OFF", self._toggle_privacy, padx=6)

        _make_sep(self.toolbar)

        # 7. OTA In-Place Update
        self.btn_ota = _make_btn(self.toolbar, "🔄 Aggiorna OTA", self._request_ota_update,
                                 bg="#064e3b", fg="#6ee7b7", h_bg="#047857", h_fg="#ffffff", padx=6)
        if self.ota_update_available:
            self.btn_ota.config(text=f"⚡ Aggiorna v{self.ota_remote_version}", bg="#059669", fg="#ffffff")

        # Right side items
        # Disconnect button
        _make_btn(self.toolbar, "✕ Disconnetti", self._close_viewer, bg="#3b171f", fg="#f87171",
                  h_bg="#ef4444", h_fg="#ffffff", side=tk.RIGHT, padx=9, font=("Segoe UI", 9, "bold"))

        # Collapse toolbar button
        _make_btn(self.toolbar, "▲ Nascondi", self._toggle_toolbar, side=tk.RIGHT, padx=6, font=("Segoe UI", 8))

        # Fullscreen button
        self.btn_fs = _make_btn(self.toolbar, "⛶ Schermo Intero", self._toggle_fullscreen, side=tk.RIGHT, padx=7)

        # Telemetry capsule (FPS + Encryption)
        tele_frame = tk.Frame(self.toolbar, bg="#181c2c", padx=8, pady=3, highlightbackground="#272b3f", highlightthickness=1)
        tele_frame.pack(side=tk.RIGHT, padx=(0, 6))

        self.lbl_fps = tk.Label(tele_frame, text="-- FPS", bg="#181c2c", fg="#a6e3a1", font=("Consolas", 9, "bold"))
        self.lbl_fps.pack(side=tk.LEFT, padx=(0, 5))

        lbl_sec = tk.Label(tele_frame, text="🔒 AES-256", bg="#181c2c", fg="#94a3b8", font=("Segoe UI", 8))
        lbl_sec.pack(side=tk.LEFT)

        # Video Canvas
        self.canvas = tk.Canvas(self.viewer_window, bg="#0c0e14", highlightthickness=0)
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

    def _switch_to_host_panel(self):
        try:
            self.root.destroy()
            from src.host_tray import HostControlPanel
            panel = HostControlPanel()
            panel.run()
        except Exception as e:
            messagebox.showerror("Errore", f"Impossibile avviare il pannello Host: {e}")

    def _render_placeholder(self):
        if not self.canvas or self._canvas_img_id is not None:
            return
        self.canvas.delete("loading")
        c_w = max(self.canvas.winfo_width(), 1280)
        c_h = max(self.canvas.winfo_height(), 720)
        cx, cy = c_w // 2, c_h // 2
        self.canvas.create_text(cx, cy - 35, text="AETHERDESK", fill="#38bdf8", font=("Segoe UI", 24, "bold"), tags="loading")
        self.canvas.create_text(cx, cy + 5, text=f"Sessione E2EE Connessa • Host ID: {self.client.state.target_id}", fill="#cdd6f4", font=("Segoe UI", 11), tags="loading")
        self.canvas.create_text(cx, cy + 30, text="In attesa del desktop remoto...", fill="#64748b", font=("Segoe UI", 9), tags="loading")

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
        self.btn_scale.config(text=txt, bg="#3b82f6" if self.scale_mode == "1:1" else "#1e2235",
                              fg="#ffffff" if self.scale_mode == "1:1" else "#d1d5db")

    def _on_switch_mon(self, mon_id: int):
        self._active_mon = mon_id
        for m, btn in [(0, self.btn_m1), (1, self.btn_m2), (-1, self.btn_mall)]:
            btn.config(bg="#3b82f6" if m == mon_id else "#1e2235",
                       fg="#ffffff" if m == mon_id else "#d1d5db")
        self._switch_monitor(mon_id)

    def _on_select_quality(self, quality: int):
        self._active_quality = quality
        for q, btn in [(35, self.btn_q_eco), (65, self.btn_q_med), (85, self.btn_q_high)]:
            btn.config(bg="#3b82f6" if q == quality else "#1e2235",
                       fg="#ffffff" if q == quality else "#d1d5db")
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
        txt = "🔒 Privacy: ON" if self.privacy_on else "🔒 Privacy: OFF"
        bg = "#10b981" if self.privacy_on else "#1e2235"
        fg = "#ffffff" if self.privacy_on else "#d1d5db"
        self.btn_privacy.config(text=txt, bg=bg, fg=fg)
        asyncio.run_coroutine_threadsafe(self.client.toggle_privacy_mode(self.privacy_on), self.loop)

    def _switch_monitor(self, mon_id: int):
        asyncio.run_coroutine_threadsafe(self.client.switch_monitor(mon_id), self.loop)

    def _pick_and_send_file(self):
        file_path = filedialog.askopenfilename(title="Seleziona file da inviare al PC fisso")
        if not file_path:
            return
        threading.Thread(target=self._send_file_worker, args=(file_path,), daemon=True).start()

    def _send_file_worker(self, file_path: str):
        fn = os.path.basename(file_path)
        self._on_status_update(f"Inizio invio file '{fn}'...")

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
            self.root.after(0, lambda: messagebox.showinfo("File Inviato", f"Il file '{fn}' e' stato inviato al PC fisso nella cartella Download!"))
        except Exception as e:
            self._on_status_update(f"Errore invio file: {e}")

    def _check_git_updates_background(self, notify_if_none: bool = False):
        """Query GitHub Releases API in background to detect new versions automatically."""
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
                    self._on_status_update(f"Nuovo aggiornamento Git disponibile: v{info['tag']}!")
                else:
                    tag = info.get("tag", get_app_version())
                    self.btn_git_update.config(
                        text=f"✓ Git v{tag} (Aggiornato)",
                        bg="#181c2c",
                        fg="#34d399"
                    )
                    if notify_if_none:
                        messagebox.showinfo("Aggiornamento Git", f"AetherDesk e' gia' aggiornato all'ultima versione disponibile su Git (v{tag}).")
            self.root.after(0, update_ui)
        except Exception:
            pass

    def _on_git_update_click(self):
        """Handler for Git auto-update button in login footer."""
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
            self.btn_git_update.config(text="🔄 Git: Controllo...")
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
                    self._on_status_update(f"Download da Git in corso: {pct}%...")
                self.root.after(0, up)

            ok, temp_path_or_err = download_github_binary(download_url, progress_callback=on_progress)
            if not ok:
                self.ota_downloading = False
                def err():
                    messagebox.showerror("Errore Download Git", temp_path_or_err)
                    if self.btn_git_update:
                        self.btn_git_update.config(text="🔄 Riprova Git", state=tk.NORMAL, bg="#181c2c", fg="#f87171")
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
                c_h = max((self.viewer_window.winfo_height() - 42) if self.viewer_window else 720, 720)

            img = Image.open(io.BytesIO(jpeg_bytes))
            # Resize keeping aspect ratio with high quality lanczos filter
            if getattr(self, "scale_mode", "fit") == "fit":
                img.thumbnail((c_w, c_h), Image.Resampling.LANCZOS)
            self.current_img_dims = img.size
            photo = ImageTk.PhotoImage(img)
            self.photo_img = photo
            # Center on canvas without delete('all') churn
            pos_x = (c_w - img.width) // 2
            pos_y = (c_h - img.height) // 2
            if self._canvas_img_id is None:
                self.canvas.delete("loading")
                self._canvas_img_id = self.canvas.create_image(pos_x, pos_y, anchor="nw", image=photo)
            else:
                self.canvas.coords(self._canvas_img_id, pos_x, pos_y)
                self.canvas.itemconfig(self._canvas_img_id, image=photo)
        except Exception as e:
            logger.error(f"Render frame error: {e}")

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
        nx, ny = self._calc_norm_coords(event)
        cmd = {
            "type": "mouse_double_click",
            "button": button,
            "x": nx,
            "y": ny
        }
        asyncio.run_coroutine_threadsafe(self.client.send_input(cmd), self.loop)

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
        """
    )
    parser.add_argument("-c", "--connect", dest="target_id", default=None, help="Target computer ID to connect to (e.g., 315747031)")
    parser.add_argument("-p", "--password", default=None, help="Password for remote authentication")
    parser.add_argument("-b", "--broker", default=None, help="Signaling broker WebSocket URL")
    parser.add_argument("--host", action="store_true", help="Launch AetherDesk Host Control Panel")
    parser.add_argument("-v", "--version", action="version", version=f"AetherDesk Client v{get_app_version()}")
    args = parser.parse_args()

    if args.host:
        from src.host_tray import HostControlPanel
        panel = HostControlPanel()
        panel.run()
        sys.exit(0)

    gui = ClientViewerGUI(broker_url=args.broker if args.broker else "ws://127.0.0.1:8765")
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
