# Unit tests for the session condition history in alarm_history.py; run with: py -3.10 -m unittest test_alarm_history
import unittest

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


if __name__ == "__main__":
    unittest.main()
