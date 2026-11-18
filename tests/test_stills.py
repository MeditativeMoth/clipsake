import os
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from timeline import stills
from timeline.frames import Segment, Still, VideoFormat

FAST = VideoFormat("160", "avc1.42C00B", 256, 144, 15)
SHARP = VideoFormat("298", "avc1.4D4020", 1280, 720, 60)
PIXEL = b"P6 1 1 255 \x00\x00\x00"  # a one-pixel PPM, as ffmpeg gives


class FakeStream:
    """Stands in for frames.Stream: still() returns a one-pixel still, or raises for `broken` moments."""

    def __init__(self, broken=()) -> None:
        self.fast, self.sharp = FAST, SHARP
        self.broken = set(broken)
        self.calls = []
        self.lock = threading.Lock()

    def still(self, moment, fmt, snap=False):
        with self.lock:
            self.calls.append((moment, fmt.itag, snap))
        if moment in self.broken:
            raise RuntimeError("no segment there")
        segment = Segment(int(moment // 5), moment // 5 * 5, 5.0)
        return Still(moment, segment, 0.0, fmt, PIXEL, 0, 0.0, 0.0, 0.0)


def wait_for(condition, timeout=5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)


class StillLoaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.loader = stills.StillLoader()
        self.addCleanup(self.loader.close)
        self.shown, self.failed, self.thumbs = [], [], []
        self.loader.stillShown.connect(self.shown.append)
        self.loader.stillFailed.connect(self.failed.append)
        self.loader.thumbnail.connect(lambda start, pixmap: self.thumbs.append(start))

    def test_rough_then_sharp(self):
        stream = FakeStream()
        self.loader.set_stream(stream)
        self.loader.show(1000.0)
        wait_for(lambda: len(self.shown) == 2)
        self.assertEqual([itag for _, itag, _ in stream.calls], ["160", "298"])  # rough first, sharp once it rests
        self.assertEqual(self.shown[-1].width(), 1)

    def test_nothing_without_a_stream(self):
        self.loader.show(1000.0)
        self.loader.load_thumbnails([1000.0])
        wait_for(lambda: False, timeout=0.3)
        self.assertEqual((self.shown, self.thumbs), ([], []))

    def test_a_failure_for_the_latest_moment_is_reported(self):
        self.loader.set_stream(FakeStream(broken={1000.0}))
        self.loader.show(1000.0)
        wait_for(lambda: self.failed)
        self.assertEqual(self.failed[0], "No still here: no segment there")

    def test_results_for_a_closed_stream_are_dropped(self):
        stream = FakeStream()
        self.loader.set_stream(stream)
        self.loader.show(1000.0)
        self.loader.set_stream(None)  # closed before the result comes back
        wait_for(lambda: stream.calls, timeout=1)
        wait_for(lambda: False, timeout=0.4)  # past the sharp delay too
        self.assertEqual(self.shown, [])

    def test_thumbnails(self):
        stream = FakeStream()
        self.loader.set_stream(stream)
        self.loader.load_thumbnails([1002.0, 1012.0, 1022.0])
        wait_for(lambda: len(self.thumbs) == 3)
        self.assertEqual(sorted(self.thumbs), [1000.0, 1010.0, 1020.0])  # each segment's start
        self.assertTrue(all(snap for _, _, snap in stream.calls))

    def test_at_most_a_batch_of_thumbnails(self):
        stream = FakeStream()
        self.loader.set_stream(stream)
        self.loader.load_thumbnails([float(t) for t in range(0, 5 * 100, 5)])
        wait_for(lambda: len(self.thumbs) == stills.MAX_THUMBS_PER_REQUEST)
        wait_for(lambda: False, timeout=0.2)
        self.assertEqual(len(self.thumbs), stills.MAX_THUMBS_PER_REQUEST)


class LaneTests(unittest.TestCase):
    def test_only_the_newest_waiting_job_runs(self):
        lane = stills.Lane("test")
        self.addCleanup(lane.close)
        busy, ran = threading.Event(), []
        lane.submit(lambda: (busy.wait(2), ran.append("first")))
        time.sleep(0.05)  # the first is running now
        lane.submit(lambda: ran.append("second"))
        lane.submit(lambda: ran.append("third"))  # replaces the second, which was only waiting
        busy.set()
        deadline = time.monotonic() + 2
        while len(ran) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(ran, ["first", "third"])


if __name__ == "__main__":
    unittest.main()
