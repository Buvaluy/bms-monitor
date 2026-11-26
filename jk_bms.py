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
TYPE_CELL_INFO = 0x02
TYPE_DEVICE_INFO = 0x03

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

    @property
    def alarms(self) -> list:
        return [n for i, n in enumerate(ALARM_BITS) if self.alarm_mask >> i & 1]


def parse_cell_info(f: bytes) -> CellInfo:
    u16 = lambda o: struct.unpack_from("<H", f, o)[0]
    i16 = lambda o: struct.unpack_from("<h", f, o)[0]
    u32 = lambda o: struct.unpack_from("<I", f, o)[0]
    i32 = lambda o: struct.unpack_from("<i", f, o)[0]
    mask = u32(70)
    present = [k for k in range(32) if mask >> k & 1]
    # Offsets 254.. hold extra sensors; -2000 (-200.0 °C) marks an unconnected one.
    temps = [i16(o) / 10 for o in (162, 164, 256, 258)]
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
    )


def parse_device_info(f: bytes) -> dict:
    s = lambda a, b: f[a:b].split(b"\x00")[0].decode(errors="replace")
    return {"model": s(6, 22), "hw": s(22, 30), "sw": s(30, 38), "name": s(46, 62),
            "mfg_date": s(78, 86), "serial": s(86, 97)}


class JkBms:
    def __init__(self, address: str):
        self.address = address
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
        # The settings frame follows device info; a command sent while it streams is ignored.
        await asyncio.sleep(2.0)

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
            del self._buf[:start + FRAME_LEN]
            if sum(frame[:-1]) & 0xFF != frame[-1]:
                continue
            self._latest[frame[4]] = (time.monotonic(), frame)
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

    async def read_cell_info(self) -> CellInfo:
        # After 0x96 the BMS keeps streaming cell info about once a second; reuse a fresh frame.
        ts, frame = self._latest.get(TYPE_CELL_INFO, (0.0, None))
        if time.monotonic() - ts > 5.0:
            frame = await self._request(CMD_CELL_INFO, TYPE_CELL_INFO)
        return parse_cell_info(frame)


async def find_bms(timeout: float = 10.0):
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    for dev, adv in found.values():
        if JK_MANUFACTURER_ID in adv.manufacturer_data and SERVICE_UUID in [u.lower() for u in adv.service_uuids]:
            return dev.address, adv.local_name or dev.name
    return None, None


async def console(address: str = None):
    if not address:
        address, name = await find_bms()
        if not address:
            sys.exit("JK BMS not found")
        print(f"found {name} at {address}")
    bms = JkBms(address)
    await bms.connect()
    print(bms.device_info)
    try:
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
