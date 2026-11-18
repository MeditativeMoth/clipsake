"""Clip a past moment of a live stream: ypb downloads it, ffmpeg finishes it.

Usage: python -m timeline.clipper --interval START/END --output CLIP.mp4 [--precise] VIDEO_ID

The GUI runs this as a child process and reads its output line by line:
    Progress: <done>/<total> <percent>%   while ypb downloads segments
    Saved to '<path>'                     when the clip is finished
Anything else is shown in the log.

ypb (https://github.com/xymaxim/ypb) finds the requested moments and has
yt-dlp download them, whole segments at a time, as H.264 + AAC in an MP4.
ffmpeg then either cuts that to the exact times (--precise) or just rewraps
it with its index at the front ("faststart") so playback starts at once.

ypb's own --cut isn't used: it copies the audio while seeking, which keeps a
few seconds from before the start and shifts the whole clip later, leaving a
gap at its beginning. Instead the cut uses the times ypb writes into the file
(InputStartTime, ActualStartTime, ...), and re-encodes the audio as well.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from timeline.runtime import hide_child_consoles, stop_with_app

# A yt-dlp plugin that drops formats ypb would crash on (see its docstring).
PLUGIN_DIR = Path(__file__).resolve().parent / "ytdlp_plugins"
PLUGIN = "DropIncompleteFormats:when=pre_process"

# Prefer H.264 (avc1) at up to 1080p, which plays in every player and editor,
# with AAC audio; fall back to whatever is best at up to 1080p.
VIDEO_FORMAT = "bv*[vcodec^=avc1][height<=1080]+ba[ext=m4a]/bv*[height<=1080]+ba/b"

FRAGMENT = re.compile(r"^\[download\].*\(frag (\d+)/(\d+)\)")
# Signed YouTube media URLs contain the user's IP address and are hundreds of
# characters long, so they never go into the log.
URL = re.compile(r"https?://[^\s\"'<>]+")
MAX_LINE = 400
# ypb's error when the end time is later than the stream has reached so far
# (a live stream runs a few seconds behind real time). Go prints times as
# "2026-10-30 12:09:15.484041 +0000 UTC"; the requested time also has "+00:00".
NOT_REACHED = re.compile(
    r"time (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?) ([+-]\d{4}) .*?"
    r"is after current moment: (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?) ([+-]\d{4})"
)
CATCH_UP_TRIES = 3  # how many times to wait for the stream to reach the end time
MAX_WAIT = 60.0  # seconds


# Where ypb's playback server listens: this computer only. On every
# interface (ypb's default), other machines on the network could reach it,
# and Windows would ask whether to let them.
HOST = "127.0.0.1"
# Port 0: the system picks a free one when ypb starts listening. A port chosen
# here beforehand could be taken by then, and ypb (2026.11.8 on) would then
# quietly listen on the next one instead.
ANY_PORT = "0"


def stream_arguments(video_id: str) -> list[str]:
    """What ypb serve and ypb download take after their own options: the stream, then yt-dlp's options.

    After "--", so neither ypb nor yt-dlp (which ypb hands the stream to as
    is) reads an ID that starts with "-" as an option; as a watch URL, since
    yt-dlp would. The rest goes to yt-dlp, both when ypb reads the stream's
    formats and when it downloads. --ignore-config keeps the user's own yt-dlp
    config files (a proxy, an --exec, ...) from changing what it does.
    """
    return [
        "--", f"https://www.youtube.com/watch?v={video_id}",
        "--ignore-config", "--plugin-dirs", str(PLUGIN_DIR), "--use-postprocessor", PLUGIN,
    ]


def ypb_command(ypb: str, video_id: str, interval: str, download: Path) -> list[str]:
    """The ypb command that downloads `interval` to `download` (a yt-dlp output template).

    Only yt-dlp talks to its playback server, at the address ypb gives it, so
    any free port does."""
    return [
        ypb, "download", "--host", HOST, "--port", ANY_PORT, "--interval", interval,
        *stream_arguments(video_id),
        "-f", VIDEO_FORMAT, "--merge-output-format", "mp4", "--newline", "-o", str(download),
    ]


def clean_line(line: str) -> str:
    """A line of ypb/yt-dlp output as shown in the log: URLs hidden, length capped."""
    line = URL.sub("<url>", line.rstrip())
    return line if len(line) <= MAX_LINE else line[:MAX_LINE] + "…"


def progress_line(line: str) -> str | None:
    """yt-dlp's fragment progress as "Progress: done/total percent%", or None."""
    match = FRAGMENT.match(line.strip())
    if match is None:
        return None
    done, total = map(int, match.groups())
    return f"Progress: {done}/{total} {done * 100 // max(total, 1)}%"


