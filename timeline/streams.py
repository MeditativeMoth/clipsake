"""Discover a channel's currently-live streams with yt-dlp.

Run as `python -m timeline.streams CHANNEL` to print results as JSON lines;
the GUI does this in a child process so a slow lookup can be cancelled.
"""

from __future__ import annotations

import base64
import json
import sys
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import yt_dlp

from timeline.runtime import hide_child_consoles, stop_with_app

_QUIET = {"quiet": True, "no_warnings": True, "skip_download": True}


@dataclass
class LiveStream:
    video_id: str
    title: str
    started_at: datetime | None
    thumbnail: bytes | None

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


def channel_streams_url(channel: str) -> str:
    """Accept @handle, bare handle, channel ID or any channel URL."""
    value = channel.strip().rstrip("/")
    if value.startswith(("http://", "https://")):
        base = value
    elif value.startswith("@"):
        base = f"https://www.youtube.com/{value}"
    elif value.startswith("UC") and len(value) == 24:
        base = f"https://www.youtube.com/channel/{value}"
    else:
        base = f"https://www.youtube.com/@{value}"
    for tab in ("/streams", "/live", "/videos", "/featured", "/shorts"):
        if base.endswith(tab):
            base = base[: -len(tab)]
    return base + "/streams"


def _fetch_thumbnail(video_id: str) -> bytes | None:
    try:
        url = f"https://i.ytimg.com/vi/{video_id}/mqdefault_live.jpg"
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.read()
    except OSError:
        return None


def _started_at(ydl: yt_dlp.YoutubeDL, url: str) -> datetime | None:
    try:
        info = ydl.extract_info(url, download=False, process=False)
    except yt_dlp.utils.DownloadError:
        return None
    timestamp = info.get("release_timestamp") or info.get("timestamp")
    return datetime.fromtimestamp(timestamp, UTC) if timestamp else None


def iter_live_streams(
    channel: str, scan_limit: int = 30, on_found: Callable[[int], None] | None = None
) -> Iterator[LiveStream]:
    """Yield the channel's streams that are live right now, newest first.

    `on_found` gets the number of live streams once the channel listing is in,
    before the slower per-stream details are fetched.
    """
    flat = _QUIET | {"extract_flat": "in_playlist", "playlistend": scan_limit}
    with yt_dlp.YoutubeDL(flat) as ydl:
        info = ydl.extract_info(channel_streams_url(channel), download=False)
    live = [e for e in info.get("entries") or [] if e and e.get("live_status") == "is_live"]
    if on_found:
        on_found(len(live))

    with yt_dlp.YoutubeDL(_QUIET) as ydl:
        for entry in live:
            stream = LiveStream(entry["id"], entry.get("title") or entry["id"], None, None)
            stream.started_at = _started_at(ydl, stream.url)
            stream.thumbnail = _fetch_thumbnail(stream.video_id)
            yield stream


def stream_to_json(stream: LiveStream) -> dict:
    return {
        "id": stream.video_id,
        "title": stream.title,
        "started_at": stream.started_at.timestamp() if stream.started_at else None,
        "thumbnail": base64.b64encode(stream.thumbnail).decode() if stream.thumbnail else None,
    }


def stream_from_json(data: dict) -> LiveStream:
    started_at = data.get("started_at")
    thumbnail = data.get("thumbnail")
    return LiveStream(
        data["id"],
        data["title"],
        datetime.fromtimestamp(started_at, UTC) if started_at else None,
        base64.b64decode(thumbnail) if thumbnail else None,
    )


def main() -> None:
    """Print `{"found": n}`, then one `{"stream": ...}` line per live stream."""

    def emit(message: dict) -> None:
        print(json.dumps(message), flush=True)

    hide_child_consoles()
    stop_with_app()  # SIGTERM if the app ends, even if it's killed (Linux)
    try:
        for stream in iter_live_streams(sys.argv[1], on_found=lambda count: emit({"found": count})):
            emit({"stream": stream_to_json(stream)})
    except Exception as exc:  # yt-dlp raises many types; all are user-facing
        print(exc, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
