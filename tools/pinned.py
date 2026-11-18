"""Download the pinned builds of the programs the app runs, and check their SHA-256.

The app runs programs that aren't Python packages: ypb, which downloads
past moments of a live stream, ffmpeg, which joins and cuts the result, and
ffprobe, which ypb runs. To install them:
    uv run --locked python tools/pinned.py --install
which copies them to ~/.clipsake/bin, where the app looks for them
(copies already on PATH are used instead, if all three are there).

To update one, change its version and every entry in its builds below: the
URL and SHA-256 of the new archive for each platform. A download whose
checksum doesn't match is refused.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import ssl
import sys
import tarfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

import certifi

PROJECT = Path(__file__).resolve().parent.parent
CACHE = PROJECT / "build" / "pinned"
INSTALL_DIR = Path.home() / ".clipsake" / "bin"  # timeline.runtime.BIN_DIR
USER_AGENT = "clipsake-build (+https://github.com/MeditativeMoth/clipsake)"


@dataclass(frozen=True)
class Build:
    url: str
    sha256: str


@dataclass(frozen=True)
class Tool:
    name: str  # the program's name, without .exe
    version: str
    builds: dict[tuple[str, str], Build]  # (sys.platform, machine) -> build


BTBN = "https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-09-30-13-08"
YPB = "https://github.com/xymaxim/ypb/releases/download/v2026.11.12"
RIEDL = "https://ffmpeg.martin-riedl.de/download/macos/arm64/1789931890_9.0.2"

# Windows and Linux: BtbN's static GPL builds of the 9.0 release branch, from
# a month's last build (kept for two years). Each archive has both ffmpeg and
# ffprobe, so it's downloaded once for the two.
BTBN_WIN64 = Build(
    f"{BTBN}/ffmpeg-n9.0.2-17-g2a571b6068-win64-gpl-9.0.zip",
    "a0e45723c72141975f51d8666302e614711745f3102b704ca3f82c897a58d278",
)
BTBN_LINUX64 = Build(
    f"{BTBN}/ffmpeg-n9.0.2-17-g2a571b6068-linux64-gpl-9.0.tar.xz",
    "68ee646831adaae2495618346f3bba94ff207ff83bbd34d643e7004730d66269",
)

TOOLS = {
    # macOS (Apple Silicon): Martin Riedl's signed static release builds, one
    # archive per program; the SHA-256s are in <archive URL>.sha256.
    "ffmpeg": Tool("ffmpeg", "9.0.2", {
        ("win32", "amd64"): BTBN_WIN64,
        ("linux", "x86_64"): BTBN_LINUX64,
        ("darwin", "arm64"): Build(
            f"{RIEDL}/ffmpeg.zip",
            "c8ed4c4e6978a03c485edbfe4e0a5dc2380f8a30bba5150531b31b094492d924",
        ),
    }),
    # Not run by the app itself, but by ypb.
    "ffprobe": Tool("ffprobe", "9.0.2", {
        ("win32", "amd64"): BTBN_WIN64,
        ("linux", "x86_64"): BTBN_LINUX64,
        ("darwin", "arm64"): Build(
            f"{RIEDL}/ffprobe.zip",
            "fcbe839537485eaee7a7a8bc5cbc0f90d53617e80943e8a5b2e31cb851197ea6",
        ),
    }),
    # ypb's own release builds; the SHA-256s are in the release's
    # ypb_<version>_checksums.txt.
    "ypb": Tool("ypb", "2026.11.12", {
        ("win32", "amd64"): Build(
            f"{YPB}/ypb-v2026.11.12-windows-amd64.zip",
            "51025f181f53c8d8e523e55fd618140a07ecde37223408812c0c2b149fa8129a",
        ),
        ("linux", "x86_64"): Build(
            f"{YPB}/ypb-v2026.11.12-linux-amd64.zip",
            "d88af423c586bc5a905b30c3cbd8406fc584905157d627c39f4a897a11ccf5f4",
        ),
        ("darwin", "arm64"): Build(
            f"{YPB}/ypb-v2026.11.12-darwin-arm64.zip",
            "baab5a1069ce71476a8c732ef032a800381934fb5dc281fd5bb93cacaa519f2e",
        ),
    }),
}


def exe_name(tool: Tool) -> str:
    return f"{tool.name}.exe" if sys.platform == "win32" else tool.name


def current_build(tool: Tool) -> Build:
    key = (sys.platform, platform.machine().lower())
    if key not in tool.builds:
        raise SystemExit(f"No pinned {tool.name} build for {key[0]} on {key[1]}; see tools/pinned.py")
    return tool.builds[key]


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def download(build: Build, archive: Path) -> None:
    """Download `build` to `archive`, or fail if its checksum doesn't match."""
    if archive.exists() and sha256_of(archive) == build.sha256:
        return
    archive.parent.mkdir(parents=True, exist_ok=True)
    partial = archive.with_name(archive.name + ".part")
    print(f"Downloading {build.url}", file=sys.stderr)
    context = ssl.create_default_context(cafile=certifi.where())
    # Some download hosts (Cloudflare in front of Martin Riedl's builds)
    # refuse Python's default "Python-urllib" User-Agent with 403 Forbidden.
    request = urllib.request.Request(build.url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60, context=context) as response, partial.open("wb") as file:
        shutil.copyfileobj(response, file, 1 << 20)
    actual = sha256_of(partial)
    if actual != build.sha256:
        partial.unlink()
        raise SystemExit(f"Refusing {build.url}: SHA-256 is {actual}, expected {build.sha256}")
    partial.replace(archive)


