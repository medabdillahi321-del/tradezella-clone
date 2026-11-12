import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fxsystem.clock import Window
from fxsystem.quality import Limits, QuoteGate, audit_day
from fxsystem.recorder import Recorder, Tick
from fxsystem.storage import LocalLog

UTC = timezone.utc
T0 = datetime(2026, 1, 6, 14, 0, tzinfo=UTC)  # Tuesday 09:00 New York
CONFIG = Path(__file__).resolve().parent.parent / "config" / "quality.toml"


def tick(sec, bid="1.10000", ask="1.10010", tradeable=True, base=T0):
    t = base + timedelta(seconds=sec)
    return Tick(t, Decimal(bid), Decimal(ask), tradeable, t)


class TestGate(unittest.TestCase):
    def setUp(self):
        self.g = QuoteGate(Limits.load(str(CONFIG)))

    def at(self, sec):
        return T0 + timedelta(seconds=sec)

    def test_good_price_ok(self):
        self.g.on_tick(tick(0))
        v = self.g.check(self.at(1))
        self.assertTrue(v.ok, str(v))
        self.assertEqual(str(v), "OK")

    def test_no_data_is_no_trade(self):
        v = self.g.check(self.at(0))
        self.assertFalse(v.ok)
        self.assertIn("no price received yet", v.reasons)

    def test_stale_feed(self):
        self.g.on_tick(tick(0))
        self.assertTrue(self.g.check(self.at(10)).ok)
        v = self.g.check(self.at(11))
        self.assertFalse(v.ok)
        self.assertTrue(any("stale" in r for r in v.reasons))

    def test_heartbeat_keeps_feed_fresh_but_not_price(self):
        self.g.on_tick(tick(0))
        for s in range(5, 125, 5):
            self.g.on_heartbeat(self.at(s))
        v = self.g.check(self.at(121))
        self.assertFalse(any("stale" in r for r in v.reasons))
        self.assertTrue(any("frozen" in r for r in v.reasons))

    def test_crossed_quote(self):
        self.g.on_tick(tick(0, "1.10010", "1.10000"))
        self.assertTrue(any("crossed" in r for r in self.g.check(self.at(1)).reasons))

    def test_locked_quote(self):
        self.g.on_tick(tick(0, "1.10000", "1.10000"))
        self.assertFalse(self.g.check(self.at(1)).ok)

    def test_wide_spread(self):
        self.g.on_tick(tick(0, "1.10000", "1.10030"))  # exactly 3.0 pips: allowed
        self.assertTrue(self.g.check(self.at(1)).ok)
        self.g.on_tick(tick(1, "1.10000", "1.10031"))
        self.assertTrue(any("spread 3.1" in r for r in self.g.check(self.at(2)).reasons))

    def test_untradeable(self):
        self.g.on_tick(tick(0, tradeable=False))
        self.assertIn("OANDA marked the price untradeable", self.g.check(self.at(1)).reasons)

    def test_spike_and_cooldown(self):
        self.g.on_tick(tick(0, "1.10000", "1.10010"))
        self.g.on_tick(tick(1, "1.10200", "1.10210"))  # 20 pips in one second
        self.assertTrue(any("spike of 20.0" in r for r in self.g.check(self.at(2)).reasons))
        self.g.on_tick(tick(55, "1.10201", "1.10211"))
        self.assertFalse(self.g.check(self.at(56)).ok)  # still cooling down
        self.g.on_tick(tick(61, "1.10202", "1.10212"))
        self.assertTrue(self.g.check(self.at(62)).ok)

    def test_insane_price(self):
        self.g.on_tick(tick(0, "11.00000", "11.00010"))
        self.assertTrue(any("sane range" in r for r in self.g.check(self.at(1)).reasons))

    def test_market_closed(self):
        sat = datetime(2026, 1, 10, 15, 0, tzinfo=UTC)
        self.g.on_tick(tick(0, base=sat))
        self.assertIn("market closed", self.g.check(sat + timedelta(seconds=1)).reasons)

    def test_future_price_time(self):
        self.g.on_tick(tick(10))
        self.assertTrue(any("future" in r for r in self.g.check(self.at(0)).reasons))

    def test_several_reasons_at_once(self):
        self.g.on_tick(tick(0, "1.10000", "1.10050", tradeable=False))
        self.assertGreaterEqual(len(self.g.check(self.at(30)).reasons), 3)

    def test_recorder_feeds_gate(self):
        with tempfile.TemporaryDirectory() as d:
            log = LocalLog(Path(d) / "e.sqlite")
            g = QuoteGate()
            msgs = [{"type": "PRICE", "instrument": "EUR_USD", "time": "2026-01-06T14:00:01.000000000Z",
                     "tradeable": True, "bids": [{"price": "1.10000"}], "asks": [{"price": "1.10010"}]},
                    {"type": "HEARTBEAT", "time": "2026-01-06T14:00:06.000000000Z"}]
            done = {"x": False}

            def factory():
                def gen():
                    yield from msgs
                    done["x"] = True
                return gen()
            Recorder(log, d, factory, gate=g, sleep=lambda s: None).run(lambda: done["x"])
            log.close()
            self.assertTrue(g.check(T0 + timedelta(seconds=7)).ok)


