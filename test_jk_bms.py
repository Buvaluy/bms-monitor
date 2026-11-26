# Unit tests for the JK BMS frame assembly in jk_bms.py; run with: py -3.13 -m unittest test_jk_bms
import random
import unittest

from jk_bms import FRAME_HEADER, FRAME_LEN, TYPE_CELL_INFO, JkBms, parse_cell_info


def frame(frame_type: int, rnd: random.Random) -> bytes:
    body = FRAME_HEADER + bytes([frame_type]) + rnd.randbytes(FRAME_LEN - len(FRAME_HEADER) - 2)
    return body + bytes([sum(body) & 0xFF])


class FrameAssemblyTest(unittest.TestCase):
    def test_valid_frame_after_truncated_frame_is_the_only_frame_kept(self):
        rnd = random.Random(1)
        for _ in range(3000):
            valid = frame(TYPE_CELL_INFO, rnd)
            got = []
            bms = JkBms("00:00:00:00:00:00", on_cell_info=got.append)
            cut = rnd.randint(5, FRAME_LEN - len(FRAME_HEADER))
            bms._on_notify(None, bytearray(frame(TYPE_CELL_INFO, rnd)[:cut] + valid))
            self.assertEqual(got, [parse_cell_info(valid)])


if __name__ == "__main__":
    unittest.main()
