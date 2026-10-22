import io
import os
import sys
import asyncio
import threading
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from PIL import Image, ImageTk

from src.client import RemoteClient, ClientState, ClientSettings

class ClientViewerGUI:
    """Portable GUI Application for connecting to remote desktop host."""

    def __init__(self, broker_url: str = "ws://127.0.0.1:8765"):
        self.root = tk.Tk()
        self.root.title("AetherDesk - Client Portatile")
        self.root.geometry("460x440")
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

        self.loop = asyncio.new_event_loop()
        self.network_thread = threading.Thread(target=self._run_async_loop, daemon=True)
        self.network_thread.start()

        self.viewer_window = None
        self.canvas = None
        self.photo_img = None
        self.current_img_dims = (0, 0)
        self._canvas_img_id = None

        self._build_login_ui()

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

        header = ttk.Label(container, text="AETHERDESK", style="Header.TLabel")
        header.pack(pady=(0, 2))

        subheader = ttk.Label(container, text="Controllo Remoto Ultra-Leggero (Zero-Bloat E2EE)", style="SubHeader.TLabel")
        subheader.pack(pady=(0, 10))

        saved_id, saved_pwd, saved_rem = self.settings.load()
        saved_broker = self.settings.get_broker_url()
        devices = self.settings.get_devices()

        # Device ID
        lbl_id = ttk.Label(container, text="ID Computer Remoto (9 cifre):")
        lbl_id.pack(anchor="w")

        device_options = [f"{d['id']} ({d.get('name', 'PC')})" for d in devices] if devices else []
        self.combo_id = ttk.Combobox(container, values=device_options, font=("Consolas", 13), justify="center")
        self.combo_id.pack(fill=tk.X, pady=(2, 8), ipady=3)
        self.combo_id.set(saved_id if saved_id else "123 456 789")
        self.combo_id.bind("<<ComboboxSelected>>", self._on_device_selected)

        # Password
        lbl_pwd = ttk.Label(container, text="Password di Accesso:")
        lbl_pwd.pack(anchor="w")
        self.entry_pwd = tk.Entry(container, font=("Segoe UI", 11), show="*", bg="#313244", fg="#cdd6f4", insertbackground="white")
        self.entry_pwd.pack(fill=tk.X, pady=(2, 8), ipady=3)
        if saved_pwd:
            self.entry_pwd.insert(0, saved_pwd)

        # Broker URL (LAN / Cloud / VPN)
        lbl_broker = ttk.Label(container, text="Server Broker (IP o Dominio):")
        lbl_broker.pack(anchor="w")
        self.entry_broker = tk.Entry(container, font=("Segoe UI", 10), bg="#313244", fg="#cdd6f4", insertbackground="white")
        self.entry_broker.pack(fill=tk.X, pady=(2, 8), ipady=3)
        self.entry_broker.insert(0, saved_broker if saved_broker else "ws://127.0.0.1:8765")

        self.var_remember = tk.BooleanVar(value=saved_rem)
        self.chk_remember = tk.Checkbutton(container, text="Ricorda questo computer e impostazioni",
                                           variable=self.var_remember, bg="#1e1e2e", fg="#cdd6f4",
                                           selectcolor="#313244", activebackground="#1e1e2e",
                                           activeforeground="#cdd6f4", font=("Segoe UI", 9))
        self.chk_remember.pack(anchor="w", pady=(0, 10))

        self.btn_connect = tk.Button(container, text="Connetti al Computer", bg="#89b4fa", fg="#11111b",
                                     font=("Segoe UI", 11, "bold"), relief="flat", cursor="hand2", command=self._start_connect)
        self.btn_connect.pack(fill=tk.X, ipady=6)

        self.lbl_status = ttk.Label(container, text="Pronto alla connessione.", style="Status.TLabel")
        self.lbl_status.pack(pady=(10, 0))

    def _on_device_selected(self, event=None):
        selected = self.combo_id.get().strip()
        clean_id = selected.split(" ")[0] if " " in selected else selected
        devices = self.settings.get_devices()
        for d in devices:
            if d.get("id", "").replace(" ", "") == clean_id.replace(" ", ""):
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
        dev_id = self.combo_id.get().strip().split(" ")[0]
        pwd = self.entry_pwd.get().strip()
        broker_url = self.entry_broker.get().strip()
        if not broker_url:
            broker_url = "ws://127.0.0.1:8765"

        if not ClientState.validate_id(dev_id):
            messagebox.showerror("Errore ID", "L'ID deve essere composto esattamente da 9 cifre (es. 482 910 234).")
            return

        self.btn_connect.config(state="disabled", text="Connessione in corso...")
        asyncio.run_coroutine_threadsafe(self._async_connect_task(dev_id, pwd, broker_url), self.loop)

    async def _async_connect_task(self, dev_id: str, pwd: str, broker_url: str):
        ok = await self.client.connect(dev_id, pwd, broker_url=broker_url)
        self.root.after(0, lambda: self._handle_connect_result(ok))

    def _handle_connect_result(self, success: bool):
        self.btn_connect.config(state="normal", text="Connetti al Computer")
        if success:
            dev_id = self.combo_id.get().strip().split(" ")[0]
            pwd = self.entry_pwd.get().strip()
            broker_url = self.entry_broker.get().strip()
            self.settings.save(dev_id, pwd, self.var_remember.get(), broker_url=broker_url)
            self._open_viewer_window()

    def _open_viewer_window(self):
        if self.viewer_window and tk.Toplevel.winfo_exists(self.viewer_window):
            return

        self.viewer_window = tk.Toplevel(self.root)
        self.viewer_window.title(f"AetherDesk - [{self.client.state.target_id}]")
        self.viewer_window.geometry("1280x720")
        self.viewer_window.configure(bg="#000000")
        self.viewer_window.protocol("WM_DELETE_WINDOW", self._close_viewer)

        # Toolbar
        toolbar = tk.Frame(self.viewer_window, bg="#181825", height=35)
        toolbar.pack(fill=tk.X, side=tk.TOP)

        btn_cad = tk.Button(toolbar, text="Invia Ctrl+Alt+Del", bg="#313244", fg="#cdd6f4", relief="flat",
                            font=("Segoe UI", 9), command=lambda: self._send_special("ctrl_alt_del"))
        btn_cad.pack(side=tk.LEFT, padx=5, pady=4)

        btn_wind = tk.Button(toolbar, text="Mostra Desktop (Win+D)", bg="#313244", fg="#cdd6f4", relief="flat",
                             font=("Segoe UI", 9), command=lambda: self._send_special("win_d"))
        btn_wind.pack(side=tk.LEFT, padx=5, pady=4)

        btn_fs = tk.Button(toolbar, text="Schermo Intero", bg="#313244", fg="#cdd6f4", relief="flat",
                           font=("Segoe UI", 9), command=self._toggle_fullscreen)
        btn_fs.pack(side=tk.LEFT, padx=5, pady=4)

        # File Transfer button
        btn_file = tk.Button(toolbar, text="Invia File", bg="#89b4fa", fg="#11111b", relief="flat",
                             font=("Segoe UI", 9, "bold"), command=self._pick_and_send_file)
        btn_file.pack(side=tk.LEFT, padx=5, pady=4)

        # Monitor Selector
        lbl_mon = tk.Label(toolbar, text="Monitor:", bg="#181825", fg="#a6adc8", font=("Segoe UI", 9))
        lbl_mon.pack(side=tk.LEFT, padx=(10, 2), pady=4)

        btn_m1 = tk.Button(toolbar, text="1", bg="#313244", fg="#cdd6f4", relief="flat", font=("Segoe UI", 8),
                           command=lambda: self._switch_monitor(0))
        btn_m1.pack(side=tk.LEFT, padx=2, pady=4)

        btn_m2 = tk.Button(toolbar, text="2", bg="#313244", fg="#cdd6f4", relief="flat", font=("Segoe UI", 8),
                           command=lambda: self._switch_monitor(1))
        btn_m2.pack(side=tk.LEFT, padx=2, pady=4)

        btn_mall = tk.Button(toolbar, text="Tutti", bg="#313244", fg="#cdd6f4", relief="flat", font=("Segoe UI", 8),
                             command=lambda: self._switch_monitor(-1))
        btn_mall.pack(side=tk.LEFT, padx=2, pady=4)

        # Privacy Mode (Schermo Nero Host)
        self.privacy_on = False
        self.btn_privacy = tk.Button(toolbar, text="Privacy: Schermo Nero (OFF)", bg="#313244", fg="#cdd6f4",
                                     relief="flat", font=("Segoe UI", 9), command=self._toggle_privacy)
        self.btn_privacy.pack(side=tk.LEFT, padx=5, pady=4)

        btn_disc = tk.Button(toolbar, text="Disconnetti", bg="#f38ba8", fg="#11111b", relief="flat",
                             font=("Segoe UI", 9, "bold"), command=self._close_viewer)
        btn_disc.pack(side=tk.RIGHT, padx=5, pady=4)

        # Video Canvas
        self.canvas = tk.Canvas(self.viewer_window, bg="#000000", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # Input Bindings
        self.canvas.bind("<Motion>", self._on_mouse_move)
        self.canvas.bind("<Button-1>", lambda e: self._on_mouse_btn(e, "left", "down"))
        self.canvas.bind("<ButtonRelease-1>", lambda e: self._on_mouse_btn(e, "left", "up"))
        self.canvas.bind("<Button-3>", lambda e: self._on_mouse_btn(e, "right", "down"))
        self.canvas.bind("<ButtonRelease-3>", lambda e: self._on_mouse_btn(e, "right", "up"))
        self.canvas.bind("<MouseWheel>", self._on_mouse_wheel)

        self.viewer_window.bind("<KeyPress>", self._on_key_press)
        self.viewer_window.bind("<KeyRelease>", self._on_key_release)

        # Start clipboard sync loop
        self._sync_clipboard_loop()

    def _toggle_fullscreen(self):
        is_fs = self.viewer_window.attributes("-fullscreen")
        self.viewer_window.attributes("-fullscreen", not is_fs)

    def _toggle_privacy(self):
        self.privacy_on = not self.privacy_on
        txt = "Privacy: Schermo Nero (ON)" if self.privacy_on else "Privacy: Schermo Nero (OFF)"
        bg = "#a6e3a1" if self.privacy_on else "#313244"
        fg = "#11111b" if self.privacy_on else "#cdd6f4"
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

    def _on_frame_received(self, jpeg_bytes: bytes, dims: tuple[int, int], cur: tuple[int, int]):
        if not self.canvas:
            return
        try:
            img = Image.open(io.BytesIO(jpeg_bytes))
            c_w = self.canvas.winfo_width()
            c_h = self.canvas.winfo_height()
            if c_w > 10 and c_h > 10:
                # Resize keeping aspect ratio
                img.thumbnail((c_w, c_h), Image.Resampling.BILINEAR)
                self.current_img_dims = img.size
                photo = ImageTk.PhotoImage(img)
                self.photo_img = photo
                # Center on canvas without delete('all') churn
                pos_x = (c_w - img.width) // 2
                pos_y = (c_h - img.height) // 2
                if self._canvas_img_id is None:
                    self._canvas_img_id = self.canvas.create_image(pos_x, pos_y, anchor="nw", image=photo)
                else:
                    self.canvas.coords(self._canvas_img_id, pos_x, pos_y)
                    self.canvas.itemconfig(self._canvas_img_id, image=photo)
        except Exception:
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
        nx, ny = self._calc_norm_coords(event)
        act_type = f"mouse_{action}"
        asyncio.run_coroutine_threadsafe(
            self.client.send_input({"type": "mouse_move", "x": nx, "y": ny}),
            self.loop
        )
        asyncio.run_coroutine_threadsafe(
            self.client.send_input({"type": act_type, "button": button}),
            self.loop
        )

    def _on_mouse_wheel(self, event):
        delta = event.delta
        asyncio.run_coroutine_threadsafe(
            self.client.send_input({"type": "mouse_wheel", "delta": delta}),
            self.loop
        )

    def _on_key_press(self, event):
        vk = event.keycode
        asyncio.run_coroutine_threadsafe(
            self.client.send_input({"type": "key_down", "vk": vk}),
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
    gui = ClientViewerGUI()
    gui.run()
