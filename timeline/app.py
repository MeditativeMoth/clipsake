"""The window: load a live stream, pick a clip on the timeline, see stills, clip it.

It connects the parts: timeline.frames reads the stream (through `ypb serve`),
timeline.stills fetches stills and thumbnails from it, timeline.preview plays
clip previews, timeline.widgets draws the timeline, and the clip itself is made
by the clipper (timeline.clipper).
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import socket
import sys
import threading
from datetime import datetime, UTC
from pathlib import Path

from PySide6.QtCore import (
    QEvent, QObject, QPointF, QRectF, QSettings, QSocketNotifier, QStandardPaths, Qt, QTimer, QUrl, Signal,
)
from PySide6.QtGui import (
    QColor, QDesktopServices, QIcon, QKeySequence, QPainter, QPolygonF, QPixmap, QShortcut,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QCompleter, QDialog, QDialogButtonBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QSplitter, QStackedWidget,
    QVBoxLayout, QWidget,
)
from timeline import zones
from timeline.clipper import clean_line
from timeline.frames import (
    Segment, Server, Stream, parse_time, parse_video_id,
)
from timeline.picker import StreamPicker
from timeline.preview import IDLE, PAUSED, PLAYING, PREPARING, PreviewPlayer
from timeline.stills import StillLoader
from timeline.processes import ClipRunner
from timeline.runtime import missing_tools, prepare_environment
from timeline.theme import apply_dark_theme
from timeline.times import GOTO_EXAMPLES, format_length, format_moment, parse_goto
from timeline.updates import Release, current_version, is_newer, latest_release
from timeline.widgets import DEFAULT_LENGTH, OverviewWidget, PreviewWidget, TimelineWidget, plain_tooltip
from timeline.zones import find_zone, local, set_display_zone, system_zone_name, zone_names

APP_ICON = Path(__file__).parent / "assets" / "clipsake.svg"  # window and taskbar icon
APP_ID = "io.github.MeditativeMoth.ClipSake"  # the Flatpak's ID (packaging/flatpak)
HEAD_REFRESH = 15_000  # ms between checks of the live edge
LOGGED_URL = re.compile(r'url="([^"]*)"')
# Where the Flatpak's file chooser puts a file saved outside the folders it may use.
PORTAL_FOLDER = re.compile(r"/run/user/\d+/doc/")
ZOOM_SPANS = [60, 600, 3600, 21600, 86400, None]  # keys 1-6, in seconds; None: everything available
DEFAULT_SPAN = 1800  # seconds shown when a stream opens, and by Reset zoom with no clip
KEPT_DAYS = 7  # a guess at how far back YouTube keeps, until the real oldest is found
SHORTCUTS = [  # for All shortcuts…
    ("Drag", "move along the timeline (or drag the box in the bar above it)"),
    ("Click", "with no clip, mark its start; then move its selected (brighter) end there"),
    ("Click a handle", "select that end of the clip"),
    ("Drag a red handle", "move that end"),
    ("Shift+drag", "select a whole clip"),
    ("Ctrl+click", "move the playhead (the white line) without changing the clip"),
    ("Wheel, double-click", "zoom around the mouse"),
    ("Shift+wheel", "move along"),
    ("+ / −", "zoom around the selected handle or the playhead"),
    ("1 – 6", "zoom to 1 min, 10 min, 1 h, 6 h, 1 day, all"),
    ("← / →", "nudge the selected handle or the playhead by 1 s (Shift: 10 s; Ctrl: 0.2 s); "
              "click the timeline first"),
    ("I / O", "start / end the clip at the playhead"),
    ("Z", "reset zoom: show the whole clip"),
    ("Space", "play, pause or resume the preview"),
    ("Click inside the clip", "while the preview plays or is paused: jump there"),
    ("G", "go to a time (type it under the preview)"),
    ("L", "jump to now"),
]


class Bridge(QObject):
    """Carries results from worker threads to the window (Qt queues these across threads)."""

    opened = Signal(int, object)  # load token, Stream
    open_failed = Signal(int, str)
    oldest = Signal(object, object)  # Stream, Segment
    head = Signal(object, object)  # Stream, Segment
    log = Signal(str)


class UpdateChecker(QObject):
    """Asks GitHub for the latest release off the GUI thread (one small request; see timeline.updates)."""

    newer_release = Signal(object)  # Release, only when newer than this version

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        release = latest_release()
        if release is not None and is_newer(release.version, current_version()):
            self.newer_release.emit(release)


def shorten(line: str) -> str:
    """ypb's line for the log: the googlevideo URLs it logs on each retry shortened to their
    itag and sq, and any other URL hidden (signed YouTube URLs contain the user's IP address)."""
    def brief(match: re.Match) -> str:
        found = dict(re.findall(r"[?&](itag|sq)=(\d+)", match.group(1)))
        return f"itag={found.get('itag', '?')} sq={found.get('sq', '?')}"

    return clean_line(LOGGED_URL.sub(brief, line))


class ElidedLabel(QLabel):
    """A one-line label that shortens its text with "…" to fit: in the middle by default
    ("Saved Live High…10.15.mp4"), or at the end with elide=Qt.ElideRight."""

    def __init__(self, text: str = "", elide=Qt.ElideMiddle, **kwargs) -> None:
        super().__init__(text, **kwargs)
        self.elide = elide
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)  # never widens the window

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        text = self.fontMetrics().elidedText(self.text(), self.elide, self.width())
        painter.drawText(self.rect(), int(self.alignment()), text)


