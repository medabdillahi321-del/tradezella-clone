"""Dukascopy historical ticks: a second, independent price source.

Dukascopy publishes free hourly tick files at
    https://datafeed.dukascopy.com/datafeed/EURUSD/<year>/<month 00-11>/<day>/<HH>h_ticks.bi5
Note the month starts at 00 for January.

Each file is LZMA-compressed. Inside, every tick is 20 bytes, big-endian:
    uint32 milliseconds since the start of the hour
    uint32 ask, uint32 bid      (in points: EUR/USD price x 100000)
    float32 ask volume, float32 bid volume
An empty file means no ticks that hour (for example at the weekend).

Downloading is deliberately slow and polite: every file is cached and never
fetched twice, requests are spaced out, and a "too many requests" answer stops
the download instead of retrying. Dukascopy's terms of use have not been
checked yet: see PROJECT_STATUS.md.
"""

from __future__ import annotations

import lzma
import struct
import time as _time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from fxsystem.clock import require_utc
from fxsystem.recorder import Tick

UTC = timezone.utc
BASE_URL = "https://datafeed.dukascopy.com/datafeed"
RECORD = struct.Struct(">IIIff")
# Price scale per instrument: EUR/USD has 5 decimals.
SCALE = {"EURUSD": Decimal(100000)}


class RateLimited(RuntimeError):
    """Dukascopy refused the request. Stop and try again much later."""


def hour_url(symbol: str, hour: datetime) -> str:
    h = require_utc(hour)
    return f"{BASE_URL}/{symbol}/{h.year:04d}/{h.month - 1:02d}/{h.day:02d}/{h.hour:02d}h_ticks.bi5"


def decode_hour(blob: bytes, hour: datetime, symbol: str = "EURUSD") -> list[Tick]:
    """Turn one downloaded .bi5 file into ticks."""
    if not blob:
        return []
    raw = lzma.decompress(blob)
    if len(raw) % RECORD.size:
        raise ValueError(f"Tick file is {len(raw)} bytes, not a whole number of {RECORD.size}-byte records")
    start = require_utc(hour).replace(minute=0, second=0, microsecond=0)
    scale = SCALE[symbol]
    ticks = []
    for ms, ask, bid, _av, _bv in RECORD.iter_unpack(raw):
        t = start + timedelta(milliseconds=ms)
        ticks.append(Tick(t, Decimal(bid) / scale, Decimal(ask) / scale, True, t))
    return ticks


def encode_hour(ticks: list[Tick], hour: datetime, symbol: str = "EURUSD") -> bytes:
    """The reverse of decode_hour, used by tests to build sample files."""
    start = require_utc(hour).replace(minute=0, second=0, microsecond=0)
    scale = SCALE[symbol]
    raw = b"".join(
        RECORD.pack(int((t.time - start) / timedelta(milliseconds=1)), int(t.ask * scale), int(t.bid * scale), 1.0, 1.0)
        for t in ticks
    )
    return lzma.compress(raw, format=lzma.FORMAT_ALONE) if raw else b""


Fetch = Callable[[str], bytes]


def _default_fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "fx-shadow-system/0.1 (personal research)"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited("Dukascopy answered 429 Too Many Requests") from e
        if e.code == 404:
            return b""  # no file for that hour
        raise
    except urllib.error.URLError as e:
        if isinstance(e.reason, ConnectionResetError):
            raise RateLimited("Dukascopy reset the connection (it may be blocking this network)") from e
        raise
    except ConnectionResetError as e:
        raise RateLimited("Dukascopy reset the connection (it may be blocking this network)") from e


class DukascopyCache:
    """Downloads hourly files once, keeps the raw files, and spaces requests out."""

    def __init__(
        self,
        data_dir: str | Path,
        symbol: str = "EURUSD",
        fetch: Fetch = _default_fetch,
        min_interval_s: float = 10.0,
        sleep: Callable[[float], None] = _time.sleep,
        clock: Callable[[], float] = _time.monotonic,
    ) -> None:
        self.dir = Path(data_dir) / "dukascopy" / symbol
        self.symbol = symbol
        self.fetch = fetch
        self.min_interval_s = min_interval_s
        self.sleep = sleep
        self.clock = clock
        self._last_request: float | None = None
        self.downloads = 0

    def path_for(self, hour: datetime) -> Path:
        h = require_utc(hour)
        return self.dir / f"{h:%Y}" / f"{h:%m}" / f"{h:%d}" / f"{h:%H}h_ticks.bi5"

    def hour_ticks(self, hour: datetime) -> list[Tick]:
        p = self.path_for(hour)
        if not p.exists():
            if self._last_request is not None:
                wait = self.min_interval_s - (self.clock() - self._last_request)
                if wait > 0:
                    self.sleep(wait)
            self._last_request = self.clock()
            blob = self.fetch(hour_url(self.symbol, hour))  # RateLimited propagates: stop
            self.downloads += 1
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".part")
            tmp.write_bytes(blob)
            tmp.replace(p)
        return decode_hour(p.read_bytes(), hour, self.symbol)

    def ticks(self, start: datetime, end: datetime) -> list[Tick]:
        """All ticks from start (inclusive) to end (exclusive), hour file by hour file."""
        start, end = require_utc(start), require_utc(end)
        h = start.replace(minute=0, second=0, microsecond=0)
        out: list[Tick] = []
        while h < end:
            out += [t for t in self.hour_ticks(h) if start <= t.time < end]
            h += timedelta(hours=1)
        return out
