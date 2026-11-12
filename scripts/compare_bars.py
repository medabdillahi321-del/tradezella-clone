"""Compare recorded 1-minute bars with OANDA's own 1-minute candles.

    python scripts/compare_bars.py               uses data/events.sqlite
    python scripts/compare_bars.py --data PATH

For each recorded bar it fetches OANDA's bid and ask candle for the same minute and
prints the differences in pips. Read-only.

Small differences are expected: OANDA's price stream sends at most a few prices per
second, so a very brief high or low inside a minute can be missing from the stream.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fxsystem.clock import parse_oanda_time  # noqa: E402
from fxsystem.oanda_read import OandaReader  # noqa: E402
from fxsystem.storage import LocalLog  # noqa: E402

PIP = Decimal("0.0001")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "data"))
    a = ap.parse_args()

    with LocalLog(Path(a.data) / "events.sqlite") as log:
        bars = [e.payload for e in log.events("market.bar_m1")]
    if not bars:
        print("No recorded bars yet. Run scripts/record.py first.")
        return 1

    inst = bars[0]["instrument"]
    candles = OandaReader().get(
        f"/v3/instruments/{inst}/candles",
        {"granularity": "M1", "price": "BA", "from": bars[0]["start_utc"], "to": bars[-1]["start_utc"],
         "includeFirst": "true"},
    )["candles"]
    theirs = {parse_oanda_time(c["time"]).strftime("%Y-%m-%dT%H:%M:%S.%fZ"): c for c in candles}
    # "to" excludes the last minute, so fetch it on its own.
    last = OandaReader().get(
        f"/v3/instruments/{inst}/candles",
        {"granularity": "M1", "price": "BA", "from": bars[-1]["start_utc"], "count": "1"},
    )["candles"]
    for c in last:
        theirs[parse_oanda_time(c["time"]).strftime("%Y-%m-%dT%H:%M:%S.%fZ")] = c

    worst = Decimal(0)
    exact = skipped = 0
    print(f"{'minute (UTC)':<17} {'ticks':>5}  differences in pips: bid o/h/l/c | ask o/h/l/c")
    for b in bars:
        c = theirs.get(b["start_utc"])
        if not c:
            print(f"{b['start_utc'][:16]:<17} {b['ticks']:>5}  OANDA has no candle for this minute")
            continue
        diffs = []
        for side in ("bid", "ask"):
            for k in ("o", "h", "l", "c"):
                diffs.append((Decimal(b[side][k]) - Decimal(c[side][k])) / PIP)
        if b.get("partial"):
            skipped += 1
        else:
            worst = max([worst] + [abs(d) for d in diffs])
            if all(d == 0 for d in diffs):
                exact += 1
        note = "  (incomplete minute: recording started or reconnected)" if b.get("partial") else ""
        fmt = lambda ds: "/".join(f"{d:+.1f}" for d in ds)  # noqa: E731
        print(f"{b['start_utc'][:16]:<17} {b['ticks']:>5}  {fmt(diffs[:4])} | {fmt(diffs[4:])}{note}")
    full = len(bars) - skipped
    print(f"\n{exact} of {full} complete bars match OANDA exactly. Largest difference: {worst:.1f} pips."
          f" {skipped} incomplete bar(s) not counted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
