"""The timeline (thumbnails, ruler, start/end handles, playhead) and the still preview.

Times are seconds since the Unix epoch, shown in the time zone the user
picks (see timeline.zones).
"""

from __future__ import annotations

import bisect
import html
import math
from datetime import timedelta

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QSizePolicy, QWidget

from timeline.times import from_wall_clock, wall_clock
from timeline.zones import local

ACCENT = QColor("#ff5a5f")
SELECTION = QColor(255, 90, 95, 55)
BACKGROUND = QColor("#16161a")
EMPTY_SLOT = QColor("#24242a")
UNAVAILABLE = QColor("#0c0c0e")
TEXT = QColor("#c9c9d1")
MUTED = QColor("#7d7d88")

# Ruler steps, in seconds; the smallest that keeps labels this far apart is used.
STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400]
MIN_LABEL_GAP = 90  # pixels
MIN_SPAN = 60.0  # seconds: the most the timeline zooms in (handles snap to whole seconds)
GRAB = 8  # pixels either side of a handle that pick it up
DEFAULT_LENGTH = 30  # seconds: a new clip's length when only one end is chosen


def plain_tooltip(text: str) -> str:
    """`text` as a tooltip shown as written: Qt would show text that looks like HTML as HTML."""
    return "<qt>" + html.escape(text).replace("\n", "<br>") + "</qt>"


def ruler_step(seconds_per_pixel: float) -> int:
    return next((s for s in STEPS if s / seconds_per_pixel >= MIN_LABEL_GAP), STEPS[-1])


def tick_label(moment: float, step: int) -> str:
    when = local(moment)
    if step >= 86400 or (when.hour == 0 and when.minute == 0 and when.second == 0):
        return when.strftime("%a %d %b")
    return when.strftime("%H:%M" if step >= 60 else "%H:%M:%S")


class PreviewWidget(QWidget):
    """Shows the latest still, scaled to fit (its time is shown below it, outside this widget)."""

    def __init__(self) -> None:
        super().__init__()
        self.pixmap: QPixmap | None = None
        self.message = "Choose a live stream in the sidebar to start."
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def show_still(self, pixmap: QPixmap) -> None:
        self.pixmap, self.message = pixmap, ""
        self.update()

    def show_message(self, message: str) -> None:
        self.message = message
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.black)
        area = self.rect()
        if self.pixmap and not self.pixmap.isNull():
            scaled = self.pixmap.size().scaled(area.size(), Qt.KeepAspectRatio)
            target = QRectF(
                area.x() + (area.width() - scaled.width()) / 2,
                area.y() + (area.height() - scaled.height()) / 2,
                scaled.width(), scaled.height(),
            )
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            painter.drawPixmap(target, self.pixmap, QRectF(self.pixmap.rect()))
        if self.message:
            painter.setPen(QColor("#ffffff"))
            painter.fillRect(QRectF(area.center().x() - 220, area.center().y() - 18, 440, 36), QColor(0, 0, 0, 170))
            painter.drawText(area, Qt.AlignCenter, self.message)


