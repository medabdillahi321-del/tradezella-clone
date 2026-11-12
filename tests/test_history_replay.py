import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fxsystem.clock import Window
from fxsystem.history import OandaHistory, read_bars
from fxsystem.quality import Limits
from fxsystem.replay import Intent, PlumbingTestStrategy, bar_check, run_replay
from fxsystem.storage import LocalLog

UTC = timezone.utc
T0 = datetime(2026, 1, 6, 14, 0, tzinfo=UTC)  # Tuesday 09:00 New York
WINDOW = Window("operating", "America/New_York", time(9), time(12), "approved")


def candle(t, mid=1.1, spread=0.0001, complete=True):
    b, a = f"{mid:.5f}", f"{mid + spread:.5f}"
    return {"time": t.strftime("%Y-%m-%dT%H:%M:%S.000000000Z"), "complete": complete, "volume": 10,
            "bid": {"o": b, "h": b, "l": b, "c": b}, "ask": {"o": a, "h": a, "l": a, "c": a}}


class FakeReader:
    def __init__(self, candles):
        self.candles = sorted(candles, key=lambda c: c["time"])
        self.calls = []

    def get(self, path, params):
        self.calls.append(params)
        start = params["from"].replace("Z", ".000000000Z")
        n = int(params["count"])
        return {"candles": [c for c in self.candles if c["time"] >= start][:n]}


def bar(minute, mid="1.10000", spread="0.00010", base=T0):
    m, s = Decimal(mid), Decimal(spread)
    return {"start": base + timedelta(minutes=minute),
            "bid": {k: m for k in "ohlc"}, "ask": {k: m + s for k in "ohlc"}, "volume": 1}


class TestHistory(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 1, 9, 12, tzinfo=UTC)

    def tearDown(self):
        self.dir.cleanup()

    def make(self, candles):
        self.reader = FakeReader(candles)
        return OandaHistory(self.reader, self.dir.name, now=lambda: self.now, sleep=lambda s: None)

    def test_saves_by_trading_day_and_skips_incomplete(self):
        cs = [candle(datetime(2026, 1, 5, 21, 59, tzinfo=UTC)),   # Mon 16:59 NY -> trading day Mon
              candle(datetime(2026, 1, 5, 22, 0, tzinfo=UTC)),    # Mon 17:00 NY -> trading day Tue
              candle(datetime(2026, 1, 6, 14, 0, tzinfo=UTC)),
              candle(datetime(2026, 1, 6, 14, 1, tzinfo=UTC), complete=False)]
        h = self.make(cs)
        s = h.load(date(2026, 1, 5), date(2026, 1, 6))
        self.assertEqual(s["bars"], 3)
        self.assertEqual(len(read_bars(self.dir.name, datetime(2026, 1, 5, tzinfo=UTC), datetime(2026, 1, 7, tzinfo=UTC))), 3)
        tue = (Path(self.dir.name) / "history/EUR_USD/M1/2026-01-06.csv").read_text().splitlines()
        self.assertEqual(len(tue), 3)  # header + 2 bars

    def test_pages_through_5000_limit(self):
        from fxsystem.clock import market_open
        self.now = datetime(2026, 1, 13, 12, tzinfo=UTC)  # all bars are in the past
        times = [T0 + timedelta(minutes=i) for i in range(8000)]
        cs = [candle(t) for t in times if market_open(t)]  # real data has no weekend bars
        h = self.make(cs)
        s = h.load(date(2026, 1, 6), date(2026, 1, 12))
        self.assertGreaterEqual(len(self.reader.calls), 2)
        self.assertEqual(s["bars"], len(cs))

    def test_stops_at_now(self):
        cs = [candle(T0 + timedelta(minutes=i)) for i in range(7000)]  # some "in the future"
        s = self.make(cs).load(date(2026, 1, 6), date(2026, 1, 12))
        self.assertTrue(all(b["start"] < self.now for b in read_bars(
            self.dir.name, datetime(2026, 1, 5, tzinfo=UTC), datetime(2026, 1, 13, tzinfo=UTC))))
        self.assertLess(s["bars"], 7000)

    def test_finished_days_not_fetched_again(self):
        h = self.make([candle(T0)])
        h.load(date(2026, 1, 6), date(2026, 1, 6))
        h2 = self.make([candle(T0)])
        s = h2.load(date(2026, 1, 6), date(2026, 1, 6))
        self.assertEqual((s["fetched"], s["already_saved"]), (0, 1))
        self.assertEqual(self.reader.calls, [])

    def test_unfinished_day_is_fetched_again(self):
        self.now = datetime(2026, 1, 6, 15, tzinfo=UTC)  # Tuesday's trading day is still running
        self.make([candle(T0)]).load(date(2026, 1, 6), date(2026, 1, 6))
        s = self.make([candle(T0), candle(T0 + timedelta(minutes=1))]).load(date(2026, 1, 6), date(2026, 1, 6))
        self.assertEqual((s["fetched"], s["bars"]), (1, 2))

    def test_changed_file_detected(self):
        h = self.make([candle(T0)])
        h.load(date(2026, 1, 6), date(2026, 1, 6))
        p = Path(self.dir.name) / "history/EUR_USD/M1/2026-01-06.csv"
        p.write_text(p.read_text().replace("1.10000", "1.20000"))
        self.assertEqual(h.verify(), ["2026-01-06"])


