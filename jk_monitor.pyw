#!python3.13
# Tkinter + tray monitor for a JK (Jikong) BMS over BLE; read-only, a separate app from the JBD bms_monitor.pyw.
# Pass --tray to start with the window hidden in the tray.
import asyncio
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from PIL import Image, ImageDraw, ImageFont

from alarm_history import ALARM, THRESHOLD
from jk_bms import ALARM_BITS, NOTICE_ALARMS, SETTINGS_STREAM_SECONDS, JkBms, find_all_bms, find_bms
from monitor_common import MonitorApp, MonitorWorker, read_config, run_app, write_config

CONFIG_PATH = Path(__file__).with_name("jk_config.json")
POLL_SECONDS = 5
# Without a streamed frame for this long, the monitor sends an explicit cell-info request; also the reconnect delay.
REQUEST_SECONDS = 120

GREEN, YELLOW, RED = 0, 1, 2
# Darker than the JBD monitor's palette so the two windows are easy to tell apart.
LEVEL_COLORS = {GREEN: "#0f5a27", YELLOW: "#7d5800", RED: "#9b0f1f"}
INACTIVE_COLOR = "#a0a7ae"
# MOS temp limits until the BMS settings are read; then 15 / 5 °C below the BMS's own MOS OTP.
DEFAULT_MOS_TEMP = (None, None, 65, 75)
# (red below, yellow below, yellow above, red above); None = no limit. LiFePO4 4S, 334 Ah, 200 A BMS.
THRESHOLDS = {
    "soc": (20, 50, None, None),
    "pack_v": (10.0, 12.0, 13.8, 14.6),
    "cell_v": (2.5, 3.0, 3.45, 3.65),
    "cell_delta_mv": (None, None, 30, 100),
    "temp_c": (0, 10, 35, 45),
    "mos_temp_c": DEFAULT_MOS_TEMP,
    "charge_a": (None, None, 100, 160),
    "discharge_a": (None, None, 120, 180),
}
DETAIL_LIVE_ROWS = ["Heating", "Charger", "Recovery", "DRY contacts", "Wire resistance", "BMS clock", "Uptime"]
SETTING_ROWS = ["Cell UVP", "Cell OVP", "SOC voltages", "RCV / RFV", "Current limits", "OCP / SCP", "Balancer",
                "Charge temp", "Discharge temp", "MOS OTP", "Heater", "Smart sleep", "Battery"]
CHARGE_STAGES = {0: "Bulk", 1: "Absorption", 2: "Float"}
# The BMS clock counts seconds from 2020-01-01 00:00 UTC+8.
JK_EPOCH = 1577836800 - 8 * 3600


def duration(seconds: int) -> str:
    days, rest = divmod(seconds, 86400)
    return f"{days} d {rest // 3600} h"


def level(key: str, value: float) -> int:
    red_lo, yel_lo, yel_hi, red_hi = THRESHOLDS[key]
    if (red_lo is not None and value < red_lo) or (red_hi is not None and value > red_hi):
        return RED
    if (yel_lo is not None and value < yel_lo) or (yel_hi is not None and value > yel_hi):
        return YELLOW
    return GREEN


def value_levels(c) -> dict:
    # JK reports charge current as positive, discharge as negative.
    if c.current >= 0:
        current_level = level("charge_a", c.current)
    else:
        current_level = level("discharge_a", -c.current)
    serious = [a for a in c.alarms if a not in NOTICE_ALARMS]
    return {
        "SOC": level("soc", c.soc),
        "Voltage": level("pack_v", c.voltage),
        "Current": current_level,
        "Power": current_level,
        "Capacity": level("soc", c.soc),
        "Temperature": max((level("temp_c", t) for t in c.temps_c), default=GREEN),
        "MOS temp": level("mos_temp_c", c.mos_temp_c),
        "Cells": max((level("cell_v", x) for x in c.cells), default=GREEN),
        "Cell delta": level("cell_delta_mv", round((max(c.cells) - min(c.cells)) * 1000)) if c.cells else GREEN,
        "Switches": GREEN if c.charge_on and c.discharge_on else YELLOW,
        "Alarms": RED if serious else YELLOW if c.alarms else GREEN,
    }


