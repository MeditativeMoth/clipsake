"""Check GitHub Releases for a newer version of the app.

The check is deliberately unauthenticated: credentials built into an app can be
extracted by anyone who has it. That means it only sees releases of a public
repository; for a private one GitHub answers 404, which looks the same as
"no releases yet".

The connection is verified against certifi's CA certificates rather than the
operating system's, so it behaves the same on every platform: Python's
standalone builds look for system certificates in fixed places that not every
system has, and a failure there would only ever show as "no update".
"""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
import urllib.request
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import certifi

RELEASES_REPO = "MeditativeMoth/clipsake"
LATEST_RELEASE_API = "https://api.github.com/repos/{repo}/releases/latest"
DISTRIBUTION = "clipsake"  # the name in pyproject.toml, which holds the version


@dataclass
class Release:
    version: str
    url: str  # the release's page on github.com
    download_url: str | None  # this build's file in the release (installer, disk image, Flatpak), if any


def current_version() -> str | None:
    """This app's version, from its package metadata (the built app bundles it; see the spec)."""
    try:
        return version(DISTRIBUTION)
    except PackageNotFoundError:
        return None


def parse_version(text: str) -> tuple[int, ...] | None:
    """`v0.2.0` or `0.2.0` -> (0, 2, 0); anything else (e.g. `0.2.0rc1`) -> None."""
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", text.strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def is_newer(candidate: str, current: str | None) -> bool:
    new, old = parse_version(candidate), parse_version(current or "")
    if new is None or old is None:
        return False
    width = max(len(new), len(old))
    return new + (0,) * (width - len(new)) > old + (0,) * (width - len(old))


def download_suffix() -> str | None:
    """The kind of release file this build is updated with, or None when running from source."""
    if os.environ.get("FLATPAK_ID") or Path("/.flatpak-info").exists():
        return ".flatpak"
    if getattr(sys, "frozen", False):
        return {"win32": ".exe", "darwin": ".dmg"}.get(sys.platform)
    return None


def latest_release(repo: str = RELEASES_REPO, timeout: float = 10) -> Release | None:
    """The newest published release, or None if there isn't one or it can't be read.

    GitHub's "latest release" endpoint never returns drafts or pre-releases.
    """
    request = urllib.request.Request(
        LATEST_RELEASE_API.format(repo=repo),
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"{DISTRIBUTION}/{current_version() or 'unknown'}",
        },
    )
    try:
        context = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            data = json.load(response)
    except (OSError, ValueError):  # network/HTTP errors (incl. 404) and bad JSON
        return None
    if not isinstance(data, dict):
        return None
    tag, url = data.get("tag_name"), data.get("html_url")
    # Only ever offer to open this repository's own release pages and files.
    pages = f"https://github.com/{repo}/releases/".lower()
    if not isinstance(tag, str) or not isinstance(url, str) or not url.lower().startswith(pages):
        return None
    return Release(tag.removeprefix("v"), url, download_link(data.get("assets"), repo))


def download_link(assets, repo: str = RELEASES_REPO) -> str | None:
    """The download URL of this build's file among a release's assets, if there is one."""
    suffix = download_suffix()
    if suffix is None or not isinstance(assets, list):
        return None
    files = f"https://github.com/{repo}/releases/download/".lower()
    for asset in assets:
        link = asset.get("browser_download_url") if isinstance(asset, dict) else None
        if isinstance(link, str) and link.lower().startswith(files) and link.lower().endswith(suffix):
            return link
    return None
