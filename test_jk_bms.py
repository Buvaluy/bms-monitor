# Unit tests for the JK BMS frame assembly in jk_bms.py; run with: py -3.10 -m unittest test_jk_bms
import unittest

from jk_bms import FRAME_HEADER, FRAME_LEN, TYPE_CELL_INFO, JkBms


def frame(frame_type: int) -> bytes:
    body = FRAME_HEADER + bytes([frame_type]) + bytes(FRAME_LEN - len(FRAME_HEADER) - 2)
    return body + bytes([sum(body) & 0xFF])


class FrameAssemblyTest(unittest.TestCase):
    def test_valid_frame_after_truncated_frame_is_kept(self):
        bms = JkBms("00:00:00:00:00:00")
        bms._on_notify(None, bytearray(frame(TYPE_CELL_INFO)[:100] + frame(TYPE_CELL_INFO)))
        self.assertIn(TYPE_CELL_INFO, bms._latest)


if __name__ == "__main__":
    unittest.main()
