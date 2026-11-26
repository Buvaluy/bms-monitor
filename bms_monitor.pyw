#!python3.13
# Tkinter + tray monitor for a JBD BMS over BLE; MOSFET control buttons are unlocked by the "Dev mode" switch.
# Pass --tray to start with the window hidden in the tray (used by the Windows autostart entry).
import asyncio
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from PIL import Image, ImageDraw, ImageFont

from alarm_history import ALARM, LINK, THRESHOLD
from jbd_bms import JbdBms, find_bms
from monitor_common import MonitorApp, MonitorWorker, read_config, run_app, write_config

CONFIG_PATH = Path(__file__).with_name("bms_config.json")
# The link stays open between polls, so a short interval costs two small reads, not a reconnect.
POLL_SECONDS = 15
RETRY_SECONDS = 120

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
        "Cell delta": level("cell_delta_mv", round((max(cells) - min(cells)) * 1000)) if cells else GREEN,
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
    return read_config(CONFIG_PATH)


def save_config(cfg: dict):
    write_config(CONFIG_PATH, cfg)


class UserDisconnected(Exception):
    pass


class BmsWorker(MonitorWorker):
    def __init__(self, post):
        # Register E1 sets both MOSFETs at once; this is its "disable" mask as this monitor last sent it.
        self.mos_off = 0
        self.switching = asyncio.Lock()  # one switch command at a time: each builds its mask from the last one's mos_off
        super().__init__(post, POLL_SECONDS, RETRY_SECONDS, load_config, save_config, CONFIG_PATH.name)

    async def _poll_once(self, cfg: dict):
        if not (self.bms and self.bms.is_connected):
            if self.bms:
                self.post("status", "Error: link lost; reconnecting")
            await self._disconnect()
            if not cfg.get("address"):
                self.post("status", "Scanning for BMS...")
                address, _name = await find_bms()
                if not address:
                    raise IOError("BMS not found")
                cfg["address"] = address
                self._save(cfg)
            self.post("status", f"Connecting to {cfg['address']}...")
            self.bms = JbdBms(cfg["address"])
            await self.bms.connect()
            self.post("status", f"Connected to {cfg['address']}")
        basic = await self.bms.read_basic()
        cells = await self.bms.read_cells()
        self.post("data", (basic, cells))

    def refresh(self):
        self.loop.call_soon_threadsafe(self.wake.set)

    def set_switch(self, bit: int, on: bool):
        """Turn one MOSFET (bit 0x01 charge, 0x02 output) on or off; the other keeps the state in mos_off."""
        async def job():
            async with self.switching:
                bms = self.bms
                if not (bms and bms.is_connected):
                    self.post("error", "Switch command not sent: the BMS is not connected")
                    return
                try:
                    basic = await bms.read_basic()
                except Exception as e:
                    self._switch_not_sent(e)
                    return
                # A MOSFET the BMS reports on is not disabled, whatever was sent before (the phone app writes E1 too).
                self.mos_off &= ~((0x01 if basic.charge_on else 0) | (0x02 if basic.discharge_on else 0))
                mask = self.mos_off & ~bit if on else self.mos_off | bit
                charge, discharge = not mask & 0x01, not mask & 0x02
                try:
                    await bms.set_mos(charge, discharge, before_write=self._refuse_if_paused)
                except UserDisconnected as e:
                    self._switch_not_sent(e)
                    return
                except Exception as e:
                    self.post("error", f"Switch command not confirmed: {str(e) or type(e).__name__}. "
                                       "The BMS may have applied it; check the switches after the refresh")
                else:
                    self.post("status", f"Sent: charging {'enabled' if charge else 'disabled'}, "
                                        f"output {'enabled' if discharge else 'disabled'}")
                self.mos_off = mask
                self.wake.set()
        asyncio.run_coroutine_threadsafe(job(), self.loop)

    def _refuse_if_paused(self):
        if self.paused:
            raise UserDisconnected

    def _switch_not_sent(self, e: Exception):
        if self.paused:
            self.post("status", "Switch command not sent: the BMS was disconnected")
        else:
            self.post("error", f"Switch command not sent: {str(e) or type(e).__name__}")


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


class App(MonitorApp):
    worker_class = BmsWorker
    window_title = "BMS Monitor"
    tray_name = "bms"
    tray_idle_title = "BMS: no data"
    tray_label = "BMS"
    alert_title = "Battery alert"
    history_title = "JBD BMS: session history"
    tray_image = staticmethod(tray_image)

    def __init__(self):
        self.last_basic = None
        super().__init__()

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
        enable = not (b.charge_on if which == "charge" else b.discharge_on)
        action = {
            ("charge", True): "Enable charging?",
            ("charge", False): "Disable charging? The battery will stop accepting charge.",
            ("discharge", True): "Enable output?",
            ("discharge", False): "Disable output? Everything powered by the battery will lose power.",
        }[which, enable]
        other_bit, other_on, other = ((0x02, b.discharge_on, "output") if which == "charge"
                                      else (0x01, b.charge_on, "charging"))
        if not other_on and not self.worker.mos_off & other_bit:
            action += (f"\n\n{other.capitalize()} is off now (a protection, or a lock set by another app). "
                       f"This command also allows {other} again.")
        if messagebox.askyesno("Confirm", action, parent=self.root):
            self.worker.set_switch(0x01 if which == "charge" else 0x02, enable)

    def _show_data(self, basic, cells):
        self._link_up()
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
        if self.history.observe(poll_conditions(basic, cells, prot), time.time()):
            self._history_changed()
        self._apply_levels(basic, cells, prot)
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
        new, gone = red_now - self.alerted, self.alerted - red_now
        self.alerted = red_now
        if new:
            self.status_var.set(f"ALERT: {self._alert_text(red_now)}")
            text = self._alert_text(new)
            # A Windows tray balloon holds 255 characters; pystray raises ValueError on longer text.
            self.tray.notify(text if len(text) < 256 else text[:254] + "…", self.alert_title)
        elif gone and self.status_var.get().startswith("ALERT:"):
            self.status_var.set(f"ALERT: {self._alert_text(red_now)}" if red_now
                                else f"Alert cleared at {time.strftime('%H:%M:%S')}")

    def _alert_text(self, names) -> str:
        return "; ".join(n if n.startswith("Protection: ") else
                         f"{n}: {self.soc_var.get() if n == 'SOC' else self.vars[n].get()}" for n in sorted(names))

    def _handle_event(self, kind: str, payload):
        if kind == "data":
            self._show_data(*payload)
        elif kind == "error":
            self.status_var.set(payload)
            if self.history.note(LINK, "Command failed", payload, time.time()):
                self._history_changed()
            messagebox.showerror("BMS", payload, parent=self.root)


if __name__ == "__main__":
    run_app(App, "Local\\jbd_bms_monitor")
