"""System-tray app. Tk owns the main thread; pystray and the status poller run in background
threads and hand UI work back to Tk through a queue."""

from __future__ import annotations

import ctypes
import logging
import logging.handlers
import os
import queue
import threading
import time
import tkinter as tk
import webbrowser
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import pystray

from printmgr import autostart, config, discovery, escl, icons, install, ipp
from printmgr.status import AlertTracker, PrinterStatus, fetch, reason_text

log = logging.getLogger("printmgr")
APP_NAME = "Printer Manager"
MAX_SUPPLY_LINES = 3


class App:
    def __init__(self) -> None:
        self.cfg = config.load()
        self._cfg_lock = threading.Lock()  # every cfg change is copy -> replace -> save under this
        self.status = PrinterStatus(reachable=False, host=self.cfg.host)
        self.updated: datetime | None = None
        self.alerts = AlertTracker(config.app_dir() / "alerts.json")
        self.ui_queue: queue.Queue[Callable[[], object]] = queue.Queue()
        self.wake = threading.Event()
        self.stopping = False
        self.failures = 0
        self.scanner: escl.Scanner | None = None
        self.status_window: StatusWindow | None = None
        self.scan_window: ScanWindow | None = None
        self.find_window: FindWindow | None = None

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)
        self.icon = pystray.Icon("PrinterManager", icons.render("offline"), APP_NAME, menu=self._menu())

    # ---- threading glue -------------------------------------------------------------------
    def post(self, fn: Callable[[], object]) -> None:
        self.ui_queue.put(fn)

    def _on_ui(self, fn: Callable[[], None]) -> Callable[[], None]:
        return lambda: self.post(fn)

    def _pump(self) -> None:
        while True:
            try:
                fn = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                log.exception("UI task failed")
        if self.stopping:
            self.root.destroy()  # Tk is only ever touched from its own (main) thread
            return
        self.root.after(100, self._pump)

    # ---- tray menu ------------------------------------------------------------------------
    def _supply_item(self, i: int) -> pystray.MenuItem:
        def text(_item: object) -> str:
            s = self.status.supplies[i] if i < len(self.status.supplies) else None
            if s is None:
                return ""
            level = "unknown" if s.percent is None else f"{s.percent}%"
            return f"{s.name}: {level}"

        return pystray.MenuItem(text, None, enabled=False, visible=lambda _: i < len(self.status.supplies))

    def _menu(self) -> pystray.Menu:
        item = pystray.MenuItem
        return pystray.Menu(
            item(lambda _: self.status.headline, None, enabled=False),
            *[self._supply_item(i) for i in range(MAX_SUPPLY_LINES)],
            pystray.Menu.SEPARATOR,
            item("Status…", self._on_ui(self.open_status), default=True),
            item("Scan…", self._on_ui(self.open_scan)),
            item("Printer web settings", self.open_web, enabled=lambda _: bool(self.cfg.host)),
            item("Install printer in Windows", self._on_ui(self.install_printer), enabled=lambda _: bool(self.cfg.host)),
            item("Find printer…", self._on_ui(self.open_find)),
            item("Refresh now", lambda: self.wake.set()),
            pystray.Menu.SEPARATOR,
            item("Start with Windows", self.toggle_autostart, checked=lambda _: autostart.is_enabled()),
            item("Quit", self.quit),
        )

    def open_web(self) -> None:
        webbrowser.open(f"http://{self.cfg.host}/")

    def toggle_autostart(self) -> None:
        try:
            autostart.set_enabled(not autostart.is_enabled())
        except OSError:
            log.exception("autostart toggle failed")
        self.icon.update_menu()

    def quit(self) -> None:
        # Runs on the pystray thread: only flip flags here; _pump tears Tk down on the main thread.
        self.stopping = True
        self.wake.set()
        self.icon.stop()

    # ---- polling --------------------------------------------------------------------------
    def _poll_loop(self) -> None:
        # Wait for the tray icon to exist so a startup alert is not notified into the void.
        for _ in range(50):
            if self.icon.visible or self.stopping:
                break
            time.sleep(0.1)
        while not self.stopping:
            delay = 60.0
            try:
                delay = self._poll_once()
            except Exception:
                log.exception("poll failed")
            self.wake.wait(delay)
            self.wake.clear()

    def _fetch(self) -> PrinterStatus:
        cfg = self.cfg  # one consistent snapshot; set_printer swaps the whole object
        try:
            return fetch(cfg)
        except Exception as e:  # never leave a stale "Ready" icon behind an unexpected failure
            log.exception("status fetch failed")
            return PrinterStatus(reachable=False, host=cfg.host, error=f"{type(e).__name__}: {e}")

    def _poll_once(self) -> float:
        """Returns seconds until the next poll."""
        if not self.cfg.host:
            self._autodiscover()
        st = self._fetch()
        if st.reachable:
            self.failures = 0
            if st.model and st.model != self.cfg.model:
                self.update_cfg(only_if_host=st.host, model=st.model)
        elif self.cfg.host:
            self.failures += 1
            if self.failures == 1 and self.status.reachable:
                return 10.0  # one miss is usually a Wi-Fi blip: re-check soon before saying offline
            if self.failures % 3 == 0 and self._autodiscover(moved_only=True):
                st = self._fetch()  # printer got a new DHCP address
        self._show(st)
        return float(self.cfg.poll_seconds)

    def _show(self, st: PrinterStatus) -> None:
        if self.status.severity != st.severity or self.updated is None:
            self.icon.icon = icons.render(st.severity)  # only redraw the tray icon on change
        if self.updated is None:
            log.info("first status: %s (%s)", st.headline, st.severity)
        self.status = st
        self.updated = datetime.now()
        model = (st.model or self.cfg.model or APP_NAME).replace("Brother ", "")
        self.icon.title = f"{model}: {st.headline}"[:127]
        self.icon.update_menu()
        new = self.alerts.update(st.alerts())
        if new and self.icon.HAS_NOTIFICATION:
            self.icon.notify("\n".join(new)[:250], st.model or APP_NAME)
        if self.status_window:
            self.post(self.status_window.refresh)

    def _autodiscover(self, moved_only: bool = False) -> bool:
        found = discovery.discover()  # mDNS, falling back to a subnet sweep if mDNS finds nothing
        if moved_only:
            found = [f for f in found if self.cfg.model and f.model == self.cfg.model]
        else:
            found = [f for f in found if f.is_brother] or found
        if not found or found[0].host == self.cfg.host:
            return False
        self.set_printer(found[0].host, found[0].ipp_path, found[0].model, found[0].escl_url)
        return True

    def update_cfg(self, only_if_host: str | None = None, **changes: object) -> bool:
        """Apply `changes` to a copy of cfg, swap it in and save, atomically with respect to
        every other writer. With `only_if_host`, skip if the printer changed meanwhile."""
        with self._cfg_lock:
            if only_if_host is not None and self.cfg.host != only_if_host:
                return False
            self.cfg = replace(self.cfg, **changes)  # type: ignore[arg-type]
            config.save(self.cfg)
            return True

    def set_printer(self, host: str, ipp_path: str, model: str, escl_url: str = "") -> None:
        log.info("using printer %s (%s)", host, model)
        # One swap, so the poller never sees a new host with an old path.
        self.update_cfg(host=host, ipp_path=ipp_path, model=model, escl_url=escl_url)
        self.scanner = None
        self.failures = 0
        if self.scan_window:
            self.post(self.scan_window.forget_caps)  # the old printer's scanner options no longer apply

    def get_scanner(self) -> escl.Scanner:
        if not self.cfg.host:
            raise escl.ScanError("No printer set up yet — use Find printer…")
        if self.scanner is None:
            self.scanner = escl.Scanner.locate(self.cfg.host, self.cfg.escl_url)
            if self.scanner.base != self.cfg.escl_url:
                self.update_cfg(only_if_host=self.cfg.host, escl_url=self.scanner.base)
        return self.scanner

    # ---- windows --------------------------------------------------------------------------
    def open_status(self) -> None:
        if self.status_window is None:
            self.status_window = StatusWindow(self)
        self.status_window.show()

    def open_scan(self) -> None:
        if self.scan_window is None:
            self.scan_window = ScanWindow(self)
        self.scan_window.show()

    def open_find(self) -> None:
        if self.find_window is None:
            self.find_window = FindWindow(self)
        self.find_window.show()

    def install_printer(self) -> None:
        host, path, model = self.cfg.host, self.cfg.ipp_path, self.status.model or self.cfg.model or "Brother Printer"

        def work() -> None:
            try:
                msg = install.install(host, path, model)
                self.post(lambda: messagebox.showinfo(APP_NAME, msg))
            except Exception as e:
                if not isinstance(e, (install.InstallError, OSError)):
                    log.exception("install failed")
                err = str(e) or type(e).__name__
                self.post(lambda: messagebox.showerror(APP_NAME, f"Could not install the printer:\n{err}"))

        if self.icon.HAS_NOTIFICATION:
            self.icon.notify("Adding the printer to Windows… (this can take a minute)", APP_NAME)
        threading.Thread(target=work, daemon=True).start()

    def run(self) -> None:
        threading.Thread(target=self.icon.run, daemon=True).start()
        threading.Thread(target=self._poll_loop, daemon=True).start()
        self.root.after(100, self._pump)
        self.root.mainloop()


