"""Stills from any past moment of a YouTube live stream, through ypb's playback server.

`ypb serve` serves a stream's media segments by number
(/segments/itag/{itag}/sq/{sq}). YouTube's segments are all the same length
(e.g. 5 s), numbered from 0 at the start of the stream, and each starts with
a text header that gives its number and the wall-clock time it begins:

    Sequence-Number: 152711
    Ingestion-Walltime-Us: 1793444690103580
    Target-Duration-Us: 5000000

So the segment holding a moment can be estimated from any segment already
seen, then fetched and corrected from its header; usually the first guess is
right. That's much faster than asking ypb to locate the moment (/mpd/...,
about a second, since ypb runs ffprobe). A still is then one segment
download plus one ffmpeg decode.

Measured on a 1080p stream (NASA's ISS view):
- 144p segment: ~0.03 s; 360p: ~0.1-0.3 s; 720p: ~0.3-0.5 s.
- Decoding one frame: 0.04-0.07 s at 360p, up to 0.22 s at 720p60 (it has to
  decode from the segment's start to the offset).
- A segment that doesn't exist (past the live edge, or older than YouTube
  keeps) takes ypb about 7 s to give up on, so those are avoided.

Times are plain floats: seconds since the Unix epoch (UTC).
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from collections.abc import Callable

from timeline.clipper import ANY_PORT, HOST, stream_arguments
from timeline.runtime import kill_process_tree, stop_with_this_process

HEADER_FIELD = re.compile(rb"(Sequence-Number|Ingestion-Walltime-Us|Target-Duration-Us): (\d+)")
REPRESENTATION = re.compile(r"<Representation ([^>]*)>")
ATTRIBUTE = re.compile(r'(\w+)="([^"]*)"')
START_NUMBER = re.compile(r'startNumber="(\d+)"')
# How ypb serve says where it listens: "(<<) Playback started and listening on http://127.0.0.1:52811..."
LISTENING = re.compile(rf"listening on http://{re.escape(HOST)}:(\d+)\b")
VIDEO_ID = re.compile(r"(?:[?&]v=|youtu\.be/|/live/|/shorts/|/embed/)([\w-]{11})")
BARE_ID = re.compile(r"^[\w-]{11}$")

SEGMENT_TIMEOUT = 6.0  # seconds; a segment that exists arrives in well under one
PROBE_TIMEOUT = 2.0  # when only checking whether a segment exists
READY_TIMEOUT = 120.0  # for ypb serve to start (it runs yt-dlp first)
CACHE_SEGMENTS = 400  # small (preview-quality) segments kept in memory, ~35 KB each
MAX_STEPS = 8  # segments looked at to locate a moment; one or two is usual
# For requests to ypb on this computer: never through a proxy from http_proxy and the like,
# which would otherwise get them (urllib only skips hosts listed in no_proxy).
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PLAYBACK_WORKERS = 6  # segments downloaded at once for a clip preview
# YouTube keeps about the last 7 days of a live stream. Asking for an older
# segment gets a 503, which ypb takes for a passing error and retries five
# more times over ~45 s (it has no option not to), so the search for the
# oldest segment avoids asking for missing ones where it can.
KEPT = 7 * 86400  # seconds
OLDEST_MARGIN = 600  # first look this far inside the 7 days, where segments should exist
OLDEST_STEP = 1800  # then step back this far at a time until one is missing
OLDEST_MAX_STEPS = 8


class Missing(Exception):
    """The server couldn't provide a segment (not yet streamed, or too old)."""


def parse_video_id(text: str) -> str | None:
    text = text.strip()
    if BARE_ID.match(text):
        return text
    match = VIDEO_ID.search(text)
    return match.group(1) if match else None


