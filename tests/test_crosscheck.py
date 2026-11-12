import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fxsystem.crosscheck import bars_from_ticks, compare, oanda_candles_to_bars
from fxsystem.dukascopy import DukascopyCache, RateLimited, decode_hour, encode_hour, hour_url
from fxsystem.recorder import Tick

UTC = timezone.utc
H = datetime(2026, 11, 11, 14, tzinfo=UTC)


def tick(sec, bid, ask):
    t = H + timedelta(seconds=sec)
    return Tick(t, Decimal(bid), Decimal(ask), True, t)


class TestFormat(unittest.TestCase):
    def test_url_month_starts_at_zero(self):
        self.assertEqual(hour_url("EURUSD", H),
                         "https://datafeed.dukascopy.com/datafeed/EURUSD/2026/10/11/14h_ticks.bi5")
        self.assertIn("/2026/00/05/", hour_url("EURUSD", datetime(2026, 1, 5, 9, tzinfo=UTC)))

    def test_round_trip(self):
        ticks = [tick(0.5, "1.10000", "1.10012"), tick(1800.25, "1.10123", "1.10130")]
        out = decode_hour(encode_hour(ticks, H), H)
        self.assertEqual([(t.time, t.bid, t.ask) for t in out], [(t.time, t.bid, t.ask) for t in ticks])

    def test_empty_hour(self):
        self.assertEqual(decode_hour(b"", H), [])

    def test_bad_length_refused(self):
        import lzma
        with self.assertRaises(ValueError):
            decode_hour(lzma.compress(b"x" * 21, format=lzma.FORMAT_ALONE), H)


class TestDefaultFetch(unittest.TestCase):
    def test_connection_reset_becomes_rate_limited(self):
        import urllib.error
        from unittest import mock
        from fxsystem import dukascopy
        for exc in (urllib.error.URLError(ConnectionResetError(104, "reset")), ConnectionResetError(104, "reset"),
                    TimeoutError("The read operation timed out"), urllib.error.URLError(TimeoutError("timed out"))):
            with mock.patch.object(dukascopy.urllib.request, "urlopen", side_effect=exc):
                with self.assertRaises(RateLimited):
                    dukascopy._default_fetch("https://datafeed.dukascopy.com/x")


class TestCache(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.urls, self.sleeps, self.now = [], [], [0.0]

    def tearDown(self):
        self.dir.cleanup()

    def cache(self, fetch=None):
        def default(url):
            self.urls.append(url)
            return encode_hour([tick(60, "1.1", "1.1001")], H)
        return DukascopyCache(self.dir.name, fetch=fetch or default, min_interval_s=10,
                              sleep=self.sleeps.append, clock=lambda: self.now[0])

    def test_downloads_once_then_uses_file(self):
        c = self.cache()
        c.hour_ticks(H)
        c.hour_ticks(H)
        self.assertEqual(len(self.urls), 1)
        self.assertTrue(c.path_for(H).exists())

    def test_requests_are_spaced(self):
        c = self.cache()
        c.hour_ticks(H)
        self.now[0] = 3.0
        c.hour_ticks(H + timedelta(hours=1))
        self.assertEqual(self.sleeps, [7.0])

    def test_rate_limit_stops_and_saves_nothing(self):
        def refuse(url):
            raise RateLimited("429")
        c = self.cache(refuse)
        with self.assertRaises(RateLimited):
            c.ticks(H, H + timedelta(hours=3))
        self.assertFalse(c.path_for(H).exists())

    def test_range_filters_ticks(self):
        c = self.cache(lambda url: encode_hour([tick(30, "1.1", "1.1001"), tick(3000, "1.1", "1.1001")], H))
        self.assertEqual(len(c.ticks(H, H + timedelta(minutes=10))), 1)


class TestCompare(unittest.TestCase):
    def bar(self, bid_c, ask_c, bid_h=None, bid_l=None):
        bid_h, bid_l = bid_h or bid_c, bid_l or bid_c
        spread = Decimal(ask_c) - Decimal(bid_c)
        d = lambda x: Decimal(x)  # noqa: E731
        return {"bid": {"o": d(bid_c), "h": d(bid_h), "l": d(bid_l), "c": d(bid_c)},
                "ask": {"o": d(ask_c), "h": d(bid_h) + spread, "l": d(bid_l) + spread, "c": d(ask_c)}}

    def test_same_mid_different_spreads_is_not_a_difference(self):
        a = {"m1": self.bar("1.10000", "1.10020")}  # mid 1.10010, spread 2.0
        b = {"m1": self.bar("1.10005", "1.10015")}  # mid 1.10010, spread 1.0
        r = compare(a, b, "x", "y")
        self.assertEqual(r["mid_close_diff_pips"]["max"], "0.00")
        self.assertEqual(r["median_spread_pips"], {"x": "2.00", "y": "1.00"})
        self.assertEqual(r["minutes_flagged"], 0)

    def test_big_difference_flagged(self):
        a = {"m1": self.bar("1.10000", "1.10010"), "m2": self.bar("1.10000", "1.10010")}
        b = {"m1": self.bar("1.10000", "1.10010"), "m2": self.bar("1.10030", "1.10040")}
        r = compare(a, b, "x", "y")
        self.assertEqual(r["minutes_flagged"], 1)
        self.assertEqual(r["flagged"][0]["close_pips"], "-3.0")

    def test_missing_minutes_counted(self):
        a = {"m1": self.bar("1.1", "1.1001"), "m2": self.bar("1.1", "1.1001")}
        b = {"m2": self.bar("1.1", "1.1001"), "m3": self.bar("1.1", "1.1001")}
        r = compare(a, b, "x", "y")
        self.assertEqual((r["minutes_compared"], r["minutes_only_in_x"], r["minutes_only_in_y"]), (1, 1, 1))

    def test_bars_from_ticks(self):
        bars = bars_from_ticks([tick(5, "1.10000", "1.10010"), tick(50, "1.10020", "1.10030"),
                                tick(65, "1.10010", "1.10020")])
        self.assertEqual(sorted(bars), ["2026-11-11T14:00", "2026-11-11T14:01"])
        self.assertEqual(bars["2026-11-11T14:00"]["bid"]["h"], Decimal("1.10020"))

    def test_oanda_incomplete_candles_skipped(self):
        c = {"bid": {"o": "1.1", "h": "1.1", "l": "1.1", "c": "1.1"},
             "ask": {"o": "1.1001", "h": "1.1001", "l": "1.1001", "c": "1.1001"}}
        bars = oanda_candles_to_bars([{**c, "time": "2026-11-11T14:00:00.000000000Z", "complete": True},
                                      {**c, "time": "2026-11-11T14:01:00.000000000Z", "complete": False}])
        self.assertEqual(list(bars), ["2026-11-11T14:00"])


if __name__ == "__main__":
    unittest.main()
