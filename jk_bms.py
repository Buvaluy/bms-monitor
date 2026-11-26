# JK (Jikong) BMS client over BLE, JK02_32S protocol (hw 19.x): request frames, frame assembly, cell-info parsing.
# Run directly for a console readout.
import asyncio
import struct
import sys
import time
from dataclasses import dataclass, field

from bleak import BleakClient, BleakScanner

CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
JK_MANUFACTURER_ID = 0x0B65

CMD_CELL_INFO = 0x96
CMD_DEVICE_INFO = 0x97
FRAME_HEADER = b"\x55\xAA\xEB\x90"
FRAME_LEN = 300
TYPE_SETTINGS = 0x01
TYPE_CELL_INFO = 0x02
TYPE_DEVICE_INFO = 0x03
# The settings frame follows device info; a command sent while it streams is ignored.
SETTINGS_STREAM_SECONDS = 2.0

ALARM_BITS = [
    "wire resistance", "MOS overtemp", "cell count", "current sensor", "cell overvoltage",
    "pack overvoltage", "charge overcurrent", "charge short circuit", "charge overtemp",
    "charge undertemp", "internal comm", "cell undervoltage", "pack undervoltage",
    "discharge overcurrent", "discharge short circuit", "discharge overtemp", "charge MOS",
    "discharge MOS", "GPS disconnected", "default password", "discharge on failed",
    "battery overtemp", "temp sensor", "PLC module",
]
# Bits that only remind or inform; they do not mean the pack is in trouble.
NOTICE_ALARMS = {"default password", "GPS disconnected"}


def command(cmd: int) -> bytes:
    f = bytes([0xAA, 0x55, 0x90, 0xEB, cmd, 0x00]) + bytes(13)
    return f + bytes([sum(f) & 0xFF])


@dataclass
class CellInfo:
    voltage: float
    current: float
    power: float
    soc: int
    soh: int
    remaining_ah: float
    nominal_ah: float
    cycles: int
    cycle_ah: float
    cells: list
    cell_resistance_mohm: list
    balancing: int
    balance_current: float
    alarm_mask: int
    charge_on: bool
    discharge_on: bool
    mos_temp_c: float
    temps_c: list = field(default_factory=list)
    runtime_s: int = 0
    heating: bool = False
    heating_current: float = 0.0
    precharging: bool = False
    charger_plugged: bool = False
    charge_stage: int = 0
    recovery_s: dict = field(default_factory=dict)
    dry_mask: int = 0
    clock_s: int = 0
    log_count: int = 0

    @property
    def alarms(self) -> list:
        return [ALARM_BITS[i] if i < len(ALARM_BITS) else f"bit {i}"
                for i in range(32) if self.alarm_mask >> i & 1]


def parse_cell_info(f: bytes) -> CellInfo:
    u16 = lambda o: struct.unpack_from("<H", f, o)[0]
    i16 = lambda o: struct.unpack_from("<h", f, o)[0]
    u32 = lambda o: struct.unpack_from("<I", f, o)[0]
    i32 = lambda o: struct.unpack_from("<i", f, o)[0]
    mask = u32(70)
    present = [k for k in range(32) if mask >> k & 1]
    # T1, T2, then T3..T5 stored in reverse at 258..254; -2000 (-200.0 °C) marks an unconnected sensor.
    temps = [i16(o) / 10 for o in (162, 164, 258, 256, 254)]
    timers = {"charge OCP": 206, "charge SCP": 208, "discharge OCP": 202, "discharge SCP": 204,
              "cell UVP": 210, "cell OVP": 212}
    return CellInfo(
        voltage=u32(150) / 1000,
        current=i32(158) / 1000,
        power=i32(158) / 1000 * u32(150) / 1000,
        soc=f[173],
        soh=f[190],
        remaining_ah=u32(174) / 1000,
        nominal_ah=u32(178) / 1000,
        cycles=u32(182),
        cycle_ah=u32(186) / 1000,
        cells=[u16(6 + 2 * k) / 1000 for k in present],
        cell_resistance_mohm=[u16(80 + 2 * k) for k in present],
        balancing=f[172],
        balance_current=i16(170) / 1000,
        alarm_mask=u32(166),
        charge_on=bool(f[198]),
        discharge_on=bool(f[199]),
        mos_temp_c=i16(144) / 10,
        temps_c=[t for t in temps if t > -100],
        runtime_s=u32(194),
        heating=bool(f[215]),
        heating_current=i16(236) / 1000,
        precharging=bool(f[191]),
        charger_plugged=bool(f[245]),
        charge_stage=f[280],
        recovery_s={name: u16(o) for name, o in timers.items()},
        dry_mask=f[281],
        clock_s=u32(262),
        log_count=u32(266),
    )


