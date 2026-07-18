import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox
import threading
import os
import shutil
import socket
import webbrowser

from update_checker import check_for_update
from version import __version__

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGO_PATH = os.path.join(BASE_DIR, "assets", "logo.png")

ICON_PATH = os.path.join(BASE_DIR, "icon.ico")


def _detect_lan_ip():
    """
    Bepaalt het LAN IP-adres van deze pc (bv. 192.168.1.23). Nodig omdat de
    server bindt op 0.0.0.0 (alle interfaces), maar dat adres zelf is niet
    bruikbaar om vanaf een ander apparaat naartoe te browsen.

    Trucje: een UDP-socket "verbinden" naar een extern adres verstuurt geen
    daadwerkelijke data, maar dwingt het OS wel om de uitgaande interface (en
    dus het LAN IP) te kiezen - precies wat we nodig hebben.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()

# ---------------------------------------------------------------------------
# Kleurenpalet - donker, vlak, modern (Tailwind slate-achtig)
# ---------------------------------------------------------------------------
COLOR_BG = "#0f172a"          # venster-achtergrond
COLOR_CARD = "#1e293b"        # sectie/kaart-achtergrond
COLOR_CARD_BORDER = "#334155"
COLOR_TEXT = "#e2e8f0"
COLOR_TEXT_MUTED = "#94a3b8"
COLOR_ACCENT = "#3b82f6"      # primaire actieknoppen
COLOR_ACCENT_HOVER = "#2563eb"
COLOR_SUCCESS = "#22c55e"
COLOR_DANGER = "#ef4444"
COLOR_MONO_BG = "#0b1220"     # logboek-achtergrond

FONT_BASE = ("Segoe UI", 10)
FONT_MUTED = ("Segoe UI", 9)
FONT_HEADING = ("Segoe UI Semibold", 15)
FONT_SUBHEADING = ("Segoe UI", 9)
FONT_SECTION = ("Segoe UI Semibold", 10)
FONT_MONO = ("Consolas", 9)
FONT_ADDRESS = ("Consolas", 12, "bold")


class ControlPanel(tk.Tk):
    """Desktop-controlepaneel voor SimTim Terminal - moderne donkere stijl."""

    def __init__(self, initial_host, initial_port, on_port_change, on_start, on_stop, get_status, log_path):
        super().__init__()
        self.title(f"SimTim Terminal - Control Panel (v{__version__})")
        self.geometry("680x580")
        self.minsize(600, 480)
        self.configure(bg=COLOR_BG)
        self._set_window_icon()

        self.initial_host = initial_host
        self.initial_port = initial_port
        self.on_port_change = on_port_change
        self.on_start = on_start
        self.on_stop = on_stop
        self.get_status = get_status
        self.log_path = log_path
        self._log_position = 0
        self._latest_url = None

        self._setup_style()
        self._build_header()
        self._build_status_section()
        self._build_port_section()
        self._build_log_section()
        self._build_update_section()

        self._poll_status()
        self._poll_log()
        self._check_update_async()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- Stijl ----------
    def _setup_style(self):
        """Configureert een donker, vlak ttk-thema. 'clam' is de enige
        ingebouwde thema-basis die volledige kleurcontrole toestaat."""
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure("TFrame", background=COLOR_BG)
        style.configure("Card.TFrame", background=COLOR_CARD)

        style.configure("TLabel", background=COLOR_BG, foreground=COLOR_TEXT, font=FONT_BASE)
        style.configure("Card.TLabel", background=COLOR_CARD, foreground=COLOR_TEXT, font=FONT_BASE)
        style.configure("Muted.Card.TLabel", background=COLOR_CARD, foreground=COLOR_TEXT_MUTED, font=FONT_MUTED)
        style.configure("Heading.TLabel", background=COLOR_BG, foreground=COLOR_TEXT, font=FONT_HEADING)
        style.configure("Sub.TLabel", background=COLOR_BG, foreground=COLOR_TEXT_MUTED, font=FONT_SUBHEADING)
        style.configure("Section.Card.TLabel", background=COLOR_CARD, foreground=COLOR_TEXT, font=FONT_SECTION)
        style.configure("Address.Card.TLabel", background=COLOR_CARD, foreground=COLOR_SUCCESS, font=FONT_ADDRESS)

        style.configure(
            "TButton",
            background=COLOR_ACCENT, foreground="#ffffff",
            font=FONT_BASE, borderwidth=0, focusthickness=0, padding=(12, 6),
        )
        style.map("TButton", background=[("active", COLOR_ACCENT_HOVER), ("disabled", "#475569")])

        style.configure(
            "Secondary.TButton",
            background=COLOR_CARD_BORDER, foreground=COLOR_TEXT,
            font=FONT_BASE, borderwidth=0, focusthickness=0, padding=(12, 6),
        )
        style.map("Secondary.TButton", background=[("active", "#475569"), ("disabled", "#334155")])

        style.configure(
            "TEntry",
            fieldbackground=COLOR_CARD_BORDER, foreground=COLOR_TEXT,
            insertcolor=COLOR_TEXT, borderwidth=0, padding=6,
        )

    def _set_window_icon(self):
        """
        Zet het venster-/taskbar-icoon op Windows. iconbitmap(default=...)
        past dit ook toe op alle Toplevel-vensters die vanuit dit venster
        worden geopend (bv. de messagebox-dialogen), niet alleen het
        hoofdvenster zelf.

        Faalt stil als icon.ico ontbreekt of niet geladen kan worden (bv. op
        een niet-Windows platform) - dan blijft gewoon het standaard
        Tk-icoon staan.
        """
        if os.path.exists(ICON_PATH):
            try:
                self.iconbitmap(default=ICON_PATH)
            except tk.TclError:
                pass

    def _card(self, parent, fill="x", expand=False):
        """Maakt een 'kaart': een ttk.Frame met kaart-achtergrond en wat interne padding."""
        outer = tk.Frame(parent, bg=COLOR_CARD_BORDER)
        outer.pack(fill=fill, expand=expand, padx=16, pady=(0, 12))
        inner = ttk.Frame(outer, style="Card.TFrame")
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        return inner

    # ---------- Header met logo ----------
    def _build_header(self):
        header = ttk.Frame(self, style="TFrame")
        header.pack(fill="x", padx=16, pady=(16, 12))

        self._logo_canvas = tk.Canvas(
            header, width=44, height=44, bg=COLOR_BG, highlightthickness=0
        )
        self._logo_canvas.pack(side="left", padx=(0, 12))
        self._draw_logo()

        title_frame = ttk.Frame(header, style="TFrame")
        title_frame.pack(side="left", fill="y")
        ttk.Label(title_frame, text="SimTim Terminal", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(title_frame, text=f"Control Panel  ·  v{__version__}", style="Sub.TLabel").pack(anchor="w")

    def _draw_logo(self):
        """Laadt een echt logo als LOGO_PATH bestaat, anders een getekend
        monogram-badge in de accentkleur als nette fallback."""
        if os.path.exists(LOGO_PATH):
            try:
                self._logo_image = tk.PhotoImage(file=LOGO_PATH)
                self._logo_canvas.create_image(22, 22, image=self._logo_image)
                return
            except tk.TclError:
                pass  # Val terug op het getekende badge hieronder

        self._logo_canvas.create_oval(2, 2, 42, 42, fill=COLOR_ACCENT, outline="")
        self._logo_canvas.create_text(
            22, 22, text="ST", fill="#ffffff", font=("Segoe UI Semibold", 14)
        )

    # ---------- Status ----------
    def _build_status_section(self):
        card = self._card(self)
        row = ttk.Frame(card, style="Card.TFrame")
        row.pack(fill="x", padx=16, pady=12)

        ttk.Label(row, text="STATUS", style="Section.Card.TLabel").pack(side="left")

        self.status_canvas = tk.Canvas(row, width=14, height=14, bg=COLOR_CARD, highlightthickness=0)
        self.status_dot = self.status_canvas.create_oval(1, 1, 13, 13, fill="grey")
        self.status_canvas.pack(side="left", padx=(16, 6))

        self.status_label = ttk.Label(row, text="Unknown", style="Card.TLabel")
        self.status_label.pack(side="left")

        btn_frame = ttk.Frame(row, style="Card.TFrame")
        btn_frame.pack(side="right")

        self.start_btn = ttk.Button(btn_frame, text="Start", command=self._handle_start)
        self.start_btn.pack(side="left", padx=(0, 8))

        self.stop_btn = ttk.Button(
            btn_frame, text="Stop", style="Secondary.TButton", command=self._handle_stop
        )
        self.stop_btn.pack(side="left")

    def _handle_start(self):
        ok, error = self.on_start()
        if not ok:
            messagebox.showerror("Failed to start", error or "Unknown error")

    def _handle_stop(self):
        ok, error = self.on_stop()
        if not ok:
            messagebox.showerror("Failed to stop", error or "Unknown error")

    def _poll_status(self):
        try:
            running = self.get_status()
        except Exception:
            running = False

        color = COLOR_SUCCESS if running else COLOR_DANGER
        text = "Running" if running else "Stopped"
        self.status_canvas.itemconfig(self.status_dot, fill=color)
        self.status_label.config(text=text)

        self.start_btn.config(state="disabled" if running else "normal")
        self.stop_btn.config(state="normal" if running else "disabled")

        self.after(2000, self._poll_status)

    # ---------- Adres / poort ----------
    def _build_port_section(self):
        card = self._card(self)

        ttk.Label(card, text="ADDRESS", style="Section.Card.TLabel").pack(
            anchor="w", padx=16, pady=(12, 2)
        )

        self.address_label = ttk.Label(
            card, text=self._format_address(self.initial_host, self.initial_port),
            style="Address.Card.TLabel"
        )
        self.address_label.pack(anchor="w", padx=16, pady=(0, 10))

        controls = ttk.Frame(card, style="Card.TFrame")
        controls.pack(fill="x", padx=16, pady=(0, 14))

        ttk.Label(controls, text="Port:", style="Muted.Card.TLabel").pack(side="left")

        self.port_var = tk.StringVar(value=str(self.initial_port))
        self.port_entry = ttk.Entry(controls, textvariable=self.port_var, width=8)
        self.port_entry.pack(side="left", padx=(8, 8))

        self.apply_btn = ttk.Button(controls, text="Apply & Restart", command=self._apply_port)
        self.apply_btn.pack(side="left")

        self.copy_btn = ttk.Button(
            controls, text="Copy address", style="Secondary.TButton", command=self._copy_address
        )
        self.copy_btn.pack(side="left", padx=(8, 0))

    def _format_address(self, host, port):
        if host in ("0.0.0.0", ""):
            display_ip = _detect_lan_ip() or host
            return f"http://{display_ip}:{port}"
        return f"http://{host}:{port}"

    def _copy_address(self):
        self.clipboard_clear()
        self.clipboard_append(self.address_label.cget("text"))

    def _apply_port(self):
        value = self.port_var.get().strip()
        if not value.isdigit() or not (1 <= int(value) <= 65535):
            messagebox.showerror("Invalid port", "Please enter a valid port number (1-65535).")
            return

        new_port = int(value)
        ok, error = self.on_port_change(new_port)
        if ok:
            self.address_label.config(text=self._format_address(self.initial_host, new_port))
            messagebox.showinfo("Port changed", f"Server is now running on port {new_port}.")
        else:
            messagebox.showerror("Restart failed", error or "Unknown error")

    # ---------- Log ----------
    def _build_log_section(self):
        card = self._card(self, fill="both", expand=True)

        ttk.Label(card, text="LOG", style="Section.Card.TLabel").pack(anchor="w", padx=16, pady=(12, 6))

        text_wrap = tk.Frame(card, bg=COLOR_MONO_BG)
        text_wrap.pack(fill="both", expand=True, padx=16)

        self.log_text = scrolledtext.ScrolledText(
            text_wrap, state="disabled", wrap="word", height=14,
            bg=COLOR_MONO_BG, fg=COLOR_TEXT, insertbackground=COLOR_TEXT,
            font=FONT_MONO, borderwidth=0, highlightthickness=0,
        )
        self.log_text.pack(fill="both", expand=True, padx=1, pady=1)

        export_btn = ttk.Button(
            card, text="Export to file", style="Secondary.TButton", command=self._export_log
        )
        export_btn.pack(anchor="e", padx=16, pady=12)

    def _poll_log(self):
        if os.path.exists(self.log_path):
            try:
                with open(self.log_path, "r", encoding="utf-8") as f:
                    f.seek(self._log_position)
                    new_data = f.read()
                    self._log_position = f.tell()
                if new_data:
                    self.log_text.config(state="normal")
                    self.log_text.insert("end", new_data)
                    self.log_text.see("end")
                    self.log_text.config(state="disabled")
            except OSError:
                pass

        self.after(1000, self._poll_log)

    def _export_log(self):
        if not os.path.exists(self.log_path):
            messagebox.showwarning("No log file", "No log file has been created yet.")
            return

        dest = filedialog.asksaveasfilename(
            defaultextension=".log",
            filetypes=[("Log file", "*.log"), ("All files", "*.*")],
            initialfile="simtim_export.log",
        )
        if dest:
            try:
                shutil.copyfile(self.log_path, dest)
                messagebox.showinfo("Exported", f"Log file saved as:\n{dest}")
            except OSError as e:
                messagebox.showerror("Export failed", str(e))

    # ---------- Updates ----------
    def _build_update_section(self):
        card = self._card(self)
        row = ttk.Frame(card, style="Card.TFrame")
        row.pack(fill="x", padx=16, pady=12)

        left = ttk.Frame(row, style="Card.TFrame")
        left.pack(side="left", fill="x", expand=True)
        ttk.Label(left, text="VERSION & UPDATES", style="Section.Card.TLabel").pack(anchor="w")
        self.update_label = ttk.Label(left, text="Checking...", style="Muted.Card.TLabel")
        self.update_label.pack(anchor="w", pady=(2, 0))

        right = ttk.Frame(row, style="Card.TFrame")
        right.pack(side="right")

        self.update_btn = ttk.Button(
            right, text="Check now", style="Secondary.TButton", command=self._check_update_async
        )
        self.update_btn.pack(side="left", padx=(0, 8))

        self.download_btn = ttk.Button(
            right, text="Download latest version", command=self._open_release_page, state="disabled"
        )
        self.download_btn.pack(side="left")

    def _check_update_async(self):
        self.update_label.config(text="Checking...")
        thread = threading.Thread(target=self._check_update_worker, daemon=True)
        thread.start()

    def _check_update_worker(self):
        result = check_for_update()
        # Terug naar de GUI-thread via after(), Tkinter is niet thread-safe
        self.after(0, lambda: self._handle_update_result(result))

    def _handle_update_result(self, result):
        if result is None:
            self.update_label.config(text="Check failed (no internet connection?)")
            return

        if result["update_available"]:
            self.update_label.config(text=f"New version available: {result['latest']} (v{__version__} installed)")
            self._latest_url = result["url"]
            self.download_btn.config(state="normal")
        else:
            self.update_label.config(text=f"You're running the latest version (v{__version__}).")
            self.download_btn.config(state="disabled")

    def _open_release_page(self):
        if self._latest_url:
            webbrowser.open(self._latest_url)

    def _on_close(self):
        self.destroy()