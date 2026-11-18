"""Tests for the clip runner, zone list and duration format."""

import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from timeline import clipper, processes  # noqa: E402
from timeline.picker import format_duration  # noqa: E402
from timeline.processes import ClipRunner  # noqa: E402
from timeline.zones import find_zone, zone_names  # noqa: E402


class FormattingTest(unittest.TestCase):
    def test_format_duration(self):
        cases = {
            timedelta(seconds=7): "7s",
            timedelta(minutes=2, seconds=5): "2m 05s",
            timedelta(hours=1, minutes=2, seconds=3): "1h 02m 03s",
            timedelta(hours=50): "2d 2h",
        }
        for delta, expected in cases.items():
            with self.subTest(delta=delta):
                self.assertEqual(format_duration(delta), expected)


class TimeZoneTest(unittest.TestCase):
    def test_find_zone_ignores_case_and_accepts_spaces(self):
        self.assertEqual(find_zone("america/new york"), "America/New_York")
        self.assertEqual(find_zone(" Asia/Tokyo "), "Asia/Tokyo")
        self.assertEqual(find_zone("US/Pacific"), "US/Pacific")  # aliases still work if typed
        self.assertIsNone(find_zone("Not/AZone"))
        self.assertIsNone(find_zone(""))

    def test_zone_names_lists_canonical_names(self):
        names = zone_names()
        self.assertEqual(names[0], "UTC")
        self.assertIn("America/New_York", names)
        self.assertNotIn("US/Pacific", names)
        self.assertNotIn("Etc/GMT+8", names)
        self.assertEqual(names[1:], sorted(names[1:]))


class FakeProcess:
    """Stands in for the QProcess a ClipRunner reads from."""

    def __init__(self, output: bytes = b"") -> None:
        self.output = output

    def readAllStandardOutput(self) -> bytes:
        output, self.output = self.output, b""
        return output

    def processId(self) -> int:
        return 0  # no real process, so kill_process_tree does nothing

    def kill(self) -> None:
        pass

    def deleteLater(self) -> None:
        pass


class ClipRunnerTest(unittest.TestCase):
    def setUp(self):
        self.runner = ClipRunner({})
        self.progress, self.log, self.done = [], [], []
        self.runner.progress.connect(lambda *args: self.progress.append(args))
        self.runner.log_line.connect(self.log.append)
        self.runner.done.connect(lambda *args: self.done.append(args))

    def finish(self, output: bytes, exit_code: int) -> None:
        process = FakeProcess(output)
        self.runner.process = process
        self.runner._on_finished(process, exit_code)

    def test_output_is_split_into_progress_and_log_lines(self):
        self.finish(
            b"\x1b[32mStarting\x1b[0m\nProgress: 3/10 30%\r\nProgress: 10/10 100%\n"
            b"Actual start: 30/10 sq=5\nSaved to '/clips/a.mp4'\n",
            0,
        )
        self.assertEqual(self.progress, [(3, 10, "Progress: 3/10 30%"), (10, 10, "Progress: 10/10 100%")])
        self.assertEqual(self.log, ["Starting", "Actual start: 30/10 sq=5", "Saved to '/clips/a.mp4'"])

    def test_success_reports_the_saved_file(self):
        self.finish(b"Saved to '/clips/a.mp4'\n", 0)
        self.assertEqual(self.done, [(False, Path("/clips/a.mp4"), "")])
        self.assertFalse(self.runner.running)

    def test_failure(self):
        self.finish(b"Error: stream is offline\n", 1)
        self.assertEqual(self.done, [(False, None, "Clipping failed (exit code 1). See the log.")])

    def test_exit_without_a_saved_file_is_a_failure(self):
        self.finish(b"", 0)
        self.assertEqual(self.done[0][1:], (None, "Clipping failed (exit code 0). See the log."))

    def test_cancel_is_not_a_failure(self):
        process = FakeProcess()
        self.runner.process = process
        self.runner.cancel()
        self.runner._on_finished(process, 9)
        self.assertEqual(self.done, [(True, None, "")])

    def test_cancel_stops_the_whole_process_tree(self):
        process = FakeProcess()
        self.runner.process = process
        with mock.patch.object(processes, "kill_process_tree") as kill_tree, mock.patch.object(process, "processId", return_value=4321):
            self.runner.cancel()
        kill_tree.assert_called_once_with(4321)

    def test_work_folder_is_removed_however_the_clip_ends(self):
        for exit_code in (0, 1):
            with self.subTest(exit_code=exit_code):
                self.runner.work_dir = Path(tempfile.mkdtemp())
                (self.runner.work_dir / "download.mp4.part").write_bytes(b"partial")
                work_dir = self.runner.work_dir
                self.finish(b"", exit_code)
                self.assertFalse(work_dir.exists())

    def test_arguments(self):
        start, end = datetime(2026, 10, 30, 11, 9, 9, tzinfo=UTC), datetime(2026, 10, 30, 11, 9, 29, tzinfo=UTC)
        common = [
            "--interval", "2026-10-30T11:09:09+00/2026-10-30T11:09:29+00", "--output", str(Path("/clips/a.mp4")),
            "--work-dir", str(Path("/clips/.w")), "--own-process-group",
        ]
        self.assertEqual(
            ClipRunner.arguments("abc123", start, end, Path("/clips/a.mp4"), False, Path("/clips/.w")),
            [*common, "--", "abc123"],
        )
        self.assertEqual(
            ClipRunner.arguments("abc123", start, end, Path("/clips/a.mp4"), True, Path("/clips/.w")),
            [*common, "--precise", "--", "abc123"],
        )
        self.assertEqual(
            ClipRunner.arguments("abc123", start, end, Path("/clips/a.mp4"), False, Path("/clips/.w"), overwrite=True),
            [*common, "--overwrite", "--", "abc123"],
        )

    def test_the_clipper_reads_an_id_starting_with_a_dash_as_the_id(self):
        start, end = datetime(2026, 10, 30, 11, 9, 9, tzinfo=UTC), datetime(2026, 10, 30, 11, 9, 29, tzinfo=UTC)
        args = ClipRunner.arguments("-vvvvvvvvvv", start, end, Path("/clips/a.mp4"), True, Path("/clips/.w"))
        with mock.patch.object(clipper.sys, "argv", ["clipper", *args]), \
                mock.patch.object(clipper, "clip", return_value=0) as clip, \
                mock.patch.object(clipper, "hide_child_consoles"), self.assertRaises(SystemExit):
            clipper.main()
        self.assertEqual(clip.call_args.args[0], "-vvvvvvvvvv")
        self.assertTrue(clip.call_args.args[3])  # --precise still read as an option

    def test_signals_from_an_earlier_process_are_ignored(self):
        self.runner.process = FakeProcess()
        self.runner._on_finished(FakeProcess(b"Saved to '/clips/old.mp4'\n"), 0)
        self.assertEqual((self.done, self.log), ([], []))
        self.assertTrue(self.runner.running)


if __name__ == "__main__":
    unittest.main()
