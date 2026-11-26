# Parts shared by the JK and JBD monitor apps: config file I/O, the single-instance check, the BLE worker loop,
# and the window's event pump, link-loss alert, session history and Scan dialog.
import asyncio
import ctypes
import json
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

import pystray

from alarm_history import LINK, AlarmHistory, HistoryWindow, clock
from version import __version__

# A tray balloon reports a lost link only if it stays lost this long, so short drops do not notify.
LINK_ALERT_SECONDS = 300
# The BMS is not polled while the Scan dialog is open, so an unanswered dialog closes itself to resume alarms.
SCAN_CHOICE_SECONDS = 120
ERROR_ALREADY_EXISTS = 183


def signal_bars(rssi: int) -> str:
    lit = 1 + sum(rssi >= t for t in (-87, -79, -71, -63, -55))
    return "▁▂▃▄▅▆"[:lit] + "·" * (6 - lit)


def read_config(path) -> dict:
    if not path.exists():
        return {}
    cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(cfg, dict):
        raise ValueError("not a JSON object")
    return cfg


def write_config(path, cfg: dict):
    # Write a new file and swap it in, so a crash mid-write cannot leave a truncated config.
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(path)


def another_instance_running(name: str) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ctypes.set_last_error(0)
    kernel32.CreateMutexW(None, False, name)  # never closed: the mutex lives until this process exits
    return ctypes.get_last_error() == ERROR_ALREADY_EXISTS


def run_app(app_class, mutex_name: str):
    """Start app_class unless another instance holds mutex_name; --tray starts with the window hidden."""
    title = app_class.window_title
    if another_instance_running(mutex_name):
        ctypes.windll.user32.MessageBoxW(None, f"{title} is already running; open it from the tray.", title, 0x40)
        sys.exit(1)
    app = app_class()
    if "--tray" in sys.argv:
        app.root.withdraw()
    app.root.mainloop()


class MonitorWorker:
    """Owns the asyncio loop and the BLE connection; reports to the UI through post(kind, payload).

    A subclass sets up its own state before calling __init__, which starts the loop, and implements _poll_once(cfg)
    and _find_all(); it sets scan_status, the status line shown while a Scan runs.
    """

    scan_status = ""

    def __init__(self, post, poll_seconds, retry_seconds: int, load_config, save_config, config_name: str):
        self.scan_requested = False
        self.scanning = False
        self.choice = None
        self.post = post
        self.poll_seconds, self.retry_seconds = poll_seconds, retry_seconds
        self._load_config, self._save_config, self.config_name = load_config, save_config, config_name
        self.loop = asyncio.new_event_loop()
        self.bms = None
        self.paused = False
        self.wake = asyncio.Event()
        self.task = None
        self.poll = None
        self.closing = False
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        asyncio.run_coroutine_threadsafe(self._run(), self.loop).add_done_callback(self._on_stopped)

    def _on_stopped(self, f):
        if not f.cancelled() and f.exception() is not None:
            self.post("status", f"Worker stopped: {f.exception()!r}; restart the monitor")

    async def _disconnect(self):
        if self.bms:
            try:
                await self.bms.disconnect()
            except Exception:
                pass
            self.bms = None

    def _save(self, cfg: dict):
        try:
            self._save_config(cfg)
        except OSError as e:
            self.post("status", f"Could not save {self.config_name}: {e}")

    async def _poll_once(self, cfg: dict):
        raise NotImplementedError

    async def _find_all(self) -> list:
        """Return [(address, name, rssi)] of the BMS units nearby, strongest signal first."""
        raise NotImplementedError

    def _bms_changed(self):
        """The user chose another BMS: drop state that belongs to the previous one."""

    async def _handle_requests(self, cfg: dict) -> bool:
        """Run a Scan or apply its choice; True keeps this round from polling."""
        if self.choice is not None:
            address, self.choice = self.choice, None
            if address and address != cfg.get("address"):
                await self._disconnect()
                self._bms_changed()
                cfg["address"] = address
                self._save(cfg)
            self.scanning = False
        if self.scan_requested:
            self.scan_requested, self.scanning = False, True
            # A connected BMS does not advertise, so the current one is released to show up in the list.
            await self._disconnect()
            self.post("status", self.scan_status)
            try:
                self.post("scan", (await self._find_all(), cfg.get("address")))
            except Exception as e:
                self.post("scan", ([], cfg.get("address")))
                self.post("status", f"Scan failed: {str(e) or type(e).__name__}")
            return True
        return self.scanning

    def scan(self):
        def apply():
            self.scan_requested = True
            self.wake.set()
        self.loop.call_soon_threadsafe(apply)

    def choose(self, address):
        """Finish a scan: connect to address, or pass "" to keep the configured BMS."""
        def apply():
            self.choice = address
            self.wake.set()
        self.loop.call_soon_threadsafe(apply)

    async def _run(self):
        self.task = asyncio.current_task()
        try:
            cfg = self._load_config()
        except (OSError, ValueError) as e:
            cfg = {}
            self.post("status", f"Could not read {self.config_name}: {e}")
        # Not "while True": before Python 3.12, wait_for can swallow the cancel that close() sends.
        while not self.closing:
            delay = None
            if await self._handle_requests(cfg):
                pass
            elif self.paused:
                await self._disconnect()
                self.post("status", "Disconnected; the battery is free for the phone")
            else:
                delay = self.poll_seconds
                self.poll = asyncio.ensure_future(self._poll_once(cfg))
                try:
                    await self.poll
                except asyncio.CancelledError:
                    if self.closing:
                        raise
                    await self._disconnect()
                except Exception as e:
                    await self._disconnect()
                    delay = self.retry_seconds
                    self.post("status", f"Error: {str(e) or type(e).__name__}; "
                                        f"next try in {self.retry_seconds // 60} min or on Refresh")
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    def set_paused(self, paused: bool):
        def apply():
            self.paused = paused
            self.wake.set()
            if paused and self.poll:
                self.poll.cancel()
        self.loop.call_soon_threadsafe(apply)

    def close(self):
        """Stop the poll loop, including a connect in progress, then release the BLE connection before exit."""
        async def job():
            self.paused = self.closing = True
            if self.task:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
            await self._disconnect()
        return asyncio.run_coroutine_threadsafe(job(), self.loop)


