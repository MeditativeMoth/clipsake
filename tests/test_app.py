import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from timeline import app


class FakeServer:
    """Stands in for frames.Server: records whether it was stopped."""

    created: list["FakeServer"] = []

    def __init__(self, video_id, env, on_line) -> None:
        self.video_id = video_id
        self.stopped = threading.Event()
        FakeServer.created.append(self)

    def stop(self) -> None:
        self.stopped.set()


class FakeStream:
    """Stands in for frames.Stream: open() waits until released, as ypb takes a few seconds."""

    release = threading.Event()
    opening = threading.Event()  # set once open() has started

    def __init__(self, server, env) -> None:
        self.server = server

    def open(self) -> None:
        FakeStream.opening.set()
        FakeStream.release.wait(5)
        raise RuntimeError("released")  # ends the thread; the test doesn't need it to open


class CloseWhileOpeningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        folder = tempfile.mkdtemp()
        ini = str(Path(folder) / "settings.ini")
        patches = [
            # Never the user's own settings: load() saves the last stream.
            mock.patch.object(app, "QSettings", lambda *_args: QSettings(ini, QSettings.IniFormat)),
            mock.patch.object(app, "Server", FakeServer),
            mock.patch.object(app, "Stream", FakeStream),
            mock.patch.object(app, "missing_tools", return_value=[]),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        FakeServer.created.clear()
        FakeStream.release.clear()
        FakeStream.opening.clear()
        self.addCleanup(FakeStream.release.set)
        self.window = app.Window({})

    def load(self, video_id: str) -> FakeServer:
        self.window.url_edit.setText(video_id)
        FakeStream.opening.clear()
        self.window.load()
        self.assertTrue(FakeStream.opening.wait(5))
        return FakeServer.created[-1]

    def test_closing_while_a_stream_opens_stops_its_ypb(self):
        server = self.load("ROboUHfPUrU")
        self.window.close()
        self.assertTrue(server.stopped.is_set())

    def test_closing_after_two_quick_loads_stops_both(self):
        first = self.load("ROboUHfPUrU")
        second = self.load("M3HKLzjvKPc")
        self.window.close()
        self.assertTrue(first.stopped.is_set())
        self.assertTrue(second.stopped.is_set())


if __name__ == "__main__":
    unittest.main()