class Window:
    title = APP_NAME

    def __init__(self, app: App) -> None:
        self.app = app
        self.top = tk.Toplevel(app.root)
        self.top.title(self.title)
        self.top.resizable(False, False)
        self.top.protocol("WM_DELETE_WINDOW", self.top.withdraw)
        self.frame = ttk.Frame(self.top, padding=14)
        self.frame.grid(sticky="nsew")

    def show(self) -> None:
        self.top.deiconify()
        self.top.lift()
        self.top.focus_force()


class StatusWindow(Window):
    title = "Printer status"

    def __init__(self, app: App) -> None:
        super().__init__(app)
        self.body = ttk.Frame(self.frame)
        self.body.grid(row=0, column=0, sticky="nsew")
        buttons = ttk.Frame(self.frame)
        buttons.grid(row=1, column=0, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Refresh", command=app.wake.set).pack(side="left", padx=4)
        ttk.Button(buttons, text="Web settings", command=app.open_web).pack(side="left", padx=4)
        ttk.Button(buttons, text="Close", command=self.top.withdraw).pack(side="left", padx=4)
        self.refresh()

    def refresh(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()
        st = self.app.status
        row = 0

        def line(label: str, value: str) -> None:
            nonlocal row
            ttk.Label(self.body, text=label, foreground="#555").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=2)
            ttk.Label(self.body, text=value).grid(row=row, column=1, sticky="w", pady=2)
            row += 1

        line("Printer", st.model or self.app.cfg.model or "—")
        line("Address", self.app.cfg.host or "not set — use Find printer…")
        line("Status", st.headline)
        for lvl, reason in st.problems[1:]:
            line("", f"{'⚠' if lvl == 'warning' else '✖'} {reason_text(reason)}")
        if st.message and st.message.lower() not in st.headline.lower():
            line("Message", st.message)
        for s in st.supplies:
            ttk.Label(self.body, text=s.name, foreground="#555").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=2)
            cell = ttk.Frame(self.body)
            cell.grid(row=row, column=1, sticky="w", pady=2)
            bar = ttk.Progressbar(cell, length=180, maximum=100, value=s.percent or 0)
            bar.pack(side="left")
            ttk.Label(cell, text=" unknown" if s.percent is None else f" {s.percent}%").pack(side="left")
            row += 1
        if st.reachable and not st.supplies:
            line("Supplies", "not reported by the printer")
        if st.page_count is not None:
            line("Pages printed", f"{st.page_count:,}")
        if not st.reachable and st.error:
            line("Detail", st.error[:80])
        if self.app.updated:
            line("Updated", self.app.updated.strftime("%I:%M:%S %p").lstrip("0"))


