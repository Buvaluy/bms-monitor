#!python3.10
# Tkinter + tray monitor for a JBD BMS over BLE; MOSFET control buttons are unlocked by the "Dev mode" switch.
# Pass --tray to start with the window hidden in the tray (used by the Windows autostart entry).
import asyncio
import json
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import pystray
from PIL import Image, ImageDraw, ImageFont

from alarm_history import ALARM, LINK, THRESHOLD, AlarmHistory, HistoryWindow
from jbd_bms import JbdBms, find_bms

CONFIG_PATH = Path(__file__).with_name("bms_config.json")
POLL_SECONDS = 120

PROTECTION_BITS = [
    "cell overvoltage", "cell undervoltage", "pack overvoltage", "pack undervoltage",
    "charge overtemp", "charge undertemp", "discharge overtemp", "discharge undertemp",
    "charge overcurrent", "discharge overcurrent", "short circuit", "front-end IC error",
    "MOS software lock", "charge timeout",
]

GREEN, YELLOW, RED = 0, 1, 2
LEVEL_COLORS = {GREEN: "#1a7f37", YELLOW: "#b58100", RED: "#cf222e"}
INACTIVE_COLOR = "#a0a7ae"
# (red below, yellow below, yellow above, red above); None = no limit. LiFePO4 4S, 140 Ah, 100 A BMS.
THRESHOLDS = {
    "soc": (20, 50, None, None),
    "pack_v": (11.2, 12.0, 13.8, 14.6),
    "cell_v": (2.8, 3.0, 3.45, 3.6),
    "cell_delta_mv": (None, None, 30, 100),
    "temp_c": (0, 10, 35, 45),
    "charge_a": (None, None, 28, 70),
    "discharge_a": (None, None, 50, 80),
}


def level(key: str, value: float) -> int:
    red_lo, yel_lo, yel_hi, red_hi = THRESHOLDS[key]
    if (red_lo is not None and value < red_lo) or (red_hi is not None and value > red_hi):
        return RED
    if (yel_lo is not None and value < yel_lo) or (yel_hi is not None and value > yel_hi):
        return YELLOW
    return GREEN


def value_levels(basic, cells, prot) -> dict:
    # JBD reports charge current as positive, discharge as negative.
    if basic.current >= 0:
        current_level = level("charge_a", basic.current)
    else:
        current_level = level("discharge_a", -basic.current)
    return {
        "SOC": level("soc", basic.soc),
        "Voltage": level("pack_v", basic.voltage),
        "Current": current_level,
        "Power": current_level,
        "Capacity": level("soc", basic.soc),
        "Temperature": max((level("temp_c", t) for t in basic.temps_c), default=GREEN),
        "Cells": max((level("cell_v", c) for c in cells), default=GREEN),
        "Cell delta": level("cell_delta_mv", (max(cells) - min(cells)) * 1000) if cells else GREEN,
        "Switches": GREEN if basic.charge_on and basic.discharge_on else YELLOW,
        "Protection": RED if prot else GREEN,
    }


def poll_conditions(basic, cells, prot) -> dict:
    """Return {name: (kind, detail)} for every protection bit and every value past its red limit in one poll."""
    texts = {
        "SOC": f"{basic.soc}%",
        "Voltage": f"{basic.voltage:.2f} V",
        "Current": f"{basic.current:+.2f} A",
        "Temperature": "  ".join(f"{t:.1f} °C" for t in basic.temps_c),
        "Cells": f"{min(cells):.3f} .. {max(cells):.3f} V" if cells else "",
        "Cell delta": f"{(max(cells) - min(cells)) * 1000:.0f} mV" if cells else "",
    }
    levels = value_levels(basic, cells, prot)
    found = {name: (THRESHOLD, text) for name, text in texts.items() if levels[name] == RED}
    found.update({f"Protection: {p}": (ALARM, "") for p in prot})
    return found


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


