"""The time zone times are shown in, which the user picks (this computer's
zone until changed).

Conversions use Python's tzdata package rather than the OS zone data Qt
relies on, since OS data can lag behind changes to a zone's rules.
"""

from __future__ import annotations

import functools
from datetime import datetime
from zoneinfo import ZoneInfo, available_timezones

DEFAULT_TZ_NAME = "UTC"  # if this computer's zone can't be told
# The picker offers canonical "Region/City" names, leaving out legacy aliases
# such as US/Pacific or Etc/GMT+8 (still accepted if typed).
ZONE_REGIONS = ("Africa", "America", "Antarctica", "Arctic", "Asia", "Atlantic", "Australia", "Europe", "Indian", "Pacific")

display_zone = ZoneInfo(DEFAULT_TZ_NAME)  # changed with set_display_zone


def set_display_zone(name: str) -> None:
    global display_zone
    display_zone = ZoneInfo(name)


def local(moment: float) -> datetime:
    """`moment` (seconds since the epoch) in the display time zone."""
    return datetime.fromtimestamp(moment, display_zone)


def from_local(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> float:
    """The moment showing this date and time in the display time zone (the earlier one, if shown twice)."""
    return datetime(year, month, day, hour, minute, second, tzinfo=display_zone).timestamp()


def system_zone_name() -> str:
    """This computer's time zone (from Qt, which reads it from the OS), or UTC."""
    from PySide6.QtCore import QTimeZone

    name = bytes(QTimeZone.systemTimeZoneId()).decode(errors="replace")
    return find_zone(name) or DEFAULT_TZ_NAME


@functools.cache
def all_zones() -> frozenset[str]:
    return frozenset(available_timezones())  # scans the zone database, so done once


def zone_names() -> list[str]:
    """Time zones offered in the picker: UTC, then every Region/City name."""
    return ["UTC", *sorted(name for name in all_zones() if name.split("/")[0] in ZONE_REGIONS)]


def find_zone(text: str) -> str | None:
    """The time zone name matching `text` (ignoring case), or None."""
    wanted = text.strip().replace(" ", "_").lower()
    return next((name for name in all_zones() if name.lower() == wanted), None)
