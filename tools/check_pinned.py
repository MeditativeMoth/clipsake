"""Check whether newer releases exist of the programs pinned in tools/pinned.py.

ypb and ffmpeg aren't Python packages, so pip-audit can't see
them. To check:
    uv run --locked python tools/check_pinned.py [--create-issues]
It prints what it finds. With --create-issues it also opens a GitHub issue
(with the gh CLI) for each finding, unless an issue with the same title
already exists, open or closed, so each new version is reported once.

It only reports; updating a pin stays a manual step (the version, URLs and
SHA-256s in tools/pinned.py), because a new ypb or ffmpeg should be tried on
real streams before it ships.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import certifi

TOOLS = Path(__file__).resolve().parent
REPO_PINNED = "tools/pinned.py"
GITHUB_API = "https://api.github.com"
RIEDL_HISTORY = "https://ffmpeg.martin-riedl.de/info/history/macos/arm64/release"
# BtbN keeps a month's last build for two years; warn this long before.
BTBN_KEPT_DAYS = 730
BTBN_WARN_DAYS = 90

BTBN_ASSET = re.compile(r"^ffmpeg-n(\d+(?:\.\d+)+)-.*-(win64|linux64)-gpl-[\d.]+\.(?:zip|tar\.xz)$")
RIEDL_ENTRY = re.compile(r"\d{10}_(\d+(?:\.\d+)+)")
BTBN_TAG = re.compile(r"autobuild-(\d{4})-(\d\d)-(\d\d)-")


def load_pinned():
    """tools/pinned.py (tools/ isn't a package, so it's loaded from its path)."""
    spec = importlib.util.spec_from_file_location("pinned_tools", TOOLS / "pinned.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up while it runs
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class Finding:
    title: str  # also what identifies it: one issue per title
    body: str


def version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.removeprefix("v").split("."))


def newer(candidate: str, current: str) -> bool:
    new, old = version_tuple(candidate), version_tuple(current)
    width = max(len(new), len(old))
    return new + (0,) * (width - len(new)) > old + (0,) * (width - len(old))


def fetch(url: str, *, github: bool = False) -> str:
    headers = {"User-Agent": "clipsake-check (+https://github.com/MeditativeMoth/clipsake)"}
    if github:
        headers["Accept"] = "application/vnd.github+json"
        if token := os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
            headers["Authorization"] = f"Bearer {token}"
    context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30, context=context) as response:
        return response.read().decode("utf-8", "replace")


def btbn_release_version(asset_names: list[str]) -> str | None:
    """The newest FFmpeg release that BtbN builds statically for both Windows and Linux."""
    platforms: dict[str, set[str]] = {}
    for name in asset_names:
        if (match := BTBN_ASSET.match(name)) and "shared" not in name:
            platforms.setdefault(match.group(1), set()).add(match.group(2))
    both = [version for version, found in platforms.items() if found == {"win64", "linux64"}]
    return max(both, key=version_tuple, default=None)


def riedl_release_version(history_html: str) -> str | None:
    return max(RIEDL_ENTRY.findall(history_html), key=version_tuple, default=None)


def btbn_build_date(url: str) -> date | None:
    match = BTBN_TAG.search(url)
    return date(*map(int, match.groups())) if match else None


def check(pinned, today: date, sources: dict[str, object]) -> list[Finding]:
    """Compare the pins with what's upstream. `sources` holds what was fetched."""
    findings = []
    ypb, ffmpeg = pinned.TOOLS["ypb"], pinned.TOOLS["ffmpeg"]

    if (release := sources.get("ypb")) and newer(release["tag_name"], ypb.version):
        version = release["tag_name"].removeprefix("v")
        findings.append(Finding(
            f"Update ypb to {version}",
            f"ypb {version} is out; {REPO_PINNED} pins {ypb.version}.\n\n"
            f"- Release notes: {release['html_url']}\n"
            f"- SHA-256s: `ypb_{version}_checksums.txt` in that release\n\n"
            "Update the version, URLs and SHA-256s in `tools/pinned.py` and the version and source "
            "links in `THIRD_PARTY_NOTICES.md`, then open a stream, scrub "
            "the timeline, play a preview and make a few clips (precise and not, and a cancel) "
            "before releasing.",
        ))

    btbn = sources.get("btbn")
    if btbn and (version := btbn_release_version(btbn["assets"])) and newer(version, ffmpeg.version):
        findings.append(Finding(
            f"Update ffmpeg (Windows and Linux) to {version}",
            f"BtbN builds FFmpeg {version}; {REPO_PINNED} pins {ffmpeg.version}.\n\n"
            f"- Newest build: {btbn['html_url']}\n"
            "- Pin a month's **last** `autobuild-…` release (kept for two years), not a daily one, "
            "with the SHA-256 GitHub shows for each file. ffprobe comes from the same archive, so "
            "`BTBN_WIN64` and `BTBN_LINUX64` update both. Update `THIRD_PARTY_NOTICES.md` too.\n"
            "- Check https://ffmpeg.org/security.html for what the release fixes.",
        ))

    if (html := sources.get("riedl")) and (version := riedl_release_version(html)) and newer(version, ffmpeg.version):
        findings.append(Finding(
            f"Update ffmpeg (macOS) to {version}",
            f"Martin Riedl's builds have FFmpeg {version} for macOS (Apple Silicon); {REPO_PINNED} pins "
            f"{ffmpeg.version}.\n\n"
            f"- Release history: {RIEDL_HISTORY}\n"
            "- Use the version's `ffmpeg.zip` and `ffprobe.zip`, each with the `.sha256` file next to it, "
            "and update `THIRD_PARTY_NOTICES.md` too.",
        ))

    for (platform, machine), build in ffmpeg.builds.items():
        built = btbn_build_date(build.url)
        if built is None:
            continue
        left = BTBN_KEPT_DAYS - (today - built).days
        if left <= BTBN_WARN_DAYS:
            findings.append(Finding(
                f"Pinned BtbN ffmpeg build from {built} expires soon",
                f"BtbN deletes monthly builds after two years, so the build {REPO_PINNED} pins for "
                f"{platform} ({machine}) will disappear in about {max(left, 0)} days, and builds will "
                "fail to download it. Pin a newer month's last build.",
            ))
            break  # one finding covers both platforms, which share the build
    return findings


