"""Health monitor.

    python scripts/monitor.py --once     check once and print the result
    python scripts/monitor.py            keep checking every 30 seconds (Ctrl+C to stop)

Set ALERT_WEBHOOK_URL to also get alerts on Discord, Slack or Telegram. Treat that
address like a password: anyone who has it can post to your channel.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fxsystem.clock import load_windows  # noqa: E402
from fxsystem.monitor import Alerter, MonitorConfig, check, webhook_sender  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--every", type=float, default=30, help="seconds between checks")
    ap.add_argument("--data", default=str(ROOT / "data"))
    a = ap.parse_args()

    cfg = MonitorConfig.load(ROOT / "config" / "monitor.toml")
    window = load_windows(str(ROOT / "config" / "sessions.toml"))["operating"]
    url = os.environ.get("ALERT_WEBHOOK_URL")
    alerter = Alerter(a.data, window, cfg, webhook_sender(url) if url else None)
    Path(a.data).mkdir(parents=True, exist_ok=True)
    last_verify = 0.0
    try:
        return loop(a, cfg, window, alerter, last_verify)
    except KeyboardInterrupt:
        return 0


def loop(a, cfg, window, alerter, last_verify) -> int:
    while True:
        now = datetime.now(timezone.utc)
        verify = time.monotonic() - last_verify > 3600  # check the tamper chain once an hour
        findings = check(a.data, now, window, cfg, verify_chain=verify)
        if verify:
            last_verify = time.monotonic()
        alerter.handle(findings, now)
        if a.once:
            for f in findings:
                print(f"{f.level:<8} {f.message}")
            if not any(f.level in ("WARN", "CRITICAL") for f in findings):
                print("OK       Everything looks healthy.")
            return 1 if any(f.level == "CRITICAL" for f in findings) else 0
        time.sleep(a.every)


if __name__ == "__main__":
    raise SystemExit(main())