def go_time(moment: str, offset: str) -> datetime:
    """Parse a time as Go prints it: "2026-10-30 12:09:15.484041" and "+0000"."""
    whole, _, fraction = moment.partition(".")
    parsed = datetime.strptime(f"{whole} {offset}", "%Y-%m-%d %H:%M:%S %z")
    return parsed + timedelta(microseconds=int(fraction.ljust(6, "0")[:6])) if fraction else parsed


def seconds_until_reached(lines: list[str]) -> float | None:
    """If ypb failed because the stream hasn't reached the end time yet, how far behind it is."""
    for line in reversed(lines):
        if match := NOT_REACHED.search(line):
            requested, current = go_time(*match.group(1, 2)), go_time(*match.group(3, 4))
            return max((requested - current).total_seconds(), 0.0)
    return None


def friendly_error(lines: list[str], exit_code: int) -> str:
    """A short explanation of why ypb failed, from the last lines it printed."""
    text = "\n".join(lines)
    if seconds_until_reached(lines) is not None:
        return "The stream hasn't reached the end time yet. Try again in a moment, or choose an earlier end time."
    if "locating interval" in text or "locating moment" in text:
        return (
            "Couldn't find those moments in the stream. YouTube may no longer keep them, "
            "or the stream wasn't live then."
        )
    if "unable to find yt-dlp" in text:
        return "yt-dlp wasn't found (see the README)."
    for line in reversed(lines):
        if line.startswith(("ERROR:", "ypb: error:")):
            return clean_line(line)
    return f"ypb stopped unexpectedly (exit code {exit_code})."


def read_tags(ffmpeg: str, path: Path) -> dict[str, str]:
    """The file's metadata tags, read with ffmpeg's ffmetadata output."""
    result = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path), "-f", "ffmetadata", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    )
    tags = {}
    for line in result.stdout.splitlines():
        # Global tags come first; stream sections start with "[".
        if line.startswith("["):
            break
        if "=" in line and not line.startswith(";"):
            key, value = line.split("=", 1)
            tags[key] = re.sub(r"\\(.)", r"\1", value)  # ffmetadata escapes = ; # \ with "\"
    return tags


def cut_range(tags: dict[str, str]) -> tuple[float, float]:
    """(offset into the download, length) of the requested interval, from ypb's tags."""

    def moment(key: str) -> datetime:
        if key not in tags:
            raise ValueError(f"the download has no {key} tag")
        return datetime.fromisoformat(tags[key].replace("Z", "+00:00"))

    offset = (moment("InputStartTime") - moment("ActualStartTime")).total_seconds()
    length = (moment("InputEndTime") - moment("InputStartTime")).total_seconds()
    if offset < 0 or length <= 0:
        raise ValueError(f"unexpected times in the download (offset {offset}s, length {length}s)")
    return offset, length


def finish_command(ffmpeg: str, source: Path, target: Path, cut: tuple[float, float] | None) -> list[str]:
    """ffmpeg command that writes the finished clip: cut and re-encoded, or rewrapped."""
    command = [ffmpeg, "-v", "error", "-nostdin"]
    if cut is None:
        command += ["-i", str(source), "-map", "0", "-c", "copy"]
    else:
        offset, length = cut
        command += [
            "-ss", f"{offset:.3f}", "-i", str(source), "-t", f"{length:.3f}", "-map", "0",
            "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-c:a", "aac", "-b:a", "192k",
        ]
    # faststart: index at the front, so playback can begin before the whole
    # file is read (matters when streamed or uploaded).
    return command + ["-map_metadata", "0", "-movflags", "+faststart+use_metadata_tags", "-y", str(target)]


def run_ypb(command: list[str]) -> tuple[int, list[str]]:
    """Run ypb, passing its output on as progress and log lines; returns (exit code, last lines).

    ypb's own error lines ("ypb: error: ...") aren't printed here, only kept in
    the returned lines: the caller shows them unless it's going to try again.
    """
    tail: list[str] = []
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    ) as process:
        for line in process.stdout:
            if not line.strip():
                continue
            if (progress := progress_line(line)) is not None:
                print(progress, flush=True)
                continue
            if line.lstrip().startswith("[download]") and "%" in line:
                continue  # progress without a fragment count
            if "Forcing generic information extractor" in line or "Unknown codec unknown" in line:
                # Expected (yt-dlp reads ypb's local playback server; the plugin
                # reports the unidentified formats it skips), but sounds alarming.
                continue
            if not line.startswith("ypb: error:"):
                print(clean_line(line), flush=True)
            tail = (tail + [line.strip()])[-20:]
    return process.returncode, tail


