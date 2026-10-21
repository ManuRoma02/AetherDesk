import os
import sys
import tkinter as tk
from tkinter import ttk, messagebox
import threading
import asyncio

from src.security import SecurityManager
from src.host_daemon import HostDaemon

class HostControlPanel:
    """Desktop Host Control Panel to view Device ID, configure password and monitor status."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Remote Desktop - Pannello Host")
        self.root.geometry("480x420")
        self.root.configure(bg="#1e1e2e")
        self.root.resizable(False, False)

        self.security = SecurityManager()
        self.daemon = None
        self.daemon_thread = None

        self._build_ui()
        self._update_status()

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TLabel", background="#1e1e2e", foreground="#cdd6f4", font=("Segoe UI", 10))
        style.configure("Header.TLabel", background="#1e1e2e", foreground="#a6e3a1", font=("Segoe UI", 16, "bold"))
        style.configure("SubHeader.TLabel", background="#1e1e2e", foreground="#9399b2", font=("Segoe UI", 9))
        style.configure("ID.TLabel", background="#313244", foreground="#f9e2af", font=("Consolas", 22, "bold"))

        container = tk.Frame(self.root, bg="#1e1e2e", padx=25, pady=20)
        container.pack(fill=tk.BOTH, expand=True)

        header = ttk.Label(container, text="Computer Fisso (Host Remoto)", style="Header.TLabel")
        header.pack(pady=(0, 2))

        sub = ttk.Label(container, text="Pronto per ricevere connessioni dal portatile", style="SubHeader.TLabel")
        sub.pack(pady=(0, 15))

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
        pwd_frame = tk.Frame(container, bg="#313244", padx=15, pady=12, highlightbackground="#45475a", highlightthickness=1)
        pwd_frame.pack(fill=tk.X, pady=(0, 15))

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

        # Status & Controls
        self.lbl_status = tk.Label(container, text="Stato: Attivo e pronto all'ascolto", bg="#1e1e2e", fg="#89b4fa",
                                   font=("Segoe UI", 10, "bold"))
        self.lbl_status.pack(pady=(5, 12))

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
            self.daemon = HostDaemon()
            self.daemon_thread = threading.Thread(target=self._run_daemon, daemon=True)
            self.daemon_thread.start()
            self.btn_daemon.config(text="Arresta Demone", bg="#f38ba8")
            self.lbl_status.config(text="Stato: Demone in esecuzione (Pronto)", fg="#a6e3a1")
        else:
            self.daemon.stop()
            self.btn_daemon.config(text="Avvia Demone Manuale", bg="#89b4fa")
            self.lbl_status.config(text="Stato: Demone arrestato", fg="#f38ba8")

    def _run_daemon(self):
        asyncio.run(self.daemon.start())

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
