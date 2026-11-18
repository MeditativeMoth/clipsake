"""Stills of the open stream, fetched in the background: for the preview, and the timeline's thumbnails.

Two kinds of still for the preview:
- rough: the stream's smallest format (144p). Shown while you drag; usually
  well under 0.2 s once the area has been seen.
- sharp: up to 720p. Fetched once the moment has stayed put for SHARP_DELAY.
Each has its own worker (a Lane) that only ever runs the newest request, so a
slow sharp still never holds up the rough ones, and stale requests are dropped.

Thumbnails are rough stills of segment starts, a few at a time; a newer batch
makes the waiting ones of an older batch give up.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap

from timeline.frames import Still, Stream
from timeline.times import format_moment

SHARP_DELAY = 200  # ms the moment must stay put before the sharp still is fetched
THUMB_WORKERS = 3
MAX_THUMBS_PER_REQUEST = 40


class Lane:
    """A worker thread that only runs the newest job submitted; older waiting ones are dropped."""

    def __init__(self, name: str) -> None:
        self._condition = threading.Condition()
        self._job: Callable[[], None] | None = None
        self._closed = False
        threading.Thread(target=self._run, name=name, daemon=True).start()

    def submit(self, job: Callable[[], None]) -> None:
        with self._condition:
            self._job = job
            self._condition.notify()

    def clear(self) -> None:
        with self._condition:
            self._job = None

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify()

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._job is None and not self._closed:
                    self._condition.wait()
                if self._closed:
                    return
                job, self._job = self._job, None
            job()


def to_pixmap(still: Still) -> QPixmap:
    return QPixmap.fromImage(QImage.fromData(still.image, "PPM"))


class StillLoader(QObject):
    """Use from the GUI thread; results come back through the signals."""

    stillShown = Signal(object)  # QPixmap: the newest still for the preview
    stillFailed = Signal(str)  # why there's no still for the moment asked for last
    thumbnail = Signal(float, object)  # a segment's start, QPixmap of its first frame
    log = Signal(str)

    _still = Signal(object, int, str, object)  # Stream, request number, "rough"/"sharp", Still or Exception
    _thumb = Signal(object, object)  # Stream, Still

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.stream: Stream | None = None
        self.target: float | None = None  # the moment asked for last
        self._request = 0  # numbers each still request; only newer results replace the preview
        self._shown = 0
        self._generation = 0  # numbers each batch of thumbnails
        self._rough, self._sharp = Lane("rough stills"), Lane("sharp stills")
        self._pool = ThreadPoolExecutor(THUMB_WORKERS, thread_name_prefix="thumbnails")
        self._sharp_timer = QTimer(self, singleShot=True, interval=SHARP_DELAY, timeout=self._request_sharp)
        self._still.connect(self._on_still)
        self._thumb.connect(self._on_thumb)

    def set_stream(self, stream: Stream | None) -> None:
        """Take stills from `stream` from now on (None: none); anything still coming is dropped."""
        self._rough.clear()
        self._sharp.clear()
        self._sharp_timer.stop()
        self._generation += 1
        self.stream, self.target = stream, None

    def show(self, moment: float) -> None:
        """Fetch the still at `moment`: a rough one now, a sharp one once the moment stays put."""
        if self.stream is None:
            return
        self.target = moment
        self._sharp.clear()
        self._submit(self._rough, "rough", moment, self.stream.fast)
        self._sharp_timer.start()  # restarts it, so it fires once the moment stops changing

    def load_thumbnails(self, moments: list[float]) -> None:
        """Fetch the thumbnails at `moments` (the first ones first; at most MAX_THUMBS_PER_REQUEST),
        instead of any still waiting from the last batch."""
        if self.stream is None:
            return
        self._generation += 1
        for moment in moments[:MAX_THUMBS_PER_REQUEST]:
            self._pool.submit(self._thumb_job, self.stream, self._generation, moment)

    def close(self) -> None:
        self._rough.close()
        self._sharp.close()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _request_sharp(self) -> None:
        stream = self.stream
        if stream is None or self.target is None or stream.sharp == stream.fast:
            return
        self._submit(self._sharp, "sharp", self.target, stream.sharp)

    def _submit(self, lane: Lane, kind: str, moment: float, fmt) -> None:
        self._request += 1
        number, stream = self._request, self.stream

        def job() -> None:
            try:
                result = stream.still(moment, fmt)
            except Exception as error:  # reported through stillFailed / log
                result = error
            self._still.emit(stream, number, kind, result)

        lane.submit(job)

    def _on_still(self, stream: Stream, number: int, kind: str, result) -> None:
        if stream is not self.stream:
            return
        if isinstance(result, Exception):
            self.log.emit(f"{kind} still failed: {result}")
            if number == self._request:
                self.stillFailed.emit(f"No still here: {result}")
            return
        self.log.emit(f"{kind:5} {format_moment(result.moment)}  {result.summary()}")
        if number < self._shown:  # a newer still is already showing
            return
        self._shown = number
        self.stillShown.emit(to_pixmap(result))

    def _thumb_job(self, stream: Stream, generation: int, moment: float) -> None:
        if generation != self._generation:  # the view has moved on
            return
        try:
            self._thumb.emit(stream, stream.still(moment, stream.fast, snap=True))
        except Exception as error:
            self.log.emit(f"thumbnail failed: {error}")

    def _on_thumb(self, stream: Stream, still: Still) -> None:
        if stream is self.stream:
            self.thumbnail.emit(still.segment.start, to_pixmap(still))
