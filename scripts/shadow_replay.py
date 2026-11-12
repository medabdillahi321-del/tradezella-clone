"""Run saved history through the full shadow pipeline: strategy, data check,
risk engine and simulator, on a virtual $10,000 account.

    python scripts/shadow_replay.py --from 2026-10-13 --to 2026-11-11
    python scripts/shadow_replay.py --from 2026-10-13 --to 2026-11-11 --partial 1:0.5:be

--flat-at HH:MM closes any trade still open at that New York time on its entry day.
--partial AT_R:FRACTION[:be] closes FRACTION of the position at AT_R R, and with
":be" moves the stop on the rest to the entry price. It can be given more than once.

Uses the placeholder "plumbing-test" strategy: the results say nothing about any
real strategy. They only show the pipeline works.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fxsystem.clock import load_windows, trading_day_bounds  # noqa: E402
from fxsystem.history import read_bars  # noqa: E402
from fxsystem.quality import Limits  # noqa: E402
from fxsystem.replay import PlumbingTestStrategy  # noqa: E402
from fxsystem.risk import RiskConfig, RiskEngine  # noqa: E402
from fxsystem.shadow import Partial, run_shadow  # noqa: E402


def parse_partial(s: str) -> Partial:
    parts = s.split(":")
    return Partial(Decimal(parts[0]), Decimal(parts[1]), len(parts) > 2 and parts[2] == "be")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="first", required=True)
    ap.add_argument("--to", dest="last", required=True)
    ap.add_argument("--equity", default="10000")
    ap.add_argument("--partial", action="append", default=[])
    ap.add_argument("--flat-at", help="close any open trade at this New York time, e.g. 16:55")
    ap.add_argument("--data", default=str(ROOT / "data"))
    a = ap.parse_args()

    start = trading_day_bounds(date.fromisoformat(a.first))[0]
    end = trading_day_bounds(date.fromisoformat(a.last))[1]
    bars = read_bars(a.data, start, end)
    if not bars:
        print("No saved history for those days. Run scripts/load_history.py first.")
        return 1
    window = load_windows(str(ROOT / "config" / "sessions.toml"))["operating"]
    engine = RiskEngine(RiskConfig.load(ROOT / "config" / "risk.toml"), window, a.data)
    r = run_shadow(bars, PlumbingTestStrategy(window), engine, a.data, Decimal(a.equity),
                   Limits.load(str(ROOT / "config" / "quality.toml")),
                   [parse_partial(p) for p in a.partial],
                   flat_at=time.fromisoformat(a.flat_at) if a.flat_at else None)
    print("PLACEHOLDER STRATEGY: these numbers test the pipeline, not a trading idea.\n")
    print(json.dumps({"bars": r.bars, "ideas": r.ideas, "approved": r.approved,
                      "entries_cancelled": r.cancelled_entries, "refused_reasons": r.refused,
                      **r.summary()}, indent=2))
    print(f"\nFingerprint: {r.fingerprint[:16]}\nDetails: {r.log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