def frame_conditions(c) -> dict:
    """Return {name: (kind, detail)} for every alarm bit and every value past its red limit in frame c."""
    texts = {
        "SOC": f"{c.soc}%",
        "Voltage": f"{c.voltage:.2f} V",
        "Current": f"{c.current:+.2f} A",
        "Temperature": "  ".join(f"{t:.1f} °C" for t in c.temps_c),
        "MOS temp": f"{c.mos_temp_c:.1f} °C",
        "Cells": f"{min(c.cells):.3f} .. {max(c.cells):.3f} V" if c.cells else "",
        "Cell delta": f"{(max(c.cells) - min(c.cells)) * 1000:.0f} mV" if c.cells else "",
    }
    levels = value_levels(c)
    found = {name: (THRESHOLD, text) for name, text in texts.items() if levels[name] == RED}
    found.update({f"Alarm: {a}": (ALARM, "notice" if a in NOTICE_ALARMS else "") for a in c.alarms})
    return found


def load_config() -> dict:
    return read_config(CONFIG_PATH)


def save_config(cfg: dict):
    write_config(CONFIG_PATH, cfg)


class BmsWorker(MonitorWorker):
    scan_status = "Scanning for JK BMS (10 s)..."

    def __init__(self, post):
        self.force = False
        super().__init__(post, POLL_SECONDS, REQUEST_SECONDS, load_config, save_config, CONFIG_PATH.name)

    async def _poll_once(self, cfg: dict):
        if not (self.bms and self.bms.is_connected):
            if self.bms:
                self.post("status", "Error: link lost; reconnecting")
            await self._disconnect()
            if not cfg.get("address"):
                self.post("status", "Scanning for JK BMS...")
                address, _name = await find_bms()
                if not address:
                    raise IOError("JK BMS not found")
                cfg["address"] = address
                self._save(cfg)
            self.post("status", f"Connecting to {cfg['address']}...")
            self.bms = JkBms(cfg["address"], on_cell_info=lambda c: self.post("frame", (time.time(), c)))
            await self.bms.connect()
            self.post("device", self.bms.device_info)
            self.post("settings", self.bms.latest_settings()[1])
            self.post("status", f"Connected to {cfg['address']}")
        force, self.force = self.force, False
        if force:
            try:
                self.post("settings", await self.bms.read_settings())
                self.post("device", self.bms.device_info)
            except TimeoutError as e:
                self.post("status", f"Settings not refreshed: {e}")
            await asyncio.sleep(SETTINGS_STREAM_SECONDS)
        # The reply arrives as a "frame" like any streamed cell-info frame, so it is not posted here.
        if force or self.bms.cell_info_age() > REQUEST_SECONDS:
            await self.bms.read_cell_info(max_age=0)

    async def _find_all(self) -> list:
        return await find_all_bms()

    def refresh(self):
        def apply():
            self.force = True
            self.wake.set()
        self.loop.call_soon_threadsafe(apply)


def tray_image(soc) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if soc is None:
        color = (90, 90, 90)
    elif soc >= 50:
        color = (20, 100, 40)
    elif soc >= 20:
        color = (140, 95, 10)
    else:
        color = (150, 25, 30)
    # Square corners tell this icon apart from the rounded JBD one.
    draw.rectangle((0, 0, 63, 63), fill=color)
    text = "?" if soc is None else str(soc)
    try:
        font = ImageFont.truetype("arialbd.ttf", 40 if len(text) < 3 else 30)
    except OSError:
        font = ImageFont.load_default()
    draw.text((32, 32), text, fill="white", font=font, anchor="mm")
    return img


