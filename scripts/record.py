"""Record OANDA practice prices for EUR/USD.

    python scripts/record.py                 run until you press Ctrl+C
    python scripts/record.py --minutes 10    run for 10 minutes, then stop

Ticks go to data/ticks/EUR_USD/<trading day>.csv; finished 1-minute bars and any
connection problems go to the event log, data/events.sqlite.
Needs OANDA_TOKEN set. Read-only: it cannot place orders.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fxsystem.oanda_read import OandaReader  # noqa: E402
from fxsystem.recorder import Recorder  # noqa: E402
from fxsystem.storage import LocalLog  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=0, help="stop after this many minutes (0 = run until Ctrl+C)")
    ap.add_argument("--instrument", default="EUR_USD")
    ap.add_argument("--data", default=str(ROOT / "data"))
    a = ap.parse_args()

    reader = OandaReader()
    account = reader.accounts()[0]["id"]
    path = f"/v3/accounts/{account}/pricing/stream"
    end = time.monotonic() + a.minutes * 60 if a.minutes else None

    with LocalLog(Path(a.data) / "events.sqlite") as log:
        rec = Recorder(log, a.data, lambda: reader.stream(path, {"instruments": a.instrument}), a.instrument)
        print(f"Recording {a.instrument}. Press Ctrl+C to stop.")
        try:
            rec.run(should_stop=lambda: end is not None and time.monotonic() >= end)
        except KeyboardInterrupt:
            pass
        s = rec.stats
        print(f"Stopped. {s['ticks']} prices, {s['bars']} finished 1-minute bars, "
              f"{s['reconnects']} reconnects, {s['duplicates']} repeated prices skipped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