class MonitorApp:
    """The window and tray icon of one monitor.

    A subclass sets the class attributes below, sets up its own state before calling __init__, and implements
    _build_ui() and _handle_event(kind, payload) for the event kinds its worker posts besides status and scan.
    _build_ui() creates scan_btn, which calls _scan.
    """

    worker_class = None
    bms_kind = ""
    window_title = ""
    tray_name = ""
    tray_idle_title = ""
    tray_label = ""
    alert_title = ""
    history_title = ""
    tray_image = None

    def __init__(self):
        self.scan_busy = False
        self.events = queue.Queue()
        self.alerted = set()
        self.history = AlarmHistory()
        self.history_win = None
        self.tray_soc = None
        self.worker = None
        self.closing = False
        self.no_link_since = None
        self.link_alert_job = None
        self.link_alerted = False

        self.root = tk.Tk()
        self.root.title(f"{self.window_title} {__version__}")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.root.withdraw)
        self.root.bind("<<BmsEvent>>", lambda _e: self._pump())
        self._build_ui()

        self.tray = pystray.Icon(self.tray_name, self.tray_image(None), self.tray_idle_title, menu=pystray.Menu(
            pystray.MenuItem("Show", lambda: self.post("show", None), default=True),
            pystray.MenuItem("Exit", lambda: self.post("exit", None)),
        ))
        self.tray.run_detached()

        # Cross-thread event_generate needs a running mainloop, so the worker starts from inside it.
        self.root.after(0, lambda: setattr(self, "worker", self.worker_class(self.post)))

    def post(self, kind: str, payload):
        """Thread-safe: queue an event and wake the Tk loop instead of polling the queue on a timer."""
        self.events.put((kind, payload))
        try:
            self.root.event_generate("<<BmsEvent>>", when="tail")
        except (RuntimeError, tk.TclError):
            pass

    def _refresh(self):
        if self.worker:
            self.status_var.set("Refreshing...")
            self.worker.refresh()

    def _pump(self):
        while not self.closing and not self.events.empty():
            kind, payload = self.events.get()
            if kind == "status":
                self.status_var.set(payload)
                self._track_status(payload)
            elif kind == "show":
                self.root.deiconify()
                self.root.lift()
            elif kind == "exit":
                self.closing = True
                self.root.withdraw()
                self.tray.stop()
                self._finish_exit(self.worker.close() if self.worker else None, time.monotonic() + 5)
                return
            elif kind == "scan":
                self._show_scan(*payload)
            else:
                self._handle_event(kind, payload)

    def _track_status(self, text: str):
        now = time.time()
        if text.startswith(("Error:", "Worker stopped")):
            changed = self.history.link_down(text, now)
            self._link_lost(by_user=False)
        elif text.startswith("Connected to"):
            changed = self.history.link_up(now)
        elif text.startswith("Could not"):
            changed = self.history.note(LINK, "Config", text, now)
        elif text.startswith("Disconnected;"):
            changed = self.history.cut(now)
            self._link_lost(by_user=True)
        elif text.startswith("Scan failed"):
            changed = self.history.note(LINK, "Scan failed", text, now)
        elif text.startswith("Scanning"):
            changed = self.history.cut(now)
            # "Scanning" also comes from the automatic search when no address is saved; only a Scan click is the user's.
            self._link_lost(by_user=self.scan_busy)
        else:
            changed = False
        if changed:
            self._history_changed()

    def _scan(self):
        if self.worker and self.connect_btn.cget("text") == "Disconnect":
            self.scan_busy = True
            self.scan_btn.config(state="disabled")
            self.worker.scan()

    def _show_scan(self, found: list, current: str):
        self.status_var.set(f"Found {len(found)} {self.bms_kind}; choose one or Cancel")
        self.root.deiconify()
        dlg = tk.Toplevel(self.root)
        dlg.title(f"Choose {self.bms_kind}")
        dlg.resizable(False, False)
        dlg.transient(self.root)
        lb = tk.Listbox(dlg, width=66, height=max(3, min(len(found), 8)), font=("Cascadia Mono", 10))
        lb.pack(padx=8, pady=8)
        for address, name, rssi in found:
            lb.insert("end", f"{name:<18} {address}  {signal_bars(rssi)}  {rssi} dBm" + ("  (current)" if address == current else ""))
        if found:
            lb.selection_set(0)
        else:
            lb.insert("end", f"No {self.bms_kind} found; disconnect the phone and rescan")

        def close_dialog():
            dlg.after_cancel(timeout)
            dlg.destroy()

        def finish(address):
            close_dialog()
            self.scan_busy = False
            self.scan_btn.config(state="normal" if self.connect_btn.cget("text") == "Disconnect" else "disabled")
            if address and address != current:
                self._forget_bms(address)
            self.worker.choose(address)

        def rescan():
            if self.connect_btn.cget("text") != "Disconnect":
                finish("")
                return
            close_dialog()
            # The worker checks a scan request before the open choice, so no choose("") is sent to race with it.
            self.worker.scan()

        def connect():
            sel = lb.curselection()
            if found and sel:
                finish(found[sel[0]][0])

        buttons = ttk.Frame(dlg, padding=(8, 0, 8, 8))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Connect", command=connect,
                   state="normal" if found else "disabled").pack(side="left")
        ttk.Button(buttons, text="Rescan", command=rescan).pack(side="left", padx=8)
        ttk.Button(buttons, text="Cancel", command=lambda: finish("")).pack(side="right")
        lb.bind("<Double-Button-1>", lambda _e: connect())
        dlg.protocol("WM_DELETE_WINDOW", lambda: finish(""))
        timeout = dlg.after(SCAN_CHOICE_SECONDS * 1000, lambda: finish(""))

    def _forget_bms(self, address: str):
        """Another BMS was chosen: drop the alert state read from the previous one."""
        self.alerted = set()
        if self.history.note(LINK, "BMS changed", address, time.time()):
            self._history_changed()

    def _link_lost(self, by_user: bool):
        if self.no_link_since is None:
            self.no_link_since = time.time()
            self.tray_soc = None
            self.tray.icon = self.tray_image(None)
            self.tray.title = f"{self.tray_label}: no link since {clock(self.no_link_since)}, last {self.soc_var.get()}"
        if by_user:
            self._cancel_link_alert()
            self.link_alerted = False
        elif not self.link_alert_job and not self.link_alerted:
            self.link_alert_job = self.root.after(LINK_ALERT_SECONDS * 1000, self._link_alert)

    def _link_alert(self):
        self.link_alert_job, self.link_alerted = None, True
        self.tray.notify(f"No link to the BMS since {clock(self.no_link_since)}; alarms are not checked",
                         self.alert_title)

    def _link_up(self):
        """Data arrived: the link counts as up only now, since a connect can succeed while reads fail."""
        self.no_link_since, self.link_alerted = None, False
        self._cancel_link_alert()

    def _cancel_link_alert(self):
        if self.link_alert_job:
            self.root.after_cancel(self.link_alert_job)
            self.link_alert_job = None

    def _history_changed(self):
        active = self.history.active_count()
        self.history_btn.config(text=f"History ({len(self.history.entries)}"
                                     + (f", {active} active)" if active else ")"))
        if self.history_win and self.history_win.alive():
            self.history_win.refresh()

    def _show_history(self):
        if self.history_win and self.history_win.alive():
            self.history_win.show()
        else:
            self.history_win = HistoryWindow(self.root, self.history, self.history_title)

    def _finish_exit(self, fut, deadline):
        # Tk keeps running while the worker disconnects, so a worker post cannot block the disconnect.
        if fut and not fut.done() and time.monotonic() < deadline:
            self.root.after(100, self._finish_exit, fut, deadline)
        else:
            self.root.destroy()