class ScanWindow(Window):
    title = "Scan"
    SOURCES = {"Flatbed": "Platen", "Document feeder": "Feeder", "Document feeder (2-sided)": "Duplex"}
    COLORS = {"Color": "RGB24", "Grayscale": "Grayscale8", "Black & white": "BlackAndWhite1"}
    FORMATS = {"PDF": "pdf", "JPEG": "jpg"}

    def __init__(self, app: App) -> None:
        super().__init__(app)
        cfg = app.cfg
        self.caps: escl.ScannerCaps | None = None
        self.crop_note = ""
        self.last_files: list[Path] = []
        initial_source = _key(self.SOURCES, cfg.scan_source, "Flatbed")
        self.source = tk.StringVar(value="Document feeder" if cfg.scan_source == "Duplex" else initial_source)
        self.color = tk.StringVar(value=_key(self.COLORS, cfg.scan_color, "Color"))
        self.dpi = tk.StringVar(value=str(cfg.scan_dpi))
        self.size = tk.StringVar(value=cfg.scan_size if cfg.scan_size in escl.PAGE_SIZES else "Letter")
        self.fmt = tk.StringVar(value=_key(self.FORMATS, cfg.scan_format, "PDF"))
        self.folder = tk.StringVar(value=cfg.scan_dir)

        f = self.frame
        # Until capabilities arrive, offer only safe choices (no 2-sided, only standard dpi).
        self.source_box = self._combo(0, "Source", self.source, ["Flatbed", "Document feeder"])
        self.color_box = self._combo(1, "Color", self.color, list(self.COLORS))
        self.dpi_box = self._combo(2, "Resolution (dpi)", self.dpi, ["100", "200", "300", "600"])
        self._combo(3, "Page size", self.size, list(escl.PAGE_SIZES))
        self._combo(4, "Save as", self.fmt, list(self.FORMATS))
        ttk.Label(f, text="Folder").grid(row=5, column=0, sticky="w", pady=3)
        folder_row = ttk.Frame(f)
        folder_row.grid(row=5, column=1, sticky="we", pady=3)
        ttk.Entry(folder_row, textvariable=self.folder, width=34).pack(side="left")
        ttk.Button(folder_row, text="…", width=3, command=self._browse).pack(side="left", padx=(4, 0))
        self.source_box.bind("<<ComboboxSelected>>", lambda _e: self._apply_source())

        buttons = ttk.Frame(f)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self.open_btn = ttk.Button(buttons, text="Open result", command=self._open_result, state="disabled")
        self.open_btn.pack(side="left", padx=4)
        self.scan_btn = ttk.Button(buttons, text="Scan", command=self._scan)
        self.scan_btn.pack(side="left", padx=4)
        self.status_var = tk.StringVar(value="Connecting to scanner…")
        ttk.Label(f, textvariable=self.status_var, foreground="#555", wraplength=360).grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(10, 0)
        )

    def _combo(self, row: int, label: str, var: tk.StringVar, values: list[str]) -> ttk.Combobox:
        ttk.Label(self.frame, text=label).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 12))
        box = ttk.Combobox(self.frame, textvariable=var, values=values, state="readonly", width=32)
        box.grid(row=row, column=1, sticky="w", pady=3)
        return box

    def show(self) -> None:
        super().show()
        if self.caps is None:
            threading.Thread(target=self._load_caps, daemon=True).start()

    def _load_caps(self) -> None:
        try:
            caps = self.app.get_scanner().capabilities()
        except Exception as e:  # every failure must reach the window, or it sticks on "Connecting…"
            if not isinstance(e, (OSError, escl.ScanError)):
                log.exception("loading scanner capabilities failed")
            msg = f"Scanner not reachable: {e} — click Scan to retry."
            self.app.scanner = None
            self.app.post(lambda: self.status_var.set(msg))
            return
        self.app.post(lambda: self._apply_caps(caps))

    def forget_caps(self) -> None:
        self.caps = None
        self.source_box["values"] = ["Flatbed", "Document feeder"]
        self.status_var.set("Printer changed — click Scan to connect to its scanner.")

    def _apply_caps(self, caps: escl.ScannerCaps) -> None:
        self.caps = caps
        sources = [k for k, v in self.SOURCES.items() if (v == "Platen" and caps.platen) or (v == "Feeder" and caps.adf) or (v == "Duplex" and caps.adf and caps.adf_duplex)]
        self.source_box["values"] = sources or list(self.SOURCES)
        if sources and self.source.get() not in sources:
            self.source.set(sources[0])
        self._apply_source()
        self.status_var.set(f"Ready — {caps.make_and_model or 'scanner connected'}")

    def _apply_source(self) -> None:
        if not self.caps:
            return
        src = self.caps.platen if self.SOURCES[self.source.get()] == "Platen" else self.caps.adf
        if not src:
            return
        colors = [k for k, v in self.COLORS.items() if not src.color_modes or v in src.color_modes]
        self.color_box["values"] = colors
        if self.color.get() not in colors and colors:
            self.color.set(colors[0])
        if src.resolutions:
            values = [str(r) for r in src.resolutions if r <= 1200]
            self.dpi_box["values"] = values
            if self.dpi.get() not in values:
                self.dpi.set("300" if "300" in values else values[len(values) // 2])

    def _browse(self) -> None:
        chosen = filedialog.askdirectory(parent=self.top, initialdir=self.folder.get())
        if chosen:
            self.folder.set(chosen)

    def _scan(self) -> None:
        if self.caps is None:  # not connected yet (or it failed): connect first, then the user scans
            self.status_var.set("Connecting to scanner…")
            threading.Thread(target=self._load_caps, daemon=True).start()
            return
        self.app.update_cfg(
            scan_source=self.SOURCES[self.source.get()],
            scan_color=self.COLORS[self.color.get()],
            scan_dpi=int(self.dpi.get()),
            scan_size=self.size.get(),
            scan_format=self.FORMATS[self.fmt.get()],
            scan_dir=self.folder.get().strip() or config.Config().scan_dir,
        )
        cfg = self.app.cfg
        self.scan_btn.configure(state="disabled")
        self.status_var.set("Scanning…")
        size = escl.PAGE_SIZES[cfg.scan_size]
        src = self.caps.platen if cfg.scan_source == "Platen" else self.caps.adf
        self.crop_note = ""
        if src and size[1] > src.max_height:
            where = "flatbed" if cfg.scan_source == "Platen" else "feeder"
            self.crop_note = f" (cropped to the {where}'s {src.max_height / 300:.1f}-inch maximum)"
        settings = (cfg.scan_source, cfg.scan_color, cfg.scan_dpi, escl.PAGE_SIZES[cfg.scan_size], cfg.scan_format, Path(cfg.scan_dir))
        threading.Thread(target=self._scan_worker, args=settings, daemon=True).start()

    def _scan_worker(self, source: str, color: str, dpi: int, size: tuple[int, int], fmt: str, folder: Path) -> None:
        try:
            scanner = self.app.get_scanner()
            pages = scanner.scan(
                source, color, dpi, size,
                progress=lambda n: self.app.post(lambda: self.status_var.set(f"Scanned page {n}…")),
                caps=self.caps,
                waiting=lambda: self.app.post(lambda: self.status_var.set("Waiting for the scanner to finish another job…")),
            )
            name = datetime.now().strftime("Scan_%Y-%m-%d_%H%M%S")
            files = escl.save_pages(pages, folder, name, fmt, dpi)
        except Exception as e:  # every failure must re-enable the Scan button
            if not isinstance(e, (OSError, escl.ScanError)):
                log.exception("scan failed")
            msg = str(e) or type(e).__name__
            self.app.scanner = None
            self.app.post(lambda: self._done([], f"Scan failed: {msg}"))
            return
        pages_txt = f"{len(pages)} page{'s' if len(pages) != 1 else ''}"
        note = self.crop_note
        self.app.post(lambda: self._done(files, f"Saved {pages_txt} → {files[0].name}{note}"))

    def _done(self, files: list[Path], message: str) -> None:
        self.scan_btn.configure(state="normal")
        self.status_var.set(message)
        if files:
            self.last_files = files
            self.open_btn.configure(state="normal")

    def _open_result(self) -> None:
        if self.last_files:
            target = self.last_files[0] if len(self.last_files) == 1 else self.last_files[0].parent
            os.startfile(target)  # type: ignore[attr-defined]


class FindWindow(Window):
    title = "Find printer"

    def __init__(self, app: App) -> None:
        super().__init__(app)
        self.found: list[discovery.Found] = []
        f = self.frame
        ttk.Label(f, text="Printers on your network:").grid(row=0, column=0, columnspan=2, sticky="w")
        self.listbox = tk.Listbox(f, width=56, height=6)
        self.listbox.grid(row=1, column=0, columnspan=2, sticky="we", pady=4)
        row = ttk.Frame(f)
        row.grid(row=2, column=0, columnspan=2, sticky="e")
        self.search_btn = ttk.Button(row, text="Search again", command=self._search)
        self.search_btn.pack(side="left", padx=4)
        ttk.Button(row, text="Use selected", command=self._use_selected).pack(side="left", padx=4)
        ttk.Label(f, text="Or enter its IP address:").grid(row=3, column=0, sticky="w", pady=(12, 0))
        self.ip = tk.StringVar(value=app.cfg.host)
        manual = ttk.Frame(f)
        manual.grid(row=4, column=0, columnspan=2, sticky="we")
        ttk.Entry(manual, textvariable=self.ip, width=24).pack(side="left")
        ttk.Button(manual, text="Use this address", command=lambda: self._use(self.ip.get().strip())).pack(side="left", padx=6)
        self.status_var = tk.StringVar()
        ttk.Label(f, textvariable=self.status_var, foreground="#555", wraplength=380).grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 0))

    def show(self) -> None:
        super().show()
        if not self.found:
            self._search()

    def _search(self) -> None:
        self.search_btn.configure(state="disabled")
        self.status_var.set("Searching (about 5 seconds; longer if a full network sweep is needed)…")

        def work() -> None:
            try:
                found = discovery.discover()
            except Exception:
                log.exception("discovery failed")
                found = []
            self.app.post(lambda: self._show_found(found))

        threading.Thread(target=work, daemon=True).start()

    def _show_found(self, found: list[discovery.Found]) -> None:
        self.found = found
        self.listbox.delete(0, "end")
        for item in found:
            self.listbox.insert("end", item.label)
        if found:
            self.listbox.selection_set(0)
        self.search_btn.configure(state="normal")
        self.status_var.set(f"Found {len(found)} printer(s)." if found else "No printers found. Is it on Wi-Fi? You can also type its IP (printer menu: Settings → Network → WLAN → TCP/IP).")

    def _use_selected(self) -> None:
        sel = self.listbox.curselection()
        if sel:
            f = self.found[sel[0]]
            self._use(f.host, f.escl_url)

    def _use(self, host: str, escl_url: str = "") -> None:
        if not host:
            return
        self.status_var.set(f"Checking {host}…")

        def work() -> None:
            try:
                path, attrs = ipp.probe(host)
            except Exception as e:
                if not isinstance(e, (OSError, ipp.IppError)):
                    log.exception("probe failed")
                msg = f"No printer answered at {host} ({e})."
                self.app.post(lambda: self.status_var.set(msg))
                return
            model = str(ipp.first(attrs, "printer-make-and-model", ""))
            self.app.set_printer(host, path, model, escl_url)
            self.app.wake.set()
            self.app.post(lambda: self.status_var.set(f"Now using {model or host}."))

        threading.Thread(target=work, daemon=True).start()


def _key(mapping: dict[str, str], value: str, default: str) -> str:
    return next((k for k, v in mapping.items() if v == value), default)


def _single_instance() -> bool:
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.CreateMutexW(None, False, "Local\\PrinterManagerSingleInstance")
    return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def main() -> None:
    handler = logging.handlers.RotatingFileHandler(
        config.app_dir() / "printmgr.log", maxBytes=512_000, backupCount=1, encoding="utf-8"
    )
    logging.basicConfig(
        handlers=[handler],
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # The windowed build has no stderr: route uncaught thread exceptions to the log.
    threading.excepthook = lambda a: log.error(
        "uncaught exception in thread %s", a.thread.name if a.thread else "?",
        exc_info=(a.exc_type, a.exc_value, a.exc_traceback),
    )
    log.info("start (pid %d)", os.getpid())
    if not _single_instance():
        return
    try:
        autostart.repair()  # keep the Run entry pointing at this exe if the install moved
    except OSError:
        log.exception("autostart repair failed")
    App().run()
