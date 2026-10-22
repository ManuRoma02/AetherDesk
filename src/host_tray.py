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
from src.ota import get_app_version, cleanup_old_versions

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
    "warning": "#f59e0b",         # Amber
    "danger": "#ef4444",          # Crimson disconnect
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

        # Set official window icon
        icon_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "assets", "icon.png"))
        if os.path.exists(icon_path):
            try:
                self.icon_photo = tk.PhotoImage(file=icon_path)
                self.root.iconphoto(True, self.icon_photo)
            except Exception:
                pass

        try:
            cleanup_old_versions()
        except Exception:
            pass

        self.security = SecurityManager()
        self.daemon = None
        self.daemon_thread = None
        self.local_broker = None
        self.pwd_visible = False

        self._build_ui()
        self._toggle_daemon()
        self._update_status()

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

        lbl_dot = tk.Label(title_row, text="●", bg=THEME["bg"], fg=THEME["cyan"], font=("Segoe UI", 12, "bold"))
        lbl_dot.pack(side=tk.LEFT, padx=(0, 6))

        lbl_brand = tk.Label(title_row, text="AETHERDESK HOST", bg=THEME["bg"], fg=THEME["text_main"],
                             font=("Segoe UI", 16, "bold"))
        lbl_brand.pack(side=tk.LEFT)

        lbl_badge = tk.Label(title_row, text=f"v{get_app_version()}", bg="#161b2b", fg=THEME["cyan"],
                             font=("Consolas", 8, "bold"), padx=6, pady=1)
        lbl_badge.pack(side=tk.LEFT, padx=(8, 0))

        lbl_sub = tk.Label(header_bar, text="Host Non Presidiato • In ascolto connessioni remote",
                           bg=THEME["bg"], fg=THEME["text_dim"], font=("Segoe UI", 9))
        lbl_sub.pack(anchor="w", pady=(1, 0))

        # 2. Bento Card: Device ID
        id_card = tk.Frame(container, bg=THEME["card"], padx=18, pady=14,
                           highlightthickness=1, highlightbackground=THEME["card_border"])
        id_card.pack(fill=tk.X, pady=(0, 10))

        lbl_id_title = tk.Label(id_card, text="●  IL TUO ID DISPOSITIVO (DA DARE AL CLIENT)", bg=THEME["card"],
                                fg=THEME["cyan"], font=("Segoe UI", 8, "bold"))
        lbl_id_title.pack(anchor="w", pady=(0, 6))

        id_val_box = tk.Frame(id_card, bg="#0d101a", padx=12, pady=8,
                              highlightthickness=1, highlightbackground=THEME["input_border"])
        id_val_box.pack(fill=tk.X)

        dev_id = self.security.get_or_create_device_id()
        self.lbl_id = tk.Label(id_val_box, text=dev_id, bg="#0d101a", fg="#fbbf24",
                               font=("Consolas", 22, "bold"))
        self.lbl_id.pack(side=tk.LEFT)

        btn_copy = tk.Button(id_val_box, text="📋 Copia ID", bg="#1b2032", fg=THEME["text_main"],
                             font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2", padx=10, pady=3,
                             command=self._copy_id)
        btn_copy.pack(side=tk.RIGHT)
        bind_hover(btn_copy, "#1b2032", "#262d44", THEME["text_main"], THEME["cyan"])

        # 3. Bento Card: Password Config
        pwd_card = tk.Frame(container, bg=THEME["card"], padx=18, pady=12,
                            highlightthickness=1, highlightbackground=THEME["card_border"])
        pwd_card.pack(fill=tk.X, pady=(0, 10))

        lbl_pwd_title = tk.Label(pwd_card, text="●  PASSWORD DI ACCESSO REMOTO", bg=THEME["card"],
                                 fg=THEME["text_muted"], font=("Segoe UI", 8, "bold"))
        lbl_pwd_title.pack(anchor="w", pady=(0, 6))

        pwd_row = tk.Frame(pwd_card, bg=THEME["input_bg"], highlightthickness=1,
                           highlightbackground=THEME["input_border"], padx=8, pady=3)
        pwd_row.pack(fill=tk.X)

        self.entry_pwd = tk.Entry(pwd_row, show="*", font=("Segoe UI", 11), bg=THEME["input_bg"],
                                  fg=THEME["text_main"], insertbackground="white", relief="flat")
        self.entry_pwd.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3)
        self.entry_pwd.bind("<Return>", lambda e: self._save_password())
        bind_focus_ring(self.entry_pwd, pwd_row)

        self.btn_eye = tk.Button(pwd_row, text="👁", bg="#1b2032", fg=THEME["text_muted"],
                                 font=("Segoe UI", 9), relief="flat", cursor="hand2", padx=6,
                                 command=self._toggle_pwd_view)
        self.btn_eye.pack(side=tk.LEFT, padx=(0, 6))
        bind_hover(self.btn_eye, "#1b2032", "#262d44", THEME["text_muted"], THEME["text_main"])

        btn_save_pwd = tk.Button(pwd_row, text="Salva Password", bg="#10b981", fg="#ffffff",
                                 font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2", padx=10, pady=2,
                                 command=self._save_password)
        btn_save_pwd.pack(side=tk.RIGHT)
        bind_hover(btn_save_pwd, "#10b981", "#059669", "#ffffff", "#ffffff")

        # 4. Bento Card: Network Endpoints
        net_card = tk.Frame(container, bg=THEME["card"], padx=18, pady=12,
                            highlightthickness=1, highlightbackground=THEME["card_border"])
        net_card.pack(fill=tk.X, pady=(0, 10))

        lbl_net_title = tk.Label(net_card, text="●  INDIRIZZI BROKER DA INSERIRE NEL CLIENT", bg=THEME["card"],
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

        self.btn_daemon = tk.Button(btn_row, text="Arresta Demone", bg="#ef4444", fg="#ffffff",
                                    font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2",
                                    command=self._toggle_daemon)
        self.btn_daemon.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6, padx=(0, 4))
        bind_hover(self.btn_daemon, "#ef4444", "#dc2626", "#ffffff", "#ffffff")

        btn_fw = tk.Button(btn_row, text="🛡 Sblocca Firewall", bg="#1b2032", fg=THEME["warning"],
                           font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2",
                           command=self._open_firewall_unblock)
        btn_fw.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6, padx=4)
        bind_hover(btn_fw, "#1b2032", "#26221c", THEME["warning"], "#fde047")

        btn_svc = tk.Button(btn_row, text="⚙ Installa Servizio", bg="#1b2032", fg=THEME["text_muted"],
                            font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2",
                            command=self._open_installer)
        btn_svc.pack(side=tk.RIGHT, fill=tk.X, expand=True, ipady=6, padx=(4, 0))
        bind_hover(btn_svc, "#1b2032", "#262d44", THEME["text_muted"], THEME["text_main"])

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

    def _copy_text(self, text: str, label: str = "Testo"):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        messagebox.showinfo("Copiato", f"{label} copiato negli appunti:\n{text}")

    def _copy_id(self):
        dev_id = self.security.get_or_create_device_id()
        self._copy_text(dev_id, "ID Dispositivo")

    def _save_password(self):
        new_pwd = self.entry_pwd.get().strip()
        if not new_pwd:
            messagebox.showwarning("Attenzione", "Inserisci una password valida non vuota.")
            return
        self.security.set_password(new_pwd)
        self.entry_pwd.delete(0, tk.END)
        messagebox.showinfo("Successo", "Password di accesso remoto salvata con successo!")

    def _toggle_daemon(self):
        if not self.daemon or not self.daemon.running:
            broker_url = self.entry_broker.get().strip() or "ws://127.0.0.1:8765"
            self.daemon = HostDaemon(broker_url=broker_url)
            self.daemon_thread = threading.Thread(target=self._run_daemon, daemon=True)
            self.daemon_thread.start()
            self.btn_daemon.config(text="Arresta Demone", bg="#ef4444")
            self.lbl_status.config(text="● Servizio Host Attivo (Pronto all'ascolto)", fg=THEME["success"])
        else:
            self.daemon.stop()
            if self.local_broker:
                try:
                    asyncio.run(self.local_broker.stop())
                except Exception:
                    pass
                self.local_broker = None
            self.btn_daemon.config(text="Avvia Demone", bg=THEME["accent"])
            self.lbl_status.config(text="● Servizio Host Arrestato", fg=THEME["danger"])

    def _run_daemon(self):
        async def runner():
            if self.var_local_broker.get():
                try:
                    self.local_broker = SignalingBroker(host="0.0.0.0", port=8765)
                    await self.local_broker.start()
                except Exception:
                    pass
            await self.daemon.start()
        asyncio.run(runner())

    def _open_installer(self):
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

    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    app = HostControlPanel()
    app.run()
