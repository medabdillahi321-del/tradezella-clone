"""Price recorder: saves every OANDA price and builds 1-minute bars.

What it does:
- Reads OANDA's practice price stream (read-only).
- Writes every price (tick) to a daily CSV file: data/ticks/<INSTRUMENT>/<trading day>.csv.
  Files roll over at 17:00 New York, the same trading day as the rest of the system.
- Builds 1-minute bid and ask bars and writes each finished bar to the event log
  as "market.bar_m1". A bar is finished when OANDA's own clock (a price or a
  heartbeat) has passed the end of that minute, so a bar is never closed early.
- If the stream goes quiet or drops, it logs it, waits, reconnects, and logs the
  gap. Prices repeated after a reconnect are skipped.

It never decides anything and never places orders.
"""

from __future__ import annotations

import csv
import os
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterable, Iterator

from fxsystem.clock import parse_oanda_time, trading_day
from fxsystem.storage import LocalLog

UTC = timezone.utc
TICK_FIELDS = ["time_utc", "bid", "ask", "tradeable", "received_utc"]


@dataclass(frozen=True)
class Tick:
    time: datetime
    bid: Decimal
    ask: Decimal
    tradeable: bool
    received: datetime


def parse_price(msg: dict, received: datetime) -> Tick:
    return Tick(
        time=parse_oanda_time(msg["time"]),
        bid=Decimal(msg["bids"][0]["price"]),
        ask=Decimal(msg["asks"][0]["price"]),
        tradeable=bool(msg.get("tradeable", False)),
        received=received,
    )


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class Bar:
    start: datetime
    bid_o: Decimal
    bid_h: Decimal
    bid_l: Decimal
    bid_c: Decimal
    ask_o: Decimal
    ask_h: Decimal
    ask_l: Decimal
    ask_c: Decimal
    ticks: int = 1
    spread_min: Decimal = field(default=Decimal(0))
    spread_max: Decimal = field(default=Decimal(0))
    untradeable_ticks: int = 0
    # True when the recorder connected or reconnected during this minute, so prices
    # from earlier in the minute may be missing. Such bars are kept but flagged.
    partial: bool = False

    @classmethod
    def first(cls, t: Tick, start: datetime) -> "Bar":
        s = t.ask - t.bid
        return cls(start, t.bid, t.bid, t.bid, t.bid, t.ask, t.ask, t.ask, t.ask,
                   1, s, s, 0 if t.tradeable else 1)

    def add(self, t: Tick) -> None:
        self.bid_h, self.bid_l, self.bid_c = max(self.bid_h, t.bid), min(self.bid_l, t.bid), t.bid
        self.ask_h, self.ask_l, self.ask_c = max(self.ask_h, t.ask), min(self.ask_l, t.ask), t.ask
        s = t.ask - t.bid
        self.spread_min, self.spread_max = min(self.spread_min, s), max(self.spread_max, s)
        self.ticks += 1
        self.untradeable_ticks += 0 if t.tradeable else 1

    def payload(self, instrument: str) -> dict:
        return {
            "instrument": instrument,
            "start_utc": _iso(self.start),
            "bid": {"o": str(self.bid_o), "h": str(self.bid_h), "l": str(self.bid_l), "c": str(self.bid_c)},
            "ask": {"o": str(self.ask_o), "h": str(self.ask_h), "l": str(self.ask_l), "c": str(self.ask_c)},
            "ticks": self.ticks,
            "spread_min": str(self.spread_min),
            "spread_max": str(self.spread_max),
            "untradeable_ticks": self.untradeable_ticks,
            "partial": self.partial,
            "source": "oanda_practice_stream",
        }


