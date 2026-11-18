"""yt-dlp plugin: drop formats that ypb can't handle, before ypb sees them.

Some live streams list formats yt-dlp can't identify (e.g. itags 386 and 387:
no codec, size, frame rate or sample rate). ypb (2026.10.1, and still 2026.11.12)
reads those fields without checking for them and crashes ("invalid memory address or nil
pointer dereference" in fetchers/ytdlp.go). ypb passes the clipper's yt-dlp
options on, so the clipper loads this folder with --plugin-dirs and runs this
postprocessor at "pre_process", which is after yt-dlp has listed the formats
and before it picks formats or writes the info file ypb reads.

Once ypb skips such formats itself, this becomes a no-op: it only removes
formats that lack those fields.
"""

from yt_dlp.postprocessor.common import PostProcessor


def is_complete(fmt: dict) -> bool:
    """Whether a format has every field ypb reads for its kind."""
    if fmt.get("vcodec") == "none":  # ypb treats this as audio
        return fmt.get("asr") is not None
    return all(fmt.get(key) is not None for key in ("width", "height", "fps"))


class DropIncompleteFormatsPP(PostProcessor):
    def run(self, info):
        # Only YouTube's own format list. ypb's second yt-dlp run reads ypb's
        # local playback server (yt-dlp's "generic" extractor), whose formats
        # ypb made itself.
        if info.get("extractor_key") != "Youtube":
            return [], info
        formats = info.get("formats") or []
        dropped = [f.get("format_id", "?") for f in formats if not is_complete(f)]
        if dropped:
            self.to_screen(f"Skipping formats yt-dlp couldn't identify: {', '.join(dropped)}")
            info["formats"] = [f for f in formats if is_complete(f)]
        return [], info