class BmsWorker:
    """Owns the asyncio loop and the BLE connection; reports to the UI through post(kind, payload)."""

    def __init__(self, post):
        self.post = post
        self.loop = asyncio.new_event_loop()
        self.bms = None
        self.paused = False
        self.wake = asyncio.Event()
        self.task = None
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

    async def _poll_once(self, cfg: dict):
        if not (self.bms and self.bms.is_connected):
            await self._disconnect()
            if not cfg.get("address"):
                self.post("status", "Scanning for BMS...")
                address, _name = await find_bms()
                if not address:
                    raise IOError("BMS not found")
                cfg["address"] = address
                save_config(cfg)
            self.post("status", f"Connecting to {cfg['address']}...")
            self.bms = JbdBms(cfg["address"])
            await self.bms.connect()
            self.post("status", f"Connected to {cfg['address']}")
        basic = await self.bms.read_basic()
        cells = await self.bms.read_cells()
        self.post("data", (basic, cells))

    async def _run(self):
        self.task = asyncio.current_task()
        cfg = load_config()
        while True:
            if self.paused:
                await self._disconnect()
                self.post("status", "Disconnected; the battery is free for the phone")
            else:
                try:
                    await self._poll_once(cfg)
                except Exception as e:
                    await self._disconnect()
                    self.post("status", f"Error: {str(e) or type(e).__name__}; next try in {POLL_SECONDS // 60} min or on Refresh")
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=None if self.paused else POLL_SECONDS)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    def refresh(self):
        self.loop.call_soon_threadsafe(self.wake.set)

    def set_paused(self, paused: bool):
        def apply():
            self.paused = paused
            self.wake.set()
        self.loop.call_soon_threadsafe(apply)

    def close(self):
        """Stop the poll loop, including a connect in progress, then release the BLE connection before exit."""
        async def job():
            self.paused = True
            if self.task:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
            await self._disconnect()
        return asyncio.run_coroutine_threadsafe(job(), self.loop)

    def set_mos(self, charge: bool, discharge: bool):
        async def job():
            if not (self.bms and self.bms.is_connected):
                self.post("error", "Switch command not sent: the BMS is not connected")
                return
            try:
                await self.bms.set_mos(charge, discharge)
                self.post("status", f"Sent: charging {'enabled' if charge else 'disabled'}, "
                                    f"output {'enabled' if discharge else 'disabled'}")
                self.wake.set()
            except Exception as e:
                self.post("error", f"Switch command failed: {str(e) or type(e).__name__}")
        asyncio.run_coroutine_threadsafe(job(), self.loop)


def tray_image(soc) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if soc is None:
        color = (128, 128, 128)
    elif soc >= 50:
        color = (46, 160, 67)
    elif soc >= 20:
        color = (210, 153, 34)
    else:
        color = (218, 54, 51)
    draw.rounded_rectangle((0, 0, 63, 63), radius=10, fill=color)
    text = "?" if soc is None else str(soc)
    try:
        font = ImageFont.truetype("arialbd.ttf", 40 if len(text) < 3 else 30)
    except OSError:
        font = ImageFont.load_default()
    draw.text((32, 32), text, fill="white", font=font, anchor="mm")
    return img