def extract_program(archive: Path, target: Path) -> None:
    """Copy the one program named like `target` in `archive` (at any depth) to `target`."""
    name = target.name
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            members = [m for m in zf.namelist() if m.rsplit("/", 1)[-1] == name]
            if len(members) != 1:
                raise SystemExit(f"Expected one {name} in {archive.name}, found {members}")
            with zf.open(members[0]) as source, target.open("wb") as dest:
                shutil.copyfileobj(source, dest)
    else:
        with tarfile.open(archive) as tf:
            members = [m for m in tf.getmembers() if m.isfile() and m.name.rsplit("/", 1)[-1] == name]
            if len(members) != 1:
                raise SystemExit(f"Expected one {name} in {archive.name}, found {[m.name for m in members]}")
            with tf.extractfile(members[0]) as source, target.open("wb") as dest:
                shutil.copyfileobj(source, dest)
    target.chmod(0o755)


def fetch(name: str) -> Path:
    """The checked program `name` for this platform, downloading it if needed."""
    tool = TOOLS[name]
    build = current_build(tool)
    target = CACHE / tool.name / tool.version / exe_name(tool)
    stamp = target.with_name(target.name + ".sha256")  # which archive it came from
    if target.exists() and stamp.exists() and stamp.read_text().strip() == build.sha256:
        return target
    # Named after the archive, not the program, so ffmpeg and ffprobe share
    # one download where they come in one archive.
    archive = CACHE / "downloads" / build.url.rsplit("/", 1)[-1]
    download(build, archive)
    target.parent.mkdir(parents=True, exist_ok=True)
    extract_program(archive, target)
    stamp.write_text(build.sha256 + "\n")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("tools", nargs="*", metavar="TOOL", help=f"{' and/or '.join(TOOLS)} (default: all)")
    parser.add_argument("--install", action="store_true", help=f"also copy them to {INSTALL_DIR}")
    args = parser.parse_args()
    if unknown := set(args.tools) - set(TOOLS):
        parser.error(f"unknown tool(s): {', '.join(sorted(unknown))}")
    for name in args.tools or TOOLS:
        program = fetch(name)
        if args.install:
            INSTALL_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(program, INSTALL_DIR / program.name)
            program = INSTALL_DIR / program.name
        print(program)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONNOUSERSITE", "1")
    main()
