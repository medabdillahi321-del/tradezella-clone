"""Milestone 0: check this computer is ready.

Run from the project folder:
    python scripts/check_env.py            (offline checks only)
    python scripts/check_env.py --online   (also asks OANDA for your practice accounts)

Prints one line per check and ends with READY or NOT READY.
It never prints your API token and never places an order.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

results: list[tuple[bool, str]] = []


def check(ok: bool, text: str) -> None:
    results.append((ok, text))
    print(f"{'OK  ' if ok else 'FAIL'}  {text}")


def main() -> int:
    v = sys.version_info
    check(v >= (3, 11), f"Python {v.major}.{v.minor} (3.11 or newer needed)")

    try:
        from zoneinfo import ZoneInfo

        for z in ("America/New_York", "Europe/London", "Asia/Tokyo"):
            ZoneInfo(z)
        check(True, "Time zone database")
    except Exception as e:  # noqa: BLE001
        check(False, f"Time zone database: {e}. On Windows run: python -m pip install tzdata")

    free_gb = shutil.disk_usage(Path.cwd()).free / 1e9
    check(free_gb >= 5, f"Free disk space {free_gb:.0f} GB (5 GB or more suggested for tick recordings)")

    try:
        from fxsystem.clock import load_windows

        w = load_windows(str(Path(__file__).resolve().parent.parent / "config" / "sessions.toml"))
        check("operating" in w, "Session config loads")
    except Exception as e:  # noqa: BLE001
        check(False, f"Session config: {e}")

    has_token = bool(os.environ.get("OANDA_TOKEN"))
    check(has_token, "OANDA_TOKEN is set" if has_token else "OANDA_TOKEN is not set (needed for live data)")

    if "--online" in sys.argv and has_token:
        try:
            from fxsystem.oanda_read import OandaReader

            accounts = OandaReader().accounts()
            check(bool(accounts), f"OANDA practice API answered: {len(accounts)} account(s)")
        except Exception as e:  # noqa: BLE001
            check(False, f"OANDA practice API: {type(e).__name__}: {e}")

    ready = all(ok for ok, _ in results)
    print("\nREADY" if ready else "\nNOT READY: fix the FAIL lines above")
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
