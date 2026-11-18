import hashlib
import importlib.util
import io
import re
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parent.parent / "tools"


def load_tool(name: str):
    """tools/ isn't a package, so load tools/<name>.py from its path."""
    spec = importlib.util.spec_from_file_location(f"tool_{name}", TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up while it runs
    spec.loader.exec_module(module)
    return module


pinned = load_tool("pinned")


class PinsTest(unittest.TestCase):
    def test_every_tool_is_pinned_for_each_platform_over_https(self):
        self.assertEqual(set(pinned.TOOLS), {"ffmpeg", "ffprobe", "ypb"})
        for tool in pinned.TOOLS.values():
            self.assertEqual(set(tool.builds), {("win32", "amd64"), ("linux", "x86_64"), ("darwin", "arm64")})
            for key, build in tool.builds.items():
                with self.subTest(tool=tool.name, platform=key):
                    self.assertTrue(build.url.startswith("https://"))
                    self.assertIn(tool.version, build.url)
                    self.assertRegex(build.sha256, r"^[0-9a-f]{64}$")

    def test_ffprobe_is_from_the_same_ffmpeg_builds(self):
        ffmpeg, ffprobe = pinned.TOOLS["ffmpeg"], pinned.TOOLS["ffprobe"]
        self.assertEqual(ffprobe.version, ffmpeg.version)
        for key in ("win32", "amd64"), ("linux", "x86_64"):
            self.assertIs(ffprobe.builds[key], ffmpeg.builds[key])  # one archive with both
        mac = ("darwin", "arm64")
        self.assertEqual(ffprobe.builds[mac].url.rsplit("/", 1)[0], ffmpeg.builds[mac].url.rsplit("/", 1)[0])


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.source = self.tmp / "source.zip"
        self.source.write_bytes(b"pretend archive")
        self.good = hashlib.sha256(b"pretend archive").hexdigest()

    def build(self, sha256: str):
        return pinned.Build(self.source.as_uri(), sha256)

    def test_matching_checksum_is_accepted(self):
        archive = self.tmp / "downloads" / "a.zip"
        pinned.download(self.build(self.good), archive)
        self.assertEqual(archive.read_bytes(), b"pretend archive")

    def test_wrong_checksum_is_refused_and_nothing_is_kept(self):
        archive = self.tmp / "downloads" / "a.zip"
        with self.assertRaises(SystemExit) as raised:
            pinned.download(self.build("0" * 64), archive)
        self.assertIn("SHA-256", str(raised.exception))
        self.assertEqual(list(archive.parent.iterdir()), [])

    def test_sends_its_own_user_agent(self):
        # Python's default "Python-urllib" User-Agent is refused by some hosts.
        reply = mock.MagicMock(__enter__=lambda self: io.BytesIO(b"pretend archive"))
        with mock.patch("urllib.request.urlopen", return_value=reply) as urlopen:
            pinned.download(self.build(self.good), self.tmp / "a.zip")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("User-agent"), pinned.USER_AGENT)

    def test_a_tampered_cached_archive_is_downloaded_again(self):
        archive = self.tmp / "a.zip"
        archive.write_bytes(b"tampered")
        pinned.download(self.build(self.good), archive)
        self.assertEqual(archive.read_bytes(), b"pretend archive")


class ExtractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.target = self.tmp / "out" / "ffmpeg"
        self.target.parent.mkdir()

    def make_zip(self, members: dict[str, bytes]) -> Path:
        path = self.tmp / "build.zip"
        with zipfile.ZipFile(path, "w") as zf:
            for name, data in members.items():
                zf.writestr(name, data)
        return path

    def test_finds_the_program_in_a_subfolder_of_a_zip(self):
        archive = self.make_zip({"b/bin/ffmpeg": b"binary", "b/bin/ffprobe": b"other", "b/doc/ffmpeg.html": b""})
        pinned.extract_program(archive, self.target)
        self.assertEqual(self.target.read_bytes(), b"binary")
        if sys.platform != "win32":
            self.assertTrue(self.target.stat().st_mode & 0o111)

    def test_finds_the_program_in_a_tar_xz(self):
        archive = self.tmp / "build.tar.xz"
        with tarfile.open(archive, "w:xz") as tf:
            info = tarfile.TarInfo("b/bin/ffmpeg")
            info.size = 6
            tf.addfile(info, io.BytesIO(b"binary"))
        pinned.extract_program(archive, self.target)
        self.assertEqual(self.target.read_bytes(), b"binary")

    def test_missing_or_ambiguous_program_is_an_error(self):
        for members in ({"b/bin/ffprobe": b""}, {"a/ffmpeg": b"1", "b/ffmpeg": b"2"}):
            with self.subTest(members=list(members)), self.assertRaises(SystemExit):
                pinned.extract_program(self.make_zip(members), self.target)


class FetchTest(unittest.TestCase):
    def test_ffmpeg_and_ffprobe_share_one_download(self):
        tmp = Path(tempfile.mkdtemp())
        source = tmp / "ffmpeg-build.zip"
        with zipfile.ZipFile(source, "w") as zf:
            zf.writestr("b/bin/ffmpeg", b"ffmpeg")
            zf.writestr("b/bin/ffprobe", b"ffprobe")
        build = pinned.Build(source.as_uri(), hashlib.sha256(source.read_bytes()).hexdigest())
        tools = {name: pinned.Tool(name, "1.0", {("os", "cpu"): build}) for name in ("ffmpeg", "ffprobe")}
        urlopen = pinned.urllib.request.urlopen
        with (mock.patch.object(pinned, "CACHE", tmp / "cache"), mock.patch.object(pinned, "TOOLS", tools),
              mock.patch.object(pinned.sys, "platform", "os"), mock.patch.object(pinned.platform, "machine", return_value="CPU"),
              mock.patch("urllib.request.urlopen", wraps=urlopen) as opened):
            programs = [pinned.fetch("ffmpeg"), pinned.fetch("ffprobe")]
        self.assertEqual([p.read_bytes() for p in programs], [b"ffmpeg", b"ffprobe"])
        self.assertEqual(opened.call_count, 1)  # ffprobe used the archive ffmpeg's fetch kept
        self.assertEqual([p.name for p in (tmp / "cache" / "downloads").iterdir()], [source.name])


class CurrentBuildTest(unittest.TestCase):
    def test_unsupported_platform_is_an_error(self):
        with mock.patch.object(pinned.sys, "platform", "sunos5"), self.assertRaises(SystemExit) as raised:
            pinned.current_build(pinned.TOOLS["ypb"])
        self.assertTrue(re.search(r"No pinned ypb build", str(raised.exception)))


class NoticesTest(unittest.TestCase):
    """THIRD_PARTY_NOTICES.md names the pinned builds' versions and their source."""

    def test_notices_match_the_pinned_builds(self):
        notices = (TOOLS.parent / "THIRD_PARTY_NOTICES.md").read_text()
        ypb = pinned.TOOLS["ypb"].version
        btbn = pinned.BTBN.rsplit("/", 1)[-1]  # autobuild-YYYY-MM-DD-HH-MM
        commit = re.search(r"-g([0-9a-f]+)-", pinned.BTBN_LINUX64.url).group(1)
        riedl = pinned.RIEDL.rsplit("/", 1)[-1]  # <timestamp>_<version>, the macOS builds
        ffmpeg = pinned.TOOLS["ffmpeg"].version
        for expected in (f"**ypb** {ypb}", f"ypb/tree/v{ypb}", f"FFmpeg-Builds/tree/{btbn}",
                         f"FFmpeg/tree/{commit}", f"`{btbn}`", f"{ffmpeg}, from BtbN",
                         f"`{riedl}`", f"FFmpeg/tree/n{riedl.split('_')[1]}"):
            self.assertTrue(expected in notices, f"{expected!r} isn't in THIRD_PARTY_NOTICES.md: update it along with tools/pinned.py")


