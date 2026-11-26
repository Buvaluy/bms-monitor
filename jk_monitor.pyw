#!python3.10
# Tkinter + tray monitor for a JK (Jikong) BMS over BLE; read-only, a separate app from the JBD bms_monitor.pyw.
# Pass --tray to start with the window hidden in the tray.
import asyncio
import json
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import pystray
from PIL import Image, ImageDraw, ImageFont

from jk_bms import NOTICE_ALARMS, JkBms, find_bms

CONFIG_PATH = Path(__file__).with_name("jk_config.json")
POLL_SECONDS = 120

GREEN, YELLOW, RED = 0, 1, 2
LEVEL_COLORS = {GREEN: "#1a7f37", YELLOW: "#b58100", RED: "#cf222e"}
# (red below, yellow below, yellow above, red above); None = no limit. LiFePO4 4S, 334 Ah, 200 A BMS.
THRESHOLDS = {
    "soc": (20, 50, None, None),
    "pack_v": (11.2, 12.0, 13.8, 14.6),
    "cell_v": (2.8, 3.0, 3.45, 3.6),
    "cell_delta_mv": (None, None, 30, 100),
    "temp_c": (0, 10, 35, 45),
    "charge_a": (None, None, 100, 160),
    "discharge_a": (None, None, 120, 180),
}


