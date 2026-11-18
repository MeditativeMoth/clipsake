import os
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from timeline.frames import Missing, Playback, Segment, VideoFormat
from timeline.preview import IDLE, PREPARING, PreviewPlayer

FMT = VideoFormat("298", "avc1.4D4020", 1280, 720, 60)
CLIP = (102.0, 128.0)


class FakeStream:
    """Stands in for frames.Stream: `playback` waits until released, then returns `result`
    (a Playback, built for the path it's given, or an exception to raise)."""

    def __init__(self, result="playback") -> None:
        self.result = result
        self.release = threading.Event()
        self.cancelled = None

    def playback(self, start, end, fmt, path, progress, cancelled, reuse):
        self.cancelled = cancelled
        progress(1, 2)
        self.release.wait(5)
        if cancelled():
            raise Missing("cancelled")
        if isinstance(self.result, Exception):
            raise self.result
        path.write_bytes(b"not really a video")
        segments = tuple(Segment(sq, 100.0 + 5 * (sq - 1), 5.0) for sq in range(1, 7))
        return Playback(path, start, end, fmt, segments, 0.1)


def wait_for(condition, timeout=5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)


class PreviewPlayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.player = PreviewPlayer()
        self.addCleanup(self.player.close)
        self.events = []
        self.player.preparing.connect(lambda text: self.events.append(("preparing", text)))
        self.player.started.connect(lambda: self.events.append(("started",)))
        self.player.finished.connect(lambda message, kept: self.events.append(("finished", message, kept)))
        self.player.positionChanged.connect(lambda moment: self.events.append(("position", moment)))

    def test_preparing_reports_progress(self):
        stream = FakeStream()
        self.player.play(stream, CLIP, FMT)
        self.assertEqual(self.player.state, PREPARING)
        wait_for(lambda: ("preparing", "Preparing the preview: 1 of 2 segments…") in self.events)
        self.assertIn(("preparing", "Preparing the preview…"), self.events)
        stream.release.set()

    def test_toggle_while_preparing_cancels(self):
        stream = FakeStream()
        self.player.play(stream, CLIP, FMT)
        wait_for(lambda: stream.cancelled is not None)
        self.player.toggle()
        self.assertEqual(self.player.state, IDLE)
        self.assertTrue(stream.cancelled())
        self.assertEqual(self.events[-1], ("finished", "", False))
        stream.release.set()
        wait_for(lambda: not list(self.player.folder.glob("*.mp4")), timeout=1)
        self.assertNotIn(("started",), self.events)  # the late result is ignored

    def test_a_failed_download_is_reported(self):
        stream = FakeStream(RuntimeError("ffmpeg couldn't join the segments"))
        self.player.play(stream, CLIP, FMT)
        stream.release.set()
        wait_for(lambda: self.player.state == IDLE)
        self.assertEqual(self.events[-1],
                         ("finished", "Couldn't prepare the preview: ffmpeg couldn't join the segments", False))

    def test_ready_then_seek_and_stop(self):
        stream = FakeStream()
        self.player.play(stream, CLIP, FMT)
        stream.release.set()
        wait_for(lambda: ("started",) in self.events)
        self.assertEqual(self.player.playback.clip_start, 102.0)
        self.player.seek(500.0)  # past the clip: its end
        self.assertEqual(self.events[-1], ("position", 128.0))
        self.player.stop(keep_frame=True)
        self.assertEqual(self.player.state, IDLE)
        self.assertEqual(self.events[-1], ("finished", "", True))

    def test_seek_does_nothing_until_ready(self):
        self.player.seek(110.0)
        self.assertEqual(self.events, [])

    def test_a_new_preview_replaces_the_one_preparing(self):
        first, second = FakeStream(), FakeStream()
        self.player.play(first, CLIP, FMT)
        wait_for(lambda: first.cancelled is not None)
        self.player.play(second, (110.0, 120.0), FMT)
        self.assertTrue(first.cancelled())
        self.assertEqual(self.player.clip, (110.0, 120.0))
        first.release.set()
        second.release.set()
        wait_for(lambda: ("started",) in self.events)
        self.assertEqual(self.player.playback.clip_start, 110.0)


if __name__ == "__main__":
    unittest.main()