check_pinned = load_tool("check_pinned")


class CheckPinnedTest(unittest.TestCase):
    """tools/check_pinned.py, with made-up upstream information."""

    TODAY = check_pinned.date(2026, 11, 8)
    CURRENT = {
        "ypb": {"tag_name": "v2026.11.6", "html_url": "https://github.com/xymaxim/ypb/releases/tag/v2026.11.6"},
        "btbn": {"html_url": "https://github.com/BtbN/FFmpeg-Builds/releases/tag/autobuild-2026-10-29-19-17", "assets": [
            "ffmpeg-n8.1.3-25-g611cec792a-win64-gpl-8.1.zip", "ffmpeg-n8.1.3-25-g611cec792a-linux64-gpl-8.1.tar.xz",
            "ffmpeg-n9.0.2-37-gae19868753-win64-gpl-9.0.zip", "ffmpeg-n9.0.2-37-gae19868753-linux64-gpl-9.0.tar.xz",
            "ffmpeg-n9.0.2-37-gae19868753-win64-gpl-shared-9.0.zip", "checksums.sha256",
        ]},
        "riedl": "<a>1787073674_9.0.1</a> <a>1789931890_9.0.2</a>",
    }

    def titles(self, sources, today=TODAY):
        return [f.title for f in check_pinned.check(pinned, today, sources)]

    def test_nothing_to_report_when_pins_are_current(self):
        self.assertEqual(self.titles(self.CURRENT), [])

    def test_reports_newer_releases(self):
        newer = self.CURRENT | {
            "ypb": {"tag_name": "v2026.12.1", "html_url": "https://github.com/xymaxim/ypb/releases/tag/v2026.12.1"},
            "btbn": self.CURRENT["btbn"] | {"assets": [
                "ffmpeg-n9.1-3-gabc-win64-gpl-9.1.zip", "ffmpeg-n9.1-3-gabc-linux64-gpl-9.1.tar.xz",
            ]},
            "riedl": self.CURRENT["riedl"] + " <a>1791000000_9.1</a>",
        }
        self.assertEqual(self.titles(newer), [
            "Update ypb to 2026.12.1", "Update ffmpeg (Windows and Linux) to 9.1", "Update ffmpeg (macOS) to 9.1",
        ])

    def test_btbn_release_needs_both_platforms_and_ignores_branch_commits(self):
        self.assertEqual(check_pinned.btbn_release_version(self.CURRENT["btbn"]["assets"]), "9.0.2")
        self.assertIsNone(check_pinned.btbn_release_version(["ffmpeg-n9.1-1-gabc-win64-gpl-9.1.zip"]))

    def test_warns_before_the_pinned_btbn_build_is_deleted(self):
        self.assertEqual(self.titles(self.CURRENT, today=check_pinned.date(2028, 6, 15)), [])  # 106 days left
        self.assertEqual(
            self.titles(self.CURRENT, today=check_pinned.date(2028, 8, 15)),
            ["Pinned BtbN ffmpeg build from 2026-09-30 expires soon"],
        )

    def test_reports_each_version_once(self):
        findings = [
            check_pinned.Finding("Update ypb to 2026.12.1", "..."),
            check_pinned.Finding("Update ffmpeg (macOS) to 9.1", "..."),
        ]
        listed = mock.Mock(stdout='[{"title": "Update ypb to 2026.12.1"}]')
        with mock.patch.object(check_pinned.subprocess, "run", return_value=listed) as run, \
                mock.patch("builtins.print"):
            check_pinned.create_issues(findings, "owner/repo")
        created = [c.args[0] for c in run.call_args_list if c.args[0][:3] == ["gh", "issue", "create"]]
        self.assertEqual(len(created), 1)
        self.assertIn("Update ffmpeg (macOS) to 9.1", created[0])



if __name__ == "__main__":
    unittest.main()