def minute_start(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


class TickFile:
    """Appends ticks to one CSV per trading day, flushed to disk regularly."""

    def __init__(self, root: Path, instrument: str, fsync_every: float = 5.0) -> None:
        self.dir = root / "ticks" / instrument
        self.dir.mkdir(parents=True, exist_ok=True)
        self.day = None
        self.f = None
        self.w = None
        self.fsync_every = fsync_every
        self._last_sync = 0.0

    def write(self, t: Tick) -> None:
        day = trading_day(t.time)
        if day != self.day:
            self.close()
            path = self.dir / f"{day.isoformat()}.csv"
            new = not path.exists() or path.stat().st_size == 0
            self.f = open(path, "a", newline="", encoding="utf-8")
            self.w = csv.writer(self.f)
            if new:
                self.w.writerow(TICK_FIELDS)
            self.day = day
        self.w.writerow([_iso(t.time), str(t.bid), str(t.ask), int(t.tradeable), _iso(t.received)])
        now = _time.monotonic()
        if now - self._last_sync >= self.fsync_every:
            self.flush()
            self._last_sync = now

    def flush(self) -> None:
        if self.f:
            self.f.flush()
            os.fsync(self.f.fileno())

    def close(self) -> None:
        if self.f:
            self.flush()
            self.f.close()
            self.f = None


StreamFactory = Callable[[], Iterable[dict]]


class Recorder:
    def __init__(
        self,
        log: LocalLog,
        data_dir: str | Path,
        stream_factory: StreamFactory,
        instrument: str = "EUR_USD",
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = _time.sleep,
        max_backoff: float = 60.0,
        gate=None,
    ) -> None:
        self.log = log
        self.instrument = instrument
        self.ticks = TickFile(Path(data_dir), instrument)
        self.stream_factory = stream_factory
        self.now = now
        self.sleep = sleep
        self.max_backoff = max_backoff
        self.gate = gate  # optional QuoteGate, fed every price and heartbeat
        self.bar: Bar | None = None
        self.last_tick_time: datetime | None = None
        self.last_server_time: datetime | None = None
        self._just_connected = False
        self.stats = {"ticks": 0, "bars": 0, "duplicates": 0, "reconnects": 0, "heartbeats": 0}

    # ---- per-message handling ----
    def on_message(self, msg: dict) -> None:
        kind = msg.get("type")
        connected_minute = None
        if self._just_connected and "time" in msg:
            self._just_connected = False
            connected_minute = minute_start(parse_oanda_time(msg["time"]))
            if self.bar is not None and self.bar.start == connected_minute:
                self.bar.partial = True  # this minute spans a drop in the connection
            self._partial_minute = connected_minute
        if kind == "HEARTBEAT":
            self.stats["heartbeats"] += 1
            hb_time = parse_oanda_time(msg["time"])
            if self.gate:
                self.gate.on_heartbeat(hb_time)
            self._advance(hb_time)
        elif kind == "PRICE":
            if msg.get("instrument", self.instrument) != self.instrument:
                return
            t = parse_price(msg, self.now())
            if self.last_tick_time and t.time <= self.last_tick_time:
                self.stats["duplicates"] += 1  # repeated after a reconnect
                return
            self._advance(t.time)
            if self.gate:
                self.gate.on_tick(t)
            self.ticks.write(t)
            self.stats["ticks"] += 1
            self.last_tick_time = t.time
            m = minute_start(t.time)
            if self.bar is None:
                self.bar = Bar.first(t, m)
                self.bar.partial = m == getattr(self, "_partial_minute", None)
            else:
                self.bar.add(t)

    def _advance(self, server_time: datetime) -> None:
        """OANDA's clock moved on: close the current bar once its minute has ended."""
        self.last_server_time = server_time
        if self.bar and server_time >= self.bar.start + timedelta(minutes=1):
            self._emit_bar()

    def _emit_bar(self) -> None:
        b = self.bar
        self.bar = None
        self.log.append(
            "market.bar_m1",
            b.start,
            b.payload(self.instrument),
            idem_key=f"bar:{self.instrument}:M1:{_iso(b.start)}",
        )
        self.stats["bars"] += 1

    # ---- connection loop ----
    def run(self, should_stop: Callable[[], bool] = lambda: False) -> None:
        self.log.append("system.recorder_start", self.now(), {"instrument": self.instrument})
        backoff = 1.0
        down_since: datetime | None = None
        try:
            while not should_stop():
                try:
                    stream: Iterator[dict] = iter(self.stream_factory())
                    self._just_connected = True
                    for msg in stream:
                        if down_since is not None:
                            self._log_gap(down_since)
                            down_since = None
                        backoff = 1.0
                        self.on_message(msg)
                        if should_stop():
                            return
                    raise ConnectionError("stream ended")
                except Exception as e:  # noqa: BLE001  (timeouts, drops, bad lines)
                    if should_stop():
                        return
                    if down_since is None:
                        down_since = self.last_server_time or self.now()
                    self.stats["reconnects"] += 1
                    self.log.append(
                        "system.stream_error",
                        self.now(),
                        {"instrument": self.instrument, "error": f"{type(e).__name__}: {e}"[:300], "retry_in_s": backoff},
                    )
                    self.sleep(backoff)
                    backoff = min(backoff * 2, self.max_backoff)
        finally:
            self.ticks.close()
            # An unfinished bar is not written: its minute had not ended.
            self.log.append("system.recorder_stop", self.now(), {"instrument": self.instrument, **self.stats})

    def _log_gap(self, since: datetime) -> None:
        self.log.append(
            "market.gap",
            since,
            {"instrument": self.instrument, "from_utc": _iso(since), "to_utc": _iso(self.now()),
             "reason": "stream reconnect"},
        )
