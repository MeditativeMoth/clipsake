# PyInstaller build spec. From the project root (build_windows.ps1 and
# build_macos.sh run it):
#     uv run --locked pyinstaller --noconfirm clipsake.spec
# The app is written to dist/ClipSake/ (ClipSake.exe), and on macOS also
# wrapped as dist/ClipSake.app.

import importlib.util
import shutil
from importlib.metadata import version
import site
import sys
from pathlib import Path

# Only build from the project's venv. Outside one, Python also loads packages
# from the user's personal folder (pip install --user), and those would be
# bundled instead of the locked, audited versions in uv.lock.
if sys.prefix == sys.base_prefix or site.ENABLE_USER_SITE:
    raise SystemExit(
        "Refusing to build: not running in the project's venv with user site-packages "
        f"disabled (prefix={sys.prefix}). Run `uv sync --locked`, then "
        "`uv run --locked pyinstaller --noconfirm clipsake.spec`; "
        "if it still fails, delete .venv and sync again."
    )

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

APP_NAME = "ClipSake"
exe_suffix = ".exe" if sys.platform == "win32" else ""

# deno is installed next to the venv's Python by the `deno` package; yt-dlp
# needs it to solve YouTube's JavaScript challenges, so it is bundled
# (runtime.py puts it on PATH).
deno = Path(sys.executable).parent / f"deno{exe_suffix}"
if not deno.exists():
    deno = Path(shutil.which("deno") or "")
if not deno.is_file():
    raise SystemExit("deno not found; run `uv sync --locked` first")

# ypb, ffmpeg and ffprobe are the pinned builds, downloaded and checked
# against their SHA-256 by tools/pinned.py (tools/ isn't a package, so it's
# loaded from its path). They're bundled next to deno, and runtime.py puts the
# bundle first on PATH, so the built app always runs these tested copies.
_spec = importlib.util.spec_from_file_location("pinned_tools", Path("tools/pinned.py").resolve())
pinned_tools = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = pinned_tools  # dataclasses look the module up while it runs
_spec.loader.exec_module(pinned_tools)
programs = [deno] + [pinned_tools.fetch(name) for name in ("ypb", "ffmpeg", "ffprobe")]

datas = []
for package in ("yt_dlp_ejs", "tzdata"):
    datas += collect_data_files(package)  # JS challenge solvers, zone data
# The app's own package metadata, where timeline.updates reads its version.
datas += copy_metadata("clipsake")
datas.append(("timeline/check.svg", "timeline"))  # the theme's checkbox tick
datas.append(("timeline/assets/clipsake.svg", "timeline/assets"))  # window icon
# The yt-dlp plugin that ypb's yt-dlp loads with --plugin-dirs; it must stay a .py file on disk.
_plugin = "timeline/ytdlp_plugins/clipsake/yt_dlp_plugins/postprocessor"
datas.append((f"{_plugin}/timeline_formats.py", _plugin))
if sys.platform == "darwin":
    # The licenses, inside the app (Contents/Resources); the Windows installer installs them itself.
    datas += [("LICENSE", "."), ("THIRD_PARTY_NOTICES.md", "."), ("licenses/LGPL-3.0.txt", "licenses")]

a = Analysis(
    ["timeline/__main__.py"],
    pathex=["."],
    binaries=[(str(program), ".") for program in programs],
    datas=datas,
    # yt-dlp's own PyInstaller hook (found automatically) covers the yt-dlp program.
    hiddenimports=["timeline.streams", "timeline.clipper", "yt_dlp"],
    excludes=["PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtPdf", "tkinter"],
)

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    console=False,
    # The .exe's icon (Explorer, Start menu): timeline/assets/clipsake.svg as an .ico.
    # (On macOS the .app's icon, below, is used instead.)
    icon="timeline/assets/clipsake.ico" if sys.platform == "win32" else None,
)
# A second copy of the executable named yt-dlp, next to the first. ypb runs
# yt-dlp as a program found on PATH; __main__ sees this name and runs the
# bundled yt-dlp, so there's one yt-dlp, locked and audited with everything
# else. It's a console program, so on Windows the tools yt-dlp starts share
# its (hidden) console instead of each opening a window.
ytdlp_exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="yt-dlp",
    console=True,
)
coll = COLLECT(exe, ytdlp_exe, a.binaries, a.datas, name=APP_NAME)

if sys.platform == "darwin":
    # The macOS app: Contents/MacOS has both executables (the app and yt-dlp),
    # Contents/Frameworks the bundled programs and libraries.
    app_version = version("clipsake")
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon="timeline/assets/clipsake.icns",  # clipsake.svg as an .icns
        bundle_identifier="io.github.MeditativeMoth.ClipSake",  # the Flatpak's ID too
        version=app_version,
        info_plist={
            "CFBundleDisplayName": APP_NAME,
            "CFBundleVersion": app_version,
            "LSApplicationCategoryType": "public.app-category.video",
            "LSMinimumSystemVersion": "14.0",  # PySide6's wheels need macOS 14
            "NSHighResolutionCapable": True,
        },
    )
