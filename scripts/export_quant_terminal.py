"""Export a shadow run for Quant Terminal.

    python scripts/export_quant_terminal.py --from 2026-10-13 --to 2026-11-11
    python scripts/export_quant_terminal.py --from 2026-10-13 --to 2026-11-11 --run data/replays/<run>.sqlite

Without --run it exports the most recent shadow run. --from and --to choose which
saved price history goes into the bar file. Files go to data/exports/<run>/; read
HOW_TO_OPEN.txt there.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fxsystem.clock import trading_day_bounds  # noqa: E402
from fxsystem.history import read_bars  # noqa: E402
from fxsystem.qt_export import export_run  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="first", required=True)
    ap.add_argument("--to", dest="last", required=True)
    ap.add_argument("--run", help="a shadow run log (default: the most recent)")
    ap.add_argument("--data", default=str(ROOT / "data"))
    a = ap.parse_args()

    run = Path(a.run) if a.run else max((Path(a.data) / "replays").glob("*-shadow-*.sqlite"), default=None,
                                        key=lambda p: p.name)
    if run is None or not run.exists():
        print("No shadow run found. Run scripts/shadow_replay.py first.")
        return 1
    bars = read_bars(a.data, trading_day_bounds(date.fromisoformat(a.first))[0],
                     trading_day_bounds(date.fromisoformat(a.last))[1])
    out = export_run(run, bars, a.data)
    print(f"Exported {run.name}\n  to {out}\nOpen HOW_TO_OPEN.txt there for the steps.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
