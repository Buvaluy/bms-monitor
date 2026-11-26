# Unit tests for the JK and JBD monitor apps with fake BLE devices, tray and widgets; run with: py -3.13 -m unittest test_monitors
import asyncio
import collections
import importlib.machinery
import importlib.util
import os
import queue
import tempfile
import threading
import time
import tkinter as tk
import types
import unittest
from pathlib import Path
from tkinter import ttk
from unittest import mock

import alarm_history
import jbd_bms
import jk_bms
import monitor_common


def load_pyw(name):
    loader = importlib.machinery.SourceFileLoader(name, str(Path(__file__).with_name(f"{name}.pyw")))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(module)
    return module


jkm, jbm = load_pyw("jk_monitor"), load_pyw("bms_monitor")
ERROR = "Error: AA is not advertising (out of range or connected to a phone); next try in 2 min or on Refresh"
DEVICE = {"model": "JK", "hw": "19", "sw": "19", "serial": "1", "uptime_s": 0, "power_on_count": 1}


class Var:
    def __init__(self, value="--"):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Widget:
    def __init__(self, text=""):
        self.opts = {"text": text}

    def config(self, **kw):
        self.opts.update(kw)

    def cget(self, key):
        return self.opts.get(key)

    def __setitem__(self, key, value):
        self.opts[key] = value


class Root:
    def __init__(self):
        self.jobs = {}

    def after(self, _ms, func, *args):
        key = object()
        self.jobs[key] = (func, args)
        return key

    def after_cancel(self, key):
        self.jobs.pop(key, None)

    def run_jobs(self):
        for key in list(self.jobs):
            func, args = self.jobs.pop(key)
            func(*args)


class Tray:
    def __init__(self):
        self.icon, self.title, self.notes = None, "", []

    def notify(self, text, title=None):
        from pystray._util import win32
        win32.NOTIFYICONDATAW(szInfo=text, szInfoTitle=title or "")
        self.notes.append(text)


def fake_app(module, root=None):
    app = module.App.__new__(module.App)
    app.events, app.alerted, app.history = queue.Queue(), set(), alarm_history.AlarmHistory()
    app.history_win, app.tray_soc, app.worker, app.closing = None, None, None, False
    app.no_link_since, app.link_alert_job, app.link_alerted = None, None, False
    app.root, app.tray = root or Root(), Tray()
    app.soc_var, app.soc_label, app.soc_bar = Var("--%"), Widget(), Widget()
    app.vars, app.detail_vars = collections.defaultdict(Var), collections.defaultdict(Var)
    app.updated_var, app.status_var = Var("Updated: --"), Var("Starting...")
    app.history_btn, app.connect_btn, app.refresh_btn = Widget("History"), Widget("Disconnect"), Widget("Refresh")
    rows = ["Voltage", "Current", "Power", "Capacity", "Cycles", "Temperature", "Cells", "Cell delta", "Switches"]
    if module is jkm:
        app.scan_busy, app.uptime_text, app.scan_btn = False, "--", Widget("Scan")
        app.alarm_labels = {n: Widget() for n in jk_bms.ALARM_BITS}
        rows += ["MOS temp", "Balancing", "Alarms"]
    else:
        app.last_basic, app.dev_mode, app.chg_btn, app.dsg_btn = None, Var(True), Widget(), Widget()
        app.prot_labels = {n: Widget() for n in jbm.PROTECTION_BITS}
        rows += ["Protection"]
    app.value_labels = {n: Widget() for n in rows}
    return app


def jk_cell(cells=(3.3, 3.3, 3.3, 3.3), alarm_mask=0, mos=30.0, soc=85):
    return jk_bms.CellInfo(voltage=13.2, current=1.0, power=13.2, soc=soc, soh=100, remaining_ah=200.0,
                           nominal_ah=334.0, cycles=1, cycle_ah=1.0, cells=list(cells),
                           cell_resistance_mohm=[0] * len(cells), balancing=0, balance_current=0.0,
                           alarm_mask=alarm_mask, charge_on=True, discharge_on=True, mos_temp_c=mos,
                           temps_c=[25.0, 25.0])


def jbd_basic(prot=0, charge_on=True, discharge_on=True, soc=85):
    return jbd_bms.BasicInfo(voltage=13.2, current=1.0, remaining_ah=100.0, nominal_ah=140.0, cycles=1,
                             balance_mask=0, protection_mask=prot, soc=soc, charge_on=charge_on,
                             discharge_on=discharge_on, cell_count=4, temps_c=[25.0, 25.0])


