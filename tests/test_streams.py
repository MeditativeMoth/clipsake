import unittest
from datetime import UTC, datetime

from timeline.streams import LiveStream, channel_streams_url, stream_from_json, stream_to_json

CHANNEL_ID = "UC" + "x" * 22


class ChannelStreamsUrlTest(unittest.TestCase):
    def test_accepted_forms(self):
        cases = {
            "@somechannel": "https://www.youtube.com/@somechannel/streams",
            "somechannel": "https://www.youtube.com/@somechannel/streams",
            "  @somechannel/ ": "https://www.youtube.com/@somechannel/streams",
            CHANNEL_ID: f"https://www.youtube.com/channel/{CHANNEL_ID}/streams",
            "https://www.youtube.com/@somechannel": "https://www.youtube.com/@somechannel/streams",
        }
        for channel, expected in cases.items():
            with self.subTest(channel=channel):
                self.assertEqual(channel_streams_url(channel), expected)

    def test_other_channel_tabs_are_replaced(self):
        for tab in ("streams", "live", "videos", "featured", "shorts"):
            with self.subTest(tab=tab):
                self.assertEqual(
                    channel_streams_url(f"https://www.youtube.com/@somechannel/{tab}"),
                    "https://www.youtube.com/@somechannel/streams",
                )


class StreamJsonTest(unittest.TestCase):
    def test_round_trip(self):
        stream = LiveStream("abc123", "A title", datetime(2026, 10, 29, 18, 30, tzinfo=UTC), b"\x89PNG data")
        self.assertEqual(stream_from_json(stream_to_json(stream)), stream)

    def test_round_trip_without_optional_fields(self):
        stream = LiveStream("abc123", "A title", None, None)
        self.assertEqual(stream_from_json(stream_to_json(stream)), stream)

    def test_url(self):
        self.assertEqual(LiveStream("abc123", "", None, None).url, "https://www.youtube.com/watch?v=abc123")


if __name__ == "__main__":
    unittest.main()
