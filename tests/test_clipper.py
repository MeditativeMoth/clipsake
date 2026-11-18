import errno
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from timeline import clipper

# The tags ypb wrote into a real download (20 s requested, segments from 4.812 s earlier).
# ypb's error when the end time is ahead of the stream (from a real run).
NOT_REACHED = (
    "ypb: error: locating interval: resolving end moment '2026-10-30 12:09:17 +0000 +00:00': "
    "time 2026-10-30 12:09:17 +0000 +00:00 is after current moment: 2026-10-30 12:09:15.484041 +0000 UTC"
)

TAGS = {
    "InputStartTime": "2026-10-30T11:11:10.000Z",
    "InputEndTime": "2026-10-30T11:11:30.000Z",
    "ActualStartTime": "2026-10-30T11:11:05.188Z",
    "ActualEndTime": "2026-10-30T11:11:30.459Z",
}


class CommandTest(unittest.TestCase):
    def test_ypb_command_asks_yt_dlp_for_h264_mp4_at_the_given_path(self):
        command = clipper.ypb_command("ypb", "abc123", "A/B", Path("/w/download.%(ext)s"))
        self.assertEqual(command[:10], ["ypb", "download", "--host", "127.0.0.1", "--port", "0",
                                        "--interval", "A/B", "--", "https://www.youtube.com/watch?v=abc123"])
        ytdlp = command[command.index("--") + 2:]
        self.assertEqual(ytdlp[ytdlp.index("-f") + 1], clipper.VIDEO_FORMAT)
        self.assertIn("avc1", clipper.VIDEO_FORMAT)
        self.assertEqual(ytdlp[ytdlp.index("--merge-output-format") + 1], "mp4")
        self.assertEqual(ytdlp[ytdlp.index("-o") + 1], str(Path("/w/download.%(ext)s")))
        self.assertIn("--newline", ytdlp)

    def test_an_id_starting_with_a_dash_is_never_read_as_an_option(self):
        # Neither by ypb (it's after "--") nor by yt-dlp (it's inside a URL).
        command = clipper.ypb_command("ypb", "-vvvvvvvvvv", "A/B", Path("/w/download.%(ext)s"))
        self.assertNotIn("-vvvvvvvvvv", command)
        self.assertEqual(command[command.index("--") + 1], "https://www.youtube.com/watch?v=-vvvvvvvvvv")

    def test_yt_dlp_ignores_the_users_own_config(self):
        command = clipper.ypb_command("ypb", "abc123", "A/B", Path("/w/download.%(ext)s"))
        self.assertIn("--ignore-config", command[command.index("--"):])

    def test_finish_without_cut_copies_and_adds_faststart(self):
        command = clipper.finish_command("ffmpeg", Path("in.mp4"), Path("out.mp4"), None)
        self.assertIn("copy", command)
        self.assertNotIn("libx264", command)
        self.assertIn("+faststart+use_metadata_tags", command)
        self.assertEqual(command[-1], "out.mp4")

    def test_finish_with_cut_reencodes_audio_and_video(self):
        command = clipper.finish_command("ffmpeg", Path("in.mp4"), Path("out.mp4"), (4.812, 20.0))
        self.assertEqual(command[command.index("-ss") + 1], "4.812")
        self.assertEqual(command[command.index("-t") + 1], "20.000")
        self.assertLess(command.index("-ss"), command.index("-i"))  # seek before reading
        self.assertEqual(command[command.index("-c:v") + 1], "libx264")
        self.assertEqual(command[command.index("-c:a") + 1], "aac")  # not copied: ypb's --cut bug
        self.assertIn("+faststart+use_metadata_tags", command)


