"""Start the live system: price recorder, live data check and health monitor together.

    python scripts/launch.py                 run until you press Ctrl+C
    python scripts/launch.py --minutes 30    run for 30 minutes, then stop

Every minute it prints one status line: the latest price, the spread, whether the
data check says OK or NO TRADE (and why), and how many bars were recorded.
When it stops, it prints the data quality report for the day.

It records prices and watches the system. It does not trade: there is no strategy yet,
and nothing in this project can place an order.
Needs OANDA_TOKEN set (see README.md).
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fxsystem.clock import NEW_YORK, load_windows, market_open, trading_day  # noqa: E402
from fxsystem.monitor import Alerter, MonitorConfig, check, webhook_sender  # noqa: E402
from fxsystem.oanda_read import OandaReader  # noqa: E402
from fxsystem.quality import Limits, QuoteGate, audit_day  # noqa: E402
from fxsystem.recorder import Recorder  # noqa: E402
from fxsystem.storage import LocalLog  # noqa: E402

UTC = timezone.utc


def main() -> int:
    import os

    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=0, help="stop after this many minutes (0 = until Ctrl+C)")
    ap.add_argument("--data", default=str(ROOT / "data"))
    a = ap.parse_args()
    data = Path(a.data)
    data.mkdir(parents=True, exist_ok=True)

    window = load_windows(str(ROOT / "config" / "sessions.toml"))["operating"]
    limits = Limits.load(str(ROOT / "config" / "quality.toml"))
    mcfg = MonitorConfig.load(ROOT / "config" / "monitor.toml")
    url = os.environ.get("ALERT_WEBHOOK_URL")
    alerter = Alerter(data, window, mcfg, webhook_sender(url) if url else None)

    reader = OandaReader()
    account = reader.accounts()[0]["id"]
    stream_path = f"/v3/accounts/{account}/pricing/stream"
    gate = QuoteGate(limits)
    stop = threading.Event()
    holder: dict = {}

    def record() -> None:
        # The recorder gets its own database connection, in its own thread.
        with LocalLog(data / "events.sqlite") as log:
            rec = Recorder(log, data, lambda: reader.stream(stream_path, {"instruments": "EUR_USD"}),
                           "EUR_USD", gate=gate)
            holder["rec"] = rec
            rec.run(should_stop=stop.is_set)

    t = threading.Thread(target=record, name="recorder", daemon=True)
    t.start()
    end = time.monotonic() + a.minutes * 60 if a.minutes else None
    now = datetime.now(UTC)
    print(f"FX shadow system started {now.astimezone(NEW_YORK):%Y-%m-%d %H:%M} New York.")
    print(f"Operating window {window.start:%H:%M}-{window.end:%H:%M} New York. "
          f"Market {'open' if market_open(now) else 'closed'}. Recording only, no trading. Ctrl+C to stop.\n")

    try:
        while not stop.is_set():
            for _ in range(60):  # status and health check once a minute
                if (end and time.monotonic() >= end) or not t.is_alive():
                    stop.set()
                    break
                time.sleep(1)
            now = datetime.now(UTC)
            alerter.handle(check(data, now, window, mcfg), now)
            rec = holder.get("rec")
            last = gate.last
            verdict = gate.check(now)
            price = f"bid {last.bid} ask {last.ask} spread {(last.ask - last.bid) / limits.pip:.1f} pips" if last else "no price yet"
            where = "IN WINDOW" if window.contains(now) else "outside window"
            print(f"{now.astimezone(NEW_YORK):%H:%M} NY  {price}  |  {verdict}  |  "
                  f"bars {rec.stats['bars'] if rec else 0}  |  {where}")
    except KeyboardInterrupt:
        print("\nStopping...")
    stop.set()
    t.join(timeout=20)

    now = datetime.now(UTC)
    with LocalLog(data / "events.sqlite") as log:
        report = audit_day(log, data, trading_day(now), limits=limits, window=window, until=now)
    rec = holder.get("rec")
    if rec:
        s = rec.stats
        print(f"\nStopped. {s['ticks']} prices, {s['bars']} bars, {s['reconnects']} reconnects.")
    print(f"Data quality today: {report['verdict']}. Median spread {report['median_spread_pips']} pips, "
          f"{report['missing_minutes']} missing minutes while recording, {len(report['spikes'])} spikes, "
          f"{len(report['frozen_periods'])} frozen periods.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
