"""Times as the app shows and reads them, in the display time zone (timeline.zones).

Times are seconds since the Unix epoch. format_moment / format_length show them;
parse_goto reads what's typed in Go to and the Start / End fields; wall_clock /
from_wall_clock help put ruler ticks on round local times.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from timeline.zones import from_local, local


def wall_clock(moment: float) -> float:
    """`moment` as local wall-clock seconds (the epoch time it would be if local time were UTC)."""
    return moment + local(moment).utcoffset().total_seconds()


def from_wall_clock(wall: float) -> float:
    """The moment showing local time `wall` (the later one, if the clocks show it twice)."""
    moment = wall - local(wall).utcoffset().total_seconds()
    return wall - local(moment).utcoffset().total_seconds()


def format_moment(moment: float, seconds: bool = True) -> str:
    return local(moment).strftime("%a %d %b %H:%M:%S" if seconds else "%a %d %b %H:%M")


def format_length(seconds: float) -> str:
    seconds = round(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
GOTO = re.compile(
    r"^\s*(?:(?P<day>today|yesterday|[a-z]{3})[a-z]*,?\s+)?"
    r"(?:(?P<mday>\d{1,2})\s+(?P<month>[a-z]{3})[a-z]*\.?(?:\s+(?P<year>\d{4}))?,?\s+"
    r"|(?P<date>\d{4}-\d\d-\d\d)[ t]+)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d\d))?(?::(?P<second>\d\d))?\s*(?P<ampm>am|pm)?\s*$",
    re.IGNORECASE,
)
GOTO_EXAMPLES = "14:30, 9pm, yesterday 9:15, Fri 22:00 or 29 Oct 08:00"


def parse_goto(text: str, latest: float) -> float | None:
    """A moment typed as e.g. "14:30", "9pm", "yesterday 9:15", "Fri 22:00:05",
    "29 Oct 08:00", "Sat 31 Oct 14:03:22" (as format_moment shows it) or
    "2026-10-29 08:00", in the display time zone. A time without a day means its
    most recent occurrence at or before `latest` (the live edge); a weekday, the
    most recent such day; a date without a year, the most recent such date.
    None if it can't be read."""
    match = GOTO.match(text.strip())
    if not match:
        return None
    hour, minute, second = int(match["hour"]), int(match["minute"] or 0), int(match["second"] or 0)
    if match["ampm"]:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if match["ampm"].lower() == "pm" else 0)
    if hour > 23 or minute > 59 or second > 59:
        return None
    newest = local(latest)
    word = (match["day"] or "").lower()
    if word and word not in ("today", "yesterday") and word[:3] not in WEEKDAYS:
        return None
    try:
        if match["date"]:
            candidates = [datetime.strptime(match["date"], "%Y-%m-%d").date()]
        elif match["mday"]:
            if match["month"][:3].lower() not in MONTHS:
                return None
            month, mday = MONTHS.index(match["month"][:3].lower()) + 1, int(match["mday"])
            if match["year"]:
                candidates = [datetime(int(match["year"]), month, mday).date()]
            else:  # this year's, unless that's still to come
                day = datetime(newest.year, month, mday).date() if (month, mday) <= (newest.month, newest.day) else None
                candidates = [day or datetime(newest.year - 1, month, mday).date()]
        elif word == "today":
            candidates = [newest.date()]
        elif word == "yesterday":
            candidates = [newest.date() - timedelta(days=1)]
        elif word:
            back = (newest.weekday() - WEEKDAYS.index(word[:3])) % 7
            candidates = [newest.date() - timedelta(days=back), newest.date() - timedelta(days=back + 7)]
        else:
            candidates = [newest.date() - timedelta(days=n) for n in range(2)]
    except ValueError:  # no such date, e.g. 30 Feb
        return None
    # A day that was named is used even if the time is after `latest`; a guessed one isn't.
    explicit = bool(match["date"] or match["mday"]) or word in ("today", "yesterday")
    for day in candidates:
        moment = from_local(day.year, day.month, day.day, hour, minute, second)
        if moment <= latest or explicit:
            return moment
    return None