def load_plugin():
    """The yt-dlp plugin module, loaded from its file like yt-dlp does."""
    import importlib.util

    path = clipper.PLUGIN_DIR / "clipsake" / "yt_dlp_plugins" / "postprocessor" / "timeline_formats.py"
    spec = importlib.util.spec_from_file_location("timeline_formats", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PluginTest(unittest.TestCase):
    """The yt-dlp plugin that keeps ypb from crashing on unidentified formats."""

    VIDEO = {"format_id": "137", "vcodec": "avc1.640028", "acodec": "none", "width": 1920, "height": 1080, "fps": 30}
    AUDIO = {"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2", "asr": 44100}
    UNKNOWN = {"format_id": "386", "vcodec": None, "acodec": None, "width": None, "height": None, "fps": None}

    def test_ypb_command_loads_it(self):
        command = clipper.ypb_command("ypb", "abc123", "A/B", Path("/w/download.%(ext)s"))
        ytdlp = command[command.index("--") + 2:]
        self.assertEqual(ytdlp[ytdlp.index("--plugin-dirs") + 1], str(clipper.PLUGIN_DIR))
        self.assertEqual(ytdlp[ytdlp.index("--use-postprocessor") + 1], "DropIncompleteFormats:when=pre_process")
        self.assertTrue((clipper.PLUGIN_DIR / "clipsake" / "yt_dlp_plugins" / "postprocessor").is_dir())

    def test_drops_formats_ypb_would_crash_on(self):
        plugin = load_plugin()
        pp = plugin.DropIncompleteFormatsPP()
        info = {"extractor_key": "Youtube", "formats": [self.VIDEO, self.AUDIO, self.UNKNOWN, self.AUDIO | {"asr": None}]}
        with mock.patch.object(pp, "to_screen") as said:
            files, info = pp.run(info)
        self.assertEqual((files, info["formats"]), ([], [self.VIDEO, self.AUDIO]))
        self.assertIn("386", said.call_args.args[0])

    def test_leaves_complete_and_non_youtube_lists_alone(self):
        pp = load_plugin().DropIncompleteFormatsPP()
        complete = {"extractor_key": "Youtube", "formats": [self.VIDEO, self.AUDIO]}
        generic = {"extractor_key": "Generic", "formats": [self.UNKNOWN]}
        for info in (complete, generic):
            with self.subTest(extractor=info["extractor_key"]), mock.patch.object(pp, "to_screen") as said:
                self.assertEqual(pp.run(dict(info))[1]["formats"], info["formats"])
                said.assert_not_called()


class OutputTest(unittest.TestCase):
    def test_fragment_progress_becomes_a_progress_line(self):
        line = "[download]  45.7% of ~ 700.07KiB at  383.25KiB/s ETA Unknown (frag 4/7)\n"
        self.assertEqual(clipper.progress_line(line), "Progress: 4/7 57%")
        self.assertIsNone(clipper.progress_line("[download] Destination: download.f137.mp4"))
        self.assertIsNone(clipper.progress_line("Actual start: Fri, 30 Oct 2026 11:09:09 +0000, sq=1246534"))

    def test_urls_are_hidden_and_long_lines_capped(self):
        line = 'Get "https://rr2---sn-x.googlevideo.com/videoplayback?ip=203.0.113.7&sig=abc": giving up'
        self.assertEqual(clipper.clean_line(line), 'Get "<url>": giving up')
        self.assertEqual(len(clipper.clean_line("x" * 1000)), clipper.MAX_LINE + 1)

    def test_friendly_errors(self):
        cases = {
            ("ypb: error: locating interval: resolving start moment '2026-08-01': fetching segment",):
                "Couldn't find those moments",
            ("ypb: error: unable to find yt-dlp: exec: not found",): "yt-dlp wasn't found",
            ("ERROR: [youtube] xxxxxxxxxxx: This video is unavailable",
             "ypb: error: creating playback: fetching video info for playback: exit status 1"):
                "ypb: error: creating playback",
            ("something odd",): "ypb stopped unexpectedly (exit code 2)",
        }
        for lines, expected in cases.items():
            with self.subTest(lines=lines):
                self.assertIn(expected, clipper.friendly_error(list(lines), 2))

    def test_end_time_not_reached_yet(self):
        self.assertAlmostEqual(clipper.seconds_until_reached(["something", NOT_REACHED]), 1.515959)
        self.assertIsNone(clipper.seconds_until_reached(["ypb: error: locating interval: resolving start moment"]))
        self.assertIn("hasn't reached the end time", clipper.friendly_error([NOT_REACHED], 1))

    def test_go_time(self):
        self.assertEqual(
            clipper.go_time("2026-10-30 12:09:15.484041", "+0000").isoformat(), "2026-10-30T12:09:15.484041+00:00"
        )
        self.assertEqual(clipper.go_time("2026-10-30 12:09:17", "-0700").isoformat(), "2026-10-30T12:09:17-07:00")
        self.assertEqual(clipper.go_time("2026-10-30 12:09:15.5", "+0000").microsecond, 500000)

    def test_friendly_error_hides_urls(self):
        error = clipper.friendly_error(["ypb: error: requesting segment: Get https://x.googlevideo.com/?ip=1.2.3.4"], 1)
        self.assertNotIn("1.2.3.4", error)


class TagsTest(unittest.TestCase):
    def test_read_tags_parses_ffmetadata(self):
        ffmetadata = (
            ";FFMETADATA1\nmajor_brand=isom\nInputStartTime=2026-10-30T11:11:10.000Z\n"
            "Comment=a\\=b\\;c\n[STREAM]\nhandler_name=VideoHandler\n"
        )
        result = subprocess.CompletedProcess([], 0, stdout=ffmetadata, stderr="")
        with mock.patch.object(clipper.subprocess, "run", return_value=result):
            tags = clipper.read_tags("ffmpeg", Path("in.mp4"))
        self.assertEqual(
            tags, {"major_brand": "isom", "InputStartTime": "2026-10-30T11:11:10.000Z", "Comment": "a=b;c"}
        )

    def test_cut_range_from_ypbs_tags(self):
        offset, length = clipper.cut_range(TAGS)
        self.assertAlmostEqual(offset, 4.812)
        self.assertAlmostEqual(length, 20.0)

    def test_cut_range_refuses_missing_or_odd_tags(self):
        missing = {k: v for k, v in TAGS.items() if k != "ActualStartTime"}
        backwards = TAGS | {"ActualStartTime": "2026-10-30T11:11:15.000Z"}
        for tags in (missing, backwards):
            with self.subTest(tags=tags), self.assertRaises(ValueError):
                clipper.cut_range(tags)


class ClipTest(unittest.TestCase):
    """clip() with ypb and ffmpeg replaced by stand-ins."""

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.work = self.folder / ".work"
        self.work.mkdir()
        self.output = self.folder / "clip.mp4"
        which = mock.patch.object(clipper.shutil, "which", side_effect=lambda name: f"/bin/{name}")
        which.start()
        self.addCleanup(which.stop)

    def fake_ypb(self, exit_code: int, download: bool = True, tail: tuple[str, ...] = ()):
        def run(command):
            if download:
                (self.work / "download.mp4").write_bytes(b"segments")
            return exit_code, list(tail)

        return mock.patch.object(clipper, "run_ypb", side_effect=run)

    def fake_ffmpeg(self):
        def run(command, **kwargs):
            if "ffmetadata" in command:
                stdout = "".join(f"{k}={v}\n" for k, v in TAGS.items())
                return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")
            Path(command[-1]).write_bytes(b"finished")  # the finish command's output
            return subprocess.CompletedProcess(command, 0)

        return mock.patch.object(clipper.subprocess, "run", side_effect=run)

    def test_precise_clip_is_cut_and_saved(self):
        with self.fake_ypb(0), self.fake_ffmpeg() as run, mock.patch("builtins.print") as printed:
            self.assertEqual(clipper.clip("abc123", "A/B", self.output, True, self.work), 0)
        self.assertEqual(self.output.read_bytes(), b"finished")
        finish = run.call_args_list[-1].args[0]
        self.assertEqual(finish[finish.index("-ss") + 1], "4.812")
        printed.assert_any_call(f"Saved to '{self.output}'", flush=True)

    def test_waits_for_the_stream_to_reach_the_end_time(self):
        results = iter([(1, [NOT_REACHED]), (0, [])])

        def run(command):
            exit_code, tail = next(results)
            if exit_code == 0:
                (self.work / "download.mp4").write_bytes(b"segments")
            return exit_code, tail

        with mock.patch.object(clipper, "run_ypb", side_effect=run) as ypb, self.fake_ffmpeg(), \
                mock.patch.object(clipper.time, "sleep") as sleep, mock.patch("builtins.print"):
            self.assertEqual(clipper.clip("abc123", "A/B", self.output, False, self.work), 0)
        self.assertEqual(ypb.call_count, 2)
        self.assertAlmostEqual(sleep.call_args.args[0], 3.515959)  # 1.52 s behind, plus 2 s
        self.assertTrue(self.output.exists())

    def test_ypb_error_is_hidden_when_retrying_and_shown_when_giving_up(self):
        lines = ["(<<) Locating start and end moments...", NOT_REACHED]
        printed = []
        with mock.patch("builtins.print", side_effect=lambda text, **kw: printed.append(text)):
            with mock.patch.object(clipper.subprocess, "Popen") as popen:
                popen.return_value.__enter__.return_value.stdout = iter(line + "\n" for line in lines)
                popen.return_value.__enter__.return_value.returncode = 1
                popen.return_value.returncode = 1
                clipper.run_ypb(["ypb"])
        self.assertEqual(printed, ["(<<) Locating start and end moments..."])  # error held back

        with mock.patch.object(clipper, "run_ypb", return_value=(1, [NOT_REACHED])), \
                mock.patch.object(clipper.time, "sleep") as sleep, mock.patch("builtins.print") as shown:
            clipper.clip("abc123", "A/B", self.output, False, self.work)
        texts = [call.args[0] for call in shown.call_args_list]
        self.assertEqual(sum(t.startswith("ypb: error:") for t in texts), 1)  # only after the last try
        self.assertEqual(sleep.call_count, clipper.CATCH_UP_TRIES - 1)  # no wait after the last try

    def test_gives_up_waiting_after_a_few_tries(self):
        with mock.patch.object(clipper, "run_ypb", return_value=(1, [NOT_REACHED])) as ypb, \
                mock.patch.object(clipper.time, "sleep"), mock.patch("builtins.print") as printed:
            self.assertEqual(clipper.clip("abc123", "A/B", self.output, False, self.work), 1)
        self.assertEqual(ypb.call_count, clipper.CATCH_UP_TRIES)
        self.assertIn("hasn't reached the end time", printed.call_args.args[0])

    def test_failed_download_reports_why_and_saves_nothing(self):
        tail = ("ypb: error: locating interval: resolving start moment",)
        with self.fake_ypb(1, download=False, tail=tail), mock.patch("builtins.print") as printed:
            self.assertEqual(clipper.clip("abc123", "A/B", self.output, False, self.work), 1)
        self.assertFalse(self.output.exists())
        self.assertIn("Couldn't find those moments", printed.call_args.args[0])

    def test_existing_clip_is_not_overwritten(self):
        self.output.write_bytes(b"keep me")
        with self.fake_ypb(0) as ypb, mock.patch("builtins.print"):
            self.assertEqual(clipper.clip("abc123", "A/B", self.output, False, self.work), 1)
        ypb.assert_not_called()
        self.assertEqual(self.output.read_bytes(), b"keep me")

    def test_overwrite_replaces_once_finished(self):
        self.output.write_bytes(b"old")
        with self.fake_ypb(0), self.fake_ffmpeg(), mock.patch("builtins.print"):
            self.assertEqual(clipper.clip("abc123", "A/B", self.output, False, self.work, overwrite=True), 0)
        self.assertEqual(self.output.read_bytes(), b"finished")

    def test_overwrite_keeps_the_old_file_if_clipping_fails(self):
        self.output.write_bytes(b"old")
        with self.fake_ypb(1, download=False), mock.patch("builtins.print"):
            self.assertEqual(clipper.clip("abc123", "A/B", self.output, False, self.work, overwrite=True), 1)
        self.assertEqual(self.output.read_bytes(), b"old")

    def test_a_clip_finished_on_another_file_system_is_copied(self):
        moved = OSError(errno.EXDEV, "Invalid cross-device link")
        with self.fake_ypb(0), self.fake_ffmpeg(), mock.patch("builtins.print"), \
                mock.patch.object(clipper.os, "replace", side_effect=moved):
            self.assertEqual(clipper.clip("abc123", "A/B", self.output, False, self.work), 0)
        self.assertEqual(self.output.read_bytes(), b"finished")


class WorkFolderTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)

    def test_next_to_the_clip(self):
        work = clipper.new_work_folder(self.folder / "clip.mp4")
        self.assertEqual(work.parent, self.folder)
        self.assertTrue(work.name.startswith(".clipsake-"))

    def test_in_the_cache_folder_where_the_clips_folder_cant_be_written(self):
        # The Flatpak, saving through the file chooser's portal somewhere it may write only that file.
        real_mkdtemp = tempfile.mkdtemp

        def mkdtemp(prefix, dir):
            if Path(dir) == self.folder / "portal":
                raise PermissionError(errno.EPERM, "Operation not permitted")
            return real_mkdtemp(prefix=prefix, dir=dir)

        with mock.patch.dict(clipper.os.environ, {"XDG_CACHE_HOME": str(self.folder / "cache")}), \
                mock.patch.object(clipper.tempfile, "mkdtemp", side_effect=mkdtemp):
            work = clipper.new_work_folder(self.folder / "portal" / "clip.mp4")
        self.assertEqual(work.parent, self.folder / "cache" / "clipsake")
        self.assertTrue(work.is_dir())


if __name__ == "__main__":
    unittest.main()
