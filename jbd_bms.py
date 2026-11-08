# JBD (Xiaoxiang) BMS client over BLE: frame building/parsing and a polling client; run directly for a console readout.
import asyncio
import sys
from dataclasses import dataclass, field

from bleak import BleakClient, BleakScanner

SERVICE_UUID = "0000ff00-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"

REG_BASIC = 0x03
REG_CELLS = 0x04
REG_MOS = 0xE1


def checksum(payload: bytes) -> bytes:
    return ((0x10000 - sum(payload)) & 0xFFFF).to_bytes(2, "big")


def read_frame(reg: int) -> bytes:
    body = bytes([reg, 0x00])
    return b"\xDD\xA5" + body + checksum(body) + b"\x77"


def write_frame(reg: int, data: bytes) -> bytes:
    body = bytes([reg, len(data)]) + data
    return b"\xDD\x5A" + body + checksum(body) + b"\x77"


@dataclass
class BasicInfo:
    voltage: float
    current: float
    remaining_ah: float
    nominal_ah: float
    cycles: int
    balance_mask: int
    protection_mask: int
    soc: int
    charge_on: bool
    discharge_on: bool
    cell_count: int
    temps_c: list = field(default_factory=list)

    @property
    def power(self) -> float:
        return self.voltage * self.current


def parse_basic(d: bytes) -> BasicInfo:
    u16 = lambda i: int.from_bytes(d[i:i + 2], "big")
    ntc = d[22]
    return BasicInfo(
        voltage=u16(0) / 100,
        current=int.from_bytes(d[2:4], "big", signed=True) / 100,
        remaining_ah=u16(4) / 100,
        nominal_ah=u16(6) / 100,
        cycles=u16(8),
        balance_mask=u16(12) | (u16(14) << 16),
        protection_mask=u16(16),
        soc=d[19],
        charge_on=bool(d[20] & 0x01),
        discharge_on=bool(d[20] & 0x02),
        cell_count=d[21],
        temps_c=[(u16(23 + 2 * i) - 2731) / 10 for i in range(ntc)],
    )


def parse_cells(d: bytes) -> list:
    return [int.from_bytes(d[i:i + 2], "big") / 1000 for i in range(0, len(d), 2)]


class JbdBms:
    def __init__(self, address: str):
        self.address = address
        self._client = None
        self._buf = bytearray()
        self._reply = None
        self._lock = asyncio.Lock()

    async def connect(self):
        self._client = BleakClient(self.address, timeout=20.0)
        await self._client.connect()
        await self._client.start_notify(NOTIFY_UUID, self._on_notify)

    async def disconnect(self):
        if self._client:
            await self._client.disconnect()

    @property
    def is_connected(self) -> bool:
        return bool(self._client and self._client.is_connected)

    def _on_notify(self, _sender, data: bytearray):
        if data[:1] == b"\xDD":
            self._buf = bytearray()
        self._buf += data
        # Frame: DD reg status len data[len] crc[2] 77; replies arrive split into 20-byte chunks.
        if len(self._buf) >= 4 and len(self._buf) >= self._buf[3] + 7 and self._reply and not self._reply.done():
            self._reply.set_result(bytes(self._buf))

    async def _transact(self, frame: bytes) -> bytes:
        async with self._lock:
            self._reply = asyncio.get_running_loop().create_future()
            await self._client.write_gatt_char(WRITE_UUID, frame, response=False)
            resp = await asyncio.wait_for(self._reply, timeout=5.0)
        length = resp[3]
        if resp[2] != 0x00:
            raise IOError(f"BMS error status 0x{resp[2]:02X} for reg 0x{frame[2]:02X}")
        if checksum(resp[2:4 + length]) != resp[4 + length:6 + length]:
            raise IOError(f"bad checksum: {resp.hex(' ')}")
        return resp[4:4 + length]

    async def read_basic(self) -> BasicInfo:
        return parse_basic(await self._transact(read_frame(REG_BASIC)))

    async def read_cells(self) -> list:
        return parse_cells(await self._transact(read_frame(REG_CELLS)))

    async def set_mos(self, charge: bool, discharge: bool):
        # Register E1 takes a "disable" mask: bit0 = charge off, bit1 = discharge off.
        mask = (0 if charge else 0x01) | (0 if discharge else 0x02)
        await self._transact(write_frame(REG_MOS, bytes([0x00, mask])))


async def find_bms(timeout: float = 10.0):
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    for dev, adv in found.values():
        if SERVICE_UUID in [u.lower() for u in adv.service_uuids]:
            return dev.address, adv.local_name or dev.name
    return None, None


async def console(address: str = None):
    if not address:
        address, name = await find_bms()
        if not address:
            sys.exit("BMS not found")
        print(f"found {name} at {address}")
    bms = JbdBms(address)
    await bms.connect()
    try:
        while True:
            b = await bms.read_basic()
            cells = await bms.read_cells()
            print(f"SOC {b.soc}%  {b.voltage:.2f} V  {b.current:+.2f} A  {b.power:+.1f} W  "
                  f"{b.remaining_ah:.1f}/{b.nominal_ah:.1f} Ah  cycles {b.cycles}  "
                  f"CHG {'on' if b.charge_on else 'off'}  DSG {'on' if b.discharge_on else 'off'}  "
                  f"prot 0x{b.protection_mask:04X}  T {b.temps_c}  cells {cells}")
            await asyncio.sleep(3)
    finally:
        await bms.disconnect()


if __name__ == "__main__":
    asyncio.run(console(sys.argv[1] if len(sys.argv) > 1 else None))