def gather() -> dict[str, object]:
    """Fetch the upstream information `check` compares against."""
    releases = json.loads(fetch(f"{GITHUB_API}/repos/BtbN/FFmpeg-Builds/releases?per_page=20", github=True))
    newest = next((r for r in releases if r["tag_name"].startswith("autobuild-")), None)
    return {
        "ypb": json.loads(fetch(f"{GITHUB_API}/repos/xymaxim/ypb/releases/latest", github=True)),
        "btbn": newest and {"html_url": newest["html_url"], "assets": [a["name"] for a in newest["assets"]]},
        "riedl": fetch(RIEDL_HISTORY),
    }


def existing_titles(repo: str) -> set[str]:
    """Titles of this repository's issues, open and closed (most recent 500)."""
    result = subprocess.run(
        ["gh", "issue", "list", "--repo", repo, "--state", "all", "--limit", "500", "--json", "title"],
        check=True, capture_output=True, text=True,
    )
    return {issue["title"] for issue in json.loads(result.stdout)}


def create_issues(findings: list[Finding], repo: str) -> None:
    known = existing_titles(repo)
    for finding in findings:
        if finding.title in known:
            print(f"Already reported: {finding.title}")
            continue
        subprocess.run(
            ["gh", "issue", "create", "--repo", repo, "--title", finding.title, "--body", finding.body], check=True
        )
        print(f"Opened an issue: {finding.title}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--create-issues", action="store_true", help="open a GitHub issue for each finding")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "MeditativeMoth/clipsake"))
    args = parser.parse_args()

    findings = check(load_pinned(), datetime.now(UTC).date(), gather())
    if not findings:
        print("The pinned ypb and ffmpeg builds are the newest releases.")
    for finding in findings:
        print(f"- {finding.title}")
    if args.create_issues and findings:
        create_issues(findings, args.repo)


if __name__ == "__main__":
    main()
