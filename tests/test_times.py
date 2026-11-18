import unittest
from datetime import UTC, datetime

from timeline import times, zones


class ZoneTestCase(unittest.TestCase):
    """Runs with the display zone set to ZONE, and puts the previous one back afterwards."""

    ZONE = "Europe/Berlin"  # clocks went back from 03:00 to 02:00 on Sun 25 Oct 2026

    def setUp(self):
        previous = zones.display_zone.key
        self.addCleanup(zones.set_display_zone, previous)
        zones.set_display_zone(self.ZONE)


class FormatTests(ZoneTestCase):
    def test_format_length(self):
        self.assertEqual(times.format_length(65), "1:05")
        self.assertEqual(times.format_length(3725), "1:02:05")

    def test_default_zone_is_this_computers(self):
        name = zones.system_zone_name()
        self.assertEqual(zones.find_zone(name), name)  # a zone the picker knows, or UTC

    def test_times_follow_the_display_zone(self):
        moment = datetime(2026, 10, 31, 19, 30, tzinfo=UTC).timestamp()
        self.assertEqual(times.format_moment(moment), "Sat 31 Oct 20:30:00")
        zones.set_display_zone("America/New_York")
        self.assertEqual(times.format_moment(moment), "Sat 31 Oct 15:30:00")
        self.assertEqual(zones.from_local(2026, 10, 31, 15, 30), moment)

    def test_wall_clock_round_trip(self):
        start = datetime(2026, 10, 24, tzinfo=UTC).timestamp()
        for hours in range(0, 72, 5):
            moment = start + hours * 3600
            self.assertEqual(times.from_wall_clock(times.wall_clock(moment)), moment)


def at(text: str) -> float:
    """`text` read in the display zone."""
    return datetime.fromisoformat(text).replace(tzinfo=zones.display_zone).timestamp()


class GotoTests(ZoneTestCase):
    LIVE = "2026-10-31 12:30:00"  # a Saturday

    def goto(self, text):
        return times.parse_goto(text, at(self.LIVE))

    def test_time_alone_is_its_latest_occurrence(self):
        self.assertEqual(self.goto("9:15"), at("2026-10-31 09:15:00"))
        self.assertEqual(self.goto("14:30"), at("2026-10-30 14:30:00"))  # not yet today
        self.assertEqual(self.goto("9pm"), at("2026-10-30 21:00:00"))
        self.assertEqual(self.goto("12:05:09 am"), at("2026-10-31 00:05:09"))

    def test_days(self):
        self.assertEqual(self.goto("yesterday 9:15"), at("2026-10-30 09:15:00"))
        self.assertEqual(self.goto("Fri 22:00"), at("2026-10-30 22:00:00"))
        self.assertEqual(self.goto("friday 22:00"), at("2026-10-30 22:00:00"))
        self.assertEqual(self.goto("sat 08:00"), at("2026-10-31 08:00:00"))
        self.assertEqual(self.goto("sat 18:00"), at("2026-10-24 18:00:00"))  # last week's
        self.assertEqual(self.goto("2026-10-29 08:00"), at("2026-10-29 08:00:00"))
        self.assertEqual(self.goto("Sun 02:30"), at("2026-10-25 02:30:00"))  # across the clock change

    def test_day_and_month(self):
        self.assertEqual(self.goto("29 Oct 08:00"), at("2026-10-29 08:00:00"))
        self.assertEqual(self.goto("29 october 8am"), at("2026-10-29 08:00:00"))
        self.assertEqual(self.goto("Thu 29 Oct 08:00:05"), at("2026-10-29 08:00:05"))
        self.assertEqual(self.goto("29 Oct 2026 08:00"), at("2026-10-29 08:00:00"))
        self.assertEqual(self.goto("31 Oct 14:00"), at("2026-10-31 14:00:00"))  # named, so even if after live
        self.assertEqual(self.goto("2 Nov 10:00"), at("2025-11-02 10:00:00"))  # not yet this year: last year's

    def test_reads_what_the_fields_show(self):
        for zone in ("Europe/Berlin", "America/New_York", "Asia/Tokyo"):
            zones.set_display_zone(zone)
            for moment in (at(self.LIVE) - 3600, at("2026-10-25 02:30:00"), at("2026-10-26 23:59:59")):
                with self.subTest(zone=zone, moment=moment):
                    self.assertEqual(self.goto(times.format_moment(moment)), moment)

    def test_read_in_the_display_zone(self):
        zones.set_display_zone("America/New_York")
        self.assertEqual(self.goto("9:15"), datetime(2026, 10, 31, 13, 15, tzinfo=UTC).timestamp())

    def test_nonsense(self):
        for text in ("", "noon", "25:00", "13pm", "blursday 10:00", "2026-02-30 10:00", "10:61", "30 Feb 10:00",
                     "29 Smarch 10:00", "Xyz 29 Oct 10:00"):
            self.assertIsNone(self.goto(text), text)


if __name__ == "__main__":
    unittest.main()