class App:
    def __init__(self):
        self.events = queue.Queue()
        self.last_basic = None
        self.alerted = set()
        self.history = AlarmHistory()
        self.history_win = None
        self.tray_soc = None
        self.worker = None
        self.closing = False

        self.root = tk.Tk()
        self.root.title("BMS Monitor")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.root.withdraw)
        self.root.bind("<<BmsEvent>>", lambda _e: self._pump())
        self._build_ui()

        self.tray = pystray.Icon("bms", tray_image(None), "BMS: no data", menu=pystray.Menu(
            pystray.MenuItem("Show", lambda: self.post("show", None), default=True),
            pystray.MenuItem("Exit", lambda: self.post("exit", None)),
        ))
        self.tray.run_detached()

        # Cross-thread event_generate needs a running mainloop, so the worker starts from inside it.
        self.root.after(0, lambda: setattr(self, "worker", BmsWorker(self.post)))

    def post(self, kind: str, payload):
        """Thread-safe: queue an event and wake the Tk loop instead of polling the queue on a timer."""
        self.events.put((kind, payload))
        try:
            self.root.event_generate("<<BmsEvent>>", when="tail")
        except (RuntimeError, tk.TclError):
            pass

    def _build_ui(self):
        pad = {"padx": 8, "pady": 2}
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")
        self.soc_var = tk.StringVar(value="--%")
        self.soc_label = ttk.Label(top, textvariable=self.soc_var, font=("Segoe UI", 28, "bold"))
        self.soc_label.pack(side="left")
        self.soc_bar = ttk.Progressbar(top, length=200, maximum=100)
        self.soc_bar.pack(side="left", padx=12)

        grid = ttk.Frame(self.root, padding=(8, 0))
        grid.pack(fill="x")
        self.vars = {}
        self.value_labels = {}
        rows = ["Voltage", "Current", "Power", "Capacity", "Cycles", "Temperature",
                "Cells", "Cell delta", "Switches", "Protection"]
        for i, name in enumerate(rows):
            ttk.Label(grid, text=name + ":").grid(row=i, column=0, sticky="w", **pad)
            self.vars[name] = tk.StringVar(value="--")
            self.value_labels[name] = ttk.Label(grid, textvariable=self.vars[name])
            self.value_labels[name].grid(row=i, column=1, sticky="w", **pad)
        self.value_labels["Protection"].config(wraplength=420)

        prot_box = ttk.LabelFrame(self.root, text="All protections", padding=8)
        prot_box.pack(fill="x", padx=8, pady=(8, 0))
        self.prot_labels = {}
        for i, name in enumerate(PROTECTION_BITS):
            lbl = ttk.Label(prot_box, text=f"{i:>2}  {name}", foreground=INACTIVE_COLOR, width=24)
            lbl.grid(row=i % 7, column=i // 7, sticky="w", padx=4)
            self.prot_labels[name] = lbl

        conn =ttk.Frame(self.root, padding=(8, 8, 8, 0))
        conn.pack(fill="x")
        self.refresh_btn = ttk.Button(conn, text="Refresh", command=self._refresh)
        self.refresh_btn.pack(side="left")
        self.connect_btn = ttk.Button(conn, text="Disconnect", command=self._toggle_connection)
        self.connect_btn.pack(side="left", padx=8)
        self.history_btn = ttk.Button(conn, text="History", command=self._show_history)
        self.history_btn.pack(side="left", padx=(0, 8))
        self.updated_var = tk.StringVar(value="Updated: --")
        ttk.Label(conn, textvariable=self.updated_var, foreground="gray").pack(side="left")

        ctl = ttk.LabelFrame(self.root, text="Control", padding=8)
        ctl.pack(fill="x", padx=8, pady=8)
        self.dev_mode = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctl, text="Dev mode", variable=self.dev_mode,
                        command=self._update_buttons).pack(side="left")
        self.chg_btn = ttk.Button(ctl, text="Charging", command=lambda: self._toggle("charge"))
        self.chg_btn.pack(side="left", padx=8)
        self.dsg_btn = ttk.Button(ctl, text="Output", command=lambda: self._toggle("discharge"))
        self.dsg_btn.pack(side="left")

        self.status_var = tk.StringVar(value="Starting...")
        ttk.Label(self.root, textvariable=self.status_var, foreground="gray", wraplength=500,
                  padding=(8, 0, 8, 8)).pack(fill="x")
        self._update_buttons()

    def _refresh(self):
        if self.worker:
            self.status_var.set("Refreshing...")
            self.worker.refresh()

    def _toggle_connection(self):
        paused = self.connect_btn.cget("text") == "Disconnect"
        self.connect_btn.config(text="Connect" if paused else "Disconnect")
        self.refresh_btn.config(state="disabled" if paused else "normal")
        self._update_buttons()
        if self.worker:
            self.worker.set_paused(paused)

    def _update_buttons(self):
        b = self.last_basic
        connected = self.connect_btn.cget("text") == "Disconnect"
        state = "normal" if self.dev_mode.get() and b and connected else "disabled"
        for btn, name, on in ((self.chg_btn, "charging", b and b.charge_on), (self.dsg_btn, "output", b and b.discharge_on)):
            btn.config(state=state, text=f"{'Disable' if on else 'Enable'} {name}" if b else name.capitalize())

    def _toggle(self, which: str):
        b = self.last_basic
        charge, discharge = b.charge_on, b.discharge_on
        if which == "charge":
            charge = not charge
        else:
            discharge = not discharge
        enable = charge if which == "charge" else discharge
        action = {
            ("charge", True): "Enable charging?",
            ("charge", False): "Disable charging? The battery will stop accepting charge.",
            ("discharge", True): "Enable output?",
            ("discharge", False): "Disable output? Everything powered by the battery will lose power.",
        }[which, enable]
        if messagebox.askyesno("Confirm", action, parent=self.root):
            self.worker.set_mos(charge, discharge)

    def _show_data(self, basic, cells):
        if self.status_var.get() == "Refreshing...":
            self.status_var.set("Refreshed")
        self.last_basic = basic
        self.soc_var.set(f"{basic.soc}%")
        self.soc_bar["value"] = basic.soc
        v = self.vars
        v["Voltage"].set(f"{basic.voltage:.2f} V")
        v["Current"].set(f"{basic.current:+.2f} A")
        v["Power"].set(f"{basic.power:+.1f} W")
        v["Capacity"].set(f"{basic.remaining_ah:.1f} / {basic.nominal_ah:.1f} Ah")
        v["Cycles"].set(str(basic.cycles))
        v["Temperature"].set("  ".join(f"{t:.1f} °C" for t in basic.temps_c) or "--")
        v["Cells"].set("  ".join(
            f"{c:.3f}{'*' if basic.balance_mask >> i & 1 else ''}" for i, c in enumerate(cells)))
        v["Cell delta"].set(f"{(max(cells) - min(cells)) * 1000:.0f} mV" if cells else "--")
        v["Switches"].set(f"Charging: {'enabled' if basic.charge_on else 'DISABLED'}, "
                          f"Output: {'enabled' if basic.discharge_on else 'DISABLED'}")
        prot = [PROTECTION_BITS[i] if i < len(PROTECTION_BITS) else f"bit {i}"
                for i in range(16) if basic.protection_mask >> i & 1]
        v["Protection"].set(", ".join(prot) or "none")
        for name, lbl in self.prot_labels.items():
            on = name in prot
            lbl.config(foreground=LEVEL_COLORS[RED] if on else INACTIVE_COLOR,
                       font=("Segoe UI", 9, "bold" if on else "normal"))
        self._apply_levels(basic, cells, prot)
        if self.history.observe(poll_conditions(basic, cells, prot), time.time()):
            self._history_changed()
        self._update_buttons()
        self.updated_var.set(f"Updated: {time.strftime('%H:%M:%S')}")
        if basic.soc != self.tray_soc:
            self.tray_soc = basic.soc
            self.tray.icon = tray_image(basic.soc)
        self.tray.title = f"BMS {basic.soc}%  {basic.voltage:.2f} V  {basic.current:+.2f} A"

    def _apply_levels(self, basic, cells, prot):
        levels = value_levels(basic, cells, prot)
        self.soc_label.config(foreground=LEVEL_COLORS[levels["SOC"]])
        for name, lvl in levels.items():
            if name in self.value_labels:
                self.value_labels[name].config(foreground=LEVEL_COLORS[lvl])

        # Each protection is tracked on its own, so a new one notifies even while the row is already red.
        red_now = {n for n, lvl in levels.items() if lvl == RED and n not in ("Power", "Capacity", "Protection")}
        red_now |= {f"Protection: {p}" for p in prot}
        new = red_now - self.alerted
        self.alerted = red_now
        if new:
            text = "; ".join(n if n.startswith("Protection: ") else
                             f"{n}: {self.soc_var.get() if n == 'SOC' else self.vars[n].get()}" for n in sorted(new))
            self.status_var.set(f"ALERT: {text}")
            self.tray.notify(text, "Battery alert")

    def _pump(self):
        while not self.closing and not self.events.empty():
            kind, payload = self.events.get()
            if kind == "data":
                self._show_data(*payload)
            elif kind == "status":
                self.status_var.set(payload)
                self._track_status(payload)
            elif kind == "error":
                self.status_var.set(payload)
                if self.history.note(LINK, "Command failed", payload, time.time()):
                    self._history_changed()
                messagebox.showerror("BMS", payload, parent=self.root)
            elif kind == "show":
                self.root.deiconify()
                self.root.lift()
            elif kind == "exit":
                self.closing = True
                self.root.withdraw()
                self.tray.stop()
                self._finish_exit(self.worker.close() if self.worker else None, time.monotonic() + 5)
                return

    def _track_status(self, text: str):
        now = time.time()
        if text.startswith(("Error:", "Worker stopped")):
            changed = self.history.link_down(text, now)
        elif text.startswith("Connected to"):
            changed = self.history.link_up(now)
        elif text.startswith("Disconnected;"):
            changed = self.history.cut(now)
        else:
            changed = False
        if changed:
            self._history_changed()

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
            self.history_win = HistoryWindow(self.root, self.history, "JBD BMS: session history")

    def _finish_exit(self, fut, deadline):
        # Tk keeps running while the worker disconnects, so a worker post cannot block the disconnect.
        if fut and not fut.done() and time.monotonic() < deadline:
            self.root.after(100, self._finish_exit, fut, deadline)
        else:
            self.root.destroy()


if __name__ == "__main__":
    app = App()
    if "--tray" in sys.argv:
        app.root.withdraw()
    app.root.mainloop()
