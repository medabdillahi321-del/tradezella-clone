"""Replay runner and the strategy slot.

The replay feeds saved 1-minute bars to a strategy one at a time, in time order.
A bar is handed over only once it has closed (its decision time is the bar's start
plus one minute), and the strategy only ever sees bars up to that point: it cannot
look ahead.

Before the strategy is asked, a bar-level data check runs. If the data is not fit
(spread too wide, crossed prices, a gap just before the bar, market closed), the
answer is NO TRADE and the strategy is not consulted.

Each run writes to its own event log under data/replays/, never to the live log,
and records exactly what went in (data file hashes, strategy name and version,
settings), so a run can be repeated and must give the same result.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Protocol, Sequence

from fxsystem.clock import Window, market_open
from fxsystem.quality import Limits
from fxsystem.storage import LocalLog, canonical_json

UTC = timezone.utc


@dataclass(frozen=True)
class Intent:
    """A trade idea. Only the shadow simulator (milestone 8) turns it into a virtual fill."""

    side: str            # "buy" or "sell"
    stop_pips: Decimal
    target_pips: Decimal
    reason: str
    entry: str = "next_bar_open"

    def __post_init__(self):
        if self.side not in ("buy", "sell"):
            raise ValueError(f"side must be buy or sell, not {self.side!r}")
        if self.stop_pips <= 0:
            raise ValueError("every intent needs a positive stop distance")


class Strategy(Protocol):
    name: str
    version: str

    def on_bar(self, bars: Sequence[dict], now: datetime) -> list[Intent]:
        """bars: every bar so far, oldest first; bars[-1] has just closed. now: its close time."""
        ...


class PlumbingTestStrategy:
    """NOT A TRADING STRATEGY. A fixed, simple rule used only to exercise the system:
    buy when the mid close crosses above its 20-bar average, sell when it crosses below,
    inside the operating window, at most one idea every 30 minutes, 10-pip stop, 20-pip target.
    """

    name = "plumbing-test"
    version = "1"

    def __init__(self, window: Window, length: int = 20, cooldown_bars: int = 30) -> None:
        self.window = window
        self.length = length
        self.cooldown = cooldown_bars
        self.last_signal_index = -10**9

    def on_bar(self, bars: Sequence[dict], now: datetime) -> list[Intent]:
        i = len(bars) - 1
        if i < self.length or not self.window.contains(bars[-1]["start"]):
            return []
        if i - self.last_signal_index < self.cooldown:
            return []
        mid = [(b["bid"]["c"] + b["ask"]["c"]) / 2 for b in bars[-(self.length + 1):]]
        avg_now = sum(mid[1:]) / self.length
        avg_prev = sum(mid[:-1]) / self.length
        side = None
        if mid[-2] <= avg_prev and mid[-1] > avg_now:
            side = "buy"
        elif mid[-2] >= avg_prev and mid[-1] < avg_now:
            side = "sell"
        if not side:
            return []
        self.last_signal_index = i
        return [Intent(side, Decimal(10), Decimal(20), f"mid crossed {'above' if side == 'buy' else 'below'} 20-bar average")]


def bar_check(bar: dict, prev: dict | None, limits: Limits, max_gap_minutes: int = 5) -> list[str]:
    """Bar-level data reasons for NO TRADE (empty list = fit to use)."""
    reasons = []
    if not market_open(bar["start"]):
        reasons.append("market closed")
    spread = (bar["ask"]["c"] - bar["bid"]["c"]) / limits.pip
    if bar["ask"]["c"] <= bar["bid"]["c"]:
        reasons.append("crossed or locked close")
    elif spread > limits.max_spread_pips:
        reasons.append(f"spread {spread:.1f} pips at close")
    if prev is not None:
        gap = (bar["start"] - prev["start"]) / timedelta(minutes=1)
        if gap > max_gap_minutes and market_open(prev["start"] + timedelta(minutes=1)):
            reasons.append(f"{gap:.0f}-minute gap before this bar")
    return reasons


@dataclass
class ReplayResult:
    run_id: str
    bars: int
    intents: int
    no_trade: dict = field(default_factory=dict)
    fingerprint: str = ""
    log_path: str = ""


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _reason_key(reason: str) -> str:
    for word, key in (("closed", "market_closed"), ("crossed", "crossed"), ("spread", "wide_spread"), ("gap", "gap")):
        if word in reason:
            return key
    return "other"


def run_replay(
    bars: list[dict],
    strategy: Strategy,
    out_dir: str | Path,
    limits: Limits = Limits(),
    inputs: dict | None = None,
    started: datetime | None = None,
) -> ReplayResult:
    """Feed bars to the strategy in order. Returns a summary; full details go to the run's log."""
    config = {
        "strategy": strategy.name,
        "strategy_version": strategy.version,
        "limits": {k: str(v) for k, v in limits.__dict__.items()},
        "inputs": inputs or {},
        "first_bar": _iso(bars[0]["start"]) if bars else None,
        "last_bar": _iso(bars[-1]["start"]) if bars else None,
        "bar_count": len(bars),
    }
    config_hash = hashlib.sha256(canonical_json(config).encode()).hexdigest()[:12]
    started = started or datetime.now(UTC)
    run_id = f"{started:%Y%m%dT%H%M%SZ}-{strategy.name}-{config_hash}"
    path = Path(out_dir) / "replays" / f"{run_id}.sqlite"

    fp = hashlib.sha256()
    result = ReplayResult(run_id, len(bars), 0, log_path=str(path))
    with LocalLog(path) as log:
        log.append("system.replay_start", started, {"run_id": run_id, **config})
        for i, bar in enumerate(bars):
            now = bar["start"] + timedelta(minutes=1)  # the bar is known only once closed
            reasons = bar_check(bar, bars[i - 1] if i else None, limits)
            if reasons:
                for r in reasons:
                    key = _reason_key(r)
                    result.no_trade[key] = result.no_trade.get(key, 0) + 1
                continue
            # Only bars up to and including this one: no look-ahead.
            for intent in strategy.on_bar(bars[: i + 1], now):
                payload = {
                    "run_id": run_id,
                    "strategy": strategy.name,
                    "strategy_version": strategy.version,
                    "signal_bar_utc": _iso(bar["start"]),
                    "decided_utc": _iso(now),
                    "side": intent.side,
                    "entry": intent.entry,
                    "stop_pips": str(intent.stop_pips),
                    "target_pips": str(intent.target_pips),
                    "reason": intent.reason,
                    "bid_close": str(bar["bid"]["c"]),
                    "ask_close": str(bar["ask"]["c"]),
                }
                log.append("shadow.intent", now, payload, idem_key=f"{run_id}:{_iso(bar['start'])}:{intent.side}")
                fp.update(canonical_json(payload | {"run_id": ""}).encode())
                result.intents += 1
        result.fingerprint = fp.hexdigest()
        log.append("system.replay_stop", started, {
            "run_id": run_id, "bars": result.bars, "intents": result.intents,
            "no_trade_bars_by_reason": result.no_trade, "fingerprint": result.fingerprint,
        })
    return result