class TimelineWidget(QWidget):
    """A zoomable timeline of the stream's available past.

    - Drag (left or middle button): move along the timeline.
    - Shift+drag: select from where you pressed to where you let go.
    - Drag a handle: move the start or end, then (once let go) show the whole
      clip. Handles snap to whole seconds, the precision the clipper takes.
    - Click while the clip's preview is playing or paused (`previewing`), inside
      the clip: seekRequested, to jump there; the clip stays as it is.
    - Click: with no clip yet, start a DEFAULT_LENGTH one there; once there
      is one, move its selected end (the handle touched last, drawn
      brighter) there (past the other end, that moves along, keeping the
      length), then show the whole clip. Clicking a handle selects it. Ctrl+click moves the
      playhead (the white line), which a click then moves until a handle is
      selected again.
    - Wheel: zoom around the mouse; Shift+wheel or a sideways scroll: pan.
    - Double-click: zoom in 4x there. +/-: zoom in/out around the focus
      (the handle you touched last, or the playhead).
    - Left/Right: move whatever you touched last by 1 s (Shift: 10 s,
      Ctrl: 0.2 s; the playhead only).

    Whenever the playhead or a handle moves, previewRequested gives the
    moment to show.
    """

    previewRequested = Signal(float)
    seekRequested = Signal(float)  # a click inside the clip while its preview plays (see `previewing`)
    selectionChanged = Signal()
    thumbnailsWanted = Signal()
    changed = Signal()  # anything shown changed (the overview repaints)

    STRIP = 64  # thumbnail strip height
    RULER = 28

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(self.STRIP + self.RULER + 26)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.available: tuple[float, float] | None = None  # (oldest, live edge)
        self.oldest_known = False  # whether available[0] is the real oldest moment, or a guess
        self.view = (0.0, 1.0)
        self.selection: tuple[float, float] | None = None
        self.playhead: float | None = None
        self.shown: float | None = None  # the moment in the preview, set by the window: zooming's last resort
        self.previewing = False  # a clip preview is playing or paused: clicks inside the clip seek
        self._following = False  # whether the playhead is where a clip preview left it (follow_playback)
        self.active = "playhead"  # "playhead", "start" or "end": what the arrow keys move
        self.follow_live = True
        self._thumbs: dict[float, QPixmap] = {}  # segment start -> its first frame
        self._thumb_starts: list[float] = []  # sorted
        self._drag: str | None = None  # "start", "end", "new", "pan"
        self._moved = False  # whether the mouse has moved far enough since the press to be a drag
        self._press_x = 0.0
        self._press_t = 0.0
        self._press_view = self.view

    def _changed(self) -> None:
        self.update()
        self.changed.emit()

    # Geometry

    @property
    def span(self) -> float:
        return self.view[1] - self.view[0]

    def x_to_t(self, x: float) -> float:
        return self.view[0] + x / max(self.width(), 1) * self.span

    def t_to_x(self, t: float) -> float:
        return (t - self.view[0]) / self.span * self.width()

    def clamp(self, t: float) -> float:
        if not self.available:
            return t
        return min(max(t, self.available[0]), self.available[1])

    # State changes

    def reset(self) -> None:
        self.available, self.selection, self.playhead = None, None, None
        self._following = False
        self._thumbs.clear()
        self._thumb_starts.clear()
        self._changed()

    def set_available(self, oldest: float, live_edge: float, oldest_known: bool) -> None:
        first = self.available is None
        self.available, self.oldest_known = (oldest, live_edge), oldest_known
        if first:
            self.set_view(live_edge - 1800, live_edge)
        elif self.follow_live:
            self.set_view(live_edge - self.span, live_edge)
        else:
            self.set_view(*self.view)
        self._changed()

    def set_view(self, start: float, end: float) -> None:
        if end - start < MIN_SPAN:  # zoomed in too far: widen around the middle
            middle = (start + end) / 2
            start, end = middle - MIN_SPAN / 2, middle + MIN_SPAN / 2
        if self.available:
            oldest, edge = self.available
            # A little room past each end, so the edges are easy to grab.
            margin = min(end - start, edge - oldest) * 0.02
            full = edge - oldest + 2 * margin
            span = min(max(end - start, MIN_SPAN), max(full, MIN_SPAN))
            start = min(max(start, oldest - margin), edge + margin - span)
            end = start + span
            self.follow_live = end >= edge
        self.view = (start, end)
        self._changed()
        self.thumbnailsWanted.emit()

    def show_range(self, start: float, end: float) -> None:
        pad = max((end - start) * 0.15, 5)
        self.set_view(start - pad, end + pad)

    def show_all(self) -> None:
        if self.available:
            self.set_view(*self.available)

    def focus(self) -> float:
        """Where zooming centres: the handle being worked on, else the playhead, else the moment
        in the preview, else the view's middle."""
        if self.selection and self.active in ("start", "end"):
            return self.selection[0] if self.active == "start" else self.selection[1]
        if self.playhead is not None:
            return self.playhead
        if self.shown is not None:
            return self.shown
        return sum(self.view) / 2

    def zoom_to_span(self, span: float) -> None:
        centre = self.focus()
        self.set_view(centre - span / 2, centre + span / 2)

    def zoom_by(self, factor: float, anchor: float | None = None) -> None:
        """Zoom in (factor < 1) or out, keeping `anchor` (default: the focus) where it is on screen."""
        anchor = self.focus() if anchor is None else anchor
        factor = max(factor, MIN_SPAN / self.span)  # at the limit, stay put rather than drift
        if not self.view[0] <= anchor <= self.view[1]:  # focus off-screen: bring it to the middle
            self.set_view(anchor - self.span * factor / 2, anchor + self.span * factor / 2)
            return
        self.set_view(anchor - (anchor - self.view[0]) * factor, anchor + (self.view[1] - anchor) * factor)

    def go_to(self, moment: float, span: float = 600) -> None:
        """Put the playhead at `moment` and show the `span` seconds around it."""
        self.set_view(moment - span / 2, moment + span / 2)
        self.set_playhead(moment)

    def set_playhead(self, t: float) -> None:
        """Move the playhead, which the arrow keys then move, and ask for the still there."""
        self.playhead = self.clamp(t)
        self.active = "playhead"
        self._following = False
        self._changed()
        self.previewRequested.emit(self.playhead)

    def start_playback(self) -> None:
        """A clip preview is playing: clicks inside the clip seek (seekRequested) until end_playback()."""
        self.previewing = True

    def follow_playback(self, t: float) -> None:
        """Move the playhead along with a clip preview playing. What a click or the arrow keys
        move stays as it was, so they do what they would have done without the preview."""
        self.playhead = self.clamp(t)
        self._following = True
        self._changed()

    def end_playback(self) -> None:
        """A clip preview stopped: take away the playhead, unless it has been put somewhere since."""
        self.previewing = False
        if not self._following:
            return
        self.playhead, self._following = None, False
        if self.active == "playhead" and self.selection:
            self.active = "end"  # not a line that's gone
        self._changed()

    def set_selection(self, start: float, end: float, moved: str | None = None) -> None:
        start, end = sorted((round(self.clamp(start)), round(self.clamp(end))))
        if end - start < 1:
            end = start + 1
        self.selection = (start, end)
        self._changed()
        self.selectionChanged.emit()
        if moved:
            self.active = moved
            self.previewRequested.emit(self.selection[0] if moved == "start" else self.selection[1])

    def start_clip_at(self, t: float) -> None:
        """A new DEFAULT_LENGTH clip starting at `t`."""
        t = round(self.clamp(t))
        self.set_selection(t, t + DEFAULT_LENGTH, "start")

    def move_handle(self, handle: str, t: float) -> None:
        """Move the clip's start or end to `t`. Past the other end, that end moves along,
        keeping the clip's length, so the one moved stays the start or end."""
        start, end = self.selection
        length, t = end - start, round(self.clamp(t))
        if handle == "start":
            start, end = t, (end if t < end else t + length)
        else:
            start, end = (start if t > start else t - length), t
        self.set_selection(start, end, handle)

    def add_thumb(self, start: float, pixmap: QPixmap) -> None:
        if start not in self._thumbs:
            bisect.insort(self._thumb_starts, start)
        self._thumbs[start] = pixmap
        self._changed()

    # Thumbnails

    def slot_width(self) -> float:
        return self.STRIP * 16 / 9

    def thumb_slots(self) -> list[tuple[float, float]]:
        """(left x, moment) for each thumbnail position in view, within the available range."""
        if not self.available:
            return []
        width = self.slot_width()
        slots = []
        # Slots are fixed in time (not in pixels), so panning reuses them.
        seconds = width * self.span / max(self.width(), 1)
        first = math.floor(self.view[0] / seconds)
        for index in range(first, first + math.ceil(self.width() / width) + 2):
            moment = (index + 0.5) * seconds
            if self.available[0] <= moment <= self.available[1]:
                slots.append((self.t_to_x(index * seconds), moment))
        return slots

    def thumb_near(self, moment: float, tolerance: float) -> QPixmap | None:
        index = bisect.bisect_left(self._thumb_starts, moment)
        best = None
        for start in self._thumb_starts[max(index - 1, 0): index + 1]:
            if abs(start - moment) <= tolerance and (best is None or abs(start - moment) < abs(best - moment)):
                best = start
        return None if best is None else self._thumbs[best]

    def missing_thumbs(self) -> list[float]:
        tolerance = self.slot_tolerance()
        return [moment for _, moment in self.thumb_slots() if self.thumb_near(moment, tolerance) is None]

    def slot_tolerance(self) -> float:
        seconds = self.slot_width() * self.span / max(self.width(), 1)
        return max(seconds / 2, 5.0)  # a segment is ~5 s, so closer than that can't be had

    # Painting

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), BACKGROUND)
        if not self.available:
            painter.setPen(MUTED)
            painter.drawText(self.rect(), Qt.AlignCenter, "No stream loaded")
            return
        strip = QRectF(0, 4, self.width(), self.STRIP)
        self._paint_strip(painter, strip)
        self._paint_ruler(painter, QRectF(0, strip.bottom() + 2, self.width(), self.RULER))
        self._paint_unavailable(painter)
        self._paint_selection(painter)
        self._paint_playhead(painter)

    def _paint_strip(self, painter: QPainter, strip: QRectF) -> None:
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        width, tolerance = self.slot_width(), self.slot_tolerance()
        for x, moment in self.thumb_slots():
            slot = QRectF(x + 1, strip.top(), width - 2, strip.height())
            pixmap = self.thumb_near(moment, tolerance)
            if pixmap:
                painter.drawPixmap(slot, pixmap, QRectF(pixmap.rect()))
            else:
                painter.fillRect(slot, EMPTY_SLOT)

    def _paint_ruler(self, painter: QPainter, ruler: QRectF) -> None:
        seconds_per_pixel = self.span / max(self.width(), 1)
        step = ruler_step(seconds_per_pixel)
        small = step / 5 if step % 5 == 0 else step / 2
        painter.setFont(QFont(self.font().family(), 8))
        metrics = painter.fontMetrics()
        handles = [self.t_to_x(t) for t in self.selection] if self.selection else []
        # Ticks fall on round local (wall-clock) times, which stay round
        # across a daylight saving change.
        wall = math.floor(wall_clock(self.view[0]) / small) * small
        while (tick := from_wall_clock(wall)) <= self.view[1]:
            x = self.t_to_x(tick)
            major = round(wall) % step == 0
            painter.setPen(QPen(TEXT if major else MUTED, 1))
            painter.drawLine(QPointF(x, ruler.top()), QPointF(x, ruler.top() + (8 if major else 4)))
            label = tick_label(tick, step)
            left, right = x + 3, x + 3 + metrics.horizontalAdvance(label)
            # Not for a tick off the left edge (it would be cut off), nor where a handle's line would cross it.
            if major and x >= 0 and not any(left - 3 <= h <= right + 3 for h in handles):
                painter.drawText(QRectF(left, ruler.top() + 6, 120, 16), Qt.AlignLeft, label)
            wall += small

    def _paint_unavailable(self, painter: QPainter) -> None:
        oldest, edge = self.available
        height = self.STRIP + self.RULER + 6
        if self.t_to_x(oldest) > 0:
            painter.fillRect(QRectF(0, 0, self.t_to_x(oldest), height), UNAVAILABLE)
            if not self.oldest_known:
                painter.setPen(MUTED)
                painter.drawText(QRectF(4, 4, max(self.t_to_x(oldest) - 8, 0), 40), Qt.AlignRight, "finding\noldest…")
        x = self.t_to_x(edge)
        if x < self.width():
            painter.fillRect(QRectF(x, 0, self.width() - x, height), UNAVAILABLE)
            painter.setPen(QPen(ACCENT, 2))
            painter.drawLine(QPointF(x, 0), QPointF(x, height))
            painter.drawText(QRectF(x + 4, 4, 60, 16), Qt.AlignLeft, "LIVE")

    def _paint_selection(self, painter: QPainter) -> None:
        if not self.selection:
            return
        bottom = self.STRIP + self.RULER + 6
        left, right = (self.t_to_x(t) for t in self.selection)
        painter.fillRect(QRectF(left, 0, right - left, bottom), SELECTION)
        for x, name in ((left, "start"), (right, "end")):
            painter.setPen(QPen(ACCENT, 3 if self.active == name else 2))
            painter.drawLine(QPointF(x, 0), QPointF(x, bottom))
            tab = QRectF(x - 6, bottom, 12, 18)
            painter.setRenderHint(QPainter.Antialiasing)
            path = QPainterPath()
            path.addRoundedRect(tab, 3, 3)
            painter.fillPath(path, ACCENT.lighter(120) if self.active == name else ACCENT)
            painter.setRenderHint(QPainter.Antialiasing, False)

    def _paint_playhead(self, painter: QPainter) -> None:
        if self.playhead is None:
            return
        x = self.t_to_x(self.playhead)
        painter.setPen(QPen(QColor("#ffffff"), 3 if self.active == "playhead" else 1))
        painter.drawLine(QPointF(x, 0), QPointF(x, self.STRIP + self.RULER + 6))

    # Mouse and keyboard

    def handle_at(self, x: float) -> str | None:
        if not self.selection:
            return None
        distances = {name: abs(self.t_to_x(t) - x) for name, t in zip(("start", "end"), self.selection)}
        name = min(distances, key=distances.get)
        return name if distances[name] <= GRAB else None

    def mousePressEvent(self, event) -> None:
        if not self.available:
            return
        x = event.position().x()
        self._press_x, self._press_t, self._press_view = x, self.x_to_t(x), self.view
        self._moved = False
        if event.button() == Qt.MiddleButton:
            self._drag = "pan"
        elif event.button() == Qt.LeftButton:
            if event.modifiers() & Qt.ShiftModifier:
                self._drag = "new"
            elif handle := self.handle_at(x):
                self._drag = self.active = handle
                self._changed()
                self.previewRequested.emit(self.selection[0] if handle == "start" else self.selection[1])
            else:
                self._drag = "pan"
            if self._drag == "pan":
                self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event) -> None:
        x = event.position().x()
        if self._drag is None:
            self.setCursor(Qt.SizeHorCursor if self.handle_at(x) else Qt.OpenHandCursor)
            return
        t = self.x_to_t(x)
        self._moved = self._moved or abs(x - self._press_x) > 3
        if not self._moved:
            return  # a click so far, not a drag
        if self._drag == "pan":
            shift = (x - self._press_x) / max(self.width(), 1) * (self._press_view[1] - self._press_view[0])
            self.set_view(self._press_view[0] - shift, self._press_view[1] - shift)
        elif self._drag == "new":
            # Dragging left from the press point moves the start.
            moved = "end" if t >= self._press_t else "start"
            self.set_selection(self._press_t, t, moved)
        else:
            start, end = self.selection
            start, end = (t, end) if self._drag == "start" else (start, t)
            if start > end:  # dragged one handle past the other: swap roles
                self._drag = "end" if self._drag == "start" else "start"
            self.set_selection(start, end, self._drag)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._drag in ("pan", "new") and not self._moved:
            if (self.previewing and self.selection and not event.modifiers() & Qt.ControlModifier
                    and self.selection[0] <= self._press_t <= self.selection[1]):
                self.seekRequested.emit(self._press_t)
            elif event.modifiers() & Qt.ControlModifier:
                self.set_playhead(self._press_t)
            elif self.selection is None:
                self.start_clip_at(self._press_t)
                self.show_range(*self.selection)  # a new clip: show the whole of it
            elif self.active in ("start", "end"):
                self.move_handle(self.active, self._press_t)
                self.show_range(*self.selection)  # an end moved with the mouse: show the whole clip
            else:
                self.set_playhead(self._press_t)
        elif self._drag in ("start", "end", "new") and self._moved and self.selection:
            self.show_range(*self.selection)  # once let go, not while dragging
        self._drag = None
        self.setCursor(Qt.SizeHorCursor if self.handle_at(event.position().x()) else Qt.OpenHandCursor)

    def wheelEvent(self, event) -> None:
        if not self.available:
            return
        delta = event.angleDelta()
        pan = delta.x() or (delta.y() if event.modifiers() & Qt.ShiftModifier else 0)
        if pan:
            shift = -pan / 120 * self.span * 0.1
            self.set_view(self.view[0] + shift, self.view[1] + shift)
        elif delta.y():
            self.zoom_by(0.8 ** (delta.y() / 120), self.x_to_t(event.position().x()))
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        if self.available and event.button() == Qt.LeftButton and not self.handle_at(event.position().x()):
            self.zoom_by(0.25, self.x_to_t(event.position().x()))

    def keyPressEvent(self, event) -> None:
        # The window also makes +/- shortcuts; this catches them with the timeline focused if
        # the keyboard's Shift for "+" keeps the shortcut from matching.
        if event.key() in (Qt.Key_Plus, Qt.Key_Equal, Qt.Key_Minus) and self.available:
            self.zoom_by(0.5 if event.key() != Qt.Key_Minus else 2.0)
            return
        if event.key() not in (Qt.Key_Left, Qt.Key_Right) or not self.available:
            super().keyPressEvent(event)
            return
        sign = 1 if event.key() == Qt.Key_Right else -1
        modifiers = event.modifiers()
        step = 10 if modifiers & Qt.ShiftModifier else 1
        if self.active == "playhead" or not self.selection:
            if modifiers & Qt.ControlModifier:
                step = 0.2
            self.set_playhead((self.playhead or self.available[1]) + sign * step)
            self.keep_visible(self.playhead)
        else:
            start, end = self.selection
            if self.active == "start":
                start = min(start + sign * step, end - 1)
            else:
                end = max(end + sign * step, start + 1)
            self.set_selection(start, end, self.active)
            self.keep_visible(start if self.active == "start" else end)

    def keep_visible(self, t: float) -> None:
        if not self.view[0] <= t <= self.view[1]:
            self.set_view(t - self.span / 2, t + self.span / 2)