def data_event(module, cells, soc=85):
    return ("data", jk_cell(cells, soc=soc)) if module is jkm else ("data", (jbd_basic(soc=soc), list(cells)))


def cell_frame():
    body = jk_bms.FRAME_HEADER + bytes([jk_bms.TYPE_CELL_INFO]) + bytes(jk_bms.FRAME_LEN - 6)
    return body + bytes([sum(body) & 0xFF])


async def no_op():
    return None


async def found_bms(timeout=10.0):
    return "AA", "test BMS"


def deny(_cfg):
    raise PermissionError(13, "Permission denied", "config.json")


class FakeBms:
    connect_s = 0.0

    def __init__(self, address, on_cell_info=None):
        self.address, self.connected, self.device_info, self.mos = address, False, dict(DEVICE), []
        self.basic = jbd_basic()
        self.hold, self.held = False, []

    async def connect(self):
        await asyncio.sleep(FakeBms.connect_s)
        self.connected = True

    async def disconnect(self):
        self.connected = False

    @property
    def is_connected(self):
        return self.connected

    def latest_settings(self):
        return None, None

    def cell_info_age(self):
        return 0.0

    async def read_cell_info(self, max_age=5.0):
        return jk_cell()

    async def read_basic(self):
        return self.basic

    async def read_cells(self):
        return [3.3] * 4

    async def set_mos(self, charge, discharge, before_write=None):
        if self.hold:
            self.held.append(asyncio.get_running_loop().create_future())
            await self.held[-1]
        if before_write:
            before_write()
        self.mos.append((charge, discharge))


class SilentOn97(jk_bms.JkBms):
    async def connect(self):
        loop = asyncio.get_running_loop()

        async def write(_uuid, data, response=False):
            if data[4] == jk_bms.CMD_CELL_INFO:
                loop.call_soon(self._on_notify, None, bytearray(cell_frame()))
        self._client = types.SimpleNamespace(write_gatt_char=write, is_connected=True, disconnect=no_op)
        self.device_info = dict(DEVICE)


class Recorder:
    def __init__(self):
        self.items, self.cond = [], threading.Condition()

    def post(self, kind, payload):
        with self.cond:
            self.items.append((kind, payload))
            self.cond.notify_all()

    def wait_for(self, kind, prefix="", start=0, timeout=5.0):
        def seen():
            return any(k == kind and str(p).startswith(prefix) for k, p in self.items[start:])
        with self.cond:
            return self.cond.wait_for(seen, timeout)

    def statuses(self, start=0):
        return [p for k, p in self.items[start:] if k == "status"]


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


def stop_worker(worker):
    worker.close().result(5)
    worker.loop.call_soon_threadsafe(worker.loop.stop)
    wait_until(lambda: not worker.loop.is_running())
    worker.loop.close()


def dialog(root):
    return [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)][-1]


def button(widget, text):
    for child in widget.winfo_children():
        if isinstance(child, ttk.Button) and str(child.cget("text")) == text:
            return child
        found = button(child, text)
        if found:
            return found
    return None