class TestAudit(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.log = LocalLog(self.root / "events.sqlite")
        self.window = Window("operating", "America/New_York", time(9, 0), time(12, 0), "approved")

    def tearDown(self):
        self.log.close()
        self.dir.cleanup()

    def bar(self, minute, partial=False):
        start = T0 + timedelta(minutes=minute)
        self.log.append("market.bar_m1", start, {
            "instrument": "EUR_USD", "start_utc": start.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "partial": partial})

    def write_ticks(self, rows):
        p = self.root / "ticks" / "EUR_USD"
        p.mkdir(parents=True)
        with open(p / "2026-01-06.csv", "w") as f:
            f.write("time_utc,bid,ask,tradeable,received_utc\n")
            for sec, bid, ask in rows:
                t = (T0 + timedelta(seconds=sec)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                f.write(f"{t},{bid},{ask},1,{t}\n")

    def audit(self, minutes):
        return audit_day(self.log, self.root, date(2026, 1, 6), window=self.window,
                         until=T0 + timedelta(minutes=minutes))

    def test_clean_window(self):
        for m in range(5):
            self.bar(m)
        self.write_ticks([(s, "1.10000", "1.10010") if s % 2 else (s, "1.10001", "1.10011") for s in range(0, 300, 10)])
        r = self.audit(5)
        self.assertEqual(r["missing_minutes_in_operating_window"], 0)
        self.assertEqual(r["verdict"], "clean")
        self.assertEqual(r["median_spread_pips"], "1.0")

    def test_missing_minutes_found(self):
        for m in (0, 1, 3, 4):
            self.bar(m, partial=(m == 0))
        r = self.audit(5)
        self.assertEqual(r["missing_minutes_in_operating_window"], 1)
        self.assertEqual(r["first_missing"], ["2026-01-06T14:02:00Z"])
        self.assertEqual(r["open_minutes_not_recorded"], 16 * 60)  # since 17:00 New York
        self.assertEqual(r["incomplete_bars"], 1)
        self.assertEqual(r["verdict"], "problems found")

    def test_tick_problems_found(self):
        for m in range(5):
            self.bar(m)
        self.write_ticks([
            (0, "1.10000", "1.10010"),
            (130, "1.10001", "1.10011"),  # unchanged for 130 s: frozen
            (131, "1.10300", "1.10310"),  # 30-pip jump: spike
            (132, "1.10310", "1.10300"),  # crossed
            (133, "1.10300", "1.10400"),  # 10-pip spread
        ])
        r = self.audit(5)
        self.assertEqual(len(r["frozen_periods"]), 1)
        self.assertEqual(r["spikes"][0]["pips"], "29.9")
        self.assertEqual(r["crossed_quotes"], 1)
        self.assertEqual(r["wide_spread_ticks"], 1)

    def test_time_after_a_clean_stop_is_not_missing(self):
        self.log.append("system.recorder_start", T0, {})
        self.bar(0); self.bar(1)
        self.log.append("system.recorder_stop", T0 + timedelta(minutes=2), {})
        r = self.audit(10)
        self.assertEqual(r["missing_minutes"], 0)
        self.assertEqual(r["open_minutes_not_recorded"], 16 * 60 + 8)

    def test_time_after_a_crash_is_missing(self):
        self.log.append("system.recorder_start", T0, {})
        self.bar(0); self.bar(1)  # no stop event: the recorder died
        r = self.audit(10)
        self.assertEqual(r["missing_minutes_in_operating_window"], 8)

    def test_weekend_minutes_not_counted_as_missing(self):
        r = audit_day(self.log, self.root, date(2026, 1, 10), window=self.window)  # a Saturday
        self.assertEqual(r["missing_minutes"], 0)


if __name__ == "__main__":
    unittest.main()
