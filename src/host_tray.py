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

class HostControlPanel:
    """Desktop Host Control Panel to view Device ID, configure password and monitor status."""

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
        self._center_window(self.root, 520, 630)
        self.root.configure(bg="#0a0c14")
        self.root.resizable(False, False)

        # Set official window icon
        icon_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "assets", "icon.png"))
        if os.path.exists(icon_path):
            try:
                self.icon_photo = tk.PhotoImage(file=icon_path)
                self.root.iconphoto(True, self.icon_photo)
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
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TLabel", background="#0a0c14", foreground="#cdd6f4", font=("Segoe UI", 10))
        style.configure("Header.TLabel", background="#0a0c14", foreground="#38bdf8", font=("Segoe UI", 18, "bold"))
        style.configure("SubHeader.TLabel", background="#0a0c14", foreground="#94a3b8", font=("Segoe UI", 9))
        style.configure("ID.TLabel", background="#141726", foreground="#38bdf8", font=("Consolas", 22, "bold"))

        container = tk.Frame(self.root, bg="#0a0c14", padx=25, pady=16)
        container.pack(fill=tk.BOTH, expand=True)

        header = ttk.Label(container, text="AETHERDESK HOST", style="Header.TLabel")
        header.pack(pady=(0, 2))

        sub = ttk.Label(container, text="Host Non Presidiato - Pronto all'ascolto", style="SubHeader.TLabel")
        sub.pack(pady=(0, 10))

        # Device ID Card
        id_frame = tk.Frame(container, bg="#141726", padx=15, pady=10, highlightbackground="#282e44", highlightthickness=1)
        id_frame.pack(fill=tk.X, pady=(0, 10))

        lbl_id_title = tk.Label(id_frame, text="IL TUO ID DISPOSITIVO:", bg="#141726", fg="#94a3b8", font=("Segoe UI", 9, "bold"))
        lbl_id_title.pack(anchor="w")

        id_row = tk.Frame(id_frame, bg="#141726")
        id_row.pack(fill=tk.X, pady=2)

        dev_id = self.security.get_or_create_device_id()
        self.lbl_id = tk.Label(id_row, text=dev_id, bg="#141726", fg="#fbbf24", font=("Consolas", 20, "bold"))
        self.lbl_id.pack(side=tk.LEFT)

        btn_copy = tk.Button(id_row, text="Copia ID", bg="#1e293b", fg="#cdd6f4", relief="flat",
                             font=("Segoe UI", 9), cursor="hand2", command=self._copy_id)
        btn_copy.pack(side=tk.RIGHT)

        # Network Endpoints Card (Where to find server URL for Client)
        net_frame = tk.Frame(container, bg="#141726", padx=15, pady=10, highlightbackground="#282e44", highlightthickness=1)
        net_frame.pack(fill=tk.X, pady=(0, 10))

        lbl_net_title = tk.Label(net_frame, text="INDIRIZZI BROKER DA INSERIRE NEL CLIENT:", bg="#141726", fg="#94a3b8", font=("Segoe UI", 9, "bold"))
        lbl_net_title.pack(anchor="w", pady=(0, 4))

        for net_label, net_url in self._get_network_endpoints():
            row = tk.Frame(net_frame, bg="#141726")
            row.pack(fill=tk.X, pady=2)
            lbl_l = tk.Label(row, text=f"{net_label}:", bg="#141726", fg="#cdd6f4", font=("Segoe UI", 8, "bold"), width=18, anchor="w")
            lbl_l.pack(side=tk.LEFT)
            lbl_u = tk.Label(row, text=net_url, bg="#181c2c", fg="#34d399", font=("Consolas", 9), padx=6, pady=2)
            lbl_u.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
            btn_cp_u = tk.Button(row, text="Copia", bg="#1e293b", fg="#cdd6f4", relief="flat", font=("Segoe UI", 8),
                                 cursor="hand2", command=lambda u=net_url: self._copy_text(u, "URL Server"))
            btn_cp_u.pack(side=tk.RIGHT)

        # Password Config Card
        pwd_frame = tk.Frame(container, bg="#141726", padx=15, pady=10, highlightbackground="#282e44", highlightthickness=1)
        pwd_frame.pack(fill=tk.X, pady=(0, 10))

        lbl_pwd_title = tk.Label(pwd_frame, text="PASSWORD DI ACCESSO REMOTO:", bg="#141726", fg="#94a3b8", font=("Segoe UI", 9, "bold"))
        lbl_pwd_title.pack(anchor="w", pady=(0, 4))

        pwd_input_frame = tk.Frame(pwd_frame, bg="#141726")
        pwd_input_frame.pack(fill=tk.X)

        self.entry_pwd = tk.Entry(pwd_input_frame, show="*", font=("Segoe UI", 11), bg="#181c2c", fg="#f1f5f9",
                                  insertbackground="white", relief="flat")
        self.entry_pwd.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4, padx=(0, 4))
        self.entry_pwd.bind("<Return>", lambda e: self._save_password())

        self.btn_eye = tk.Button(pwd_input_frame, text="👁", bg="#1e293b", fg="#cdd6f4", font=("Segoe UI", 9),
                                 relief="flat", cursor="hand2", command=self._toggle_pwd_view)
        self.btn_eye.pack(side=tk.LEFT, ipady=2, padx=(0, 6))

        btn_save_pwd = tk.Button(pwd_input_frame, text="Salva Password", bg="#10b981", fg="#ffffff",
                                 font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2", command=self._save_password)
        btn_save_pwd.pack(side=tk.RIGHT, ipady=3, padx=2)

        # Broker Config Card
        broker_frame = tk.Frame(container, bg="#141726", padx=15, pady=8, highlightbackground="#282e44", highlightthickness=1)
        broker_frame.pack(fill=tk.X, pady=(0, 10))

        lbl_brk_title = tk.Label(broker_frame, text="OPZIONI SERVER BROKER:", bg="#141726", fg="#94a3b8", font=("Segoe UI", 9, "bold"))
        lbl_brk_title.pack(anchor="w", pady=(0, 2))

        self.var_local_broker = tk.BooleanVar(value=True)
        self.chk_local_broker = tk.Checkbutton(broker_frame, text="Avvia Server Broker Locale integrato (Porta 8765)",
                                               variable=self.var_local_broker, bg="#141726", fg="#cdd6f4",
                                               selectcolor="#181c2c", activebackground="#141726",
                                               activeforeground="#cdd6f4", font=("Segoe UI", 9))
        self.chk_local_broker.pack(anchor="w", pady=(0, 2))

        brk_url_frame = tk.Frame(broker_frame, bg="#141726")
        brk_url_frame.pack(fill=tk.X)
        lbl_brk_url = tk.Label(brk_url_frame, text="URL Ascolto:", bg="#141726", fg="#94a3b8", font=("Segoe UI", 9))
        lbl_brk_url.pack(side=tk.LEFT, padx=(0, 5))
        self.entry_broker = tk.Entry(brk_url_frame, font=("Segoe UI", 9), bg="#181c2c", fg="#f1f5f9",
                                     insertbackground="white", relief="flat")
        self.entry_broker.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3)
        self.entry_broker.insert(0, "ws://127.0.0.1:8765")

        # Status & Controls
        self.lbl_status = tk.Label(container, text="Stato: In ascolto (Nessun client connesso)", bg="#0a0c14", fg="#38bdf8",
                                   font=("Segoe UI", 10, "bold"))
        self.lbl_status.pack(pady=(2, 8))

        btn_frame = tk.Frame(container, bg="#0a0c14")
        btn_frame.pack(fill=tk.X)

        self.btn_daemon = tk.Button(btn_frame, text="Arresta Demone", bg="#ef4444", fg="#ffffff",
                                    font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2", command=self._toggle_daemon)
        self.btn_daemon.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=5, padx=(0, 3))

        btn_fw = tk.Button(btn_frame, text="Sblocca Firewall", bg="#f59e0b", fg="#0a0c14",
                           font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2", command=self._open_firewall_unblock)
        btn_fw.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=5, padx=3)

        btn_svc = tk.Button(btn_frame, text="Installa Servizio", bg="#64748b", fg="#ffffff",
                            font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2", command=self._open_installer)
        btn_svc.pack(side=tk.RIGHT, fill=tk.X, expand=True, ipady=5, padx=(3, 0))

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
            self.btn_daemon.config(text="Arresta Demone", bg="#f38ba8")
            self.lbl_status.config(text="Stato: Demone in esecuzione (Pronto)", fg="#a6e3a1")
        else:
            self.daemon.stop()
            if self.local_broker:
                try:
                    asyncio.run(self.local_broker.stop())
                except Exception:
                    pass
                self.local_broker = None
            self.btn_daemon.config(text="Avvia Demone Manuale", bg="#89b4fa")
            self.lbl_status.config(text="Stato: Demone arrestato", fg="#f38ba8")

    def _run_daemon(self):
        async def runner():
            if self.var_local_broker.get():
                try:
                    self.local_broker = SignalingBroker(host="0.0.0.0", port=8765)
                    await self.local_broker.start()
                except Exception as e:
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
                self.lbl_status.config(text=f"Stato: Connesso ({clients} client attivo/i)", fg="#a6e3a1")
            else:
                self.lbl_status.config(text="Stato: In ascolto su porta 8765 (Pronto)", fg="#89b4fa")
        else:
            self.lbl_status.config(text="Stato: Demone arrestato", fg="#f38ba8")
        self.root.after(2000, self._update_status)

    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    app = HostControlPanel()
    app.run()
