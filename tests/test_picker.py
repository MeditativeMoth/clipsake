import os
import unittest
from datetime import datetime, timedelta, UTC

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from timeline.streams import LiveStream

from timeline.picker import TITLE_LENGTH, item_text


class ItemTextTests(unittest.TestCase):
    def test_uptime_and_short_title(self):
        started = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)
        stream = LiveStream("ROboUHfPUrU", "Live High-Definition Views from the International Space Station", started, None)
        title, live = item_text(stream, now=started + timedelta(hours=3, minutes=5)).split("\n")
        self.assertEqual(len(title), TITLE_LENGTH)
        self.assertTrue(title.endswith("…"))
        self.assertTrue(live.startswith("● LIVE  since "))
        self.assertTrue(live.endswith("· 3h 05m 00s"))

    def test_unknown_start(self):
        self.assertEqual(item_text(LiveStream("x" * 11, "Kittens", None, None)), "Kittens\n● LIVE")


if __name__ == "__main__":
    unittest.main()
