import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication

from timeline import widgets

app = QApplication.instance() or QApplication([])


class RulerTests(unittest.TestCase):
    def test_ruler_step_keeps_labels_apart(self):
        self.assertEqual(widgets.ruler_step(0.01), 1)
        self.assertEqual(widgets.ruler_step(1.0), 120)
        self.assertEqual(widgets.ruler_step(10_000), 86400)


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.timeline = widgets.TimelineWidget()
        self.timeline.resize(1000, 120)
        self.timeline.set_available(1000.0, 5000.0, oldest_known=True)
        self.previews = []
        self.timeline.previewRequested.connect(self.previews.append)

    def test_starts_on_the_last_half_hour(self):
        self.assertEqual(self.timeline.view, (5000.0 - 1800, 5000.0))
        self.assertTrue(self.timeline.follow_live)

    def test_selection_snaps_sorts_and_clamps(self):
        self.timeline.set_selection(4321.7, 900.0, moved="start")
        self.assertEqual(self.timeline.selection, (1000, 4322))
        self.assertEqual(self.previews, [1000])

    def test_selection_at_least_a_second(self):
        self.timeline.set_selection(2000.2, 2000.4)
        self.assertEqual(self.timeline.selection, (2000, 2001))

    def test_follows_the_live_edge(self):
        self.timeline.set_available(1000.0, 5100.0, oldest_known=True)
        self.assertEqual(self.timeline.view, (5100.0 - 1800, 5100.0))

    def test_stays_put_when_scrolled_back(self):
        self.timeline.set_view(2000, 2500)
        self.timeline.set_available(1000.0, 5100.0, oldest_known=True)
        self.assertEqual(self.timeline.view, (2000, 2500))

    def test_zoom_limits(self):
        self.timeline.set_view(3000, 3001)
        self.assertEqual(self.timeline.span, widgets.MIN_SPAN)
        self.assertEqual(sum(self.timeline.view) / 2, 3000.5)  # widened around the middle
        self.timeline.zoom_by(0.5, anchor=3010)
        self.assertEqual(self.timeline.span, widgets.MIN_SPAN)  # can't zoom in further
        self.assertEqual(sum(self.timeline.view) / 2, 3000.5)  # nor drift
        self.timeline.set_view(-1e9, 1e9)
        self.assertLessEqual(self.timeline.span, 4000 * 1.05)

    def test_thumbnail_slots_only_where_available(self):
        self.timeline.set_view(0, 2000)
        slots = self.timeline.thumb_slots()
        self.assertTrue(slots)
        self.assertTrue(all(1000 <= moment <= 5000 for _, moment in slots))

    def test_nearest_thumbnail(self):
        from PySide6.QtGui import QPixmap
        pixmap = QPixmap(16, 9)
        self.timeline.add_thumb(2000.0, pixmap)
        self.assertIs(self.timeline.thumb_near(2003.0, 5.0), pixmap)
        self.assertIsNone(self.timeline.thumb_near(2010.0, 5.0))