def clip(video_id: str, interval: str, output: Path, precise: bool, work: Path, overwrite: bool = False) -> int:
    """Download and finish the clip. With `overwrite`, an existing `output` is replaced,
    but only once the new clip is finished, so it's kept if this fails or is cancelled."""
    ypb, ffmpeg = shutil.which("ypb"), shutil.which("ffmpeg")
    if ypb is None or ffmpeg is None:
        print(f"Error: {'ypb' if ypb is None else 'ffmpeg'} wasn't found (see the README).", flush=True)
        return 1
    if output.exists() and not overwrite:
        print(f"Error: {output} already exists.", flush=True)
        return 1

    for attempt in range(1, CATCH_UP_TRIES + 1):
        exit_code, tail = run_ypb(ypb_command(ypb, video_id, interval, work / "download.%(ext)s"))
        behind = seconds_until_reached(tail) if exit_code != 0 else None
        if behind is None or attempt == CATCH_UP_TRIES:
            break
        # A live stream runs a few seconds behind real time, so an end time
        # close to "now" may not have been reached yet: wait for it.
        wait = min(behind + 2, MAX_WAIT)
        print(f"The stream hasn't reached the end time yet; trying again in {wait:.0f} s…", flush=True)
        time.sleep(wait)
    downloads = [p for p in work.glob("download.*") if not p.name.endswith((".part", ".ytdl"))]
    if exit_code != 0 or len(downloads) != 1:
        for line in tail:
            if line.startswith("ypb: error:"):
                print(clean_line(line), flush=True)  # the details, for the log
        print(f"Error: {friendly_error(tail, exit_code)}", flush=True)
        return 1

    print("Cutting to the exact times…" if precise else "Finishing the clip…", flush=True)
    try:
        cut = cut_range(read_tags(ffmpeg, downloads[0])) if precise else None
        finished = work / "finished.mp4"
        subprocess.run(finish_command(ffmpeg, downloads[0], finished, cut), check=True, stdin=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, ValueError) as exc:
        print(f"Error: couldn't finish the clip: {exc}", flush=True)
        return 1
    try:  # the work folder is usually next to the output, so this is a rename, not a copy
        os.replace(finished, output)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
        shutil.copyfile(finished, output)  # a work folder elsewhere (see new_work_folder)
    print(f"Saved to '{output}'", flush=True)
    return 0


def new_work_folder(output: Path) -> Path:
    """A new folder for the clip's intermediate files: next to `output`, so the finished
    clip only needs renaming. Where that can't be made, in the user's cache folder. That
    happens in the Flatpak when the clip is saved somewhere it was only allowed to write
    that one file (through the file chooser's portal)."""
    try:
        return Path(tempfile.mkdtemp(prefix=".clipsake-", dir=output.parent))
    except OSError:
        cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "clipsake"
        cache.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="clip-", dir=cache))


def stop_everything(work: Path) -> None:
    """On SIGTERM (the app has ended, see main): stop ypb and ffmpeg (this process group), remove
    the unfinished clip's work folder and exit. The app isn't there any more to do either."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)  # the group's SIGTERM reaches this process too
    with contextlib.suppress(OSError):
        os.killpg(0, signal.SIGTERM)
    time.sleep(0.5)  # for them to stop writing there
    shutil.rmtree(work, ignore_errors=True)
    os._exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Clip a past moment of a YouTube live stream.")
    parser.add_argument("video_id")
    parser.add_argument("--interval", required=True, help="START/END, e.g. 2026-10-30T11:09:09+00/2026-10-30T11:09:29+00")
    parser.add_argument("--output", required=True, type=Path, help="the finished clip (.mp4)")
    parser.add_argument("--precise", action="store_true", help="cut to the exact times (re-encodes; slower)")
    parser.add_argument("--overwrite", action="store_true", help="replace --output if it exists (once the clip is done)")
    parser.add_argument("--work-dir", type=Path, help="folder for intermediate files (default: a new one next to --output, if possible)")
    parser.add_argument(
        "--own-process-group", action="store_true",
        help="become a process group leader, so the GUI can stop this and the programs it runs at once",
    )
    args = parser.parse_args()

    hide_child_consoles()
    work = args.work_dir or new_work_folder(args.output)
    if args.own_process_group and os.name == "posix":
        os.setpgrp()
        signal.signal(signal.SIGTERM, lambda _signal, _frame: stop_everything(work))
    stop_with_app()  # SIGTERM if the app ends, even if it's killed (Linux)
    try:
        sys.exit(clip(args.video_id, args.interval, args.output, args.precise, work, args.overwrite))
    finally:
        if args.work_dir is None:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
