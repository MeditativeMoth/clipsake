"""Write the Flatpak build's download lists from uv.lock and tools/pinned.py.

flatpak-builder downloads every file a build needs itself, checks each one's
SHA-256, then builds without network access. This writes those lists to
packaging/flatpak/:

- python-packages.json: the wheel of each locked package (uv.lock, without
  the dev group) that suits the Flatpak runtime's Python on x86-64 Linux;
- pinned-programs.json: the pinned ypb, ffmpeg and ffprobe builds.

build_flatpak.sh runs it before each build, so the Flatpak gets the same
versions as everything else. To run it by hand:
    uv run --locked python tools/flatpak_sources.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

from packaging.markers import Marker
from packaging.tags import Tag, compatible_tags, cpython_tags
from packaging.utils import parse_wheel_filename

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pinned  # noqa: E402

PROJECT = pinned.PROJECT
OUTPUT = PROJECT / "packaging" / "flatpak"

# The Flatpak runtime (org.gnome.Platform 51, in the manifest):
# its Python and glibc. Change these along with the runtime.
PYTHON = (3, 14)
GLIBC = (2, 44)
MACHINE = "x86_64"

MARKER_ENVIRONMENT = {
    "implementation_name": "cpython",
    "platform_python_implementation": "CPython",
    "python_version": f"{PYTHON[0]}.{PYTHON[1]}",
    "python_full_version": f"{PYTHON[0]}.{PYTHON[1]}.0",
    "sys_platform": "linux",
    "platform_system": "Linux",
    "platform_machine": MACHINE,
    "os_name": "posix",
}


def platforms() -> list[str]:
    """The wheel platform tags the runtime accepts, best first (as pip ranks them)."""
    tags = [f"manylinux_{GLIBC[0]}_{minor}_{MACHINE}" for minor in range(GLIBC[1], 16, -1)]
    return tags + [f"manylinux2014_{MACHINE}", f"manylinux2010_{MACHINE}", f"manylinux1_{MACHINE}"]


def supported_tags() -> list[Tag]:
    return [*cpython_tags(PYTHON, platforms=platforms()), *compatible_tags(PYTHON, f"cp{PYTHON[0]}{PYTHON[1]}", platforms())]


def locked_packages() -> list[dict]:
    exported = subprocess.run(
        ["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--format", "pylock.toml",
         "--quiet"],
        cwd=PROJECT, check=True, capture_output=True, text=True,
    ).stdout
    return tomllib.loads(exported)["packages"]


def best_wheel(package: dict, ranking: dict[Tag, int]) -> dict:
    def rank(wheel: dict) -> int:
        name = wheel["url"].rsplit("/", 1)[-1]
        tags = parse_wheel_filename(name)[3]
        return min((ranking[tag] for tag in tags if tag in ranking), default=len(ranking))

    wheels = [wheel for wheel in package.get("wheels", []) if rank(wheel) < len(ranking)]
    if not wheels:
        raise SystemExit(f"{package['name']} {package['version']} has no wheel for Python "
                         f"{MARKER_ENVIRONMENT['python_version']} on {MACHINE} Linux")
    return min(wheels, key=rank)


def python_packages() -> dict:
    ranking = {tag: index for index, tag in enumerate(supported_tags())}
    sources = []
    for package in locked_packages():
        if "marker" in package and not Marker(package["marker"]).evaluate(MARKER_ENVIRONMENT):
            continue
        wheel = best_wheel(package, ranking)
        sources.append({"type": "file", "url": wheel["url"], "sha256": wheel["hashes"]["sha256"]})
    return {
        "name": "python-packages",
        "buildsystem": "simple",
        "build-commands": [
            'pip3 install --no-index --no-deps --no-compile --prefix="$FLATPAK_DEST" *.whl',
        ],
        "sources": sources,
    }


def pinned_programs() -> dict:
    sources, commands, archives = [], [], {}
    for tool in pinned.TOOLS.values():
        build = tool.builds[("linux", MACHINE)]
        archive = build.url.rsplit("/", 1)[-1]
        if archive not in archives:  # ffmpeg and ffprobe come in one archive
            archives[archive] = tool.name
            sources.append({"type": "archive", "url": build.url, "sha256": build.sha256,
                            "strip-components": 0, "dest": archives[archive]})
        commands.append(f'install -Dm755 "$(find {archives[archive]} -type f -name {tool.name})" '
                        f'"$FLATPAK_DEST/bin/{tool.name}"')
    return {"name": "pinned-programs", "buildsystem": "simple", "build-commands": commands, "sources": sources}


def write(name: str, module: dict) -> None:
    path = OUTPUT / name
    text = json.dumps(module, indent=4) + "\n"
    if not path.exists() or path.read_text() != text:
        path.write_text(text)
        print(f"Updated {path.relative_to(PROJECT)}", file=sys.stderr)


def main() -> None:
    write("python-packages.json", python_packages())
    write("pinned-programs.json", pinned_programs())


if __name__ == "__main__":
    main()