def level(key: str, value: float) -> int:
    red_lo, yel_lo, yel_hi, red_hi = THRESHOLDS[key]
    if (red_lo is not None and value < red_lo) or (red_hi is not None and value > red_hi):
        return RED
    if (yel_lo is not None and value < yel_lo) or (yel_hi is not None and value > yel_hi):
        return YELLOW
    return GREEN


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
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        asyncio.run_coroutine_threadsafe(self._run(), self.loop)

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
                self.post("status", "Scanning for JK BMS...")
                address, _name = await find_bms()
                if not address:
                    raise IOError("JK BMS not found")
                cfg["address"] = address
                save_config(cfg)
            self.post("status", f"Connecting to {cfg['address']}...")
            self.bms = JkBms(cfg["address"])
            await self.bms.connect()
            self.post("device", self.bms.device_info)
            self.post("status", f"Connected to {cfg['address']}")
        self.post("data", await self.bms.read_cell_info())

    async def _run(self):
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
                    self.post("status", f"Error: {e or type(e).__name__}; next try in {POLL_SECONDS // 60} min or on Refresh")
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
    # Square corners tell this icon apart from the rounded JBD one.
    draw.rectangle((0, 0, 63, 63), fill=color)
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
        self.alerted = set()
        self.tray_soc = None
        self.worker = None

        self.root = tk.Tk()
        self.root.title("JK BMS Monitor")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.root.withdraw)
        self.root.bind("<<BmsEvent>>", lambda _e: self._pump())
        self._build_ui()

        self.tray = pystray.Icon("jk_bms", tray_image(None), "JK BMS: no data", menu=pystray.Menu(
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
        rows = ["Voltage", "Current", "Power", "Capacity", "Cycles", "Temperature", "MOS temp",
                "Cells", "Cell delta", "Balancing", "Switches", "Alarms", "Device"]
        for i, name in enumerate(rows):
            ttk.Label(grid, text=name + ":").grid(row=i, column=0, sticky="w", **pad)
            self.vars[name] = tk.StringVar(value="--")
            self.value_labels[name] = ttk.Label(grid, textvariable=self.vars[name])
            self.value_labels[name].grid(row=i, column=1, sticky="w", **pad)

        conn = ttk.Frame(self.root, padding=(8, 8, 8, 0))
        conn.pack(fill="x")
        self.refresh_btn = ttk.Button(conn, text="Refresh", command=self._refresh)
        self.refresh_btn.pack(side="left")
        self.connect_btn = ttk.Button(conn, text="Disconnect", command=self._toggle_connection)
        self.connect_btn.pack(side="left", padx=8)
        self.updated_var = tk.StringVar(value="Updated: --")
        ttk.Label(conn, textvariable=self.updated_var, foreground="gray").pack(side="left")

        self.status_var = tk.StringVar(value="Starting...")
        ttk.Label(self.root, textvariable=self.status_var, foreground="gray",
                  padding=(8, 8, 8, 8)).pack(fill="x")

    def _refresh(self):
        if self.worker:
            self.status_var.set("Refreshing...")
            self.worker.refresh()

    def _toggle_connection(self):
        paused = self.connect_btn.cget("text") == "Disconnect"
        self.connect_btn.config(text="Connect" if paused else "Disconnect")
        self.refresh_btn.config(state="disabled" if paused else "normal")
        if self.worker:
            self.worker.set_paused(paused)

    def _show_device(self, info: dict):
        self.vars["Device"].set(f"{info['model']}  hw {info['hw']}  sw {info['sw']}  s/n {info['serial']}")

    def _show_data(self, c):
        self.soc_var.set(f"{c.soc}%")
        self.soc_bar["value"] = c.soc
        v = self.vars
        v["Voltage"].set(f"{c.voltage:.2f} V")
        v["Current"].set(f"{c.current:+.2f} A")
        v["Power"].set(f"{c.power:+.1f} W")
        v["Capacity"].set(f"{c.remaining_ah:.1f} / {c.nominal_ah:.1f} Ah  (SOH {c.soh}%)")
        v["Cycles"].set(f"{c.cycles}  ({c.cycle_ah:.0f} Ah total)")
        v["Temperature"].set("  ".join(f"{t:.1f} °C" for t in c.temps_c) or "--")
        v["MOS temp"].set(f"{c.mos_temp_c:.1f} °C")
        v["Cells"].set("  ".join(f"{x:.3f}" for x in c.cells))
        v["Cell delta"].set(f"{(max(c.cells) - min(c.cells)) * 1000:.0f} mV" if c.cells else "--")
        v["Balancing"].set({0: "off", 1: "charging", 2: "discharging"}.get(c.balancing, str(c.balancing))
                           + (f", {c.balance_current:+.3f} A" if c.balancing else ""))
        v["Switches"].set(f"Charging: {'enabled' if c.charge_on else 'DISABLED'}, "
                          f"Output: {'enabled' if c.discharge_on else 'DISABLED'}")
        v["Alarms"].set(", ".join(c.alarms) or "none")
        self._apply_levels(c)
        self.updated_var.set(f"Updated: {time.strftime('%H:%M:%S')}")
        if c.soc != self.tray_soc:
            self.tray_soc = c.soc
            self.tray.icon = tray_image(c.soc)
        self.tray.title = f"JK {c.soc}%  {c.voltage:.2f} V  {c.current:+.2f} A"

    def _apply_levels(self, c):
        # JK reports charge current as positive, discharge as negative.
        if c.current >= 0:
            current_level = level("charge_a", c.current)
        else:
            current_level = level("discharge_a", -c.current)
        serious = [a for a in c.alarms if a not in NOTICE_ALARMS]
        levels = {
            "SOC": level("soc", c.soc),
            "Voltage": level("pack_v", c.voltage),
            "Current": current_level,
            "Power": current_level,
            "Capacity": level("soc", c.soc),
            "Temperature": max((level("temp_c", t) for t in c.temps_c), default=GREEN),
            "MOS temp": level("temp_c", c.mos_temp_c),
            "Cells": max((level("cell_v", x) for x in c.cells), default=GREEN),
            "Cell delta": level("cell_delta_mv", (max(c.cells) - min(c.cells)) * 1000) if c.cells else GREEN,
            "Switches": GREEN if c.charge_on and c.discharge_on else YELLOW,
            "Alarms": RED if serious else YELLOW if c.alarms else GREEN,
        }
        self.soc_label.config(foreground=LEVEL_COLORS[levels["SOC"]])
        for name, lvl in levels.items():
            if name in self.value_labels:
                self.value_labels[name].config(foreground=LEVEL_COLORS[lvl])

        red_now = {n for n, lvl in levels.items() if lvl == RED and n not in ("Power", "Capacity")}
        new = red_now - self.alerted
        self.alerted = red_now
        if new:
            text = "; ".join(f"{n}: {self.soc_var.get() if n == 'SOC' else self.vars[n].get()}" for n in sorted(new))
            self.status_var.set(f"ALERT: {text}")
            self.tray.notify(text, "JK battery alert")

    def _pump(self):
        while not self.events.empty():
            kind, payload = self.events.get()
            if kind == "data":
                self._show_data(payload)
            elif kind == "device":
                self._show_device(payload)
            elif kind == "status":
                self.status_var.set(payload)
            elif kind == "show":
                self.root.deiconify()
                self.root.lift()
            elif kind == "exit":
                self.tray.stop()
                self.root.destroy()
                return


if __name__ == "__main__":
    app = App()
    if "--tray" in sys.argv:
        app.root.withdraw()
    app.root.mainloop()