class OverviewWidget(QWidget):
    """A thin bar showing everything available, with a box for the part the timeline shows.

    Click anywhere to move the timeline there (keeping its zoom); drag to
    keep moving it. The clip shows in red and the playhead in white, so
    they're easy to find again after wandering off.
    """

    HEIGHT = 30

    def __init__(self, timeline: TimelineWidget) -> None:
        super().__init__()
        self.timeline = timeline
        timeline.changed.connect(self.update)
        self.setFixedHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self._grab_offset: float | None = None

    def x_to_t(self, x: float) -> float:
        oldest, edge = self.timeline.available
        return oldest + x / max(self.width(), 1) * (edge - oldest)

    def t_to_x(self, t: float) -> float:
        oldest, edge = self.timeline.available
        return (t - oldest) / max(edge - oldest, 1) * self.width()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), BACKGROUND)
        if not self.timeline.available:
            return
        oldest, edge = self.timeline.available
        bar = QRectF(0, 3, self.width(), self.height() - 6)
        painter.fillRect(bar, EMPTY_SLOT)
        # Midnights, labelled with the day that starts there.
        painter.setFont(QFont(self.font().family(), 8))
        day = local(oldest).replace(hour=0, minute=0, second=0, microsecond=0)
        pixels_per_day = 86400 / max(edge - oldest, 1) * self.width()
        while (moment := day.timestamp()) < edge:
            if moment > oldest:
                x = self.t_to_x(moment)
                painter.setPen(QPen(MUTED, 1))
                painter.drawLine(QPointF(x, bar.top()), QPointF(x, bar.bottom()))
            label_left = max(self.t_to_x(moment), 0)
            # The first, partial day's label sits at the left edge: only if there's room before the next one.
            room = self.t_to_x(moment) + pixels_per_day - label_left
            if pixels_per_day > 40 and room > (70 if pixels_per_day > 70 else 40):
                painter.setPen(MUTED)
                painter.drawText(QRectF(label_left + 4, bar.top(), 120, bar.height()), Qt.AlignVCenter,
                                 day.strftime("%a %d" if pixels_per_day > 70 else "%a"))
            day = (day + timedelta(days=1, hours=2)).replace(hour=0)  # +2 h: safe across DST
        if self.timeline.selection:
            left, right = (self.t_to_x(t) for t in self.timeline.selection)
            painter.fillRect(QRectF(left, bar.top(), max(right - left, 3), bar.height()), ACCENT)
        if self.timeline.playhead is not None:
            painter.setPen(QPen(QColor("#ffffff"), 2))
            x = self.t_to_x(self.timeline.playhead)
            painter.drawLine(QPointF(x, bar.top()), QPointF(x, bar.bottom()))
        left, right = (self.t_to_x(t) for t in self.timeline.view)
        box = QRectF(left, 1, max(right - left, 4), self.height() - 2)
        painter.fillRect(box, QColor(255, 255, 255, 40))
        painter.setPen(QPen(QColor("#ffffff"), 1.5))
        painter.drawRect(box)

    def mousePressEvent(self, event) -> None:
        if not self.timeline.available or event.button() != Qt.LeftButton:
            return
        t = self.x_to_t(event.position().x())
        start, end = self.timeline.view
        if not start <= t <= end:  # outside the box: centre the view here first
            self._move_view(t - (end - start) / 2)
            start, end = self.timeline.view
        self._grab_offset = t - start

    def mouseMoveEvent(self, event) -> None:
        if self._grab_offset is not None:
            self._move_view(self.x_to_t(event.position().x()) - self._grab_offset)

    def mouseReleaseEvent(self, _event) -> None:
        self._grab_offset = None

    def wheelEvent(self, event) -> None:
        self.timeline.wheelEvent(event)

    def _move_view(self, start: float) -> None:
        span = self.timeline.span
        self.timeline.set_view(start, start + span)
