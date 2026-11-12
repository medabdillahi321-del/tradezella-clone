"""Kill switch: stop all new trades immediately.

    python scripts/kill.py on      every decision becomes NO TRADE
    python scripts/kill.py off     allow decisions again (only after you have checked why)
    python scripts/kill.py         show whether it is on

It works by creating or removing the file data/KILL, so it also works if the
system is not running, and you can turn it on by hand by creating that file.
Each change is written to the event log.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fxsystem.storage import LocalLog  # noqa: E402

DATA = ROOT / "data"
FLAG = DATA / "KILL"


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg not in ("", "on", "off"):
        print(__doc__)
        return 2
    DATA.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    if arg == "on":
        FLAG.write_text(f"Kill switch turned on at {now.isoformat()}\n")
        with LocalLog(DATA / "events.sqlite") as log:
            log.append("system.kill_switch", now, {"state": "on"})
    elif arg == "off":
        FLAG.unlink(missing_ok=True)
        with LocalLog(DATA / "events.sqlite") as log:
            log.append("system.kill_switch", now, {"state": "off"})
    print("Kill switch is ON: no new trades." if FLAG.exists() else "Kill switch is off.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
