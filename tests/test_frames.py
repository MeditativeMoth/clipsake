import os
import threading
import unittest
from pathlib import Path
from datetime import datetime, UTC
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from timeline import frames
from timeline.app import shorten
from timeline.frames import Missing, Segment, Server, Stream, VideoFormat

BASE = 1_793_000_000.0


def header(sq: int, start: float) -> bytes:
    return (
        b"\x00\x00ftypdash...http://youtube.com/streaming/metadata/segment/102015\x00="
        + f"Sequence-Number: {sq}\r\nIngestion-Walltime-Us: {round(start * 1e6)}\r\n"
          f"Ingestion-Uncertainty-Us: 95\r\nTarget-Duration-Us: 5000000\r\n".encode()
        + b"\x00moof..."
    )


class FakeServer:
    """Segments 100-299, 5 s each, with the stream down for a minute before segment 200."""

    def __init__(self) -> None:
        self.requests = []

    @staticmethod
    def start(sq: int) -> float:
        return BASE + sq * 5 + (60 if sq >= 200 else 0)

    def get(self, path: str, timeout: float, accept=None) -> bytes:
        sq = int(path.rsplit("/", 1)[1])
        self.requests.append(sq)
        if not 100 <= sq < 300:
            raise Missing("500 not found")
        return header(sq, self.start(sq))


def make_stream(known=(150,)) -> tuple[Stream, FakeServer]:
    server = FakeServer()
    stream = Stream(server, {"PATH": ""})
    stream.fast = VideoFormat("160", "avc1.42C00B", 256, 144, 15)
    for sq in known:
        stream.segment(sq)
    server.requests.clear()
    return stream, server


class ParsingTests(unittest.TestCase):
    def test_video_id(self):
        for text in ("ROboUHfPUrU", "https://www.youtube.com/watch?v=ROboUHfPUrU&t=5",
                     "https://youtu.be/ROboUHfPUrU", "https://www.youtube.com/live/ROboUHfPUrU?si=x"):
            self.assertEqual(frames.parse_video_id(text), "ROboUHfPUrU")
        self.assertIsNone(frames.parse_video_id("https://www.youtube.com/@NASA"))

    def test_segment_header(self):
        self.assertEqual(frames.parse_segment_header(header(152711, 1793444690.10358)),
                         Segment(152711, 1793444690.10358, 5.0))
        self.assertIsNone(frames.parse_segment_header(b"500 streaming segment: missing base URL"))

    def test_formats(self):
        mpd = """
        <Representation id="140" bandwidth="144000" codecs="mp4a.40.2" audioSamplingRate="44100">
        <Representation id="160" codecs="avc1.42C00B" width="256" height="144" frameRate="15">
        <Representation id="134" codecs="avc1.4D401E" width="640" height="360" frameRate="30">
        <Representation id="298" codecs="avc1.4D4020" width="1280" height="720" frameRate="60">
        <Representation id="299" codecs="avc1.64002a" width="1920" height="1080" frameRate="60">
        <Representation id="394" codecs="av01.0.00M.08" width="256" height="144" frameRate="30">
        """
        formats = frames.parse_formats(mpd)
        self.assertEqual([f.itag for f in formats], ["160", "394", "134", "298", "299"])
        fast, sharp = frames.choose_formats(formats)
        self.assertEqual((fast.itag, sharp.itag), ("160", "298"))
        self.assertEqual(sharp.label, "720p60 H.264")

    def test_shorten_ypb_log(self):
        line = 'level=WARN status=503 url="https://rr4.googlevideo.com/videoplayback?aitags=1&itag=160&sq=28788&x=1"'
        self.assertEqual(shorten(line), "level=WARN status=503 itag=160 sq=28788")

    def test_shorten_hides_any_other_url(self):
        # Signed YouTube URLs contain the user's IP address.
        line = "ERROR: unable to download https://rr4.googlevideo.com/videoplayback?ip=203.0.113.9&itag=160"
        self.assertEqual(shorten(line), "ERROR: unable to download <url>")