def parse_settings(f: bytes) -> dict:
    u32 = lambda o: struct.unpack_from("<I", f, o)[0]
    v = lambda o: u32(o) / 1000
    t = lambda o: struct.unpack_from("<i", f, o)[0] / 10
    s8 = lambda o: struct.unpack_from("<b", f, o)[0]
    return {
        "smart_sleep_v": v(6), "cell_uvp": v(10), "cell_uvp_rec": v(14), "cell_ovp": v(18),
        "cell_ovp_rec": v(22), "balance_trigger_v": v(26), "soc100_v": v(30), "soc0_v": v(34),
        "rcv": v(38), "rfv": v(42), "power_off_v": v(46),
        "max_charge_a": v(50), "charge_ocp_delay_s": u32(54), "charge_ocp_rec_s": u32(58),
        "max_discharge_a": v(62), "discharge_ocp_delay_s": u32(66), "discharge_ocp_rec_s": u32(70),
        "scp_rec_s": u32(74), "max_balance_a": v(78),
        "charge_otp": t(82), "charge_otp_rec": t(86), "discharge_otp": t(90), "discharge_otp_rec": t(94),
        "charge_utp": t(98), "charge_utp_rec": t(102), "mos_otp": t(106), "mos_otp_rec": t(110),
        "cell_count": f[114], "balancer_on": bool(f[126]), "nominal_ah": v(130), "scp_delay_us": u32(134),
        "balance_start_v": v(138), "heat_start": s8(284), "heat_stop": s8(285), "smart_sleep_delay_h": f[286],
        "discharge_utp": s8(296), "discharge_utp_rec": s8(297),
    }


def parse_device_info(f: bytes) -> dict:
    s = lambda a, b: f[a:b].split(b"\x00")[0].decode(errors="replace")
    u32 = lambda o: struct.unpack_from("<I", f, o)[0]
    return {"model": s(6, 22), "hw": s(22, 30), "sw": s(30, 38), "name": s(46, 62),
            "mfg_date": s(78, 86), "serial": s(86, 97), "uptime_s": u32(38), "power_on_count": u32(42)}


