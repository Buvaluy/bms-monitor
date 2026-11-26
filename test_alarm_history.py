# Unit tests for the session condition history in alarm_history.py; run with: py -3.13 -m unittest test_alarm_history
import unittest
from unittest import mock

import alarm_history
from alarm_history import ALARM, LINK_DOWN, THRESHOLD, AlarmHistory, row


class AlarmHistoryTest(unittest.TestCase):
    def test_single_frame_alarm_stays_in_history(self):
        h = AlarmHistory()
        h.observe({}, 0)
        h.observe({"Alarm: cell overvoltage": (ALARM, "")}, 1)
        h.observe({}, 2)
        self.assertEqual([(e.name, e.start, e.end) for e in h.entries], [("Alarm: cell overvoltage", 1, 2)])
        self.assertEqual(h.active_count(), 0)

    def test_condition_lasting_several_frames_is_one_entry(self):
        h = AlarmHistory()
        for t in range(5):
            h.observe({"Cells": (THRESHOLD, "3.650 V")}, t)
        self.assertEqual(len(h.entries), 1)
        self.assertEqual(row(h.entries[0])[1], "active")

    def test_link_loss_closes_conditions_at_last_frame(self):
        h = AlarmHistory()
        h.observe({"Cells": (THRESHOLD, "3.650 V")}, 10)
        h.observe({"Cells": (THRESHOLD, "3.660 V")}, 11)
        h.link_down("Error: timeout", 50)
        h.link_down("Error: timeout", 170)
        h.link_up(200)
        cells, link = h.entries
        self.assertEqual((cells.end, cells.cut), (11, True))
        self.assertEqual((link.name, link.start, link.end, link.count), (LINK_DOWN, 50, 200, 2))

    def test_condition_returning_within_30_s_reopens_its_entry(self):
        h = AlarmHistory()
        h.observe({"Alarm: MOS overtemp": (ALARM, "")}, 0)
        for t in range(1, 101):
            h.observe({"Cells": (THRESHOLD, "3.601 V")} if t % 2 else {}, t)
        h.observe({"Cells": (THRESHOLD, "3.601 V")}, 140)
        self.assertEqual([(e.name, e.start, e.end, e.count) for e in h.entries],
                         [("Alarm: MOS overtemp", 0, 1, 1), ("Cells", 1, 100, 50), ("Cells", 140, None, 1)])
        with mock.patch.object(alarm_history, "LIMIT", 2):
            h = AlarmHistory()
            for t, name in enumerate(("A", None, "B", "C", "A")):
                h.observe({name: (THRESHOLD, "")} if name else {}, t)
        self.assertEqual([(e.name, e.end) for e in h.entries], [("C", 4), ("A", None)])
        self.assertEqual(h.active_count(), 1)


if __name__ == "__main__":
    unittest.main()
