import os
import sys
import tkinter as tk
from tkinter import ttk, messagebox
import threading
import asyncio

from src.security import SecurityManager
from src.host_daemon import HostDaemon
from src.signaling_broker import SignalingBroker

class HostControlPanel:
    """Desktop Host Control Panel to view Device ID, configure password and monitor status."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("AetherDesk - Pannello Host")
        self.root.geometry("500x520")
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

        self._build_ui()
        self._update_status()

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TLabel", background="#0a0c14", foreground="#cdd6f4", font=("Segoe UI", 10))
        style.configure("Header.TLabel", background="#0a0c14", foreground="#38bdf8", font=("Segoe UI", 18, "bold"))
        style.configure("SubHeader.TLabel", background="#0a0c14", foreground="#94a3b8", font=("Segoe UI", 9))
        style.configure("ID.TLabel", background="#141726", foreground="#38bdf8", font=("Consolas", 22, "bold"))

        container = tk.Frame(self.root, bg="#0a0c14", padx=25, pady=18)
        container.pack(fill=tk.BOTH, expand=True)

        header = ttk.Label(container, text="AETHERDESK HOST", style="Header.TLabel")
        header.pack(pady=(0, 2))

        sub = ttk.Label(container, text="Host Non Presidiato - Pronto all'ascolto", style="SubHeader.TLabel")
        sub.pack(pady=(0, 12))

        # Device ID Card
        id_frame = tk.Frame(container, bg="#313244", padx=15, pady=12, highlightbackground="#45475a", highlightthickness=1)
        id_frame.pack(fill=tk.X, pady=(0, 15))

        lbl_id_title = tk.Label(id_frame, text="IL TUO ID DISPOSITIVO:", bg="#313244", fg="#a6adc8", font=("Segoe UI", 9, "bold"))
        lbl_id_title.pack(anchor="w")

        dev_id = self.security.get_or_create_device_id()
        self.lbl_id = tk.Label(id_frame, text=dev_id, bg="#313244", fg="#f9e2af", font=("Consolas", 22, "bold"))
        self.lbl_id.pack(pady=4)

        btn_copy = tk.Button(id_frame, text="Copia ID", bg="#45475a", fg="#cdd6f4", relief="flat",
                             font=("Segoe UI", 9), cursor="hand2", command=self._copy_id)
        btn_copy.pack(anchor="e")

        # Password Config Card
        pwd_frame = tk.Frame(container, bg="#313244", padx=15, pady=10, highlightbackground="#45475a", highlightthickness=1)
        pwd_frame.pack(fill=tk.X, pady=(0, 12))

        lbl_pwd_title = tk.Label(pwd_frame, text="PASSWORD DI ACCESSO REMOTO:", bg="#313244", fg="#a6adc8", font=("Segoe UI", 9, "bold"))
        lbl_pwd_title.pack(anchor="w", pady=(0, 4))

        pwd_input_frame = tk.Frame(pwd_frame, bg="#313244")
        pwd_input_frame.pack(fill=tk.X)

        self.entry_pwd = tk.Entry(pwd_input_frame, show="*", font=("Segoe UI", 11), bg="#181825", fg="#cdd6f4",
                                  insertbackground="white", relief="flat")
        self.entry_pwd.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4, padx=(0, 8))

        btn_save_pwd = tk.Button(pwd_input_frame, text="Salva Password", bg="#a6e3a1", fg="#11111b",
                                 font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2", command=self._save_password)
        btn_save_pwd.pack(side=tk.RIGHT, ipady=3, padx=2)

        # Broker Config Card
        broker_frame = tk.Frame(container, bg="#313244", padx=15, pady=10, highlightbackground="#45475a", highlightthickness=1)
        broker_frame.pack(fill=tk.X, pady=(0, 12))

        lbl_brk_title = tk.Label(broker_frame, text="SERVER BROKER & SEGNALAZIONE:", bg="#313244", fg="#a6adc8", font=("Segoe UI", 9, "bold"))
        lbl_brk_title.pack(anchor="w", pady=(0, 4))

        self.var_local_broker = tk.BooleanVar(value=True)
        self.chk_local_broker = tk.Checkbutton(broker_frame, text="Avvia Server Broker Locale integrato (Porta 8765)",
                                               variable=self.var_local_broker, bg="#313244", fg="#cdd6f4",
                                               selectcolor="#181825", activebackground="#313244",
                                               activeforeground="#cdd6f4", font=("Segoe UI", 9))
        self.chk_local_broker.pack(anchor="w", pady=(0, 4))

        brk_url_frame = tk.Frame(broker_frame, bg="#313244")
        brk_url_frame.pack(fill=tk.X)
        lbl_brk_url = tk.Label(brk_url_frame, text="URL Broker:", bg="#313244", fg="#a6adc8", font=("Segoe UI", 9))
        lbl_brk_url.pack(side=tk.LEFT, padx=(0, 5))
        self.entry_broker = tk.Entry(brk_url_frame, font=("Segoe UI", 9), bg="#181825", fg="#cdd6f4",
                                     insertbackground="white", relief="flat")
        self.entry_broker.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3)
        self.entry_broker.insert(0, "ws://127.0.0.1:8765")

        # Status & Controls
        self.lbl_status = tk.Label(container, text="Stato: Pronto all'ascolto", bg="#1e1e2e", fg="#89b4fa",
                                   font=("Segoe UI", 10, "bold"))
        self.lbl_status.pack(pady=(4, 10))

        btn_frame = tk.Frame(container, bg="#1e1e2e")
        btn_frame.pack(fill=tk.X)

        self.btn_daemon = tk.Button(btn_frame, text="Avvia Demone Manuale", bg="#89b4fa", fg="#11111b",
                                    font=("Segoe UI", 10, "bold"), relief="flat", cursor="hand2", command=self._toggle_daemon)
        self.btn_daemon.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=5, padx=(0, 5))

        btn_svc = tk.Button(btn_frame, text="Installa come Servizio Windows", bg="#eba0ac", fg="#11111b",
                            font=("Segoe UI", 10, "bold"), relief="flat", cursor="hand2", command=self._open_installer)
        btn_svc.pack(side=tk.RIGHT, fill=tk.X, expand=True, ipady=5, padx=(5, 0))

    def _copy_id(self):
        dev_id = self.security.get_or_create_device_id()
        self.root.clipboard_clear()
        self.root.clipboard_append(dev_id)
        messagebox.showinfo("Copiato", f"ID {dev_id} copiato negli appunti!")

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
            self.lbl_status.config(text=f"Stato: Connesso ({clients} client attivi)", fg="#a6e3a1")
        self.root.after(2000, self._update_status)

    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    app = HostControlPanel()
    app.run()