class MonitorTest(unittest.TestCase):
    def start_worker(self, module, **patches):
        attrs = {"load_config": lambda: {"address": "AA"}, "save_config": lambda cfg: None, "find_bms": found_bms,
                 "JkBms" if module is jkm else "JbdBms": FakeBms}
        attrs.update(patches)
        patcher = mock.patch.multiple(module, **attrs)
        patcher.start()
        self.addCleanup(patcher.stop)
        rec = Recorder()
        worker = module.BmsWorker(rec.post)
        self.addCleanup(stop_worker, worker)
        return worker, rec

    def test_toggle_changes_only_the_clicked_switch(self):
        worker, rec = self.start_worker(jbm)
        self.assertTrue(rec.wait_for("data"))
        app = fake_app(jbm)
        app.worker = worker
        first, mark = worker.bms, len(rec.items)

        async def read_while_the_link_drops():
            worker.bms = None
            return jbd_basic()
        first.read_basic = read_while_the_link_drops
        clicks = [("discharge", jbd_basic()), ("charge", jbd_basic(discharge_on=False)),
                  ("charge", jbd_basic(charge_on=False))]
        with mock.patch.object(jbm.messagebox, "askyesno", return_value=True) as ask:
            app.last_basic = jbd_basic()
            app._toggle("discharge")
            self.assertTrue(rec.wait_for("data", start=mark))
            self.assertEqual(first.mos, [(True, False)])
            for n, (which, state) in enumerate(clicks, 1):
                worker.bms.basic = app.last_basic = state
                app._toggle(which)
                self.assertTrue(wait_until(lambda: len(worker.bms.mos) == n))
                self.assertNotIn("also allows", ask.call_args.args[1])
        self.assertEqual(worker.bms.mos, [(True, False), (False, False), (True, True)])

        last, mark = worker.bms, len(rec.items)
        last.basic, last.hold = jbd_basic(), True
        worker.set_switch(0x02, False)
        worker.set_switch(0x01, False)
        self.assertTrue(wait_until(lambda: last.held))
        last.basic = jbd_basic(discharge_on=False)
        worker.loop.call_soon_threadsafe(last.held[0].set_result, None)
        self.assertTrue(wait_until(lambda: len(last.held) == 2))
        worker.loop.call_soon_threadsafe(last.held[1].set_result, None)
        self.assertTrue(wait_until(lambda: len(last.mos) == 5))
        self.assertEqual(last.mos[3:], [(True, False), (False, False)])

        last.basic = jbd_basic(charge_on=False, discharge_on=False)
        worker.set_switch(0x01, True)
        self.assertTrue(wait_until(lambda: len(last.held) == 3))
        worker.loop.call_soon_threadsafe(last.held[2].set_exception, asyncio.TimeoutError())
        self.assertTrue(rec.wait_for("error", "Switch command not confirmed: TimeoutError", start=mark))
        self.assertEqual(worker.mos_off, 0x02)

        worker.set_switch(0x02, True)
        self.assertTrue(wait_until(lambda: len(last.held) == 4))
        worker.set_paused(True)
        worker.loop.call_soon_threadsafe(last.held[3].set_result, None)
        self.assertTrue(rec.wait_for("status", "Switch command not sent: the BMS was disconnected", start=mark))
        self.assertEqual(len(last.mos), 5)
        other = fake_app(jbm)
        other.worker, other.last_basic = types.SimpleNamespace(mos_off=0), jbd_basic(charge_on=False)
        with mock.patch.object(jbm.messagebox, "askyesno", return_value=False) as ask:
            other._toggle("discharge")
        self.assertIn("also allows charging", ask.call_args.args[1])

    def test_link_loss_greys_the_tray_and_alerts_after_a_delay(self):
        for module in (jkm, jbm):
            with self.subTest(module.__name__):
                app = fake_app(module)
                app.events.put(data_event(module, (3.3,) * 4))
                app.events.put(("status", ERROR))
                app._pump()
                self.assertEqual(app.tray.icon.getpixel((8, 8)), module.tray_image(None).getpixel((8, 8)))
                self.assertIn("no link since", app.tray.title)
                self.assertIn("last 85%", app.tray.title)
                self.assertEqual(app.tray.notes, [])
                app.root.run_jobs()
                self.assertEqual(len(app.tray.notes), 1)
                for status in (ERROR, "Connected to AA"):
                    app.events.put(("status", status))
                    app._pump()
                    app.root.run_jobs()
                self.assertEqual(len(app.tray.notes), 1)
                self.assertIsNotNone(app.no_link_since)
                app.events.put(data_event(module, (3.3,) * 4))
                app._pump()
                self.assertIsNone(app.no_link_since)
                user_stops = ["Disconnected; the battery is free for the phone"]
                if module is jkm:
                    user_stops.append("Scanning for JK BMS (10 s)...")
                    app.scan_busy = True
                for stop in user_stops:
                    for status in (ERROR, stop):
                        app.events.put(("status", status))
                    app._pump()
                    app.root.run_jobs()
                self.assertEqual(len(app.tray.notes), 1)
                if module is jkm:
                    app.scan_busy = False
                    for status in (ERROR, "Scanning for JK BMS..."):
                        app.events.put(("status", status))
                    app._pump()
                    app.root.run_jobs()
                    self.assertEqual(len(app.tray.notes), 2)
                app.events.put(("status", ERROR))
                app._pump()
                app.root.run_jobs()
                notes = len(app.tray.notes)
                for status in ("Disconnected; the battery is free for the phone", ERROR):
                    app.events.put(("status", status))
                app._pump()
                app.root.run_jobs()
                self.assertEqual(len(app.tray.notes), notes + 1)

    def test_silent_link_drop_is_recorded_in_history(self):
        for module in (jkm, jbm):
            with self.subTest(module.__name__):
                worker, rec = self.start_worker(module, POLL_SECONDS=0.2)
                self.assertTrue(rec.wait_for("data"))
                mark = len(rec.items)
                worker.bms.connected = False
                self.assertTrue(rec.wait_for("status", "Connected to", mark))
                app = fake_app(module)
                for t in (100, 101, 102):
                    app.history.observe({"Cells": (alarm_history.THRESHOLD, "3.650 V")}, t)
                for status in rec.statuses(mark):
                    app._track_status(status)
                app.history.observe({}, 126)
                self.assertEqual([(e.name, e.end, e.cut) for e in app.history.entries[:1]], [("Cells", 102, True)])
                self.assertEqual([e.name for e in app.history.entries[1:]], [alarm_history.LINK_DOWN])

    def test_long_alert_text_fits_the_tray_balloon(self):
        app = fake_app(jbm)
        app._show_data(jbd_basic(prot=(1 << len(jbm.PROTECTION_BITS)) - 1), [3.3] * 4)
        self.assertLessEqual(len(app.tray.notes[0]), 255)
        self.assertEqual(len(app.history.entries), len(jbm.PROTECTION_BITS))
        app = fake_app(jkm)
        serious = [i for i, a in enumerate(jk_bms.ALARM_BITS) if a not in jk_bms.NOTICE_ALARMS]
        app._apply_levels(jk_cell(alarm_mask=sum(1 << i for i in serious)))
        self.assertLessEqual(len(app.tray.notes[0]), 255)

    def test_cell_delta_at_exact_limits(self):
        for lo in range(2500, 3601):
            at_100, at_30 = [lo / 1000, (lo + 100) / 1000], [lo / 1000, (lo + 30) / 1000]
            self.assertEqual(jkm.value_levels(jk_cell(at_100))["Cell delta"], jkm.YELLOW)
            self.assertEqual(jkm.value_levels(jk_cell(at_30))["Cell delta"], jkm.GREEN)
            self.assertEqual(jbm.value_levels(jbd_basic(), at_100, [])["Cell delta"], jbm.YELLOW)
            self.assertEqual(jbm.value_levels(jbd_basic(), at_30, [])["Cell delta"], jbm.GREEN)

    def test_mos_temperature_follows_the_bms_setting(self):
        self.addCleanup(jkm.THRESHOLDS.__setitem__, "mos_temp_c", jkm.THRESHOLDS["mos_temp_c"])

        def mos(t):
            return jkm.value_levels(jk_cell(mos=t))["MOS temp"]
        self.assertEqual([mos(t) for t in (-5, 60, 70, 80)], [jkm.GREEN, jkm.GREEN, jkm.YELLOW, jkm.RED])
        settings = jk_bms.parse_settings(bytes(jk_bms.FRAME_LEN))
        settings["mos_otp"] = 100.0
        fake_app(jkm)._show_settings(settings)
        self.assertEqual([mos(t) for t in (80, 90, 96)], [jkm.GREEN, jkm.YELLOW, jkm.RED])

    def test_refresh_keeps_the_link_when_settings_do_not_arrive(self):
        short = types.SimpleNamespace(get_running_loop=asyncio.get_running_loop, TimeoutError=asyncio.TimeoutError,
                                      wait_for=lambda aw, timeout: asyncio.wait_for(aw, 0.2))
        patcher = mock.patch.object(jk_bms, "asyncio", short)
        patcher.start()
        self.addCleanup(patcher.stop)
        worker, rec = self.start_worker(jkm, JkBms=SilentOn97, SETTINGS_STREAM_SECONDS=0.05)
        self.assertTrue(rec.wait_for("data"))
        mark = len(rec.items)
        worker.refresh()
        self.assertTrue(rec.wait_for("data", start=mark))
        self.assertEqual(rec.statuses(mark), ["Settings not refreshed: no reply to command 0x97"])

    def test_config_problems_do_not_stop_the_monitor(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for module in (jkm, jbm):
            with self.subTest(module.__name__):
                path = Path(tmp.name) / f"{module.__name__}.json"
                path.write_bytes(b'\xef\xbb\xbf{"address": "AA"}')
                with mock.patch.object(module, "CONFIG_PATH", path):
                    self.assertEqual(module.load_config(), {"address": "AA"})
                    module.save_config({"address": "BB"})
                    self.assertEqual(module.load_config(), {"address": "BB"})
                    self.assertFalse(path.with_suffix(".tmp").exists())
                path.write_text("[]")
                _worker, rec = self.start_worker(module, CONFIG_PATH=path, load_config=module.load_config,
                                                 save_config=deny)
                self.assertTrue(rec.wait_for("data"))
                statuses = rec.statuses()
                self.assertTrue(statuses[0].startswith(f"Could not read {path.name}"))
                self.assertTrue(any(s.startswith(f"Could not save {path.name}") for s in statuses))

    def test_disconnect_cancels_a_connect_in_progress(self):
        for module in (jkm, jbm):
            with self.subTest(module.__name__), mock.patch.object(FakeBms, "connect_s", 3.0):
                worker, rec = self.start_worker(module)
                time.sleep(0.3)
                worker.set_paused(True)
                self.assertTrue(rec.wait_for("status", "Disconnected;", timeout=1.5))
                self.assertFalse(any(s.startswith("Connected to") for s in rec.statuses()))

    def test_exit_right_after_refresh_stops_the_worker(self):
        for module in (jkm, jbm):
            with self.subTest(module.__name__):
                worker, rec = self.start_worker(module)
                self.assertTrue(rec.wait_for("data"))
                worker.refresh()
                worker.close().result(2)

    def test_alert_status_clears_when_values_recover(self):
        for module in (jkm, jbm):
            with self.subTest(module.__name__):
                app = fake_app(module)
                app.events.put(data_event(module, (3.30, 3.30, 3.30, 3.45)))
                app._pump()
                self.assertEqual(app.status_var.get(), "ALERT: Cell delta: 150 mV")
                app.events.put(data_event(module, (3.30, 3.30, 3.30, 3.45), soc=15))
                app._pump()
                self.assertEqual(app.status_var.get(), "ALERT: Cell delta: 150 mV; SOC: 15%")
                self.assertEqual(app.tray.notes, ["Cell delta: 150 mV", "SOC: 15%"])
                app.events.put(data_event(module, (3.30,) * 4, soc=15))
                app._pump()
                self.assertEqual(app.status_var.get(), "ALERT: SOC: 15%")
                app.events.put(data_event(module, (3.30,) * 4))
                app._pump()
                self.assertTrue(app.status_var.get().startswith("Alert cleared"))

    def test_scan_dialog(self):
        root = tk.Tk()
        self.addCleanup(root.destroy)
        root.withdraw()
        calls = []
        app = fake_app(jkm, root)
        app.worker = types.SimpleNamespace(scan=lambda: calls.append("scan"),
                                           choose=lambda address: calls.append(f"choose {address}"))
        found = [("AA", "JK-A", -60), ("BB", "JK-B", -70)]
        app._show_scan(found, "AA")
        root.update()
        self.assertTrue(dialog(root).winfo_viewable())
        button(dialog(root), "Rescan").invoke()
        self.assertEqual(calls, ["scan"])

        app.alerted = {"Cells"}
        app.vars["Device"].set("JK-A device")
        app.detail_vars["Cell OVP"].set("3.650 V")
        app._show_scan(found, "AA")
        listbox = next(w for w in dialog(root).winfo_children() if isinstance(w, tk.Listbox))
        listbox.selection_clear(0, "end")
        listbox.selection_set(1)
        button(dialog(root), "Connect").invoke()
        self.assertEqual(calls[-1], "choose BB")
        self.assertEqual((app.alerted, app.vars["Device"].get(), app.detail_vars["Cell OVP"].get()), (set(), "--", "--"))
        self.assertEqual(app.history.entries[-1].name, "BMS changed")

    def test_only_one_instance_runs(self):
        name = f"Local\\bms_monitor_test_{os.getpid()}"
        self.assertFalse(monitor_common.another_instance_running(name))
        self.assertTrue(monitor_common.another_instance_running(name))


if __name__ == "__main__":
    unittest.main()