class JkBms:
    def __init__(self, address: str, on_cell_info=None):
        """on_cell_info(CellInfo) is called for every streamed cell-info frame, not only the ones read."""
        self.address = address
        self.on_cell_info = on_cell_info
        self.device_info = {}
        self._client = None
        self._buf = bytearray()
        self._waiters = {}
        self._latest = {}

    async def connect(self):
        # A fresh scan before connecting makes WinRT connect reliably to a weak-signal device.
        dev = await BleakScanner.find_device_by_address(self.address, timeout=15.0)
        if not dev:
            raise IOError(f"{self.address} is not advertising (out of range or connected to a phone)")
        self._client = BleakClient(dev, timeout=40.0)
        await self._client.connect()
        await self._client.start_notify(CHAR_UUID, self._on_notify)
        self.device_info = parse_device_info(await self._request(CMD_DEVICE_INFO, TYPE_DEVICE_INFO))
        await asyncio.sleep(SETTINGS_STREAM_SECONDS)

    async def disconnect(self):
        if self._client:
            await self._client.disconnect()

    @property
    def is_connected(self) -> bool:
        return bool(self._client and self._client.is_connected)

    def _on_notify(self, _sender, data: bytearray):
        # The BMS interleaves "AT\r\n" and trailing bytes with frames, so a header can start mid-chunk.
        self._buf += data
        while (start := self._buf.find(FRAME_HEADER)) >= 0 and len(self._buf) - start >= FRAME_LEN:
            frame = bytes(self._buf[start:start + FRAME_LEN])
            # A header inside the window means the frame at start was cut short (the checksum passes 1 in 256).
            if FRAME_HEADER in frame[1:] or sum(frame[:-1]) & 0xFF != frame[-1]:
                del self._buf[:start + 1]
                continue
            del self._buf[:start + FRAME_LEN]
            self._latest[frame[4]] = (time.monotonic(), frame)
            if frame[4] == TYPE_CELL_INFO and self.on_cell_info:
                self.on_cell_info(parse_cell_info(frame))
            w = self._waiters.pop(frame[4], None)
            if w and not w.done():
                w.set_result(frame)
        if len(self._buf) > 4 * FRAME_LEN:
            del self._buf[:-FRAME_LEN]

    async def _request(self, cmd: int, frame_type: int, attempts: int = 3) -> bytes:
        for attempt in range(attempts):
            fut = asyncio.get_running_loop().create_future()
            self._waiters[frame_type] = fut
            await self._client.write_gatt_char(CHAR_UUID, command(cmd), response=False)
            try:
                return await asyncio.wait_for(fut, timeout=5.0)
            except asyncio.TimeoutError:
                if attempt == attempts - 1:
                    raise TimeoutError(f"no reply to command 0x{cmd:02X}") from None

    def cell_info_age(self) -> float:
        return time.monotonic() - self._latest.get(TYPE_CELL_INFO, (float("-inf"), None))[0]

    async def read_cell_info(self, max_age: float = 5.0) -> CellInfo:
        # After 0x96 the BMS keeps streaming cell info about once a second; reuse a frame newer than max_age.
        ts, frame = self._latest.get(TYPE_CELL_INFO, (0.0, None))
        if frame is None or time.monotonic() - ts > max_age:
            frame = await self._request(CMD_CELL_INFO, TYPE_CELL_INFO)
        return parse_cell_info(frame)

    def latest_settings(self):
        ts, frame = self._latest.get(TYPE_SETTINGS, (None, None))
        return ts, parse_settings(frame) if frame else None

    async def read_settings(self) -> dict:
        # The BMS answers 0x97 with device info followed by the settings frame.
        frame = await self._request(CMD_DEVICE_INFO, TYPE_SETTINGS)
        self.device_info = parse_device_info(self._latest[TYPE_DEVICE_INFO][1])
        return parse_settings(frame)


async def find_all_bms(timeout: float = 10.0) -> list:
    """Return [(address, name, rssi)] of advertising JK BMS units, strongest signal first."""
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    return sorted(((dev.address, adv.local_name or dev.name or "?", adv.rssi) for dev, adv in found.values()
                   if JK_MANUFACTURER_ID in adv.manufacturer_data
                   and SERVICE_UUID in [u.lower() for u in adv.service_uuids]), key=lambda x: -x[2])


async def find_bms(timeout: float = 10.0):
    found = await find_all_bms(timeout)
    return found[0][:2] if found else (None, None)


async def console(address: str = None):
    if not address:
        address, name = await find_bms()
        if not address:
            sys.exit("JK BMS not found")
        print(f"found {name} at {address}")
    bms = JkBms(address)
    try:
        await bms.connect()
        print(bms.device_info)
        while True:
            c = await bms.read_cell_info()
            print(f"SOC {c.soc}%  {c.voltage:.3f} V  {c.current:+.3f} A  {c.power:+.1f} W  "
                  f"{c.remaining_ah:.1f}/{c.nominal_ah:.1f} Ah  cycles {c.cycles}  SOH {c.soh}%  "
                  f"CHG {'on' if c.charge_on else 'off'}  DSG {'on' if c.discharge_on else 'off'}  "
                  f"MOS {c.mos_temp_c} °C  T {c.temps_c}  alarms {c.alarms}  cells {c.cells}")
            await asyncio.sleep(3)
    finally:
        await bms.disconnect()


if __name__ == "__main__":
    asyncio.run(console(sys.argv[1] if len(sys.argv) > 1 else None))
