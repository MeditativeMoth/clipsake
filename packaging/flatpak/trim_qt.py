"""Remove the parts of PySide6 (Qt) the app doesn't use from the Flatpak.

The PySide6 wheels hold all of Qt: QML, 3D, Designer, charts and more, most
of the Flatpak's size. Run by the manifest after the wheels are installed,
in the Flatpak SDK:
    python3 trim_qt.py /app/lib/python3.X/site-packages/PySide6

It keeps the Python modules the app imports (USED_MODULES), the Qt plugins
in PLUGINS, and every Qt library those need (read from each file's ELF
NEEDED list, so that follows Qt's own dependencies); everything else in the
wheels' Qt goes, along with Qt's developer tools.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

# The PySide6 modules timeline/ imports. The Python modules for the other Qt
# libraries they need (e.g. QtNetwork) are kept as well.
USED_MODULES = {"QtCore", "QtGui", "QtWidgets", "QtMultimedia", "QtMultimediaWidgets"}

# Qt plugin folder -> the plugins kept in it (None: all of them).
PLUGINS: dict[str, set[str] | None] = {
    # Wayland, X11, and offscreen (no screen, for testing).
    "platforms": {"libqwayland.so", "libqxcb.so", "libqoffscreen.so"},
    "wayland-decoration-client": None,
    "wayland-graphics-integration-client": None,
    "wayland-shell-integration": None,
    "xcbglintegrations": None,
    # The desktop's file chooser (the Flatpak portal), not the GTK theme.
    "platformthemes": {"libqxdgdesktopportal.so"},
    "platforminputcontexts": {"libcomposeplatforminputcontextplugin.so", "libibusplatforminputcontextplugin.so"},
    "iconengines": None,  # SVG icons
    # The app's SVG icon, and the formats of video thumbnails.
    "imageformats": {"libqsvg.so", "libqico.so", "libqjpeg.so", "libqgif.so", "libqwebp.so"},
    "multimedia": None,  # FFmpeg, for clip previews
    "tls": None,
    "networkinformation": None,
}

# PySide6's developer tools and files for building against it.
TOOLS = [
    "assistant", "balsam", "balsamui", "designer", "linguist", "lrelease", "lupdate", "qmlformat",
    "qmllint", "qmlls", "qsb", "svgtoqml", "doc", "glue", "include", "lib", "scripts", "typesystems",
]
QT_FOLDERS = ["libexec", "metatypes", "qml"]  # Qt's build tools, type info and QML modules
TRANSLATIONS = re.compile(r"(qt|qtbase|qtmultimedia)_[a-zA-Z_]+\.qm")  # Qt's own dialogs and errors


def needed(path: Path) -> list[str]:
    """The libraries `path` (an ELF file) links to."""
    dynamic = subprocess.run(["readelf", "--dynamic", "--wide", str(path)],
                             check=True, capture_output=True, text=True).stdout
    return re.findall(r"\(NEEDED\)\s+Shared library: \[([^\]]+)\]", dynamic)


def module_library(module: str) -> str:
    return f"libQt6{module.removeprefix('Qt')}.so.6"  # QtNetwork -> libQt6Network.so.6


def remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def trim(pyside: Path) -> None:
    qt = pyside / "Qt"
    libraries = {path.name for path in (qt / "lib").iterdir()}
    modules = {path.name.split(".")[0]: path for path in pyside.glob("Qt*.abi3.so")}

    kept_plugins = []
    for folder in (qt / "plugins").iterdir():
        wanted = PLUGINS.get(folder.name, set())
        for plugin in folder.iterdir():
            if wanted is None or plugin.name in wanted:
                kept_plugins.append(plugin)
    if missing := {f"{folder}/{name}" for folder, names in PLUGINS.items() for name in names or ()} - {
            f"{plugin.parent.name}/{plugin.name}" for plugin in kept_plugins}:
        raise SystemExit(f"Not in this PySide6: {', '.join(sorted(missing))} (see PLUGINS)")

    # Every library the kept modules and plugins need. A Python module also needs the Python
    # modules of the Qt libraries it links to (QtMultimedia imports QtNetwork), but a library
    # doesn't (the FFmpeg plugin links to Qt Quick, so the library stays, not its Python module).
    kept_modules = set(USED_MODULES)
    kept_libraries: set[str] = set()
    to_check = [modules[name] for name in kept_modules] + kept_plugins + list(pyside.glob("libpyside6.abi3.so*"))
    while to_check:
        path = to_check.pop()
        for library in needed(path):
            if library in libraries and library not in kept_libraries:
                kept_libraries.add(library)
                to_check.append(qt / "lib" / library)
            module = next((name for name in modules if module_library(name) == library), None)
            if path.name.endswith(".abi3.so") and module and module not in kept_modules:
                kept_modules.add(module)
                to_check.append(modules[module])

    for name, path in modules.items():
        if name not in kept_modules:
            path.unlink()
            path.with_suffix("").with_suffix(".pyi").unlink(missing_ok=True)  # QtX.abi3.so -> QtX.pyi
    if "QtQml" not in kept_modules:
        for path in pyside.glob("libpyside6qml.abi3.so*"):
            path.unlink()
    for name in libraries - kept_libraries:
        remove(qt / "lib" / name)
    for folder in (qt / "plugins").iterdir():
        for plugin in folder.iterdir():
            if plugin not in kept_plugins:
                remove(plugin)
        if not any(folder.iterdir()):
            folder.rmdir()
    for translation in (qt / "translations").iterdir():
        if not TRANSLATIONS.fullmatch(translation.name):
            remove(translation)
    for name in TOOLS:
        if (pyside / name).exists():
            remove(pyside / name)
    for name in QT_FOLDERS:
        if (qt / name).exists():
            remove(qt / name)
    print(f"Kept {', '.join(sorted(kept_modules))} and {len(kept_libraries)} of {len(libraries)} Qt libraries")


if __name__ == "__main__":
    trim(Path(sys.argv[1]))
