# Third-party software

ClipSake is free software under the GNU General Public License, version 3
or (at your option) any later version: see `LICENSE`. Its Windows installer,
Linux Flatpak and macOS app also include the software below, each under its
own license. The exact versions are the ones in `uv.lock` (Python packages)
and `tools/pinned.py` (programs).

## Programs the app runs

These are separate programs, run by the app, and included unchanged as
their projects publish them.

- **ypb** 2026.11.12, by xymaxim: GPL-3.0 (`LICENSE`).
  Source: <https://github.com/xymaxim/ypb/tree/v2026.11.12>
- On Windows and Linux, **FFmpeg** (`ffmpeg` and `ffprobe`) 9.0.2, from BtbN's
  static builds (`autobuild-2026-09-30-13-08`, FFmpeg commit `2a571b6068`):
  GPL-3.0 (`LICENSE`), since they include x264, x265 and other GPL libraries.
  Source: FFmpeg at <https://github.com/FFmpeg/FFmpeg/tree/2a571b6068>, and
  the build scripts that fetch and build each library at the exact versions
  used: <https://github.com/BtbN/FFmpeg-Builds/tree/autobuild-2026-09-30-13-08>
- On macOS, **FFmpeg** (`ffmpeg` and `ffprobe`) 9.0.2 from Martin Riedl's
  signed static builds (`1789931890_9.0.2`): GPL-3.0 (`LICENSE`), since they
  include x264, x265 and other GPL libraries. Source: FFmpeg 9.0.2 at
  <https://github.com/FFmpeg/FFmpeg/tree/n9.0.2>, and the build script that
  fetches and builds each library: <https://git.martin-riedl.de/ffmpeg/build-script>
- **Deno**, by the Deno authors: MIT. Source: <https://github.com/denoland/deno>
  (the version in `uv.lock`, from the `deno` package).

## Libraries

- **Qt** and **Qt for Python** (PySide6, Shiboken6), by The Qt Company:
  LGPL-3.0 (`licenses/LGPL-3.0.txt`, which adds to the GPL-3.0 in `LICENSE`).
  Qt ships as separate library files in both builds, so it can be replaced.
  Source: <https://download.qt.io/official_releases/QtForPython/> and
  <https://download.qt.io/official_releases/qt/> (the PySide6 version in
  `uv.lock`). Qt Multimedia includes FFmpeg's libraries, under the LGPL-2.1
  or later; their source: <https://ffmpeg.org/releases/>.
- **yt-dlp**: Unlicense (public domain). **yt-dlp-ejs**: Unlicense, with
  parts under MIT and ISC. Source: <https://github.com/yt-dlp/yt-dlp>,
  <https://github.com/yt-dlp/ejs>
- **requests**, **tzdata**: Apache-2.0.
- **certifi**: MPL-2.0. Source: <https://github.com/certifi/python-certifi>
- **urllib3**, **brotli**, **charset-normalizer**: MIT.
- **idna**, **websockets**: BSD-3-Clause.
- **pycryptodomex**: BSD-2-Clause, with parts in the public domain.

Every Python package's source is on PyPI, at <https://pypi.org/project/NAME/>
for the version in `uv.lock`.

## Windows installer and macOS app

- **Python**, by the Python Software Foundation: PSF License.
  Source: <https://www.python.org/downloads/source/>
- **PyInstaller**'s bootloader, which starts the app: GPL-2.0 or later, with
  an exception allowing it to start programs under any license.
  Source: <https://github.com/pyinstaller/pyinstaller>
- **Inno Setup** (Windows), which makes the installer, by Jordan Russell and Martijn
  Laan: Inno Setup License. <https://jrsoftware.org/isinfo.php>

The Linux Flatpak runs on GNOME's runtime from Flathub, which isn't part of
it.
