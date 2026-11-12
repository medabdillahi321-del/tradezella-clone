"""Download OANDA 1-minute history into data/history/.

    python scripts/load_history.py --from 2026-10-01 --to 2026-11-11

Dates are trading days (each ends at 17:00 New York). Days already saved and
finished are skipped. Read-only.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fxsystem.history import OandaHistory  # noqa: E402
from fxsystem.oanda_read import OandaReader  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="first", required=True)
    ap.add_argument("--to", dest="last", required=True)
    ap.add_argument("--data", default=str(ROOT / "data"))
    a = ap.parse_args()
    h = OandaHistory(OandaReader(), a.data)
    s = h.load(date.fromisoformat(a.first), date.fromisoformat(a.last))
    print(f"{s['days']} trading days: {s['fetched']} downloaded, {s['already_saved']} already saved. "
          f"{s['bars']} one-minute bars in total ({s['requests']} requests to OANDA).")
    bad = h.verify()
    print("All saved files match their checksums." if not bad else f"CHANGED FILES: {', '.join(bad)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