class LocalRequestTests(unittest.TestCase):
    def test_ypb_serve_listens_on_this_computer_only(self):
        # On every interface, other machines could reach it, and Windows asks about the firewall.
        process = mock.MagicMock(stdout=iter([]))
        with mock.patch.object(frames.subprocess, "Popen", return_value=process) as popen:
            server = Server("abc123", {"PATH": ""}, lambda line: None)
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("--host") + 1], "127.0.0.1")
        self.assertLess(command.index("--host"), command.index("--"))  # an option to ypb, not yt-dlp

    def server_saying(self, *lines: str) -> Server:
        """A Server whose ypb prints `lines`, once they've all been read."""
        process = mock.MagicMock(stdout=iter([f"{line}\n" for line in lines]))
        read = threading.Event()
        with mock.patch.object(frames.subprocess, "Popen", return_value=process):
            server = Server("abc123", {"PATH": ""}, lambda line: line == lines[-1] and read.set())
        self.assertTrue(read.wait(5))
        return server

    def test_ypb_picks_its_port_and_the_app_reads_it_from_ypb(self):
        # A port the app chose could be taken by the time ypb listens, and ypb would quietly use another.
        server = self.server_saying("(<<) Stream 'ISS' is alive!",
                                    "(<<) Playback started and listening on http://127.0.0.1:52811...")
        self.assertEqual((server.port, server.base), (52811, "http://127.0.0.1:52811"))

    def test_until_ypb_says_where_it_listens_nothing_is_asked(self):
        server = self.server_saying("(<<) Stream 'ISS' is alive!")
        with self.assertRaises(Missing):  # wait_ready keeps waiting
            server.get("/info", timeout=1)

    def test_only_an_address_on_this_computer_is_used(self):
        server = self.server_saying("(<<) Playback started and listening on http://192.168.0.30:52811...")
        self.assertIsNone(server.base)

    def test_the_command_lets_ypb_pick_the_port(self):
        process = mock.MagicMock(stdout=iter([]))
        with mock.patch.object(frames.subprocess, "Popen", return_value=process) as popen:
            Server("abc123", {"PATH": ""}, lambda line: None)
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("--port") + 1], "0")

    def test_ypb_serve_is_only_started_from_the_main_thread(self):
        # Linux stops it when the thread that started it ends (runtime.stop_with_this_process).
        errors = []

        def start():
            try:
                Server("abc123", {"PATH": ""}, lambda line: None)
            except RuntimeError as error:
                errors.append(error)

        with mock.patch.object(frames.subprocess, "Popen") as popen:
            thread = threading.Thread(target=start)
            thread.start()
            thread.join()
        popen.assert_not_called()
        self.assertEqual(len(errors), 1)

    def test_requests_to_ypb_ignore_proxy_settings(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"from ypb")

            def log_message(self, *args):
                pass

        httpd = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)  # cleanups run last first: stop serving, then close
        self.addCleanup(httpd.shutdown)
        server = Server.__new__(Server)  # without starting ypb
        server.base = f"http://127.0.0.1:{httpd.server_port}"
        dead_proxy = {"http_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9", "no_proxy": ""}
        with mock.patch.dict(os.environ, dead_proxy):
            self.assertEqual(server.get("/info", timeout=5), b"from ypb")


class LocateTests(unittest.TestCase):
    def test_estimate_uses_nearest_anchor(self):
        anchors = [Segment(100, BASE + 500, 5), Segment(200, BASE + 1060, 5)]
        self.assertEqual(frames.estimate_sq(anchors, BASE + 1100), 208)
        self.assertEqual(frames.estimate_sq(anchors, BASE + 512), 102)

    def test_first_guess_right_without_gaps(self):
        stream, server = make_stream()
        segment, fetches = stream.locate(BASE + 170 * 5 + 2.5)
        self.assertEqual((segment.sq, fetches, server.requests), (170, 1, [170]))

    def test_corrects_across_a_gap(self):
        stream, server = make_stream()  # only knows 150, so the guess ignores the gap
        moment = FakeServer.start(250) + 1
        segment, fetches = stream.locate(moment)
        self.assertEqual(segment.sq, 250)
        self.assertLessEqual(fetches, 2)

    def test_moment_inside_the_gap_ends(self):
        stream, _ = make_stream()
        segment, _ = stream.locate(FakeServer.start(199) + 30)  # stream was down
        self.assertIn(segment.sq, (199, 200))

    def test_known_segment_costs_nothing(self):
        stream, server = make_stream(known=(150, 170))
        _, fetches = stream.locate(FakeServer.start(170) + 4)
        self.assertEqual((fetches, server.requests), (0, []))

    def test_clamped_to_live_edge(self):
        stream, server = make_stream()
        stream.head = stream.segment(299)
        segment, _ = stream.locate(FakeServer.start(299) + 500)
        self.assertEqual(segment.sq, 299)
        self.assertNotIn(300, server.requests)

    def test_find_earliest(self):
        stream, _ = make_stream()
        stream.head = stream.segment(299)
        self.assertEqual(stream.find_earliest().sq, 100)