def media_icon(kind: str, color: str = "#e8e8ec") -> QIcon:
    """A "play", "pause" or "stop" icon in the theme's text colour (the style's own are
    black, hard to see on dark buttons; text symbols like ❚❚ are missing from some fonts)."""
    size = 32
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(color))
    if kind == "play":
        painter.drawPolygon(QPolygonF([QPointF(9, 6), QPointF(26, 16), QPointF(9, 26)]))
    elif kind == "pause":
        painter.drawRect(QRectF(8, 7, 6, 18))
        painter.drawRect(QRectF(18, 7, 6, 18))
    else:
        painter.drawRect(QRectF(8, 8, 16, 16))
    painter.end()
    return QIcon(pixmap)


class Window(QMainWindow):
    def __init__(self, env: dict[str, str]) -> None:
        super().__init__()
        self.env = env
        self.setWindowTitle("ClipSake")
        self.resize(1360, 840)
        self.settings = QSettings("MeditativeMoth", "ClipSake")
        # Before any widgets are built, since they show times in this zone.
        set_display_zone(find_zone(self.settings.value("time_zone", "")) or system_zone_name())
        self.bridge = Bridge()
        self.bridge.opened.connect(self.on_opened)
        self.bridge.open_failed.connect(self.on_open_failed)
        self.bridge.oldest.connect(self.on_oldest)
        self.bridge.head.connect(self.on_head)
        self.bridge.log.connect(self.log_line)

        self.stream: Stream | None = None
        self.video_id: str | None = None  # the stream open or opening
        # ypb servers whose stream is still opening (in load's thread): closing the
        # window stops these too, or they'd keep running after the app has gone.
        self.opening: set[Server] = set()
        self.load_token = 0
        # Stills and thumbnails (timeline.stills), fetched in the background.
        self.stills = StillLoader(self)
        self.stills.stillShown.connect(lambda pixmap: self.preview.show_still(pixmap))
        self.stills.stillFailed.connect(lambda message: self.preview.show_message(message))
        self.stills.log.connect(self.log_line)

        # Clip previews (timeline.preview): downloaded in the background, then played by Qt.
        self.previewer = PreviewPlayer(self)
        self.previewer.preparing.connect(lambda text: self.preview.show_message(text))
        self.previewer.started.connect(self.on_preview_started)
        self.previewer.stateChanged.connect(self.on_preview_state)
        self.previewer.positionChanged.connect(self.on_preview_position)
        self.previewer.finished.connect(self.on_preview_finished)
        self.previewer.log.connect(self.log_line)

        self.thumb_timer = QTimer(self, singleShot=True, interval=120, timeout=self.load_thumbs)
        self.head_timer = QTimer(self, interval=HEAD_REFRESH, timeout=self.refresh_head)

        self.clipper = ClipRunner(env)
        self.clipper.progress.connect(self.on_clip_progress)
        self.clipper.log_line.connect(self.log_line)
        self.clipper.done.connect(self.on_clip_done)

        self.build()

    # Layout

    def build(self) -> None:
        self.picker = StreamPicker(self.env, self.settings)
        self.picker.chosen.connect(self.on_chosen)
        # Or a stream by URL, at the bottom of the sidebar.
        self.url_edit = QLineEdit(placeholderText="URL or video ID")
        self.url_edit.returnPressed.connect(self.load)
        self.open_box = QWidget()  # lined up with the Start / End row (see line_up_sidebar)
        open_row = QHBoxLayout(self.open_box)
        open_row.setContentsMargins(0, 0, 0, 0)
        open_row.addWidget(self.url_edit, 1)
        open_row.addWidget(QPushButton("Open", clicked=self.load))
        self.url_edit.textEdited.connect(lambda: self.show_url_error(""))
        self.url_error = QLabel(objectName="error", wordWrap=True, visible=False)
        self.url_error.setTextFormat(Qt.PlainText)
        self.picker.add_footer(QLabel("Or open a live stream by URL:", objectName="muted"))
        self.picker.add_footer(self.url_error)  # above the box, so the box stays lined up
        self.picker.add_footer(self.open_box)
        self.below_open = QWidget()  # as tall as the help line under the Start / End row
        self.picker.add_footer(self.below_open)

        # The stream's title.
        self.title = QLabel(objectName="muted", textFormat=Qt.PlainText)  # from YouTube: never as HTML
        self.title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)  # a long title is cut off, not widening the window
        self.preview = PreviewWidget()
        self.screen = QStackedWidget()  # the still preview, or the clip playing
        self.screen.addWidget(self.preview)
        self.screen.addWidget(self.previewer.video)
        self.timeline = TimelineWidget()
        self.timeline.previewRequested.connect(self.request_preview)
        self.timeline.seekRequested.connect(self.seek_preview)
        self.timeline.selectionChanged.connect(self.update_selection)
        self.timeline.thumbnailsWanted.connect(lambda: self.thumb_timer.start())
        self.stills.thumbnail.connect(self.timeline.add_thumb)

        self.overview = OverviewWidget(self.timeline)
        self.overview.setVisible(False)
        self.timeline.setVisible(False)

        # Under the preview: the time shown, which is also where to type a time to go to;
        # then the time zone and the preview resolution.
        self.shown_moment: float | None = None
        self.goto_edit = QLineEdit(placeholderText="Go to: 14:30, yesterday 9:15, Fri 22:00…", enabled=False)
        self.goto_edit.setToolTip(f"The time shown. To go to another, type it and press Enter (G): {GOTO_EXAMPLES}. "
                                  "A time without a day means its most recent occurrence; a weekday or a date "
                                  "without a year, the most recent one.")
        self.goto_edit.setMinimumWidth(190)
        self.goto_edit.returnPressed.connect(self.go_to)
        self.goto_edit.installEventFilter(self)  # left without going: show the time again (eventFilter)
        self.goto_error = QLabel(objectName="error")
        self.moment_bar = QWidget(visible=False)  # with the overview and timeline: only once a stream is open
        moment_row = QHBoxLayout(self.moment_bar)
        moment_row.setContentsMargins(0, 0, 0, 0)
        moment_row.addWidget(self.goto_edit)
        moment_row.addWidget(self.zone_picker())  # the zone that time (and every other) is shown in
        # These two are shown only once there's a clip (see update_selection); Z works either way.
        self.reset_button = self.add_button(moment_row, "Reset zoom", "Z", self.reset_zoom,
                                            "Show the whole clip, or with no clip yet, 30 minutes around the moment shown")
        self.play_button = self.add_button(moment_row, "Play preview", "Space", self.toggle_preview,
                                           "Play the clip from its start to its end, with sound")
        self.set_play_button("Play preview", "play")
        for button in (self.reset_button, self.play_button):
            button.setVisible(False)
        moment_row.addWidget(self.goto_error)
        moment_row.addStretch(1)

        # Getting around, by key only (no buttons): 1-6 zoom to a span around the focus,
        # +/- zoom in/out around it, L jumps to the live edge.
        for index, span in enumerate(ZOOM_SPANS, start=1):
            slot = self.timeline.show_all if span is None else (lambda s=span: self.timeline.zoom_to_span(s))
            self.shortcut(str(index), slot)
        for key, factor in (("+", 0.5), ("=", 0.5), ("-", 2.0)):
            self.shortcut(key, lambda f=factor: self.timeline.available and self.timeline.zoom_by(f))
        self.shortcut("L", self.go_live)
        self.shortcut("G", lambda: (self.goto_edit.setFocus(), self.goto_edit.selectAll()))

        # The clip: typed (read like Go to) or dragged.
        # Then saving it, on the same row: what happened to the last clip, Precise cut and Clip….
        self.start_edit, self.end_edit = self.time_edit("start"), self.time_edit("end")
        self.length_label = QLabel()
        self.clip_error = QLabel(objectName="error")
        # No buttons for these: I / O start / end the clip at the playhead.
        self.shortcut("I", self.set_start)
        self.shortcut("O", self.set_end)
        self.progress = QProgressBar(visible=False, maximumWidth=220)
        self.clip_status = ElidedLabel(objectName="muted")  # a long name is shortened
        self.clip_status.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.show_folder_button = QPushButton("Show in folder", visible=False, clicked=self.show_saved_folder)
        self.saved_path: Path | None = None
        self.precise = QCheckBox("Precise cut")
        self.precise.setToolTip("Cut at the exact times, not whole seconds. Re-encodes the clip, so it's slower.")
        self.clip_button = QPushButton("Clip…", objectName="primary", clicked=self.toggle_clip,
                                       enabled=False)  # "Cancel clipping" while clipping
        for widget in (self.show_folder_button, self.precise, self.clip_button):
            widget.setFocusPolicy(Qt.NoFocus)  # so Space plays the clip rather than pressing them
        clip_row = QHBoxLayout()
        clip_row.addWidget(QLabel("<b>Start</b>"))
        clip_row.addWidget(self.start_edit)
        clip_row.addWidget(QLabel("<b>End</b>"))
        clip_row.addWidget(self.end_edit)
        clip_row.addWidget(self.length_label)
        clip_row.addWidget(self.clip_error)
        clip_row.addWidget(self.clip_status, 1)
        clip_row.addWidget(self.progress, 1)
        clip_row.addWidget(self.show_folder_button)
        clip_row.addSpacing(8)
        clip_row.addWidget(self.precise)
        clip_row.addWidget(self.clip_button)

        # The essentials; the rest are in All shortcuts.
        self.help = ElidedLabel("Drag: move along · click: mark the start · drag a red handle: move it · "
                                "wheel: zoom · Space: play the preview", Qt.ElideRight, objectName="muted")
        self.help.setToolTip(self.help.text())  # all of it, when it's shortened
        shortcuts_button = QPushButton("All shortcuts…", objectName="link", focusPolicy=Qt.NoFocus,
                                       clicked=self.show_shortcuts)
        self.log_button = QPushButton(objectName="link", focusPolicy=Qt.NoFocus, clicked=self.toggle_log)
        self.update_check = QCheckBox("Check for updates", focusPolicy=Qt.NoFocus)
        self.update_check.setToolTip("At startup, ask GitHub whether a newer release of ClipSake exists")
        self.update_check.setChecked(self.settings.value("updates/enabled", True, type=bool))
        self.update_check.toggled.connect(lambda on: self.settings.setValue("updates/enabled", on))
        help_row = QHBoxLayout()
        help_row.addWidget(self.help, 1)
        help_row.addWidget(shortcuts_button)
        help_row.addWidget(self.update_check)
        help_row.addWidget(self.log_button)

        # Shown above everything when a newer release exists (see on_newer_release).
        self.pending_release: Release | None = None
        self.update_label = QLabel(textFormat=Qt.PlainText)
        self.download_button = QPushButton(objectName="link", focusPolicy=Qt.NoFocus, clicked=self.download_release)
        notes_button = QPushButton("Release notes", objectName="link", focusPolicy=Qt.NoFocus,
                                   clicked=self.open_release_notes)
        skip_button = QPushButton("Skip this version", objectName="link", focusPolicy=Qt.NoFocus,
                                  clicked=self.skip_release)
        self.update_banner = QFrame(objectName="updateBanner", visible=False)  # tinted (see theme.py)
        banner_row = QHBoxLayout(self.update_banner)
        banner_row.setContentsMargins(12, 6, 6, 6)
        banner_row.addWidget(self.update_label, 1)
        for button in (self.download_button, notes_button, skip_button):
            banner_row.addWidget(button)
        self.update_checker = UpdateChecker()
        self.update_checker.newer_release.connect(self.on_newer_release)
        self.log = QPlainTextEdit(readOnly=True, maximumBlockCount=2000)
        self.log.setPlaceholderText("Timings and ypb's output appear here.")

        upper = QWidget()
        layout = QVBoxLayout(upper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.title)
        layout.addWidget(self.screen, 1)
        layout.addWidget(self.moment_bar)
        layout.addWidget(self.overview)
        layout.addWidget(self.timeline)
        layout.addLayout(clip_row)
        layout.addLayout(help_row)

        self.picker.setMinimumWidth(240)
        columns = QSplitter(Qt.Horizontal)
        columns.addWidget(self.picker)
        columns.addWidget(upper)
        columns.setStretchFactor(1, 1)
        columns.setSizes([300, 1060])
        # The log goes under both columns, so the sidebar and the clip rows still end level when it's shown.
        rows = QSplitter(Qt.Vertical)
        rows.addWidget(columns)
        rows.addWidget(self.log)
        rows.setStretchFactor(0, 1)
        rows.setSizes([660, 140])
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.addWidget(self.update_banner)
        outer.addWidget(rows)
        self.setCentralWidget(central)
        self.line_up_sidebar(clip_row, help_row)
        self.set_log_visible(self.settings.value("show_log", False, type=bool))
        self.update_selection()

    def line_up_sidebar(self, clip_row: QHBoxLayout, help_row: QHBoxLayout) -> None:
        """Size the URL box's row like the Start / End row, and the space under it like the help
        line, so the URL box lines up with the Start and End fields (both columns end level)."""
        def height(row: QHBoxLayout) -> int:
            return max(row.itemAt(i).widget().sizeHint().height()
                       for i in range(row.count()) if row.itemAt(i).widget())

        self.open_box.setFixedHeight(height(clip_row))
        self.below_open.setFixedHeight(height(help_row))

    def show_url_error(self, text: str) -> None:
        self.url_error.setText(text)
        self.url_error.setVisible(bool(text))

    def set_play_button(self, text: str, icon: str) -> None:
        """Play preview / Pause / Resume / Cancel preview, with its icon (see media_icon)."""
        self.play_button.setText(f"{text} (Space)")
        self.play_button.setIcon(media_icon(icon))

    def shortcut(self, key: str, slot) -> None:
        """A key that works anywhere in the window, except where it's typed (text fields take it first)."""
        QShortcut(QKeySequence(key), self, slot, context=Qt.WindowShortcut)

    def set_log_visible(self, visible: bool) -> None:
        self.log.setVisible(visible)
        self.log_button.setText("Hide log" if visible else "Show log")

    def toggle_log(self) -> None:
        visible = not self.log.isVisible()
        self.set_log_visible(visible)
        self.settings.setValue("show_log", visible)

    def show_shortcuts(self) -> None:
        rows = "".join(f"<tr><td style='padding-right:16px'><b>{keys}</b></td><td>{what}</td></tr>"
                       for keys, what in SHORTCUTS)
        dialog = QDialog(self, windowTitle="Shortcuts")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(f"<table cellspacing='4'>{rows}</table>"))
        buttons = QDialogButtonBox(QDialogButtonBox.Close, rejected=dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def add_button(self, row: QHBoxLayout, text: str, key: str, slot, tip: str) -> QPushButton:
        """A button with a keyboard shortcut (see `shortcut`). It doesn't take the focus."""
        button = QPushButton(f"{text} ({key})", clicked=slot, focusPolicy=Qt.NoFocus, toolTip=f"{tip} (key: {key})")
        row.addWidget(button)
        self.shortcut(key, slot)
        return button

    def zone_picker(self) -> QComboBox:
        """An editable list of time zones; typing any part of a name searches it."""
        combo = self.zone_combo = QComboBox(editable=True, insertPolicy=QComboBox.NoInsert, maxVisibleItems=20)
        combo.addItems(zone_names())
        combo.setCurrentText(zones.display_zone.key)
        combo.setMinimumContentsLength(16)  # e.g. America/New_York; longer names scroll
        combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        combo.setToolTip("Times on the timeline, in the fields and in the stream list are shown in this time zone.\n"
                         "Type part of a city name to search.")
        completer = combo.completer()
        completer.setFilterMode(Qt.MatchContains)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        combo.activated.connect(lambda _: self.change_zone(combo.currentText()))
        combo.lineEdit().editingFinished.connect(lambda: self.change_zone(combo.currentText()))
        return combo

    def change_zone(self, text: str) -> None:
        """Show times in another time zone, and remember it."""
        name = find_zone(text)
        if name is None:  # not a zone (e.g. half typed): back to the current one
            self.zone_combo.setCurrentText(zones.display_zone.key)
            if text.strip():
                self.log_line(f"Unknown time zone: {text.strip()}")
            return
        self.zone_combo.setCurrentText(name)
        if name == zones.display_zone.key:
            return
        set_display_zone(name)
        self.settings.setValue("time_zone", name)
        self.timeline.update()
        self.overview.update()
        self.picker.refresh_uptimes()
        self.update_selection()
        if self.timeline.available:
            target = self.stills.target
            self.request_preview(target if target is not None else self.timeline.focus())  # its time, in this zone
        self.log_line(f"Times are now shown in {name}.")

    def time_edit(self, which: str) -> QLineEdit:
        edit = QLineEdit(placeholderText="e.g. 14:30:05", enabled=False)
        edit.setMinimumWidth(170)
        edit.setToolTip(f"The clip's {which}. Type a time and press Enter, as in Go to: {GOTO_EXAMPLES}.")
        edit.returnPressed.connect(lambda: self.typed_time(which, back_to_timeline=True))
        # Leaving the field also takes what was typed (but not the text as shown, untouched).
        edit.editingFinished.connect(lambda: edit.isModified() and self.typed_time(which))
        return edit

    def log_line(self, text: str) -> None:
        self.log.appendPlainText(text)

    # Loading a stream

    def on_chosen(self, video_id: str, automatic: bool) -> None:
        """A stream picked in the sidebar (or re-picked there after a search)."""
        if video_id == self.video_id or (automatic and self.video_id):
            return  # already open, or the user has opened another meanwhile
        self.url_edit.setText(f"https://www.youtube.com/watch?v={video_id}")
        self.load()

    def load(self) -> None:
        video_id = parse_video_id(self.url_edit.text())
        if not video_id:
            self.show_url_error("That isn't a YouTube URL or an 11-character video ID.")
            self.url_edit.setFocus()
            return
        self.show_url_error("")
        if missing := missing_tools():
            QMessageBox.warning(self, "Missing programs", f"Not found: {', '.join(missing)}. "
                                "Run `uv run python tools/pinned.py --install`.")
            return
        self.close_stream()
        self.video_id = video_id
        self.picker.show_current(video_id)
        self.load_token += 1
        token = self.load_token
        self.preview.show_message("Opening the stream… (this takes a few seconds)")
        self.log_line(f"Loading {video_id}")

        try:  # here, not in the thread below: see Server
            server = Server(video_id, self.env, lambda line: self.bridge.log.emit(f"ypb: {shorten(line)}"))
        except OSError as error:  # ypb couldn't be started
            self.on_open_failed(token, str(error))
            return
        self.opening.add(server)

        def work() -> None:
            try:
                stream = Stream(server, self.env)
                stream.open()
                self.bridge.opened.emit(token, stream)
            except Exception as error:  # anything here is reported to the user
                server.stop()
                self.opening.discard(server)
                self.bridge.open_failed.emit(token, str(error))

        threading.Thread(target=work, daemon=True).start()

    def on_opened(self, token: int, stream: Stream) -> None:
        self.opening.discard(stream.server)  # from here, it's self.stream or stopped below
        if token != self.load_token:  # another stream was loaded meanwhile
            threading.Thread(target=stream.server.stop, daemon=True).start()
            return
        self.stream = stream
        self.stills.set_stream(stream)  # before the first still is asked for (request_preview below)
        info = stream.info
        started = parse_time(info["actualStartTime"]) if info.get("actualStartTime") else 0.0
        self.title.setText(f"{info.get('title', '')} — {info.get('channelTitle', '')}")
        self.title.setToolTip(plain_tooltip(self.title.text()))
        name = info.get("channelTitle") or info.get("title")
        self.setWindowTitle(f"{name} — ClipSake" if name else "ClipSake")
        for widget in (self.moment_bar, self.overview, self.timeline):
            widget.setVisible(True)
        edge = stream.head.end
        self.timeline.set_available(max(started, edge - KEPT_DAYS * 86400), edge, oldest_known=False)
        self.start_edit.setEnabled(True)
        self.goto_edit.setEnabled(True)
        self.end_edit.setEnabled(True)
        self.timeline.setFocus()
        self.request_preview(edge - 1)  # the still at the live edge; no playhead until you put one
        self.update_selection()
        self.head_timer.start()
        self.log_line(f"Ready. Live edge {format_moment(edge)}; rough stills {stream.fast.label}, "
                      f"sharp {stream.sharp.label}. Finding the oldest available moment…")

        def find_oldest() -> None:
            try:
                self.bridge.oldest.emit(stream, stream.find_earliest())
            except Exception as error:
                self.bridge.log.emit(f"Couldn't find the oldest moment: {error}")

        threading.Thread(target=find_oldest, daemon=True).start()

    def on_open_failed(self, token: int, message: str) -> None:
        if token == self.load_token:
            self.preview.show_message(f"Couldn't load the stream: {message}")
            self.log_line(f"Error: {message}")

    def on_oldest(self, stream: Stream, segment: Segment) -> None:
        if stream is not self.stream:
            return
        edge = self.timeline.available[1]
        self.timeline.set_available(segment.start, edge, oldest_known=True)
        self.log_line(f"Oldest available: {format_moment(segment.start)} "
                      f"({(edge - segment.start) / 86400:.1f} days back, segment {segment.sq})")

    def refresh_head(self) -> None:
        stream = self.stream
        if stream is None:
            return

        def work() -> None:
            try:
                self.bridge.head.emit(stream, stream.refresh_head())
            except Exception as error:
                self.bridge.log.emit(f"Couldn't check the live edge: {error}")

        threading.Thread(target=work, daemon=True).start()

    def on_head(self, stream: Stream, head: Segment) -> None:
        if stream is self.stream and self.timeline.available:
            self.timeline.set_available(self.timeline.available[0], head.end, self.timeline.oldest_known)

    def close_stream(self) -> None:
        self.stop_preview()
        self.previewer.forget()
        self.head_timer.stop()
        self.stills.set_stream(None)
        if self.stream:
            threading.Thread(target=self.stream.server.stop, daemon=True).start()
        self.stream = None
        self.timeline.reset()
        self.title.clear()
        self.title.setToolTip("")
        self.setWindowTitle("ClipSake")
        for widget in (self.moment_bar, self.overview, self.timeline):
            widget.setVisible(False)
        self.start_edit.setEnabled(False)
        self.goto_edit.setEnabled(False)
        self.goto_error.clear()
        self.show_moment(None)
        self.end_edit.setEnabled(False)
        self.update_selection()

    # Stills

    def request_preview(self, moment: float) -> None:
        if self.stream is None:
            return
        self.stop_preview()  # moving around stops a preview
        if self.screen.currentWidget() is self.previewer.video:  # back to stills: let go of the clip's file
            self.previewer.release()
            self.screen.setCurrentWidget(self.preview)
        self.show_moment(moment)
        self.stills.show(moment)

    def show_moment(self, moment: float | None) -> None:
        """Show the time being previewed under the preview (unless a time is being typed there)."""
        self.shown_moment = self.timeline.shown = moment
        if not self.goto_edit.hasFocus():
            self.goto_edit.setText(format_moment(moment) if moment is not None else "")
            self.goto_error.clear()

    def eventFilter(self, watched, event) -> bool:
        if watched is self.goto_edit and event.type() == QEvent.FocusOut:
            QTimer.singleShot(0, lambda: self.show_moment(self.shown_moment))  # once the focus has gone
        return super().eventFilter(watched, event)

    # Thumbnails

    def load_thumbs(self) -> None:
        if self.stream is None:
            return
        # Nearest the middle of the view first, so the area you're looking at fills in first.
        middle = sum(self.timeline.view) / 2
        self.stills.load_thumbnails(sorted(self.timeline.missing_thumbs(), key=lambda t: abs(t - middle)))

    # Selection

    def update_selection(self) -> None:
        selection = self.timeline.selection
        if self.previewer.clip and selection != self.previewer.clip:  # the clip changed: its preview is out of date
            self.stop_preview()
        self.play_button.setEnabled(selection is not None and self.stream is not None)
        self.reset_button.setVisible(selection is not None)
        self.play_button.setVisible(selection is not None)
        # While clipping it's the Cancel button, so it stays usable whatever else changes.
        self.clip_button.setEnabled(self.clipper.running or (selection is not None and self.stream is not None))
        self.clip_error.clear()
        for edit, moment in zip((self.start_edit, self.end_edit), selection or (None, None)):
            edit.setText(format_moment(moment) if moment is not None else "")  # also clears isModified
        if selection:
            self.length_label.setText(f"<b>Length</b> {format_length(selection[1] - selection[0])}")
        else:
            self.length_label.setText("Click the timeline to mark where clipping should start.")

    def read_time(self, text: str, error: QLabel, after: float | None = None) -> float | None:
        """A typed moment within what's available, or None (with the reason shown in `error`).

        A time without a day is its most recent occurrence; with `after`, its first one
        after that instead (for a clip's end: the first such time after its start).
        """
        if not self.timeline.available:
            return None
        oldest, edge = self.timeline.available
        # The most recent occurrence by a day after `after` is the first one after it.
        moment = parse_goto(text, edge if after is None else after + 86400)
        if moment is None:
            error.setText(f"Try e.g. {GOTO_EXAMPLES}")
        elif not oldest <= moment <= edge:
            error.setText(f"Only {format_moment(oldest, False)} to now is available")
        else:
            error.clear()
            return moment
        return None

    def typed_time(self, which: str, back_to_timeline: bool = False) -> None:
        edit = getattr(self, f"{which}_edit")
        selection = self.timeline.selection
        after = selection[0] if which == "end" and selection else None
        moment = self.read_time(edit.text(), self.clip_error, after)
        if moment is None:
            return  # the text stays, to be corrected
        if which == "start":
            end = selection[1] if selection and selection[1] > moment else moment + DEFAULT_LENGTH
            self.timeline.set_selection(moment, end, "start")
        else:
            start = selection[0] if selection and selection[0] < moment else moment - DEFAULT_LENGTH
            self.timeline.set_selection(start, moment, "end")
        self.zoom_to_selection()  # a typed time may be anywhere: show the whole clip
        if back_to_timeline:
            self.timeline.setFocus()

    def go_to(self) -> None:
        moment = self.read_time(self.goto_edit.text(), self.goto_error)
        if moment is None:
            return  # the text stays, to be corrected
        self.timeline.setFocus()  # first, so the field shows the new time
        self.timeline.go_to(moment, span=min(self.timeline.span, 600))

    def go_live(self) -> None:
        if self.timeline.available:
            edge = self.timeline.available[1]
            self.timeline.set_view(edge - DEFAULT_SPAN, edge)
            self.timeline.set_playhead(edge)

    def set_start(self) -> None:
        playhead = self.timeline.playhead
        if playhead is None:
            return
        selection = self.timeline.selection
        end = selection[1] if selection and selection[1] > playhead + 1 else playhead + DEFAULT_LENGTH
        self.timeline.set_selection(playhead, end)
        self.zoom_to_selection()

    def set_end(self) -> None:
        playhead = self.timeline.playhead
        if playhead is None:
            return
        selection = self.timeline.selection
        start = selection[0] if selection and selection[0] < playhead - 1 else playhead - DEFAULT_LENGTH
        self.timeline.set_selection(start, playhead)
        self.zoom_to_selection()

    def zoom_to_selection(self) -> None:
        if self.timeline.selection:
            self.timeline.show_range(*self.timeline.selection)

    def reset_zoom(self) -> None:
        """The whole clip; with no clip, the starting zoom (DEFAULT_SPAN) around the focus
        (the playhead, or the moment shown)."""
        if self.timeline.selection:
            self.zoom_to_selection()
        elif self.timeline.available:
            self.timeline.zoom_to_span(DEFAULT_SPAN)

    # Previewing the clip

    def toggle_preview(self) -> None:
        """Play the clip preview; then pause / resume it. Before it's ready, cancel it."""
        if self.previewer.state != IDLE:
            self.previewer.toggle()
            return
        selection, stream = self.timeline.selection, self.stream
        if not selection or stream is None:
            return
        self.zoom_to_selection()  # so the playhead can be followed across the whole clip
        self.screen.setCurrentWidget(self.preview)
        self.previewer.play(stream, selection, stream.sharp)  # the sharp stills' format: 720p H.264 or below

    def stop_preview(self, message: str = "") -> None:
        """Stop previewing. The frame playing stays on screen (until you move around), unless
        there's a message to show instead or the clip itself changed."""
        self.previewer.stop(message, keep_frame=self.timeline.selection == self.previewer.clip)

    def seek_preview(self, moment: float) -> None:
        self.previewer.seek(moment)

    def on_preview_started(self) -> None:
        self.screen.setCurrentWidget(self.previewer.video)
        self.timeline.start_playback()  # clicks inside the clip now jump the preview there

    def on_preview_state(self, state: str) -> None:
        text, icon = {IDLE: ("Play preview", "play"), PREPARING: ("Cancel preview", "stop"),
                      PLAYING: ("Pause", "pause"), PAUSED: ("Resume", "play")}[state]
        self.set_play_button(text, icon)

    def on_preview_position(self, moment: float) -> None:
        self.timeline.follow_playback(moment)
        self.show_moment(self.timeline.playhead)

    def on_preview_finished(self, message: str, frame_kept: bool) -> None:
        self.timeline.end_playback()  # the playhead only shows during a preview, or where you put it
        if not frame_kept:
            self.screen.setCurrentWidget(self.preview)
            self.preview.show_message(message)  # or none: clears "Preparing the preview…"

    # Clipping

    def toggle_clip(self) -> None:
        if self.clipper.running:
            self.clipper.cancel()
        else:
            self.start_clip()

    def start_clip(self) -> None:
        if not self.timeline.selection or self.stream is None:
            return
        start, end = self.timeline.selection
        name = "".join(c if c.isalnum() or c in " -_" else "_" for c in self.stream.info.get("title", "clip"))[:60]
        stamp = local(start).strftime("%Y-%m-%d %H.%M.%S")
        suggested = self.save_folder() / f"{name.strip()} {stamp}.mp4"
        path, _ = QFileDialog.getSaveFileName(self, "Save clip", str(suggested), "MP4 video (*.mp4)")
        if not path:
            return
        # The save dialog has already asked about replacing the file chosen; ask too if adding
        # ".mp4" made it a different one that exists. The old file is replaced only once the
        # new clip is finished, so it's kept if clipping fails or is cancelled.
        output = Path(path).with_suffix(".mp4")
        if output != Path(path) and output.exists():
            answer = QMessageBox.question(self, "Replace file?", f"{output.name} already exists. Replace it?")
            if answer != QMessageBox.Yes:
                return
        self.log_line(f"Clipping {format_moment(start)} – {format_moment(end)} to {output}")
        if not PORTAL_FOLDER.match(str(output.parent)):  # a folder made for that one file
            self.settings.setValue("save_folder", str(output.parent))
        self.set_clip_status("")
        self.progress.setRange(0, 0)
        self.progress.setVisible(True)
        self.clipper.start(
            self.stream.server.video_id,  # not info["id"]: ypb gives the watch URL it was given
            datetime.fromtimestamp(start, UTC),
            datetime.fromtimestamp(end, UTC),
            output,
            self.precise.isChecked(),
            overwrite=output.exists(),
        )
        self.clip_button.setText("Cancel clipping")
        self.update_selection()

    def on_clip_progress(self, done: int, total: int, _line: str) -> None:
        self.progress.setRange(0, total)
        self.progress.setValue(done)

    def on_clip_done(self, cancelled: bool, saved, error: str) -> None:
        self.progress.setVisible(False)
        self.clip_button.setText("Clip…")
        self.update_selection()
        if cancelled:
            self.log_line("Clip cancelled.")
            self.set_clip_status("Clip cancelled")
        elif error:
            self.log_line(error)
            self.set_clip_status("Clip failed")
            box = QMessageBox(QMessageBox.Warning, "Clip failed", error, parent=self)
            box.setTextFormat(Qt.PlainText)  # may quote ypb or YouTube: never as HTML
            box.exec()
        else:
            self.log_line(f"Saved {saved}")
            self.set_clip_status(f"Saved {Path(saved).name}", Path(saved))

    def set_clip_status(self, text: str, saved: Path | None = None) -> None:
        """What happened to the last clip, beside Clip…; with Show in folder once one is saved."""
        self.clip_status.setText(text)
        self.clip_status.setToolTip(str(saved) if saved else "")
        self.saved_path = saved
        self.show_folder_button.setVisible(saved is not None)

    def show_saved_folder(self) -> None:
        if self.saved_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.saved_path.parent)))

    # Updates

    def check_for_updates(self) -> None:
        """Ask GitHub about a newer release in the background, unless that's turned off."""
        if self.update_check.isChecked():
            self.update_checker.start()

    def on_newer_release(self, release: Release) -> None:
        if self.settings.value("updates/skipped_version", "") == release.version:
            return
        self.pending_release = release
        text = f"ClipSake {release.version} is available (you have {current_version()})."
        if release.download_url:
            self.download_button.setText(f"Download {release.version}")
        else:  # from source: there's no file to download
            text += " To update, pull and run uv sync --locked."
        self.download_button.setVisible(release.download_url is not None)
        self.update_label.setText(text)
        self.update_banner.show()

    def download_release(self) -> None:
        """Open this build's file in the release (the browser downloads it); nothing is installed."""
        if self.pending_release and self.pending_release.download_url:
            QDesktopServices.openUrl(QUrl(self.pending_release.download_url))

    def open_release_notes(self) -> None:
        if self.pending_release:
            QDesktopServices.openUrl(QUrl(self.pending_release.url))

    def skip_release(self) -> None:
        if self.pending_release:
            self.settings.setValue("updates/skipped_version", self.pending_release.version)
        self.update_banner.hide()

    def save_folder(self) -> Path:
        """Where to suggest saving: the folder used last, else the Videos (Movies) folder, else home."""
        for folder in (self.settings.value("save_folder", ""),
                       QStandardPaths.writableLocation(QStandardPaths.MoviesLocation)):
            if folder and Path(folder).is_dir():
                return Path(folder)
        return Path.home()

    def closeEvent(self, event) -> None:
        self.previewer.close()
        self.picker.stop()
        if self.clipper.running:
            self.clipper.cancel()
        self.stills.close()
        if self.stream:
            self.stream.server.stop()
        for server in list(self.opening):  # a copy: load's thread may remove one meanwhile
            server.stop()
        super().closeEvent(event)