class Spy:
    name, version = "spy", "1"

    def __init__(self):
        self.seen = []

    def on_bar(self, bars, now):
        self.seen.append((len(bars), bars[-1]["start"], now))
        return []


class TestReplay(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dir.cleanup()

    def test_no_look_ahead(self):
        bars = [bar(i) for i in range(10)]
        spy = Spy()
        run_replay(bars, spy, self.dir.name)
        for i, (n, last_start, now) in enumerate(spy.seen):
            self.assertEqual(n, i + 1)  # never more bars than have closed
            self.assertEqual(last_start, bars[i]["start"])
            self.assertEqual(now, bars[i]["start"] + timedelta(minutes=1))  # decided at the bar's close

    def test_bad_bars_never_reach_the_strategy(self):
        bars = [bar(0), bar(1, spread="0.00050"), bar(2), bar(12), bar(13, mid="1.1", spread="-0.00001")]
        spy = Spy()
        r = run_replay(bars, spy, self.dir.name)
        self.assertEqual([s[1] for s in spy.seen], [bars[0]["start"], bars[2]["start"]])
        self.assertEqual(r.no_trade, {"wide_spread": 1, "gap": 1, "crossed": 1})

    def test_weekend_bars_refused(self):
        sat = datetime(2026, 1, 10, 12, tzinfo=UTC)
        r = run_replay([bar(0, base=sat)], Spy(), self.dir.name)
        self.assertEqual(r.no_trade, {"market_closed": 1})

    def wave(self, n=300):
        import math
        return [bar(i, mid=f"{1.1 + 0.0015 * math.sin(i / 9):.5f}") for i in range(n)]

    def test_repeatable(self):
        bars = self.wave()
        a = run_replay(bars, PlumbingTestStrategy(WINDOW), self.dir.name, started=datetime(2026, 2, 1, tzinfo=UTC))
        b = run_replay(bars, PlumbingTestStrategy(WINDOW), self.dir.name, started=datetime(2026, 2, 2, tzinfo=UTC))
        self.assertGreater(a.intents, 0)
        self.assertEqual((a.intents, a.fingerprint), (b.intents, b.fingerprint))
        self.assertNotEqual(a.log_path, b.log_path)  # each run has its own log

    def test_intents_logged_as_shadow_in_run_log(self):
        r = run_replay(self.wave(), PlumbingTestStrategy(WINDOW), self.dir.name)
        with LocalLog(r.log_path) as log:
            intents = log.events("shadow.intent")
            self.assertEqual(len(intents), r.intents)
            self.assertTrue(log.verify()[0])
            p = intents[0].payload
            self.assertEqual(p["strategy"], "plumbing-test")
            self.assertEqual(p["entry"], "next_bar_open")
            # Only inside 09:00-12:00 New York (14:00-17:00 UTC in January).
            for e in intents:
                self.assertTrue("14:" <= e.payload["signal_bar_utc"][11:14] < "17:")

    def test_strategy_respects_cooldown(self):
        r = run_replay(self.wave(), PlumbingTestStrategy(WINDOW, cooldown_bars=30), self.dir.name)
        with LocalLog(r.log_path) as log:
            times = [datetime.fromisoformat(e.payload["signal_bar_utc"].replace("Z", "+00:00"))
                     for e in log.events("shadow.intent")]
        self.assertTrue(all((b - a) >= timedelta(minutes=30) for a, b in zip(times, times[1:])))

    def test_intent_needs_stop(self):
        with self.assertRaises(ValueError):
            Intent("buy", Decimal(0), Decimal(20), "x")
        with self.assertRaises(ValueError):
            Intent("long", Decimal(10), Decimal(20), "x")

    def test_bar_check_ok(self):
        self.assertEqual(bar_check(bar(1), bar(0), Limits()), [])


if __name__ == "__main__":
    unittest.main()
