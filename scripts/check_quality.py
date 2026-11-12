"""End-of-day data quality report for a recorded trading day.

    python scripts/check_quality.py                   today's trading day, so far
    python scripts/check_quality.py --day 2026-11-12

Prints the report and saves it to the event log as "market.quality_report".
Changes no recorded data.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fxsystem.clock import load_windows, trading_day  # noqa: E402
from fxsystem.quality import Limits, audit_day  # noqa: E402
from fxsystem.storage import LocalLog  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", help="trading day, YYYY-MM-DD (default: the current one)")
    ap.add_argument("--data", default=str(ROOT / "data"))
    ap.add_argument("--instrument", default="EUR_USD")
    a = ap.parse_args()

    now = datetime.now(timezone.utc)
    day = date.fromisoformat(a.day) if a.day else trading_day(now)
    limits = Limits.load(str(ROOT / "config" / "quality.toml"))
    window = load_windows(str(ROOT / "config" / "sessions.toml"))["operating"]

    with LocalLog(Path(a.data) / "events.sqlite") as log:
        report = audit_day(log, a.data, day, a.instrument, limits, window, until=now)
        log.append("market.quality_report", now, report)
    print(json.dumps(report, indent=2))
    return 0 if report["verdict"] == "clean" else 1


if __name__ == "__main__":
    raise SystemExit(main())
