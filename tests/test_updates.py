import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from timeline import app, updates
from timeline.updates import RELEASES_REPO, Release, is_newer, latest_release, parse_version

PAGE = f"https://github.com/{RELEASES_REPO}/releases/tag/v0.3.0"
FILES = f"https://github.com/{RELEASES_REPO}/releases/download/v0.3.0"
ASSETS = [{"browser_download_url": f"{FILES}/{name}"}
          for name in ("ClipSake-Setup-0.3.0.exe", "ClipSake-0.3.0.dmg", "ClipSake-0.3.0.flatpak")]


class VersionTest(unittest.TestCase):
    def test_parse_version(self):
        self.assertEqual(parse_version("v0.2.0"), (0, 2, 0))
        self.assertEqual(parse_version(" 1.10 "), (1, 10))
        self.assertIsNone(parse_version("0.2.0rc1"))
        self.assertIsNone(parse_version("latest"))

    def test_is_newer(self):
        self.assertTrue(is_newer("0.3.0", "0.2.0"))
        self.assertTrue(is_newer("0.10.0", "0.9.0"))  # compared as numbers, not text
        self.assertTrue(is_newer("1.0.1", "1.0"))
        self.assertFalse(is_newer("1.0", "1.0.0"))
        self.assertFalse(is_newer("0.2.0", "0.2.0"))
        self.assertFalse(is_newer("0.1.9", "0.2.0"))
        self.assertFalse(is_newer("0.3.0rc1", "0.2.0"))

    def test_an_unknown_current_version_is_never_out_of_date(self):
        self.assertFalse(is_newer("9.9.9", None))

    def test_the_running_version_comes_from_the_package(self):
        self.assertIsNotNone(parse_version(updates.current_version()))


def github_reply(data):
    """A stand-in for urlopen's response, usable as a context manager."""
    return mock.MagicMock(__enter__=lambda self: io.StringIO(json.dumps(data)))


def build(platform: str, frozen: bool = True, flatpak: bool = False):
    """Patches that make timeline.updates see a particular kind of build."""
    return [
        mock.patch.object(updates.sys, "platform", platform),
        mock.patch.object(updates.sys, "frozen", frozen, create=True),
        mock.patch.dict(updates.os.environ, {"FLATPAK_ID": "x"} if flatpak else {}, clear=True),
        mock.patch.object(updates.Path, "exists", return_value=False),
    ]


class LatestReleaseTest(unittest.TestCase):
    def latest(self, data, *patches):
        with mock.patch("urllib.request.urlopen", return_value=github_reply(data)) as urlopen:
            for patch in patches:
                patch.start()
            try:
                return latest_release(), urlopen
            finally:
                for patch in patches:
                    patch.stop()

    def test_returns_the_release(self):
        release, _ = self.latest({"tag_name": "v0.3.0", "html_url": PAGE})
        self.assertEqual((release.version, release.url), ("0.3.0", PAGE))

    def test_links_straight_to_this_builds_download(self):
        data = {"tag_name": "v0.3.0", "html_url": PAGE, "assets": ASSETS}
        for platform, flatpak, name in (("win32", False, "ClipSake-Setup-0.3.0.exe"),
                                        ("darwin", False, "ClipSake-0.3.0.dmg"),
                                        ("linux", True, "ClipSake-0.3.0.flatpak")):
            with self.subTest(platform=platform):
                release, _ = self.latest(data, *build(platform, frozen=not flatpak, flatpak=flatpak))
                self.assertEqual(release.download_url, f"{FILES}/{name}")

    def test_from_source_there_is_no_download(self):
        data = {"tag_name": "v0.3.0", "html_url": PAGE, "assets": ASSETS}
        release, _ = self.latest(data, *build("win32", frozen=False))
        self.assertIsNone(release.download_url)
        self.assertEqual(release.url, PAGE)  # the release notes are still offered

    def test_a_release_without_this_builds_file_links_only_to_its_page(self):
        data = {"tag_name": "v0.3.0", "html_url": PAGE, "assets": ASSETS[1:]}  # no .exe
        release, _ = self.latest(data, *build("win32"))
        self.assertIsNone(release.download_url)

    def test_only_links_to_this_repositorys_releases(self):
        release, _ = self.latest({"tag_name": "v0.3.0", "html_url": "https://example.com/download"})
        self.assertIsNone(release)
        elsewhere = [{"browser_download_url": "https://example.com/ClipSake-Setup-0.3.0.exe"}]
        release, _ = self.latest({"tag_name": "v0.3.0", "html_url": PAGE, "assets": elsewhere}, *build("win32"))
        self.assertIsNone(release.download_url)

    def test_errors_mean_no_release(self):
        not_found = urllib.error.HTTPError("url", 404, "Not Found", {}, None)  # e.g. a private repository
        for error in (not_found, urllib.error.URLError("offline"), TimeoutError()):
            with self.subTest(error=error), mock.patch("urllib.request.urlopen", side_effect=error):
                self.assertIsNone(latest_release())

    def test_unexpected_replies_mean_no_release(self):
        for data in ([], {"tag_name": 3, "html_url": PAGE}, {"html_url": PAGE}):
            with self.subTest(data=data):
                self.assertIsNone(self.latest(data)[0])

    def test_checks_the_certificate_with_certifi(self):
        _, urlopen = self.latest({"tag_name": "v0.3.0", "html_url": PAGE})
        self.assertIn("context", urlopen.call_args.kwargs)


class BannerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        ini = str(Path(tempfile.mkdtemp()) / "settings.ini")
        patch = mock.patch.object(app, "QSettings", lambda *_args: QSettings(ini, QSettings.IniFormat))
        patch.start()
        self.addCleanup(patch.stop)
        self.window = app.Window({})
        self.addCleanup(self.window.close)

    def test_offers_a_download_link_for_a_newer_release(self):
        self.window.on_newer_release(Release("0.3.0", PAGE, f"{FILES}/ClipSake-Setup-0.3.0.exe"))
        self.assertFalse(self.window.update_banner.isHidden())
        self.assertIn("0.3.0 is available", self.window.update_label.text())
        self.assertFalse(self.window.download_button.isHidden())
        with mock.patch.object(app.QDesktopServices, "openUrl") as open_url:
            self.window.download_button.click()
        self.assertEqual(open_url.call_args.args[0].toString(), f"{FILES}/ClipSake-Setup-0.3.0.exe")

    def test_from_source_says_how_to_update_instead(self):
        self.window.on_newer_release(Release("0.3.0", PAGE, None))
        self.assertTrue(self.window.download_button.isHidden())
        self.assertIn("uv sync --locked", self.window.update_label.text())

    def test_a_skipped_version_is_not_offered_again(self):
        release = Release("0.3.0", PAGE, None)
        self.window.on_newer_release(release)
        self.window.skip_release()
        self.assertTrue(self.window.update_banner.isHidden())
        self.window.on_newer_release(release)
        self.assertTrue(self.window.update_banner.isHidden())

    def test_turned_off_it_never_asks_github(self):
        self.window.update_check.setChecked(False)
        with mock.patch.object(app, "latest_release") as latest:
            self.window.check_for_updates()
        latest.assert_not_called()


if __name__ == "__main__":
    unittest.main()
