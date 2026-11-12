"""Cross-check OANDA against Dukascopy for a time range.

    python scripts/crosscheck.py --day 2026-11-11            09:00-12:00 New York that day
    python scripts/crosscheck.py --from 2026-11-11T14:00 --to 2026-11-11T17:00   (UTC)

OANDA's 1-minute candles come from its API (read-only). Dukascopy's ticks are
downloaded slowly, one hourly file at a time, and kept in data/dukascopy/ so
they are never downloaded twice. The report is printed and saved to the event
log as "market.crosscheck_report".
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fxsystem.clock import load_windows  # noqa: E402
from fxsystem.crosscheck import bars_from_ticks, compare, oanda_candles_to_bars  # noqa: E402
from fxsystem.dukascopy import DukascopyCache, RateLimited, hour_url  # noqa: E402
from fxsystem.oanda_read import OandaReader  # noqa: E402
from fxsystem.storage import LocalLog  # noqa: E402

UTC = timezone.utc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", help="New York date; checks the operating window that day")
    ap.add_argument("--from", dest="start", help="UTC start, e.g. 2026-11-11T14:00")
    ap.add_argument("--to", dest="end", help="UTC end")
    ap.add_argument("--data", default=str(ROOT / "data"))
    ap.add_argument("--list-files", action="store_true",
                    help="only list the Dukascopy files needed and where to save them (for downloading in a browser)")
    a = ap.parse_args()

    if a.day:
        start, end = load_windows(str(ROOT / "config" / "sessions.toml"))["operating"].bounds(date.fromisoformat(a.day))
    elif a.start and a.end:
        start = datetime.fromisoformat(a.start).replace(tzinfo=UTC)
        end = datetime.fromisoformat(a.end).replace(tzinfo=UTC)
    else:
        ap.error("give --day, or --from and --to")

    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    cache = DukascopyCache(a.data)

    if a.list_files:
        print("Download each link in your browser and save it under the name shown.\n"
              "Files already in place are marked done.\n")
        h = start.replace(minute=0, second=0, microsecond=0)
        while h < end:
            p = cache.path_for(h)
            done = "done  " if p.exists() else "needed"
            print(f"{done}  {hour_url('EURUSD', h)}\n        save as: {p}\n")
            h += timedelta(hours=1)
        return 0
    candles = OandaReader().get("/v3/instruments/EUR_USD/candles",
                                {"granularity": "M1", "price": "BA", "from": iso(start), "to": iso(end)})["candles"]
    oanda = oanda_candles_to_bars(candles)

    try:
        duka = bars_from_ticks(cache.ticks(start, end))
    except RateLimited as e:
        print(f"Stopped: {e}. Files already downloaded are kept; run again later to continue.\n"
              f"Or download the files in your browser: run this command again with --list-files.")
        return 2

    report = compare(oanda, duka, "oanda", "dukascopy")
    report["from_utc"], report["to_utc"] = iso(start), iso(end)
    with LocalLog(Path(a.data) / "events.sqlite") as log:
        log.append("market.crosscheck_report", end, report)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
