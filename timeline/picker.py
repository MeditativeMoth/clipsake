"""Sidebar for choosing a live stream: a channel's streams that are live right now.

The lookup is StreamLoader's (which runs `timeline.streams` in a
child process so it can be cancelled). The channel and the last stream chosen
are remembered, so the next start finds and reopens the same stream.
"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC

from PySide6.QtCore import QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)
from timeline.processes import StreamLoader
from timeline.streams import LiveStream
from timeline.widgets import plain_tooltip
from timeline.zones import local

UPTIME_REFRESH = 60_000  # ms
TITLE_LENGTH = 44  # characters shown in the list, so the LIVE line stays visible


def format_duration(delta: timedelta) -> str:
    """How long a stream has been live, e.g. "3h 05m 00s"."""
    seconds = int(delta.total_seconds())
    hours, rest = divmod(abs(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours >= 48:
        return f"{hours // 24}d {hours % 24}h"
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


def short_title(title: str) -> str:
    return title if len(title) <= TITLE_LENGTH else title[: TITLE_LENGTH - 1].rstrip() + "…"


def item_text(stream: LiveStream, now: datetime | None = None) -> str:
    title = short_title(stream.title)
    if not stream.started_at:
        return f"{title}\n● LIVE"
    uptime = format_duration((now or datetime.now(UTC)) - stream.started_at)
    return f"{title}\n● LIVE  since {local(stream.started_at.timestamp()):%a %H:%M} · {uptime}"


class StreamPicker(QWidget):
    """Channel field, search button and the list of that channel's live streams.

    `chosen` gives a stream's video ID and whether the picker chose it by
    itself (re-selecting the remembered stream after a search) rather than
    the user clicking it.
    """

    chosen = Signal(str, bool)  # video ID, automatic

    def __init__(self, env: dict[str, str], settings: QSettings) -> None:
        super().__init__()
        self.settings = settings
        self.expected = 0
        self._automatic = False
        self.loader = StreamLoader(env)
        self.loader.found.connect(self.on_found)
        self.loader.stream_loaded.connect(self.on_stream_loaded)
        self.loader.done.connect(self.on_done)

        self.channel_edit = QLineEdit(settings.value("channel", ""), placeholderText="Channel: @handle, ID or URL")
        self.channel_edit.returnPressed.connect(lambda: self.loader.running or self.search())
        # A plain button: it's rarely needed after the first search, so the accent is kept for Clip….
        self.search_button = QPushButton("Find live streams", clicked=self.search)

        self.list = QListWidget(iconSize=QSize(128, 72), wordWrap=True, spacing=2)
        self.list.currentItemChanged.connect(self.on_current_changed)
        self.placeholder = QLabel("Enter a channel and press\n“Find live streams”.", objectName="muted",
                                  textFormat=Qt.PlainText,  # may show the channel typed or an error from yt-dlp
                                  alignment=Qt.AlignCenter, wordWrap=True)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.placeholder)
        self.stack.addWidget(self.list)
        self.status = QLabel(objectName="muted", wordWrap=True, textFormat=Qt.PlainText)

        title = QLabel("Live streams", objectName="sectionTitle")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(title)
        layout.addWidget(self.channel_edit)
        row = QHBoxLayout()
        row.addWidget(self.search_button, 1)
        layout.addLayout(row)
        layout.addWidget(self.stack, 1)
        layout.addWidget(self.status)
        self.footer = layout  # the window adds opening a stream by URL here (add_footer)

        self.uptime_timer = QTimer(self, interval=UPTIME_REFRESH, timeout=self.refresh_uptimes)
        self.uptime_timer.start()

    def add_footer(self, item) -> None:
        """Add a widget or layout at the bottom of the sidebar."""
        (self.footer.addLayout if hasattr(item, "addWidget") else self.footer.addWidget)(item)

    @property
    def remembered(self) -> str:
        return self.settings.value("last_stream", "")

    def search(self) -> None:
        """Start a search, or cancel the one running (the button toggles)."""
        if self.loader.running:
            self.loader.cancel()
            return
        channel = self.channel_edit.text().strip()
        if not channel:
            self.channel_edit.setFocus()
            return
        self.settings.setValue("channel", channel)
        self.expected = 0
        self.list.clear()
        self.placeholder.setText(f"Searching {channel} for live streams…")
        self.stack.setCurrentWidget(self.placeholder)
        self.status.clear()
        self._set_searching(True)
        self.loader.start(channel)

    def _set_searching(self, searching: bool) -> None:
        self.search_button.setText("Cancel search" if searching else "Find live streams")

    def on_found(self, count: int) -> None:
        self.expected = count
        if count:
            self.placeholder.setText(f"Found {count} live stream(s).\nLoading details…")

    def on_stream_loaded(self, stream: LiveStream) -> None:
        item = QListWidgetItem(item_text(stream))
        item.setData(Qt.UserRole, stream)
        item.setToolTip(plain_tooltip(f"{stream.title}\n{stream.url}"))  # a title from YouTube: never as HTML
        item.setSizeHint(QSize(0, 84))
        if stream.thumbnail:
            pixmap = QPixmap()
            pixmap.loadFromData(stream.thumbnail)
            item.setIcon(QIcon(pixmap.scaled(128, 72, Qt.KeepAspectRatio, Qt.SmoothTransformation)))
        self.list.addItem(item)
        self.stack.setCurrentWidget(self.list)
        self.status.setText(f"Loading details: {self.list.count()} of {self.expected}…")
        if stream.video_id == self.remembered and self.list.currentItem() is None:
            self._automatic = True
            try:
                self.list.setCurrentItem(item)
            finally:
                self._automatic = False

    def on_done(self, cancelled: bool, error: str) -> None:
        self._set_searching(False)
        loaded = self.list.count()
        if error:
            message = f"Couldn't load streams: {error}"
        elif cancelled:
            message = f"Search cancelled; showing {loaded} of {self.expected}." if loaded else "Search cancelled."
        elif loaded:
            message = f"{loaded} live stream(s) · checked {datetime.now():%H:%M}. Click one to open it."
        else:
            message = "No live streams on this channel right now."
        if loaded:
            self.status.setText(message)
        else:
            self.placeholder.setText(message)
            self.stack.setCurrentWidget(self.placeholder)
            self.status.clear()

    def on_current_changed(self, item: QListWidgetItem | None, _previous) -> None:
        if item is None:
            return
        stream: LiveStream = item.data(Qt.UserRole)
        self.settings.setValue("last_stream", stream.video_id)
        self.chosen.emit(stream.video_id, self._automatic)

    def show_current(self, video_id: str | None) -> None:
        """Highlight the open stream if it's listed (without opening it again)."""
        self.list.blockSignals(True)
        try:
            self.list.setCurrentItem(None)
            for row in range(self.list.count()):
                if self.list.item(row).data(Qt.UserRole).video_id == video_id:
                    self.list.setCurrentRow(row)
        finally:
            self.list.blockSignals(False)
        if video_id:
            self.settings.setValue("last_stream", video_id)

    def refresh_uptimes(self) -> None:
        for row in range(self.list.count()):
            item = self.list.item(row)
            item.setText(item_text(item.data(Qt.UserRole)))

    def stop(self) -> None:
        if self.loader.running:
            self.loader.cancel()
