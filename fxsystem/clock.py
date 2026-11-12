"""Clock and sessions: the one place that converts times.

Rules this module follows:
- Every timestamp inside the system is a timezone-aware datetime in UTC.
- Local times (New York, London, Tokyo) exist only for describing windows,
  and are converted with IANA zones, so daylight saving time is handled
  by the zone database rather than by fixed offsets.
- OANDA sends times in UTC, so there is no broker server time to convert.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")

# Forex convention: the trading day rolls over at 17:00 New York time.
DEFAULT_DAY_BOUNDARY = time(17, 0)


class NaiveTimeError(ValueError):
    """Raised when a datetime without a timezone is passed in."""


def require_utc(dt: datetime) -> datetime:
    """Return dt in UTC. Refuse naive datetimes rather than guess their zone."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise NaiveTimeError(f"Datetime has no timezone: {dt!r}")
    return dt.astimezone(UTC)


def parse_oanda_time(text: str) -> datetime:
    """Parse an OANDA RFC 3339 timestamp, e.g. '2026-11-12T14:30:05.123456789Z'.

    OANDA gives up to nine fractional digits; Python keeps six, so the
    extra digits are dropped (truncated, never rounded up into the future).
    """
    s = text.strip()
    if not s.endswith("Z"):
        raise ValueError(f"Expected a UTC timestamp ending in 'Z': {text!r}")
    s = s[:-1]
    if "." in s:
        main, frac = s.split(".", 1)
        if not frac.isdigit():
            raise ValueError(f"Bad fractional seconds in {text!r}")
        s = f"{main}.{frac[:6].ljust(6, '0')}"
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def local_to_utc(day: date, at: time, zone: ZoneInfo) -> datetime:
    """Convert a local wall-clock time on a given day to UTC.

    On a spring-forward day a local time that does not exist (for example
    02:30 in New York) maps to the instant one hour later on the clock.
    On a fall-back day an ambiguous local time uses its first occurrence.
    """
    return datetime.combine(day, at, tzinfo=zone).astimezone(UTC)


def trading_day(dt: datetime, boundary: time = DEFAULT_DAY_BOUNDARY) -> date:
    """The trading day a moment belongs to.

    A moment at or after the boundary (17:00 New York by default) belongs
    to the next calendar day. The four-trade cap and the daily loss limit
    both reset on this boundary.
    """
    ny = require_utc(dt).astimezone(NEW_YORK)
    if ny.time() >= boundary:
        return ny.date() + timedelta(days=1)
    return ny.date()


def trading_day_bounds(
    day: date, boundary: time = DEFAULT_DAY_BOUNDARY
) -> tuple[datetime, datetime]:
    """UTC start (inclusive) and end (exclusive) of a trading day."""
    start = local_to_utc(day - timedelta(days=1), boundary, NEW_YORK)
    end = local_to_utc(day, boundary, NEW_YORK)
    return start, end


def market_open(dt: datetime) -> bool:
    """Whether the forex market is open: Sunday 17:00 to Friday 17:00 New York.

    Holidays and thin-liquidity days are not handled here; the data
    quality checker deals with missing prices on those days.
    """
    ny = require_utc(dt).astimezone(NEW_YORK)
    wd = ny.weekday()  # Monday = 0 ... Sunday = 6
    if wd == 5:  # Saturday
        return False
    if wd == 4 and ny.time() >= DEFAULT_DAY_BOUNDARY:  # Friday after close
        return False
    if wd == 6 and ny.time() < DEFAULT_DAY_BOUNDARY:  # Sunday before open
        return False
    return True


@dataclass(frozen=True)
class Window:
    """A daily window defined in local time, such as 09:00-12:00 New York.

    If end is earlier than start, the window crosses local midnight and
    ends on the next day.
    """

    name: str
    zone: str
    start: time
    end: time
    status: str = "provisional"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.zone)

    def bounds(self, local_day: date) -> tuple[datetime, datetime]:
        """UTC start (inclusive) and end (exclusive) of the window opening on local_day."""
        end_day = local_day + timedelta(days=1) if self.end <= self.start else local_day
        return (
            local_to_utc(local_day, self.start, self.tz),
            local_to_utc(end_day, self.end, self.tz),
        )

    def contains(self, dt: datetime) -> bool:
        """Whether a moment falls inside this window."""
        utc = require_utc(dt)
        local_day = utc.astimezone(self.tz).date()
        # A window that crosses midnight may have opened the day before.
        for d in (local_day, local_day - timedelta(days=1)):
            start, end = self.bounds(d)
            if start <= utc < end:
                return True
        return False


def load_windows(path: str) -> dict[str, Window]:
    """Load window definitions from a TOML file (see config/sessions.toml)."""
    import tomllib

    with open(path, "rb") as f:
        raw = tomllib.load(f)
    windows = {}
    for key, w in raw.get("window", {}).items():
        ZoneInfo(w["zone"])  # fail now on a misspelt zone name
        windows[key] = Window(
            name=w.get("name", key),
            zone=w["zone"],
            start=time.fromisoformat(w["start"]),
            end=time.fromisoformat(w["end"]),
            status=w.get("status", "provisional"),
        )
    return windows
