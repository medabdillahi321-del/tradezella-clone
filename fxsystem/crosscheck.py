"""Compare two price sources minute by minute.

The main comparison is the mid price (halfway between bid and ask), because two
companies quote different spreads but the market price itself should agree
closely. Spreads are reported side by side, not counted as differences.

Differences are only reported, never "corrected": which source was right is a
question for review, not for the code.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from statistics import median

from fxsystem.clock import parse_oanda_time
from fxsystem.recorder import Bar, Tick, minute_start

PIP = Decimal("0.0001")


def bars_from_ticks(ticks: list[Tick]) -> dict[str, dict]:
    """1-minute bid/ask bars keyed by 'YYYY-MM-DDTHH:MM' (UTC), built like the recorder builds them."""
    bars: dict[str, Bar] = {}
    for t in sorted(ticks, key=lambda x: x.time):
        m = minute_start(t.time)
        k = m.strftime("%Y-%m-%dT%H:%M")
        if k in bars:
            bars[k].add(t)
        else:
            bars[k] = Bar.first(t, m)
    return {k: normalise(b.payload("")) for k, b in bars.items()}


def normalise(p: dict) -> dict:
    """Bid and ask OHLC as Decimals, from a recorder payload or an OANDA candle."""
    return {side: {k: Decimal(str(p[side][k])) for k in "ohlc"} for side in ("bid", "ask")}


def oanda_candles_to_bars(candles: list[dict]) -> dict[str, dict]:
    out = {}
    for c in candles:
        if not c.get("complete", True):
            continue
        k = parse_oanda_time(c["time"]).strftime("%Y-%m-%dT%H:%M")
        out[k] = normalise(c)
    return out


def _mid(b: dict, k: str) -> Decimal:
    return (b["bid"][k] + b["ask"][k]) / 2


def _pct(xs: list[Decimal], q: float) -> Decimal:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def compare(a: dict[str, dict], b: dict[str, dict], name_a: str, name_b: str,
            flag_pips: Decimal = Decimal("1.0")) -> dict:
    """Compare two sets of minute bars. Returns a report dict."""
    both = sorted(set(a) & set(b))
    close_diffs, range_diffs, flagged = [], [], []
    spread_a, spread_b = [], []
    for k in both:
        dc = (_mid(a[k], "c") - _mid(b[k], "c")) / PIP
        dh = (_mid(a[k], "h") - _mid(b[k], "h")) / PIP
        dl = (_mid(a[k], "l") - _mid(b[k], "l")) / PIP
        close_diffs.append(abs(dc))
        range_diffs.append(max(abs(dh), abs(dl)))
        spread_a.append((a[k]["ask"]["c"] - a[k]["bid"]["c"]) / PIP)
        spread_b.append((b[k]["ask"]["c"] - b[k]["bid"]["c"]) / PIP)
        worst = max(abs(dc), abs(dh), abs(dl))
        if worst > flag_pips:
            flagged.append({"minute": k, "close_pips": f"{dc:+.1f}", "high_pips": f"{dh:+.1f}",
                            "low_pips": f"{dl:+.1f}"})

    f = lambda x: f"{x:.2f}" if x is not None else None  # noqa: E731
    return {
        "sources": [name_a, name_b],
        "minutes_compared": len(both),
        f"minutes_only_in_{name_a}": len(set(a) - set(b)),
        f"minutes_only_in_{name_b}": len(set(b) - set(a)),
        "mid_close_diff_pips": {
            "median": f(median(close_diffs)) if both else None,
            "p95": f(_pct(close_diffs, 0.95)) if both else None,
            "max": f(max(close_diffs)) if both else None,
        },
        "mid_high_low_diff_pips_max": f(max(range_diffs)) if both else None,
        "median_spread_pips": {
            name_a: f(median(spread_a)) if both else None,
            name_b: f(median(spread_b)) if both else None,
        },
        "flag_above_pips": str(flag_pips),
        "minutes_flagged": len(flagged),
        "flagged": flagged[:30],
    }
