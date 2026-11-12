import csv
import json
import re
import tempfile
import unittest
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fxsystem.clock import Window
from fxsystem.qt_export import export_run, load_run
from fxsystem.replay import Intent
from fxsystem.risk import RiskConfig, RiskEngine
from fxsystem.shadow import run_shadow

UTC = timezone.utc
T0 = datetime(2026, 1, 6, 14, 0, tzinfo=UTC)  # 09:00 New York (EST)
WINDOW = Window("operating", "America/New_York", time(9), time(12), "approved")
CONFIG = Path(__file__).resolve().parent.parent / "config" / "risk.toml"
D = Decimal


def bar(minute, o, h=None, l=None):
    o, h, l = D(o), D(h or o), D(l or o)
    s = D("0.0001")
    return {"start": T0 + timedelta(minutes=minute), "volume": 7,
            "bid": {"o": o, "h": h, "l": l, "c": o}, "ask": {"o": o + s, "h": h + s, "l": l + s, "c": o + s}}


class Scripted:
    name, version = "scripted", "1"

    def __init__(self, plan):
        self.plan = plan

    def on_bar(self, bars, now):
        i = len(bars) - 1
        return [Intent(self.plan[i], D(10), D(20), "t")] if i in self.plan else []


class TestExport(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        # Trade 1: buy at bar 0, target hit on bar 2. Trade 2: sell at bar 3, stopped on bar 5.
        self.bars = [bar(0, "1.10000"), bar(1, "1.10000"), bar(2, "1.10050", h="1.10300"),
                     bar(3, "1.10000"), bar(4, "1.10000"), bar(5, "1.10050", h="1.10150")]
        engine = RiskEngine(RiskConfig.load(CONFIG), WINDOW, self.root)
        self.res = run_shadow(self.bars, Scripted({0: "buy", 3: "sell"}), engine, self.root)
        self.out = export_run(self.res.log_path, self.bars, self.root)

    def tearDown(self):
        self.dir.cleanup()

    def read(self, name):
        with open(self.out / name, newline="") as f:
            return list(csv.reader(f))

    def test_files_written(self):
        names = sorted(p.name for p in self.out.iterdir())
        self.assertIn("bars_EURUSD_M1.csv", names)
        self.assertIn("trades.csv", names)
        self.assertIn("HOW_TO_OPEN.txt", names)
        self.assertTrue(any(n.startswith("shadow-trades-") and n.endswith(".js") for n in names))

    def test_bars_are_mid_prices(self):
        rows = self.read("bars_EURUSD_M1.csv")
        self.assertEqual(rows[0], ["time", "open", "high", "low", "close", "volume"])
        self.assertEqual(rows[1], ["2026-01-06T14:00:00Z", "1.100050", "1.100050", "1.100050", "1.100050", "7"])
        self.assertEqual(len(rows), 7)

    def test_signal_rows(self):
        sig = next(p for p in self.out.iterdir() if p.name.startswith("signal_"))
        with open(sig, newline="") as f:
            rows = list(csv.reader(f))
        # Long from bar 0's close, flat on bar 2 (target), short from bar 3, flat on bar 5 (stop).
        self.assertEqual(rows, [["time", "position"],
                                ["2026-01-06T14:00:00Z", "1"], ["2026-01-06T14:02:00Z", "0"],
                                ["2026-01-06T14:03:00Z", "-1"], ["2026-01-06T14:05:00Z", "0"]])

    def test_trades_csv(self):
        rows = self.read("trades.csv")
        self.assertEqual(len(rows), 3)
        head = rows[0]
        t1 = dict(zip(head, rows[1]))
        self.assertEqual(t1["side"], "buy")
        self.assertEqual(t1["entry_new_york"], "2026-01-06 09:01")
        self.assertEqual(t1["stop"], "1.09910")
        self.assertIn("target", t1["exits"])
        self.assertEqual(dict(zip(head, rows[2]))["exits"].split(" ")[0], "stop")

    def test_indicator_embeds_trades(self):
        js = next(p for p in self.out.iterdir() if p.suffix == ".js").read_text()
        self.assertIn("export default", js)
        self.assertIn("DISPLAY ONLY", js)
        trades = json.loads(re.search(r"const TRADES = (\[.*?\]);", js).group(1))
        self.assertEqual([t["side"] for t in trades], ["buy", "sell"])
        self.assertEqual(trades[0]["entry"], int((T0 + timedelta(minutes=1)).timestamp()))
        self.assertEqual(trades[0]["exit"], int((T0 + timedelta(minutes=2)).timestamp()))  # bar of the exit
        self.assertEqual(trades[1]["why"], "stop")

    def test_load_run_refuses_non_shadow_log(self):
        from fxsystem.storage import LocalLog
        p = self.root / "other.sqlite"
        LocalLog(p).close()
        with self.assertRaises(ValueError):
            load_run(p)

    def test_run_log_unchanged(self):
        from fxsystem.storage import LocalLog
        with LocalLog(self.res.log_path) as log:
            before = log.count()
            self.assertTrue(log.verify()[0])
        export_run(self.res.log_path, self.bars, self.root)
        with LocalLog(self.res.log_path) as log:
            self.assertEqual(log.count(), before)


if __name__ == "__main__":
    unittest.main()
