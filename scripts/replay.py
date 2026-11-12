"""Replay saved history through the system.

    python scripts/replay.py --from 2026-10-01 --to 2026-11-11

Uses the placeholder "plumbing-test" strategy, which is NOT a trading strategy.
Each run gets its own log in data/replays/. Run the same command twice: the
fingerprint must be identical, which shows the replay is repeatable.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fxsystem.clock import load_windows, trading_day_bounds  # noqa: E402
from fxsystem.history import read_bars  # noqa: E402
from fxsystem.quality import Limits  # noqa: E402
from fxsystem.replay import PlumbingTestStrategy, run_replay  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="first", required=True, help="first trading day")
    ap.add_argument("--to", dest="last", required=True, help="last trading day")
    ap.add_argument("--data", default=str(ROOT / "data"))
    a = ap.parse_args()

    start = trading_day_bounds(date.fromisoformat(a.first))[0]
    end = trading_day_bounds(date.fromisoformat(a.last))[1]
    bars = read_bars(a.data, start, end)
    if not bars:
        print("No saved history for those days. Run scripts/load_history.py first.")
        return 1

    manifest_path = Path(a.data) / "history" / "EUR_USD" / "M1" / "manifest.json"
    window = load_windows(str(ROOT / "config" / "sessions.toml"))["operating"]
    r = run_replay(
        bars,
        PlumbingTestStrategy(window),
        a.data,
        Limits.load(str(ROOT / "config" / "quality.toml")),
        inputs={"history_manifest": manifest_path.read_text() if manifest_path.exists() else None},
    )
    print(f"Replayed {r.bars} bars: {r.intents} trade ideas from the plumbing-test strategy.")
    print(f"NO TRADE bars by reason: {r.no_trade or 'none'}")
    print(f"Fingerprint: {r.fingerprint[:16]}  (same input must always give the same fingerprint)")
    print(f"Details: {r.log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