class TooltipTests(unittest.TestCase):
    def test_html_in_a_title_is_shown_as_written(self):
        from PySide6.QtGui import QTextDocument
        tip = widgets.plain_tooltip('<img src="/etc/x.png"> <b>Live</b> & "more"\nhttps://youtu.be/x')
        document = QTextDocument()
        document.setHtml(tip)
        self.assertEqual(document.toPlainText(), '<img src="/etc/x.png"> <b>Live</b> & "more"\nhttps://youtu.be/x')


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.timeline = widgets.TimelineWidget()
        self.timeline.resize(1000, 120)
        self.timeline.set_available(0.0, 7 * 86400.0, oldest_known=True)

    def test_zoom_presets_centre_on_the_playhead(self):
        self.timeline.set_playhead(86400)
        self.timeline.zoom_to_span(600)
        self.assertEqual(self.timeline.view, (86100, 86700))

    def test_zoom_centres_on_the_handle_being_moved(self):
        self.timeline.set_playhead(86400)
        self.timeline.set_selection(3 * 86400, 3 * 86400 + 60, moved="end")
        self.timeline.zoom_to_span(3600)
        self.assertEqual(sum(self.timeline.view) / 2, 3 * 86400 + 60)

    def test_zoom_by_keeps_the_focus_in_place(self):
        self.timeline.set_view(1000, 2000)
        self.timeline.set_playhead(1250)
        self.timeline.zoom_by(0.5)
        self.assertEqual(self.timeline.view, (1125, 1625))

    def test_go_to(self):
        self.timeline.go_to(200_000, span=600)
        self.assertEqual((self.timeline.playhead, self.timeline.view), (200_000, (199_700, 200_300)))

    def test_overview_click_moves_the_view_there(self):
        overview = widgets.OverviewWidget(self.timeline)
        overview.resize(700, 30)  # 100 px a day
        self.timeline.set_view(6 * 86400, 6 * 86400 + 3600)
        from PySide6.QtCore import QPointF, Qt, QEvent
        from PySide6.QtGui import QMouseEvent
        press = QMouseEvent(QEvent.MouseButtonPress, QPointF(250, 15), QPointF(250, 15),
                            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        overview.mousePressEvent(press)
        self.assertAlmostEqual(sum(self.timeline.view) / 2, 2.5 * 86400)
        self.assertAlmostEqual(self.timeline.span, 3600)


class MouseTests(unittest.TestCase):
    """At 1000 px for 1000 s, one pixel is one second."""

    def setUp(self):
        self.timeline = widgets.TimelineWidget()
        self.timeline.resize(1000, 120)
        self.timeline.set_available(0.0, 10_000.0, oldest_known=True)
        self.timeline.set_view(5000, 6000)

    def drag(self, start_x, end_x, modifiers=None, button=None):
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        button = button or Qt.LeftButton
        modifiers = modifiers or Qt.NoModifier

        def event(kind, x, buttons):
            return QMouseEvent(kind, QPointF(x, 40), QPointF(x, 40), button, buttons, modifiers)

        self.timeline.mousePressEvent(event(QEvent.MouseButtonPress, start_x, button))
        for x in (start_x + (end_x - start_x) / 2, end_x) if end_x != start_x else ():
            self.timeline.mouseMoveEvent(event(QEvent.MouseMove, x, button))
        self.timeline.mouseReleaseEvent(event(QEvent.MouseButtonRelease, end_x, Qt.NoButton))

    def test_click_inside_the_clip_seeks_while_its_preview_plays(self):
        self.timeline.set_selection(5200, 5500, moved="end")
        self.timeline.start_playback()
        seeks = []
        self.timeline.seekRequested.connect(seeks.append)
        self.click_at(5300)
        self.assertEqual(seeks, [5300])
        self.assertEqual(self.timeline.selection, (5200, 5500))  # the end didn't move there

    def test_click_outside_the_clip_while_playing_does_as_usual(self):
        self.timeline.set_selection(5200, 5500, moved="end")
        self.timeline.start_playback()
        self.timeline.follow_playback(5300)  # the playhead moving along with the preview
        seeks = []
        self.timeline.seekRequested.connect(seeks.append)
        self.click_at(5700)
        self.assertEqual(seeks, [])
        self.assertEqual(self.timeline.selection, (5200, 5700))  # moved the selected end, as without a preview

    def test_the_playhead_goes_when_the_preview_stops(self):
        self.timeline.set_selection(5200, 5500, moved="end")
        self.timeline.follow_playback(5300)
        self.timeline.end_playback()
        self.assertIsNone(self.timeline.playhead)

    def test_a_playhead_put_somewhere_during_the_preview_stays(self):
        self.timeline.set_selection(5200, 5500, moved="end")
        self.timeline.follow_playback(5300)
        from PySide6.QtCore import Qt
        self.click_at(5700, modifiers=Qt.ControlModifier)  # which stops the preview
        self.timeline.end_playback()
        self.assertEqual(self.timeline.playhead, 5700)

    def test_with_no_playhead_zooming_centres_on_the_moment_shown(self):
        self.timeline.shown = 5900  # e.g. the live edge's still, when a stream opens
        self.timeline.zoom_to_span(60)
        self.assertEqual(self.timeline.view, (5870, 5930))

    def test_drag_moves_along(self):
        self.drag(600, 400)  # dragged left: later times come into view
        self.assertEqual(self.timeline.view, (5200, 6200))
        self.assertIsNone(self.timeline.selection)
        self.assertIsNone(self.timeline.playhead)

    def test_middle_drag_still_moves_along(self):
        from PySide6.QtCore import Qt
        self.drag(400, 600, button=Qt.MiddleButton)
        self.assertEqual(self.timeline.view, (4800, 5800))

    def test_shift_drag_selects(self):
        from PySide6.QtCore import Qt
        self.drag(200, 500, modifiers=Qt.ShiftModifier)
        self.assertEqual(self.timeline.selection, (5200, 5500))
        self.assertEqual(self.timeline.view, (5155, 5545))  # zoomed to the clip once let go

    def test_click_with_no_clip_starts_one(self):
        self.drag(300, 301)  # a small wobble is still a click
        self.assertEqual(self.timeline.selection, (5300, 5300 + widgets.DEFAULT_LENGTH))
        self.assertEqual(self.timeline.active, "start")
        self.assertEqual(self.timeline.view, (5285, 5345))  # zoomed to the new clip, as far as it goes

    def click_at(self, t, **kw):
        """Click at moment `t` (the view may have moved since the last click)."""
        x = self.timeline.t_to_x(t)
        self.drag(x, x, **kw)

    def test_click_moves_the_selected_end(self):
        self.timeline.set_selection(5200, 5500, moved="start")
        self.click_at(5100)
        self.assertEqual(self.timeline.selection, (5100, 5500))
        self.assertEqual(self.timeline.view, (5040, 5560))  # zoomed to show the clip
        self.click_at(5500)  # the end handle: selects it, moves nothing
        self.assertEqual((self.timeline.active, self.timeline.selection), ("end", (5100, 5500)))
        self.click_at(5540)
        self.assertEqual(self.timeline.selection, (5100, 5540))
        self.assertIsNone(self.timeline.playhead)

    def test_clicking_past_the_other_end_moves_it_along(self):
        self.timeline.set_selection(5200, 5500, moved="start")
        self.click_at(5700)
        self.assertEqual((self.timeline.selection, self.timeline.active), ((5700, 6000), "start"))
        self.timeline.set_selection(5200, 5500, moved="end")
        self.click_at(5100)
        self.assertEqual((self.timeline.selection, self.timeline.active), ((4800, 5100), "end"))

    def test_with_the_playhead_selected_a_click_moves_it(self):
        from PySide6.QtCore import Qt
        self.timeline.set_selection(5200, 5500, moved="start")
        self.drag(100, 100, modifiers=Qt.ControlModifier)
        self.drag(300, 300)
        self.assertEqual((self.timeline.playhead, self.timeline.selection), (5300, (5200, 5500)))

    def test_ctrl_click_with_no_clip_moves_the_playhead(self):
        from PySide6.QtCore import Qt
        self.drag(700, 700, modifiers=Qt.ControlModifier)
        self.assertEqual(self.timeline.playhead, 5700)
        self.assertIsNone(self.timeline.selection)

    def test_drag_a_handle_then_show_the_clip(self):
        self.timeline.set_selection(5200, 5500)
        self.drag(500, 700)
        self.assertEqual(self.timeline.selection, (5200, 5700))
        self.assertEqual(self.timeline.view, (5125, 5775))  # the clip, with 15% either side


if __name__ == "__main__":
    unittest.main()