class LongStream:
    """A month-old stream, 5 s segments, of which YouTube kept the last `kept` seconds."""

    def __init__(self, kept: float, length: float = 30 * 86400) -> None:
        self.head = int(length // 5)
        self.oldest = self.head - int(kept // 5)
        self.missing = []

    def get(self, path: str, timeout: float, accept=None) -> bytes:
        sq = int(path.rsplit("/", 1)[1])
        if not self.oldest <= sq <= self.head:
            self.missing.append(sq)
            raise Missing("503")
        return header(sq, BASE + sq * 5)


def long_stream(server, started: float | None = None) -> Stream:
    stream = Stream(server, {"PATH": ""})
    stream.fast = VideoFormat("160", "avc1.42C00B", 256, 144, 15)
    if started is not None:
        stream.info = {"actualStartTime": datetime.fromtimestamp(started, UTC).isoformat()}
    stream.head = stream.segment(server.head)
    return stream


class EarliestTests(unittest.TestCase):
    def test_usually_one_missing_segment(self):
        server = LongStream(kept=7 * 86400 + 5000)  # a little over 7 days, as seen on YouTube
        oldest = long_stream(server).find_earliest()
        self.assertEqual(len(server.missing), 1)
        self.assertGreaterEqual(oldest.sq, server.oldest)
        self.assertLess(oldest.sq - server.oldest, frames.OLDEST_STEP / 5)  # within half an hour

    def test_young_stream_starts_at_zero(self):
        server = LongStream(kept=30 * 86400, length=86400)
        oldest = long_stream(server, started=BASE).find_earliest()
        self.assertEqual((oldest.sq, server.missing), (0, []))

    def test_keeping_less_falls_back_to_a_search(self):
        server = LongStream(kept=2 * 86400)
        self.assertEqual(long_stream(server).find_earliest().sq, server.oldest)


class PlaybackTests(unittest.TestCase):
    def test_audio_format(self):
        mpd = ('<Representation id="139" bandwidth="64000" codecs="mp4a.40.5">'
               '<Representation id="140" bandwidth="144000" codecs="mp4a.40.2">'
               '<Representation id="160" bandwidth="192223" codecs="avc1.42C00B" width="256" height="144">')
        self.assertEqual(frames.parse_audio_itag(mpd), "140")
        self.assertIsNone(frames.parse_audio_itag('<Representation id="160" codecs="avc1" width="256" height="144">'))

    def test_media_offset(self):
        segments = [Segment(1, 100.0, 5.0), Segment(2, 105.0, 5.0), Segment(3, 130.0, 5.0)]  # an outage before 3
        self.assertEqual(frames.media_offset(segments, 102.0), 2.0)
        self.assertEqual(frames.media_offset(segments, 107.5), 7.5)
        self.assertEqual(frames.media_offset(segments, 120.0), 10.0)  # in the outage: where segment 3 starts
        self.assertEqual(frames.media_offset(segments, 133.0), 13.0)

    def test_moment_at_is_the_inverse_across_an_outage(self):
        segments = [Segment(1, 100.0, 5.0), Segment(2, 105.0, 5.0), Segment(3, 130.0, 5.0)]  # down 110-130
        self.assertEqual(frames.moment_at(segments, 2.0), 102.0)
        self.assertEqual(frames.moment_at(segments, 10.0), 130.0)  # straight after the gap
        self.assertEqual(frames.moment_at(segments, 13.0), 133.0)
        self.assertEqual(frames.moment_at(segments, 99.0), 135.0)  # past the end: the end
        for moment in (100.0, 104.2, 109.9, 131.0, 134.5):
            self.assertAlmostEqual(frames.moment_at(segments, frames.media_offset(segments, moment)), moment)

    def test_playback_converts_both_ways(self):
        segments = (Segment(1, 100.0, 5.0), Segment(2, 105.0, 5.0), Segment(3, 130.0, 5.0))
        playback = frames.Playback(Path("p.mp4"), 103.0, 132.0, None, segments, 0.5)
        self.assertEqual((playback.start, playback.end), (3.0, 12.0))
        self.assertEqual(playback.moment(11.0), 131.0)  # simple arithmetic would say 111, in the outage
        self.assertEqual(playback.position(131.0), 11.0)


if __name__ == "__main__":
    unittest.main()