class App(MonitorApp):
    worker_class = BmsWorker
    bms_kind = "JK BMS"
    window_title = "JK BMS Monitor"
    tray_name = "jk_bms"
    tray_idle_title = "JK BMS: no data"
    tray_label = "JK"
    alert_title = "JK battery alert"
    history_title = "JK BMS: session history"
    tray_image = staticmethod(tray_image)

    def __init__(self):
        self.uptime_text = "--"
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

        tabs = ttk.Notebook(self.root)
        tabs.pack(fill="x", padx=8)
        status_tab = ttk.Frame(tabs)
        details_tab = ttk.Frame(tabs, padding=(0, 0, 0, 8))
        tabs.add(status_tab, text="Status")
        tabs.add(details_tab, text="Details")

        grid = ttk.Frame(status_tab, padding=(0, 4))
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
        self.value_labels["Alarms"].config(wraplength=420)

        alarms_box = ttk.LabelFrame(status_tab, text="All alarms", padding=8)
        alarms_box.pack(fill="x", pady=(4, 8))
        self.alarm_labels = {}
        for i, name in enumerate(ALARM_BITS):
            lbl = ttk.Label(alarms_box, text=f"{i:>2}  {name}", foreground=INACTIVE_COLOR, width=24)
            lbl.grid(row=i % 8, column=i // 8, sticky="w", padx=4)
            self.alarm_labels[name] = lbl

        self.detail_vars = {}
        for title, names in (("Live", DETAIL_LIVE_ROWS), ("Settings (read at connect and on Refresh)", SETTING_ROWS)):
            box = ttk.LabelFrame(details_tab, text=title, padding=(8, 2))
            box.pack(fill="x", pady=(4, 0))
            for i, name in enumerate(names):
                ttk.Label(box, text=name + ":").grid(row=i, column=0, sticky="w", padx=(0, 8), pady=1)
                self.detail_vars[name] = tk.StringVar(value="--")
                ttk.Label(box, textvariable=self.detail_vars[name], wraplength=380).grid(
                    row=i, column=1, sticky="w", pady=1)

        conn =ttk.Frame(self.root, padding=(8, 8, 8, 0))
        conn.pack(fill="x")
        self.refresh_btn = ttk.Button(conn, text="Refresh", command=self._refresh)
        self.refresh_btn.pack(side="left")
        self.connect_btn = ttk.Button(conn, text="Disconnect", command=self._toggle_connection)
        self.connect_btn.pack(side="left", padx=(8, 0))
        self.scan_btn = ttk.Button(conn, text="Scan", command=self._scan)
        self.scan_btn.pack(side="left", padx=8)
        self.history_btn = ttk.Button(conn, text="History", command=self._show_history)
        self.history_btn.pack(side="left", padx=(0, 8))
        self.updated_var = tk.StringVar(value="Updated: --")
        ttk.Label(conn, textvariable=self.updated_var, foreground="gray").pack(side="left")

        self.status_var = tk.StringVar(value="Starting...")
        ttk.Label(self.root, textvariable=self.status_var, foreground="gray", wraplength=500,
                  padding=(8, 8, 8, 8)).pack(fill="x")

    def _toggle_connection(self):
        paused = self.connect_btn.cget("text") == "Disconnect"
        self.connect_btn.config(text="Connect" if paused else "Disconnect")
        self.refresh_btn.config(state="disabled" if paused else "normal")
        self.scan_btn.config(state="disabled" if paused or self.scan_busy else "normal")
        if self.worker:
            self.worker.set_paused(paused)

    def _forget_bms(self, address: str):
        """Another BMS was chosen: also drop the device row and settings read from the previous one."""
        THRESHOLDS["mos_temp_c"] = DEFAULT_MOS_TEMP
        self.uptime_text = "--"
        self.vars["Device"].set("--")
        for name in SETTING_ROWS:
            self.detail_vars[name].set("--")
        super()._forget_bms(address)

    def _show_device(self, info: dict):
        self.vars["Device"].set(f"{info['model']}  hw {info['hw']}  sw {info['sw']}  s/n {info['serial']}")
        self.uptime_text = f"{duration(info['uptime_s'])}, {info['power_on_count']} power-ons"

    def _show_settings(self, s):
        if not s:
            return
        THRESHOLDS["mos_temp_c"] = (None, None, s["mos_otp"] - 15, s["mos_otp"] - 5)
        d = self.detail_vars
        d["Cell UVP"].set(f"{s['cell_uvp']:.3f} V, recovery {s['cell_uvp_rec']:.3f} V, "
                          f"power off {s['power_off_v']:.3f} V")
        d["Cell OVP"].set(f"{s['cell_ovp']:.3f} V, recovery {s['cell_ovp_rec']:.3f} V")
        d["SOC voltages"].set(f"0 % = {s['soc0_v']:.3f} V, 100 % = {s['soc100_v']:.3f} V")
        d["RCV / RFV"].set(f"{s['rcv']:.3f} V / {s['rfv']:.3f} V")
        d["Current limits"].set(f"charge {s['max_charge_a']:.0f} A, discharge {s['max_discharge_a']:.0f} A, "
                                f"balance {s['max_balance_a']:.1f} A")
        d["OCP / SCP"].set(f"charge delay {s['charge_ocp_delay_s']} s, recovery {s['charge_ocp_rec_s']} s; "
                           f"discharge delay {s['discharge_ocp_delay_s']} s, recovery {s['discharge_ocp_rec_s']} s; "
                           f"SCP delay {s['scp_delay_us']} µs, recovery {s['scp_rec_s']} s")
        d["Balancer"].set(f"{'on' if s['balancer_on'] else 'OFF'}, start {s['balance_start_v']:.3f} V, "
                          f"trigger {s['balance_trigger_v'] * 1000:.0f} mV")
        d["Charge temp"].set(f"OTP {s['charge_otp']:.0f} / {s['charge_otp_rec']:.0f} °C, "
                             f"UTP {s['charge_utp']:.0f} / {s['charge_utp_rec']:.0f} °C")
        d["Discharge temp"].set(f"OTP {s['discharge_otp']:.0f} / {s['discharge_otp_rec']:.0f} °C, "
                                f"UTP {s['discharge_utp']} / {s['discharge_utp_rec']} °C")
        d["MOS OTP"].set(f"{s['mos_otp']:.0f} °C, recovery {s['mos_otp_rec']:.0f} °C")
        d["Heater"].set(f"start {s['heat_start']} °C, stop {s['heat_stop']} °C")
        d["Smart sleep"].set(f"{s['smart_sleep_v']:.3f} V, delay {s['smart_sleep_delay_h']} h")
        d["Battery"].set(f"{s['nominal_ah']:.0f} Ah, {s['cell_count']} cells")

    def _show_data(self, c):
        self._link_up()
        if self.status_var.get() == "Refreshing...":
            self.status_var.set("Refreshed")
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
        for name, lbl in self.alarm_labels.items():
            on = name in c.alarms
            color = LEVEL_COLORS[YELLOW if name in NOTICE_ALARMS else RED] if on else INACTIVE_COLOR
            lbl.config(foreground=color, font=("Segoe UI", 9, "bold" if on else "normal"))
        d = self.detail_vars
        d["Heating"].set(f"on, {c.heating_current:.2f} A" if c.heating else "off")
        d["Charger"].set(f"{CHARGE_STAGES.get(c.charge_stage, c.charge_stage)}, "
                         f"{'plugged' if c.charger_plugged else 'not plugged'}"
                         + (", precharging" if c.precharging else ""))
        d["Recovery"].set(", ".join(f"{n} {t} s" for n, t in c.recovery_s.items() if t) or "none")
        d["DRY contacts"].set(f"DRY1 {'on' if c.dry_mask & 0x02 else 'off'}, "
                              f"DRY2 {'on' if c.dry_mask & 0x04 else 'off'}")
        d["Wire resistance"].set("  ".join(f"{r} mΩ" for r in c.cell_resistance_mohm) or "--")
        d["BMS clock"].set(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(JK_EPOCH + c.clock_s))
                           if c.clock_s else "not set")
        d["Uptime"].set(f"{self.uptime_text}, runtime {duration(c.runtime_s)}, {c.log_count} log entries")
        self._apply_levels(c)
        self.updated_var.set(f"Updated: {time.strftime('%H:%M:%S')}")
        if c.soc != self.tray_soc:
            self.tray_soc = c.soc
            self.tray.icon = tray_image(c.soc)
        self.tray.title = f"JK {c.soc}%  {c.voltage:.2f} V  {c.current:+.2f} A"

    def _apply_levels(self, c):
        serious = [a for a in c.alarms if a not in NOTICE_ALARMS]
        levels = value_levels(c)
        self.soc_label.config(foreground=LEVEL_COLORS[levels["SOC"]])
        for name, lvl in levels.items():
            if name in self.value_labels:
                self.value_labels[name].config(foreground=LEVEL_COLORS[lvl])

        # Each serious alarm is tracked on its own, so a new one notifies even while the row is already red.
        red_now = {n for n, lvl in levels.items() if lvl == RED and n not in ("Power", "Capacity", "Alarms")}
        red_now |= {f"Alarm: {a}" for a in serious}
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
        return "; ".join(n if n.startswith("Alarm: ") else
                         f"{n}: {self.soc_var.get() if n == 'SOC' else self.vars[n].get()}" for n in sorted(names))

    def _handle_event(self, kind: str, payload):
        if kind == "frame":
            if self.history.observe(frame_conditions(payload[1]), payload[0]):
                self._history_changed()
            self._show_data(payload[1])
        elif kind == "device":
            self._show_device(payload)
        elif kind == "settings":
            self._show_settings(payload)


if __name__ == "__main__":
    run_app(App, "Local\\jk_bms_monitor")