def parse_time(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


@dataclass(frozen=True)
class Segment:
    sq: int
    start: float
    duration: float

    @property
    def end(self) -> float:
        return self.start + self.duration


def parse_segment_header(data: bytes) -> Segment | None:
    fields = dict(HEADER_FIELD.findall(data[:4096]))
    try:
        return Segment(
            int(fields[b"Sequence-Number"]),
            int(fields[b"Ingestion-Walltime-Us"]) / 1e6,
            int(fields.get(b"Target-Duration-Us", b"5000000")) / 1e6,
        )
    except KeyError:
        return None


@dataclass(frozen=True)
class VideoFormat:
    itag: str
    codecs: str
    width: int
    height: int
    fps: float

    @property
    def label(self) -> str:
        codec = self.codecs.split(".")[0].replace("avc1", "H.264").replace("av01", "AV1").replace("vp9", "VP9")
        return f"{self.height}p{self.fps:g} {codec}"


def parse_formats(mpd: str) -> list[VideoFormat]:
    """The video formats listed in one of ypb's MPEG-DASH manifests."""
    formats = []
    for attributes in REPRESENTATION.findall(mpd):
        values = dict(ATTRIBUTE.findall(attributes))
        if "width" in values and "height" in values:
            formats.append(VideoFormat(
                values["id"], values.get("codecs", ""), int(values["width"]), int(values["height"]),
                float(values.get("frameRate", "30")),
            ))
    return sorted(formats, key=lambda f: (f.height, f.fps, not f.codecs.startswith("avc1")))


def parse_audio_itag(mpd: str) -> str | None:
    """The best AAC audio format in a manifest (e.g. 140), or None for a stream without sound."""
    best = None
    for attributes in REPRESENTATION.findall(mpd):
        values = dict(ATTRIBUTE.findall(attributes))
        if (values.get("codecs", "").startswith("mp4a") and "width" not in values
                and (best is None or int(values.get("bandwidth", 0)) > int(best.get("bandwidth", 0)))):
            best = values
    return best["id"] if best else None


@dataclass(frozen=True)
class Playback:
    """A clip preview: a file of the whole segments covering clip_start-clip_end (stream
    times), joined end to end. Converts between stream times and positions in the file
    (seconds), allowing for outages: the file's time runs on without them."""
    path: Path
    clip_start: float
    clip_end: float
    fmt: VideoFormat
    segments: tuple[Segment, ...]
    fetch_s: float

    @property
    def start(self) -> float:
        """Where the clip starts in the file."""
        return self.position(self.clip_start)

    @property
    def end(self) -> float:
        """Where the clip ends in the file."""
        return self.position(self.clip_end)

    def position(self, moment: float) -> float:
        """Where the stream time `moment` is in the file."""
        return media_offset(self.segments, moment)

    def moment(self, position: float) -> float:
        """The stream time at `position` in the file."""
        return moment_at(self.segments, position)


def media_offset(segments, moment: float) -> float:
    """Where `moment` falls in the file made by joining `segments` (their media time runs on
    without the stream's outages; a moment in an outage gives where the next segment starts)."""
    elapsed = 0.0
    for segment in segments:
        if moment < segment.end:
            return elapsed + max(moment - segment.start, 0.0)
        elapsed += segment.duration
    return elapsed


def moment_at(segments, position: float) -> float:
    """The stream time shown at `position` in the file made by joining `segments`
    (the inverse of media_offset)."""
    elapsed = 0.0
    for segment in segments:
        if position < elapsed + segment.duration:
            return segment.start + max(position - elapsed, 0.0)
        elapsed += segment.duration
    return segments[-1].end if segments else position


def choose_formats(formats: list[VideoFormat]) -> tuple[VideoFormat, VideoFormat]:
    """(fast, sharp): the smallest format, for dragging and thumbnails, and a
    720p-or-smaller one for the still shown once the mouse stops. H.264 when
    there is one, since it decodes fastest."""
    avc = [f for f in formats if f.codecs.startswith("avc1")] or formats
    fast = min(avc, key=lambda f: f.height)
    sharp = max([f for f in avc if f.height <= 720] or avc, key=lambda f: (f.height, f.fps))
    return fast, sharp


def estimate_sq(anchors: list[Segment], moment: float) -> int:
    """The segment number most likely to hold `moment`, from the nearest known segment."""
    anchor = min(anchors, key=lambda s: abs(s.start - moment))
    return max(0, anchor.sq + math.floor((moment - anchor.start) / anchor.duration))


@dataclass
class Still:
    moment: float  # the time asked for
    segment: Segment
    offset: float  # seconds into the segment
    format: VideoFormat
    image: bytes
    fetches: int  # segment headers fetched to locate it (0 = already known)
    locate_s: float
    fetch_s: float
    decode_s: float

    def summary(self) -> str:
        return (
            f"{self.format.label}: locate {self.locate_s * 1000:.0f} ms ({self.fetches} fetches), "
            f"download {self.fetch_s * 1000:.0f} ms, decode {self.decode_s * 1000:.0f} ms, "
            f"total {(self.locate_s + self.fetch_s + self.decode_s) * 1000:.0f} ms"
        )


class Server:
    """A `ypb serve` process for one stream, on a free local port. It stops when the app does,
    even if the app is killed (on Linux; see runtime.stop_with_this_process), so it must be
    created from the main thread."""

    def __init__(self, video_id: str, env: dict[str, str], on_line: Callable[[str], None]) -> None:
        if threading.current_thread() is not threading.main_thread():
            # Linux would stop ypb as soon as this thread ended.
            raise RuntimeError("ypb serve must be started from the main thread")
        self.video_id = video_id
        # Where ypb listens, once it has said (see LISTENING): it picks a free port itself.
        self.port: int | None = None
        self.base: str | None = None
        ypb = shutil.which("ypb", path=env.get("PATH")) or "ypb"
        command = [ypb, "serve", "--host", HOST, "--port", ANY_PORT, *stream_arguments(video_id)]
        if os.name == "nt":
            platform = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
        else:
            platform = {
                "start_new_session": True,  # so the stop kills yt-dlp too, if still running
                "preexec_fn": stop_with_this_process(),
            }
        self.lines: list[str] = []
        self.process = subprocess.Popen(
            command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", **platform,
        )

        def read() -> None:
            for line in self.process.stdout:
                line = line.rstrip()
                if self.base is None and (listening := LISTENING.search(line)):
                    self.port = int(listening.group(1))
                    self.base = f"http://{HOST}:{self.port}"
                self.lines = (self.lines + [line])[-20:]
                on_line(line)

        threading.Thread(target=read, daemon=True).start()

    def get(self, path: str, timeout: float, accept: str | None = None) -> bytes:
        if self.base is None:
            raise Missing("ypb hasn't said where it listens yet")  # wait_ready waits for it
        request = urllib.request.Request(self.base + path, headers={"Accept": accept} if accept else {})
        try:
            with LOCAL.open(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            raise Missing(error.read().decode("utf-8", "replace").strip() or str(error)) from None
        except (urllib.error.URLError, TimeoutError) as error:
            raise Missing(str(error)) from None

    def wait_ready(self) -> dict:
        """Wait until the server answers; return the stream's /info."""
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                errors = [line for line in self.lines if "error" in line.lower()]
                raise RuntimeError((errors or self.lines or ["ypb stopped"])[-1])
            try:
                return json.loads(self.get("/info", timeout=2))
            except Missing:
                time.sleep(0.5)
        raise RuntimeError("ypb's playback server didn't start in time")

    def stop(self) -> None:
        if self.process.poll() is None:
            kill_process_tree(self.process.pid)
            self.process.kill()
        self.process.wait()


class Stream:
    """What's known about one stream's segments, and stills taken from them.

    Safe to use from several threads at once.
    """

    def __init__(self, server: Server, env: dict[str, str]) -> None:
        self.server = server
        self.ffmpeg = shutil.which("ffmpeg", path=env.get("PATH")) or "ffmpeg"
        self.info: dict = {}
        self.formats: list[VideoFormat] = []
        self.fast: VideoFormat | None = None
        self.sharp: VideoFormat | None = None
        self.audio_itag: str | None = None
        self.head: Segment | None = None  # newest segment seen
        self.earliest: Segment | None = None  # oldest still available, once found
        self._lock = threading.Lock()
        self._known: dict[int, Segment] = {}
        self._cache: OrderedDict[tuple[str, int], bytes] = OrderedDict()

    # Setup

    def open(self) -> None:
        """Wait for the server, then read the formats and the live edge."""
        self.info = self.server.wait_ready()
        self.refresh_head()

    def refresh_head(self) -> Segment:
        """Find the newest segment (via ypb's live manifest, ~1 s)."""
        data = json.loads(self.server.get("/mpd/now", timeout=30, accept="application/json"))
        mpd = data["mpd"]
        if not self.formats:
            self.formats = parse_formats(mpd)
            if not self.formats:
                raise RuntimeError("the stream has no video formats")
            self.fast, self.sharp = choose_formats(self.formats)
            self.audio_itag = parse_audio_itag(mpd)
        sq = int(START_NUMBER.search(mpd).group(1))
        # The manifest starts a few segments behind the edge; step forward
        # while newer ones exist, without waiting long on one that doesn't.
        head = self.segment(sq)
        for _ in range(6):
            try:
                head = self.segment(head.sq + 1, timeout=PROBE_TIMEOUT)
            except Missing:
                break
        with self._lock:
            if self.head is None or head.sq > self.head.sq:
                self.head = head
        return self.head

    def find_earliest(self) -> Segment:
        """The oldest segment YouTube still has (to within OLDEST_STEP), usually asking for
        only one missing segment (see KEPT).

        - A stream younger than KEPT still has segment 0.
        - Otherwise, check the segment from just inside KEPT ago, then step back from
          it until one is missing.
        - If either surprises (YouTube keeps less), binary-search instead, which asks
          for about half of its ~17 segments in vain.
        """
        head = self.head
        assert head is not None
        started = self.info.get("actualStartTime")
        if started and head.end - parse_time(started) < KEPT - OLDEST_MARGIN:
            if self.exists(0):
                self.earliest = self.segment(0)
                return self.earliest
            return self._bisect_earliest(0, head.sq)
        sq = max(estimate_sq([head], head.start - KEPT + OLDEST_MARGIN), 0)
        if not self.exists(sq):
            return self._bisect_earliest(sq, head.sq)
        present = self.segment(sq)
        for _ in range(OLDEST_MAX_STEPS):
            older = estimate_sq([present], present.start - OLDEST_STEP)
            if older < 0 or older >= present.sq or not self.exists(older):
                break
            present = self.segment(older)
        self.earliest = present
        return present

    def _bisect_earliest(self, missing: int, present: int) -> Segment:
        """Binary-search between a missing segment and a present later one."""
        while present - missing > 1:
            middle = (missing + present) // 2
            if self.exists(middle):
                present = middle
            else:
                missing = middle
        self.earliest = self.segment(present)
        return self.earliest

    def exists(self, sq: int) -> bool:
        try:
            self.segment(sq, timeout=PROBE_TIMEOUT)
            return True
        except Missing:
            return False

    # Segments

    def segment_bytes(self, fmt: VideoFormat, sq: int, timeout: float = SEGMENT_TIMEOUT) -> bytes:
        key = (fmt.itag, sq)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        data = self.server.get(f"/segments/itag/{fmt.itag}/sq/{sq}", timeout=timeout)
        segment = parse_segment_header(data)
        if segment is None:
            raise Missing(f"segment {sq} has no header")
        with self._lock:
            self._known[segment.sq] = segment
            if fmt == self.fast:  # only the small ones are worth keeping
                self._cache[key] = data
                while len(self._cache) > CACHE_SEGMENTS:
                    self._cache.popitem(last=False)
        return data

    def segment(self, sq: int, timeout: float = SEGMENT_TIMEOUT) -> Segment:
        with self._lock:
            if sq in self._known:
                return self._known[sq]
        self.segment_bytes(self.fast, sq, timeout)
        with self._lock:
            return self._known[sq]

    def locate(self, moment: float) -> tuple[Segment, int]:
        """The segment holding `moment`, and how many headers were fetched to find it.

        Guesses from the nearest known segment, then corrects from each
        fetched header, narrowing the range between the closest segments seen
        either side. If the stream was down at `moment` (a gap between two
        consecutive segments), returns whichever side is nearer.
        """
        with self._lock:
            anchors = list(self._known.values())
            head, earliest = self.head, self.earliest
        if head:
            moment = min(moment, head.end - 0.001)
        if earliest:
            moment = max(moment, earliest.start)
        sq = estimate_sq(anchors, moment)
        before = after = None  # nearest segments seen starting before / after the moment
        fetches = 0
        for _ in range(MAX_STEPS):
            if head:
                sq = min(sq, head.sq)
            with self._lock:
                known = sq in self._known
            segment = self.segment(sq)
            fetches += not known
            if segment.start <= moment:
                if moment < segment.end:
                    return segment, fetches
                before = segment
            else:
                after = segment
            if before and after and after.sq - before.sq <= 1:  # in a gap
                nearer = before if moment - before.end <= after.start - moment else after
                return nearer, fetches
            guess = sq + math.floor((moment - segment.start) / segment.duration)
            lowest = before.sq + 1 if before else 0
            highest = after.sq - 1 if after else guess
            if before and after and not lowest <= guess <= highest:
                sq = (lowest + highest) // 2  # the guess is off (a gap): halve the range instead
            else:
                sq = min(max(guess, lowest), highest)
            if sq == segment.sq:
                break
        return segment, fetches

    # Stills

    def decode(self, data: bytes, offset: float) -> bytes:
        """One frame `offset` seconds into a segment, as a PPM image (no encoding
        cost, and Qt reads it directly)."""
        result = subprocess.run(
            [self.ffmpeg, "-v", "error", "-i", "pipe:0", "-ss", f"{offset:.3f}", "-frames:v", "1",
             "-f", "image2pipe", "-c:v", "ppm", "pipe:1"],
            input=data, capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode != 0 or not result.stdout:
            raise RuntimeError(result.stderr.decode("utf-8", "replace").strip() or "ffmpeg found no frame")
        return result.stdout

    def still(self, moment: float, fmt: VideoFormat, snap: bool = False) -> Still:
        """The frame at `moment` (or, with `snap`, at the start of its segment)."""
        began = time.perf_counter()
        segment, fetches = self.locate(moment)
        located = time.perf_counter()
        data = self.segment_bytes(fmt, segment.sq)
        fetched = time.perf_counter()
        # Stop at the segment's last frame (at 15 fps, 4.93 s into 5 s).
        last_frame = segment.duration - 1.5 / fmt.fps
        offset = 0.0 if snap else min(max(moment - segment.start, 0.0), last_frame)
        try:
            image = self.decode(data, offset)
        except RuntimeError:
            if offset < 0.5:
                raise
            offset -= 0.5  # a segment cut short; take an earlier frame
            image = self.decode(data, offset)
        decoded = time.perf_counter()
        return Still(moment, segment, offset, fmt, image, fetches, located - began, fetched - located, decoded - fetched)

    # Clip previews

    def playback(self, start: float, end: float, fmt: VideoFormat, path: Path,
                 progress: Callable[[int, int], None] = lambda done, total: None,
                 cancelled: Callable[[], bool] = lambda: False, reuse: dict | None = None) -> Playback:
        """Download the segments covering start-end (video in `fmt`, plus sound) and join them
        into a file at `path` to play. `reuse` maps (itag, sq) to segment bytes already
        downloaded; it's updated to hold just this clip's segments, for next time."""
        began = time.perf_counter()
        first, _ = self.locate(start)
        last, _ = self.locate(end - 0.001)
        sqs = range(first.sq, max(last.sq, first.sq) + 1)
        reuse = {} if reuse is None else reuse
        itags = [fmt.itag] + ([self.audio_itag] if self.audio_itag else [])
        wanted = [(itag, sq) for itag in itags for sq in sqs]
        done = sum(key in reuse for key in wanted)
        progress(done, len(wanted))
        lock = threading.Lock()

        def fetch(key: tuple[str, int]) -> tuple[tuple[str, int], bytes]:
            nonlocal done
            if key in reuse:
                return key, reuse[key]
            if cancelled():
                raise Missing("cancelled")
            data = self.server.get(f"/segments/itag/{key[0]}/sq/{key[1]}", timeout=SEGMENT_TIMEOUT * 2)
            with lock:
                done += 1
                progress(done, len(wanted))
            return key, data

        with ThreadPoolExecutor(PLAYBACK_WORKERS) as pool:
            fetched = dict(pool.map(fetch, wanted))
        reuse.clear()
        reuse.update(fetched)
        if cancelled():
            raise Missing("cancelled")
        fetch_s = time.perf_counter() - began

        segments = [parse_segment_header(fetched[(fmt.itag, sq)]) for sq in sqs]
        if None in segments:
            raise Missing("a segment has no header")
        # YouTube's segments are fragments of one MP4 stream, so they join end to end;
        # ffmpeg then puts picture and sound in one file (copying, no re-encoding).
        inputs = []
        for itag in itags:
            part = path.with_name(f"{path.stem}.{itag}.mp4")
            part.write_bytes(b"".join(fetched[(itag, sq)] for sq in sqs))
            inputs.append(part)
        command = [self.ffmpeg, "-v", "error", "-y", "-i", str(inputs[0])]
        if len(inputs) > 1:
            command += ["-i", str(inputs[1]), "-map", "0:v:0", "-map", "1:a:0"]
        result = subprocess.run(command + ["-c", "copy", str(path)], capture_output=True,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        for part in inputs:
            part.unlink(missing_ok=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode("utf-8", "replace").strip() or "ffmpeg couldn't join the segments")
        return Playback(path, start, end, fmt, tuple(segments), fetch_s)
