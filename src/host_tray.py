"""
AetherDesk Host Control Panel (Tray UI).

Architectural Boundary:
- GUI Layer: Tkinter desktop panel running on the main UI thread.
- Host Session Layer: Dedicated daemon thread running HostDaemon with its own event loop and embedded broker.
- Threadsafe Control: UI triggers start/stop via threadsafe HostDaemon lifecycle methods with explicit timeouts.
"""

import os
import sys
import socket
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
from tkinter import ttk, messagebox
import threading
import asyncio

from src.security import SecurityManager
from src.host_daemon import HostDaemon
from src.signaling_broker import SignalingBroker
from src.ota import (
    get_app_version,
    cleanup_old_versions,
    confirm_boot,
    check_boot_state,
    OTAUpdateState,
    check_github_update,
    download_github_binary,
    apply_in_place_update
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
    "warning": "#ff9f0a",         # Apple System Orange / Amber
    "danger": "#ff453a",          # Apple System Red
    "danger_hover": "#d73a31",    # Apple Red pressed
    "danger_bg": "#331618",       # Red badge container
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
    entry_widget.bind("<FocusIn>", lambda e: container_frame.config(highlightbackground=focus_border))
    entry_widget.bind("<FocusOut>", lambda e: container_frame.config(highlightbackground=normal_border))


class HostControlPanel:
    """Desktop Host Control Panel with Apple HIG dark-mode Bento architecture."""

    @staticmethod
    def _center_window(win: tk.Tk, width: int, height: int):
        try:
            win.update_idletasks()
            sw = win.winfo_screenwidth()
            sh = win.winfo_screenheight()
            x = max(0, (sw - width) // 2)
            y = max(0, (sh - height) // 2)
            win.geometry(f"{width}x{height}+{x}+{y}")
        except Exception:
            win.geometry(f"{width}x{height}")

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("AetherDesk - Pannello Host")
        self._center_window(self.root, 520, 680)
        self.root.configure(bg=THEME["bg"])
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Set official window icon
        icon_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "assets", "icon.png"))
        if os.path.exists(icon_path):
            try:
                self.icon_photo = tk.PhotoImage(file=icon_path)
                self.root.iconphoto(True, self.icon_photo)
            except Exception:
                pass

        # Failsafe Boot Confirmation: check if running in TESTING state
        if check_boot_state() == OTAUpdateState.TESTING:
            self.root.after(2500, self._verify_and_confirm_ota_boot)

        self.security = SecurityManager()
        self.daemon = None
        self.daemon_thread = None
        self.local_broker = None
        self.pwd_visible = False
        self.btn_header_update = None
        self.git_update_info = None
        self.ota_downloading = False

        self._build_ui()
        self._toggle_daemon()
        self._update_status()

        threading.Thread(target=lambda: self._check_git_updates_background(notify_if_none=False, prompt_on_found=True), daemon=True).start()

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

    def _build_ui(self):
        container = tk.Frame(self.root, bg=THEME["bg"], padx=26, pady=20)
        container.pack(fill=tk.BOTH, expand=True)

        # 1. Header Bar
        header_bar = tk.Frame(container, bg=THEME["bg"])
        header_bar.pack(fill=tk.X, pady=(0, 16))

        title_row = tk.Frame(header_bar, bg=THEME["bg"])
        title_row.pack(anchor="w")

        lbl_dot = tk.Label(title_row, text="●", bg=THEME["bg"], fg=THEME["success"], font=("Segoe UI", 11, "bold"))
        lbl_dot.pack(side=tk.LEFT, padx=(0, 6))

        lbl_brand = tk.Label(title_row, text="AETHERDESK HOST", bg=THEME["bg"], fg=THEME["text_main"],
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

        lbl_sub = tk.Label(header_bar, text="Host Non Presidiato • In ascolto connessioni remote",
                           bg=THEME["bg"], fg=THEME["text_dim"], font=("Segoe UI", 9))
        lbl_sub.pack(anchor="w", pady=(2, 0))

        # 2. Bento Card: Device ID (Apple HIG Grouped Card)
        id_card = tk.Frame(container, bg=THEME["card"], padx=18, pady=14,
                           highlightthickness=1, highlightbackground=THEME["card_border"])
        id_card.pack(fill=tk.X, pady=(0, 10))

        lbl_id_title = tk.Label(id_card, text="●  Il Tuo ID Dispositivo (da dare al client)", bg=THEME["card"],
                                fg=THEME["accent"], font=("Segoe UI", 9, "bold"))
        lbl_id_title.pack(anchor="w", pady=(0, 6))

        id_val_box = tk.Frame(id_card, bg=THEME["input_bg"], padx=14, pady=8,
                              highlightthickness=1, highlightbackground=THEME["input_border"])
        id_val_box.pack(fill=tk.X)

        dev_id = self.security.get_or_create_device_id()
        self.lbl_id = tk.Label(id_val_box, text=dev_id, bg=THEME["input_bg"], fg=THEME["warning"],
                               font=("Consolas", 22, "bold"))
        self.lbl_id.pack(side=tk.LEFT)

        self.btn_copy_id_widget = tk.Button(id_val_box, text="📋 Copia ID", bg=THEME["card_hover"], fg=THEME["text_main"],
                                            font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2", padx=12, pady=4,
                                            highlightthickness=1, highlightbackground=THEME["card_border"],
                                            command=lambda: self._copy_id(btn_widget=self.btn_copy_id_widget))
        self.btn_copy_id_widget.pack(side=tk.RIGHT)
        bind_hover(self.btn_copy_id_widget, THEME["card_hover"], THEME["card_border"], THEME["text_main"], THEME["cyan"])

        # 3. Bento Card: Password Config
        pwd_card = tk.Frame(container, bg=THEME["card"], padx=18, pady=12,
                            highlightthickness=1, highlightbackground=THEME["card_border"])
        pwd_card.pack(fill=tk.X, pady=(0, 10))

        lbl_pwd_title = tk.Label(pwd_card, text="●  Password di Accesso Remoto", bg=THEME["card"],
                                 fg=THEME["text_muted"], font=("Segoe UI", 9, "bold"))
        lbl_pwd_title.pack(anchor="w", pady=(0, 6))

        pwd_row = tk.Frame(pwd_card, bg=THEME["input_bg"], highlightthickness=1,
                           highlightbackground=THEME["input_border"], padx=8, pady=3)
        pwd_row.pack(fill=tk.X)

        self.entry_pwd = tk.Entry(pwd_row, show="*", font=("Segoe UI", 11), bg=THEME["input_bg"],
                                  fg=THEME["text_main"], insertbackground="white", relief="flat")
        self.entry_pwd.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3)
        self.entry_pwd.bind("<Return>", lambda e: self._save_password())
        self.entry_pwd.insert(0, self.security.get_password_display())
        bind_focus_ring(self.entry_pwd, pwd_row)

        self.btn_eye = tk.Button(pwd_row, text="👁", bg=THEME["card_hover"], fg=THEME["text_muted"],
                                 font=("Segoe UI", 9), relief="flat", cursor="hand2", padx=6,
                                 command=self._toggle_pwd_view)
        self.btn_eye.pack(side=tk.LEFT, padx=(0, 4))
        bind_hover(self.btn_eye, THEME["card_hover"], THEME["card_border"], THEME["text_muted"], THEME["text_main"])

        btn_copy_pwd = tk.Button(pwd_row, text="📋 Copia", bg=THEME["card_hover"], fg=THEME["text_muted"],
                                 font=("Segoe UI", 9), relief="flat", cursor="hand2", padx=6,
                                 command=lambda: self._copy_text(self.entry_pwd.get(), "Password", btn_widget=btn_copy_pwd))
        btn_copy_pwd.pack(side=tk.LEFT, padx=(0, 6))
        bind_hover(btn_copy_pwd, THEME["card_hover"], THEME["card_border"], THEME["text_muted"], THEME["text_main"])

        self.btn_save_pwd = tk.Button(pwd_row, text="Salva Password", bg=THEME["accent"], fg="#ffffff",
                                      font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2", padx=10, pady=3,
                                      command=self._save_password)
        self.btn_save_pwd.pack(side=tk.RIGHT)
        bind_hover(self.btn_save_pwd, THEME["accent"], THEME["accent_hover"], "#ffffff", "#ffffff")

        # 4. Bento Card: Network Endpoints
        net_card = tk.Frame(container, bg=THEME["card"], padx=18, pady=12,
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

        # 5. Broker Checkbox & Url
        broker_box = tk.Frame(container, bg=THEME["card"], padx=18, pady=10,
                              highlightthickness=1, highlightbackground=THEME["card_border"])
        broker_box.pack(fill=tk.X, pady=(0, 10))

        self.var_local_broker = tk.BooleanVar(value=True)
        self.chk_local_broker = tk.Checkbutton(
            broker_box, text="Avvia Server Broker Locale integrato (Porta 8765)",
            variable=self.var_local_broker, bg=THEME["card"], fg=THEME["text_main"],
            selectcolor=THEME["input_bg"], activebackground=THEME["card"],
            activeforeground=THEME["text_main"], font=("Segoe UI", 9)
        )
        self.chk_local_broker.pack(anchor="w", pady=(0, 4))

        brk_url_row = tk.Frame(broker_box, bg=THEME["card"])
        brk_url_row.pack(fill=tk.X)

        lbl_bu = tk.Label(brk_url_row, text="URL Ascolto:", bg=THEME["card"], fg=THEME["text_dim"], font=("Segoe UI", 9))
        lbl_bu.pack(side=tk.LEFT, padx=(0, 6))

        self.entry_broker = tk.Entry(brk_url_row, font=("Consolas", 9), bg=THEME["input_bg"], fg=THEME["text_main"],
                                     insertbackground="white", relief="flat")
        self.entry_broker.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3)
        self.entry_broker.insert(0, "ws://127.0.0.1:8765")

        # 6. Status & Controls
        self.lbl_status = tk.Label(container, text="● In ascolto (Nessun client connesso)", bg=THEME["bg"],
                                   fg=THEME["cyan"], font=("Segoe UI", 9, "bold"))
        self.lbl_status.pack(pady=(4, 8))

        btn_row = tk.Frame(container, bg=THEME["bg"])
        btn_row.pack(fill=tk.X)

        self.btn_daemon = tk.Button(btn_row, text="Arresta Servizio", bg=THEME["danger"], fg="#ffffff",
                                    activebackground=THEME["danger_hover"], activeforeground="#ffffff",
                                    font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2",
                                    command=self._toggle_daemon)
        self.btn_daemon.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6, padx=(0, 4))
        bind_hover(self.btn_daemon, THEME["danger"], THEME["danger_hover"], "#ffffff", "#ffffff")

        btn_fw = tk.Button(btn_row, text="🛡 Sblocca Firewall", bg=THEME["card"], fg=THEME["warning"],
                           activebackground=THEME["card_hover"], activeforeground="#ffffff",
                           font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2",
                           highlightthickness=1, highlightbackground=THEME["card_border"],
                           command=self._open_firewall_unblock)
        btn_fw.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6, padx=4)
        bind_hover(btn_fw, THEME["card"], THEME["card_hover"], THEME["warning"], "#fde047")

        btn_svc = tk.Button(btn_row, text="⚙ Installa Servizio", bg=THEME["card"], fg=THEME["text_muted"],
                            activebackground=THEME["card_hover"], activeforeground="#ffffff",
                            font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2",
                            highlightthickness=1, highlightbackground=THEME["card_border"],
                            command=self._open_installer)
        btn_svc.pack(side=tk.RIGHT, fill=tk.X, expand=True, ipady=6, padx=(4, 0))
        bind_hover(btn_svc, THEME["card"], THEME["card_hover"], THEME["text_muted"], THEME["text_main"])

    def _open_firewall_unblock(self):
        bat_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts", "allow_firewall.bat"))
        if os.path.exists(bat_path):
            os.startfile(bat_path)
        else:
            messagebox.showerror("Errore", f"File non trovato: {bat_path}")

    def _toggle_pwd_view(self):
        self.pwd_visible = not self.pwd_visible
        self.entry_pwd.config(show="" if self.pwd_visible else "*")
        self.btn_eye.config(text="🙈" if self.pwd_visible else "👁")

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

    def _copy_id(self, btn_widget: tk.Button = None):
        dev_id = self.security.get_or_create_device_id()
        self._copy_text(dev_id, "ID Dispositivo", btn_widget=btn_widget or getattr(self, "btn_copy_id_widget", None))

    def _save_password(self):
        new_pwd = self.entry_pwd.get().strip()
        if not new_pwd:
            messagebox.showwarning("Attenzione", "Inserisci una password valida non vuota.")
            return
        self.security.set_password(new_pwd)
        self.entry_pwd.delete(0, tk.END)
        if hasattr(self, "btn_save_pwd") and self.btn_save_pwd:
            orig_text = self.btn_save_pwd.cget("text")
            self.btn_save_pwd.config(text="✓ Salvata!", bg=THEME["success"])
            def _revert_pwd():
                try:
                    self.btn_save_pwd.config(text=orig_text, bg=THEME["accent"])
                except Exception:
                    pass
            self.root.after(1500, _revert_pwd)

    def _on_close(self):
        try:
            if self.daemon:
                self.daemon.stop(timeout=2.0)
            if self.daemon_thread and self.daemon_thread.is_alive():
                self.daemon_thread.join(timeout=2.0)
                if not self.daemon_thread.is_alive():
                    self.daemon_thread = None
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def _toggle_daemon(self):
        if not self.daemon or not self.daemon.running:
            broker_url = self.entry_broker.get().strip() or "ws://127.0.0.1:8765"
            self.daemon = HostDaemon(broker_url=broker_url)
            self.daemon_thread = threading.Thread(target=self._run_daemon, daemon=True)
            self.daemon_thread.start()
            self.btn_daemon.config(text="Arresta Servizio", bg=THEME["danger"])
            self.lbl_status.config(text="● Servizio Host Attivo (Pronto all'ascolto)", fg=THEME["success"])
        else:
            if self.daemon:
                self.daemon.stop(timeout=2.0)
                self.daemon = None
            if self.daemon_thread and self.daemon_thread.is_alive():
                self.daemon_thread.join(timeout=2.0)
                if not self.daemon_thread.is_alive():
                    self.daemon_thread = None
            self.local_broker = None
            self.btn_daemon.config(text="Avvia Servizio", bg=THEME["accent"])
            self.lbl_status.config(text="● Servizio Host Arrestato", fg=THEME["danger"])

    def _run_daemon(self):
        try:
            asyncio.run(self.daemon.start())
        except Exception as e:
            pass

    def _open_installer(self):
        base_dir = getattr(sys, "_MEIPASS", os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
        bat_path = os.path.join(base_dir, "scripts", "install_service.bat")
        if not os.path.exists(bat_path):
            bat_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts", "install_service.bat"))
        if os.path.exists(bat_path):
            os.startfile(bat_path)
        else:
            messagebox.showerror("Errore", f"File non trovato: {bat_path}")

    def _update_status(self):
        if self.daemon and self.daemon.running:
            clients = len(self.daemon.connected_clients)
            if clients > 0:
                self.lbl_status.config(text=f"● Connesso ({clients} client attivo/i)", fg=THEME["success"])
            else:
                self.lbl_status.config(text="● In ascolto su porta 8765 (Pronto)", fg=THEME["cyan"])
        else:
            self.lbl_status.config(text="● Servizio Host Arrestato", fg=THEME["danger"])
        self.root.after(2000, self._update_status)

    def _on_git_update_click(self):
        if getattr(self, "ota_downloading", False):
            return
        if self.git_update_info and self.git_update_info.get("download_url"):
            info = self.git_update_info
            tag = info.get("tag", "")
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
            threading.Thread(
                target=lambda: self._check_git_updates_background(notify_if_none=True, prompt_on_found=True),
                daemon=True
            ).start()

    def _check_git_updates_background(self, notify_if_none: bool = False, prompt_on_found: bool = False):
        try:
            if self.btn_header_update:
                self.root.after(0, lambda: self.btn_header_update.config(text="🔄 Controllo...", state=tk.DISABLED))

            is_newer, info = check_github_update()

            def update_ui():
                if self.btn_header_update:
                    self.btn_header_update.config(state=tk.NORMAL)

                if is_newer and info.get("download_url"):
                    self.git_update_info = info
                    tag = info.get("tag", "")
                    if self.btn_header_update:
                        self.btn_header_update.config(
                            text=f"⚡ Aggiorna a v{tag}",
                            bg="#059669",
                            fg="#ffffff"
                        )
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
                    err = info.get("error")
                    if err:
                        if self.btn_header_update:
                            self.btn_header_update.config(text="🔄 Riprova", bg="#161b2b", fg="#f87171")
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
                        if notify_if_none:
                            messagebox.showinfo(
                                "Aggiornamenti AetherDesk",
                                f"AetherDesk e' gia' aggiornato all'ultima versione disponibile (v{get_app_version()})."
                            )
            self.root.after(0, update_ui)
        except Exception as e:
            if self.btn_header_update:
                self.root.after(0, lambda: self.btn_header_update.config(text="🔄 Riprova", state=tk.NORMAL, fg="#f87171"))

    def _start_git_download(
        self,
        download_url: str,
        target_version: str,
        expected_sha256: Optional[str] = None,
        signed_manifest: Optional[dict] = None
    ):
        self.ota_downloading = True
        if self.btn_header_update:
            self.btn_header_update.config(text="⬇ Scaricamento...", state=tk.DISABLED)

        def worker():
            def on_progress(rec, tot):
                pct = int((rec / tot) * 100) if tot > 0 else 0
                txt = f"⬇ {pct}%"
                self.root.after(0, lambda: self.btn_header_update.config(text=txt) if self.btn_header_update else None)

            success, temp_path_or_err = download_github_binary(
                download_url,
                progress_callback=on_progress,
                expected_sha256=expected_sha256,
                signed_manifest=signed_manifest
            )
            if not success:
                self.ota_downloading = False
                self.root.after(0, lambda: messagebox.showerror("Errore Download", f"Impossibile scaricare l'aggiornamento:\n{temp_path_or_err}"))
                self.root.after(0, lambda: self.btn_header_update.config(text="🔄 Riprova", state=tk.NORMAL, bg="#161b2b", fg="#f87171") if self.btn_header_update else None)
                return

            applied, app_msg = apply_in_place_update(
                temp_path_or_err,
                target_version=target_version,
                expected_sha256=expected_sha256,
                signed_manifest=signed_manifest
            )
            self.ota_downloading = False
            if applied:
                logger.info(f"HostTray: Aggiornamento applicato ({app_msg}). Arresto ordinato per riavvio.")
                try:
                    if self.daemon:
                        self.daemon.stop()
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
                self.root.after(0, lambda: messagebox.showerror("Errore Installazione", f"Impossibile applicare l'aggiornamento:\n{app_msg}"))
                self.root.after(0, lambda: self.btn_header_update.config(text="🔄 Riprova", state=tk.NORMAL) if self.btn_header_update else None)

        threading.Thread(target=worker, daemon=True).start()

    def _verify_and_confirm_ota_boot(self):
        try:
            if self.root and (self.daemon is None or self.daemon.running):
                confirmed = confirm_boot()
                if confirmed:
                    logger.info("HostTray: Aggiornamento OTA confermato con successo al boot.")
        except Exception as e:
            logger.error(f"HostTray: Errore durante la conferma del boot: {e}")

    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    app = HostControlPanel()
    app.run()
