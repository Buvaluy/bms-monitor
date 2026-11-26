# Unit tests for the JBD BMS client in jbd_bms.py; run with: py -3.13 -m unittest test_jbd_bms
import asyncio
import types
import unittest
from unittest import mock

import jbd_bms


def reply(reg: int, payload: bytes) -> bytes:
    body = bytes([0x00, len(payload)]) + payload
    return bytes([0xDD, reg]) + body + jbd_bms.checksum(body) + b"\x77"


def basic_payload(soc: int) -> bytes:
    d = bytearray(27)
    d[0:2] = (1320).to_bytes(2, "big")
    d[19], d[20], d[21], d[22] = soc, 0x03, 4, 2
    d[23:25] = d[25:27] = (2981).to_bytes(2, "big")
    return bytes(d)


class Probe:
    def __init__(self, address):
        self.address = address

    async def connect(self):
        if self.address == "11:11":
            raise OSError("characteristic ff01 not found")

    async def read_basic(self):
        return None

    async def disconnect(self):
        pass


class JbdBmsTest(unittest.TestCase):
    def test_late_reply_to_another_register_is_ignored(self):
        async def run():
            bms = jbd_bms.JbdBms("x")
            loop = asyncio.get_running_loop()

            async def write(_uuid, _frame, response=False):
                loop.call_soon(bms._on_notify, None, bytearray(reply(jbd_bms.REG_MOS, b"")))
                loop.call_soon(bms._on_notify, None, bytearray(reply(jbd_bms.REG_BASIC, basic_payload(77))))
            bms._client = types.SimpleNamespace(write_gatt_char=write)
            return await bms.read_basic()
        self.assertEqual(asyncio.run(run()).soc, 77)

    def test_disconnect_fails_a_pending_read_and_a_queued_write_is_checked_before_sending(self):
        async def run():
            bms, sent, checked = jbd_bms.JbdBms("x"), [], []

            async def write(_uuid, frame, response=False):
                sent.append(frame[2])

            async def disconnect():
                pass

            def refuse():
                checked.append(list(sent))
                raise InterruptedError
            bms._client = types.SimpleNamespace(write_gatt_char=write, disconnect=disconnect)
            read = asyncio.ensure_future(bms.read_basic())
            await asyncio.sleep(0)
            switch = asyncio.ensure_future(bms.set_mos(True, False, before_write=refuse))
            await asyncio.sleep(0)
            self.assertEqual(checked, [])
            await bms.disconnect()
            with self.assertRaises(ConnectionError):
                await asyncio.wait_for(read, 1)
            with self.assertRaises(InterruptedError):
                await switch
            self.assertEqual((sent, checked), ([jbd_bms.REG_BASIC], [[jbd_bms.REG_BASIC]]))
        asyncio.run(run())

    def test_find_bms_skips_advertisers_that_do_not_answer(self):
        def device(address, name, rssi):
            adv = types.SimpleNamespace(service_uuids=[jbd_bms.SERVICE_UUID.upper()], local_name=None, rssi=rssi)
            return types.SimpleNamespace(address=address, name=name), adv
        found = {"a": device("11:11", "lamp", -50), "b": device("33:33", "far JBD", -75),
                 "c": device("22:22", "xiaoxiang", -60)}

        async def discover(timeout, return_adv):
            return found
        with mock.patch.object(jbd_bms, "BleakScanner", types.SimpleNamespace(discover=discover)), \
                mock.patch.object(jbd_bms, "JbdBms", Probe):
            self.assertEqual(asyncio.run(jbd_bms.find_bms()), ("22:22", "xiaoxiang"))


if __name__ == "__main__":
    unittest.main()
