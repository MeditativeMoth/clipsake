"""Clip previews: download a clip's segments in the background, then play them with Qt.

PreviewPlayer goes idle → preparing (downloading) → playing ⇄ paused → idle. The
window shows `video` while one plays, follows `positionChanged` with the
playhead, and is told by `finished` when it stops, and whether the last frame
is still on screen.
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget

from timeline.frames import Missing, Playback, Stream, VideoFormat
from timeline.times import format_length

IDLE, PREPARING, PLAYING, PAUSED = "idle", "preparing", "playing", "paused"


class PreviewPlayer(QObject):
    """Plays one clip at a time. Not thread-safe: use it from the GUI thread (its download
    runs on a thread of its own and reports back through signals)."""

    preparing = Signal(str)  # a message to show while it downloads
    started = Signal()  # the file is ready: show `video`
    stateChanged = Signal(str)  # IDLE, PREPARING, PLAYING or PAUSED
    positionChanged = Signal(float)  # the stream time showing, while playing or after a seek
    finished = Signal(str, bool)  # stopped: a message to show instead (or ""), whether the last frame is kept
    log = Signal(str)

    _progress = Signal(int, int, int)  # preview number, segments done, total (from the download thread)
    _ready = Signal(int, object)  # preview number, Playback or Exception

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.folder = Path(tempfile.mkdtemp(prefix="clipsake-"))
        self.video = QVideoWidget()
        self._audio = QAudioOutput()
        self._player = QMediaPlayer(audioOutput=self._audio)
        self._player.setVideoOutput(self.video)
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._player.positionChanged.connect(self._on_position)
        self._player.playbackStateChanged.connect(lambda _state: self._announce())
        self._player.errorOccurred.connect(lambda _error, text: self.stop(f"Can't play the preview: {text}"))
        self._progress.connect(self._on_progress)
        self._ready.connect(self._on_ready)
        self._number = 0  # numbers each preview; a stopped one's results are ignored
        self._cancel = threading.Event()
        self._reuse: dict = {}  # the last preview's segments, reused if it's played again
        self._started = False  # whether the file has been started from the clip's start
        self.clip: tuple[float, float] | None = None  # the clip being previewed
        self.playback: Playback | None = None  # its file, once downloaded

    @property
    def state(self) -> str:
        if self.clip is None:
            return IDLE
        if self.playback is None:
            return PREPARING
        return PLAYING if self._player.playbackState() == QMediaPlayer.PlayingState else PAUSED

    def play(self, stream: Stream, clip: tuple[float, float], fmt: VideoFormat) -> None:
        """Download `clip` (stream times) in `fmt`, plus sound, then play it."""
        self.stop()
        self._number += 1
        number, cancel, reuse = self._number, threading.Event(), self._reuse
        self._cancel = cancel
        self.clip = clip
        path = self.folder / f"preview-{number}.mp4"  # a new name each time: a playing file may be locked

        def work() -> None:
            try:
                result = stream.playback(
                    clip[0], clip[1], fmt, path,
                    progress=lambda done, total: self._progress.emit(number, done, total),
                    cancelled=cancel.is_set, reuse=reuse,
                )
            except Exception as error:  # reported through `finished`
                result = error
            self._ready.emit(number, result)

        threading.Thread(target=work, name="clip preview", daemon=True).start()
        self.preparing.emit("Preparing the preview…")
        self._announce()

    def toggle(self) -> None:
        """Pause or resume; while it's still downloading, cancel it."""
        if self.state == PREPARING:
            self.stop()
        elif self.state == PLAYING:
            self._player.pause()
        elif self.state == PAUSED:
            self._player.play()

    def seek(self, moment: float) -> None:
        """Jump to `moment` (a stream time within the clip); playing carries on, paused stays paused."""
        if self.playback is None:
            return
        moment = min(max(moment, self.playback.clip_start), self.playback.clip_end)
        self._player.setPosition(round(self.playback.position(moment) * 1000))
        self.positionChanged.emit(moment)

    def stop(self, message: str = "", keep_frame: bool = False) -> None:
        """Stop previewing. With `keep_frame` (and something playing), the frame showing stays on
        screen until release(); otherwise the player lets go of the file at once."""
        if self.clip is None:
            return
        self._cancel.set()
        playing = self.playback
        keep_frame = keep_frame and playing is not None and not message
        self.clip, self.playback = None, None
        if keep_frame:
            self._player.pause()
            self._delete_files(keep=playing.path)
        else:
            self.release()
        if message:
            self.log.emit(message)
        self.finished.emit(message, keep_frame)
        self._announce()

    def release(self) -> None:
        """Let go of the file (and any frame kept on screen) and delete the preview files."""
        self._player.stop()
        self._player.setSource(QUrl())
        self._delete_files()

    def forget(self) -> None:
        """Drop the segments kept for playing the same clip again (another stream is opening)."""
        self._reuse.clear()

    def close(self) -> None:
        self.stop()
        self.release()
        shutil.rmtree(self.folder, ignore_errors=True)

    # From the download thread and the player

    def _on_progress(self, number: int, done: int, total: int) -> None:
        if number == self._number and self.state == PREPARING:
            self.preparing.emit(f"Preparing the preview: {done} of {total} segments…")

    def _on_ready(self, number: int, result) -> None:
        if number != self._number or self.state != PREPARING:  # stopped meanwhile
            if isinstance(result, Playback):
                result.path.unlink(missing_ok=True)
            return
        if isinstance(result, Exception):
            cancelled = isinstance(result, Missing) and str(result) == "cancelled"
            self.stop("" if cancelled else f"Couldn't prepare the preview: {result}")
            return
        self.playback, self._started = result, False
        self.log.emit(f"preview {format_length(result.clip_end - result.clip_start)} at {result.fmt.label}: "
                      f"{len(result.segments)} segments, ready in {result.fetch_s:.1f} s")
        self._player.stop()
        self._player.setSource(QUrl())
        self.started.emit()
        self._player.setSource(QUrl.fromLocalFile(str(result.path)))  # plays once loaded (_on_media_status)

    def _on_media_status(self, status) -> None:
        if not self.playback or self._player.source() != QUrl.fromLocalFile(str(self.playback.path)):
            return  # about an earlier file
        if status == QMediaPlayer.LoadedMedia and not self._started:
            # Only the first time: the player reports LoadedMedia again after a seek.
            self._started = True
            self._player.setPosition(round(self.playback.start * 1000))
            self._player.play()
        elif status == QMediaPlayer.EndOfMedia:
            self.stop(keep_frame=True)

    def _on_position(self, position: int) -> None:
        if self.state != PLAYING:
            return
        seconds = position / 1000
        if seconds >= self.playback.end:
            self.positionChanged.emit(self.playback.clip_end)
            self.stop(keep_frame=True)
        else:
            self.positionChanged.emit(self.playback.moment(seconds))

    def _announce(self) -> None:
        self.stateChanged.emit(self.state)

    def _delete_files(self, keep: Path | None = None) -> None:
        for old in self.folder.glob("preview-*.mp4"):
            if old != keep:
                with contextlib.suppress(OSError):  # still open (Windows): removed with the folder on exit
                    old.unlink()
