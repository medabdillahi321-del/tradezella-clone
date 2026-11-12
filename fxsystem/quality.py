"""Data quality: is the price good enough to act on?

Two parts:
- QuoteGate: checked live, before any (virtual) trade. It answers OK or gives the
  reasons for NO TRADE: no data yet, stale feed, frozen price, crossed quote, wide
  spread, recent spike, OANDA marked it untradeable, market closed, or a price
  outside the sane range. Unknown is never OK.
- audit_day: an end-of-day report on a recorded trading day: missing minutes,
  incomplete bars, spikes, wide spreads, frozen periods, crossed quotes and gaps.

This module only reports. It never changes or deletes recorded data.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fxsystem.clock import Window, market_open, require_utc, trading_day_bounds
from fxsystem.recorder import Tick
from fxsystem.storage import LocalLog

UTC = timezone.utc


@dataclass(frozen=True)
class Limits:
    pip: Decimal = Decimal("0.0001")
    stale_after_s: float = 10
    frozen_after_s: float = 120
    max_spread_pips: Decimal = Decimal("3.0")
    spike_pips: Decimal = Decimal("15.0")
    spike_cooldown_s: float = 60
    min_price: Decimal = Decimal("0.50")
    max_price: Decimal = Decimal("2.00")

    @classmethod
    def load(cls, path: str, section: str = "eur_usd") -> "Limits":
        import tomllib

        with open(path, "rb") as f:
            raw = tomllib.load(f)[section]
        dec = {"pip", "max_spread_pips", "spike_pips", "min_price", "max_price"}
        return cls(**{k: Decimal(str(v)) if k in dec else float(v) for k, v in raw.items()})


@dataclass
class Verdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return "OK" if self.ok else "NO TRADE: " + "; ".join(self.reasons)


class QuoteGate:
    """Keeps the latest price and decides whether it is fit to act on right now."""

    def __init__(self, limits: Limits = Limits()) -> None:
        self.limits = limits
        self.last: Tick | None = None
        self.last_seen: datetime | None = None  # last price or heartbeat, OANDA time
        self.last_change: datetime | None = None
        self.last_spike: datetime | None = None
        self.last_spike_pips: Decimal | None = None

    def on_tick(self, t: Tick) -> None:
        if self.last is not None:
            jump = abs(((t.bid + t.ask) - (self.last.bid + self.last.ask)) / 2) / self.limits.pip
            if jump > self.limits.spike_pips:
                self.last_spike, self.last_spike_pips = t.time, jump
            if t.bid != self.last.bid or t.ask != self.last.ask:
                self.last_change = t.time
        else:
            self.last_change = t.time
        self.last = t
        self.last_seen = max(self.last_seen or t.time, t.time)

    def on_heartbeat(self, server_time: datetime) -> None:
        self.last_seen = max(self.last_seen or server_time, require_utc(server_time))

    def check(self, now: datetime) -> Verdict:
        now = require_utc(now)
        L, t = self.limits, self.last
        reasons: list[str] = []
        if not market_open(now):
            reasons.append("market closed")
        if t is None:
            reasons.append("no price received yet")
            return Verdict(False, reasons)
        if (now - self.last_seen).total_seconds() > L.stale_after_s:
            reasons.append(f"feed stale: nothing from OANDA for {(now - self.last_seen).total_seconds():.0f}s")
        if self.last_change and (now - self.last_change).total_seconds() > L.frozen_after_s:
            reasons.append(f"price frozen for {(now - self.last_change).total_seconds():.0f}s")
        if not t.tradeable:
            reasons.append("OANDA marked the price untradeable")
        if t.ask <= t.bid:
            reasons.append(f"crossed or locked quote (bid {t.bid}, ask {t.ask})")
        spread = (t.ask - t.bid) / L.pip
        if spread > L.max_spread_pips:
            reasons.append(f"spread {spread:.1f} pips is above {L.max_spread_pips}")
        if not (L.min_price <= t.bid <= L.max_price and L.min_price <= t.ask <= L.max_price):
            reasons.append(f"price outside the sane range {L.min_price}-{L.max_price}")
        if self.last_spike and (now - self.last_spike).total_seconds() < L.spike_cooldown_s:
            reasons.append(f"spike of {self.last_spike_pips:.1f} pips {(now - self.last_spike).total_seconds():.0f}s ago")
        if t.time > now + timedelta(seconds=2):
            reasons.append("price time is in the future: clock problem")
        return Verdict(not reasons, reasons)


# ---------- end-of-day audit ----------

def read_ticks(path: Path) -> list[Tick]:
    from fxsystem.clock import parse_oanda_time

    out = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out.append(Tick(
                time=parse_oanda_time(row["time_utc"]),
                bid=Decimal(row["bid"]),
                ask=Decimal(row["ask"]),
                tradeable=row["tradeable"] == "1",
                received=parse_oanda_time(row["received_utc"]),
            ))
    return out


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def minute_start_of(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def audit_day(
    log: LocalLog,
    data_dir: str | Path,
    day: date,
    instrument: str = "EUR_USD",
    limits: Limits = Limits(),
    window: Window | None = None,
    until: datetime | None = None,
) -> dict:
    """Report on one recorded trading day. `until` limits the check to what has happened so far."""
    start, end = trading_day_bounds(day)
    if until is not None:
        end = min(end, require_utc(until))
    L = limits

    bars = {
        e.payload["start_utc"][:16]: e.payload
        for e in log.events("market.bar_m1")
        if e.trading_day == day.isoformat() and e.payload.get("instrument") == instrument
    }
    gaps = [
        e.payload for e in log.events("market.gap")
        if e.trading_day == day.isoformat() and e.payload.get("instrument") == instrument
    ]

    # Only minutes while the recorder was running count as missing data. Running
    # means from a recorder_start to the next recorder_stop; a crash leaves no stop,
    # so the time after a crash still counts as missing.
    day_events = [e for e in log.events("system.recorder_") if e.trading_day == day.isoformat()]
    to_dt = lambda s_: datetime.fromisoformat(s_.replace("Z", "+00:00"))  # noqa: E731
    running: list[tuple[datetime, datetime]] = []
    open_at = None
    for e in day_events:
        if e.kind == "system.recorder_start":
            open_at = minute_start_of(to_dt(e.ts_utc))
        elif e.kind == "system.recorder_stop" and open_at is not None:
            running.append((open_at, minute_start_of(to_dt(e.ts_utc))))
            open_at = None
    if open_at is not None:
        running.append((open_at, end))
    if not day_events and bars:  # bars without start/stop records: count from the first bar
        running.append((datetime.fromisoformat(min(bars) + ":00+00:00"), end))

    def was_running(m_: datetime) -> bool:
        return any(a_ <= m_ < b_ for a_, b_ in running)

    missing, missing_in_window = [], []
    not_recorded = 0
    m = start
    while m < end:
        if market_open(m) and m + timedelta(minutes=1) <= end:
            key = m.strftime("%Y-%m-%dT%H:%M")
            if not was_running(m):
                if key not in bars:
                    not_recorded += 1
            elif key not in bars:
                missing.append(m)
                if window and window.contains(m):
                    missing_in_window.append(m)
        m += timedelta(minutes=1)

    # Tick-level problems.
    tick_path = Path(data_dir) / "ticks" / instrument / f"{day.isoformat()}.csv"
    ticks = read_ticks(tick_path) if tick_path.exists() else []
    spikes, wide, crossed, frozen, untradeable = [], 0, 0, [], 0
    prev = None
    change_at = None
    for t in ticks:
        spread = (t.ask - t.bid) / L.pip
        if t.ask <= t.bid:
            crossed += 1
        if spread > L.max_spread_pips:
            wide += 1
        if not t.tradeable:
            untradeable += 1
        if prev is not None:
            jump = abs(((t.bid + t.ask) - (prev.bid + prev.ask)) / 2) / L.pip
            if jump > L.spike_pips:
                spikes.append({"at": _iso(t.time), "pips": f"{jump:.1f}"})
            if t.bid != prev.bid or t.ask != prev.ask:
                if change_at and (t.time - change_at).total_seconds() > L.frozen_after_s and market_open(change_at):
                    frozen.append({"from": _iso(change_at), "to": _iso(t.time),
                                   "seconds": int((t.time - change_at).total_seconds())})
                change_at = t.time
        else:
            change_at = t.time
        prev = t

    spreads = sorted((t.ask - t.bid) / L.pip for t in ticks)
    median_spread = f"{spreads[len(spreads) // 2]:.1f}" if spreads else None

    report = {
        "instrument": instrument,
        "trading_day": day.isoformat(),
        "checked_until_utc": _iso(end),
        "bars": len(bars),
        "incomplete_bars": sum(1 for b in bars.values() if b.get("partial")),
        "missing_minutes": len(missing),
        "missing_minutes_in_operating_window": len(missing_in_window),
        "first_missing": [_iso(x) for x in missing[:10]],
        "first_missing_in_operating_window": [_iso(x) for x in missing_in_window[:10]],
        "recorder_running": [[_iso(x), _iso(y)] for x, y in running],
        "open_minutes_not_recorded": not_recorded,
        "ticks": len(ticks),
        "median_spread_pips": median_spread,
        "wide_spread_ticks": wide,
        "crossed_quotes": crossed,
        "untradeable_ticks": untradeable,
        "spikes": spikes[:20],
        "frozen_periods": frozen[:20],
        "stream_gaps": len(gaps),
    }
    problems = (report["missing_minutes_in_operating_window"] + crossed + len(spikes) + len(frozen))
    report["verdict"] = "clean" if problems == 0 else "problems found"
    return report