def close_when_asked_to_stop(window: Window) -> None:
    """Close the window, as its close button does (stopping ypb and any clip, and deleting the
    previews), when the app is asked to stop: SIGTERM (kill, logging out), SIGHUP (its
    terminal closed) or SIGINT (Ctrl+C). Linux and macOS.

    Python only runs signal handlers between its own instructions, which it doesn't get to
    while Qt waits for events; a byte on this socket pair wakes Qt, which runs Python.
    """
    if os.name != "posix":
        return
    wake, woken = socket.socketpair()
    for end in (wake, woken):
        end.setblocking(False)
    signal.set_wakeup_fd(wake.fileno())
    notifier = QSocketNotifier(woken.fileno(), QSocketNotifier.Read, window)

    def drain() -> None:
        with contextlib.suppress(OSError):
            woken.recv(64)

    notifier.activated.connect(drain)
    window._stop_signals = (wake, woken, notifier)  # kept for as long as the window
    for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(number, lambda _number, _frame: QTimer.singleShot(0, window.close))


def main() -> None:
    env = prepare_environment()
    app = QApplication(sys.argv)
    app.setApplicationName("ClipSake")
    # The Flatpak's ID and desktop entry name, so Linux desktops match the window to its icon.
    app.setDesktopFileName(APP_ID)
    app.setWindowIcon(QIcon(str(APP_ICON)))
    apply_dark_theme(app)
    window = Window(env)
    close_when_asked_to_stop(window)
    if len(sys.argv) > 1:
        window.url_edit.setText(sys.argv[1])
        QTimer.singleShot(0, window.load)
    if window.picker.channel_edit.text():
        QTimer.singleShot(0, window.picker.search)  # reopens the last stream, if still live
    # Here rather than in Window, so building a window (as the tests do) never contacts GitHub.
    QTimer.singleShot(1500, window.check_for_updates)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
