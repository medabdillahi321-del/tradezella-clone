import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from fxsystem.clock import (
    NaiveTimeError,
    Window,
    load_windows,
    market_open,
    parse_oanda_time,
    require_utc,
    trading_day,
    trading_day_bounds,
)

UTC = timezone.utc
CONFIG = Path(__file__).resolve().parent.parent / "config" / "sessions.toml"


def u(*a):
    return datetime(*a, tzinfo=UTC)


class TestBasics(unittest.TestCase):
    def test_naive_datetime_refused(self):
        with self.assertRaises(NaiveTimeError):
            require_utc(datetime(2026, 1, 5, 14, 0))

    def test_oanda_nanoseconds_truncated(self):
        t = parse_oanda_time("2026-11-12T14:30:05.123456789Z")
        self.assertEqual(t, u(2026, 11, 12, 14, 30, 5, 123456))

    def test_oanda_whole_seconds(self):
        self.assertEqual(parse_oanda_time("2026-11-12T14:30:05Z"), u(2026, 11, 12, 14, 30, 5))

    def test_oanda_rejects_non_utc(self):
        with self.assertRaises(ValueError):
            parse_oanda_time("2026-11-12T14:30:05+01:00")


class TestOperatingWindow(unittest.TestCase):
    """09:00-12:00 New York, across both DST changes."""

    def setUp(self):
        self.w = load_windows(str(CONFIG))["operating"]

    def test_winter(self):  # EST, UTC-5
        self.assertEqual(self.w.bounds(date(2026, 1, 6)), (u(2026, 1, 6, 14), u(2026, 1, 6, 17)))

    def test_summer(self):  # EDT, UTC-4
        self.assertEqual(self.w.bounds(date(2026, 7, 7)), (u(2026, 7, 7, 13), u(2026, 7, 7, 16)))

    def test_day_after_spring_forward(self):  # US clocks change 2026-03-08
        self.assertEqual(self.w.bounds(date(2026, 3, 9))[0], u(2026, 3, 9, 13))

    def test_day_after_fall_back(self):  # US clocks change 2026-11-01
        self.assertEqual(self.w.bounds(date(2026, 11, 2))[0], u(2026, 11, 2, 14))

    def test_contains_edges(self):
        self.assertFalse(self.w.contains(u(2026, 1, 6, 13, 59, 59)))
        self.assertTrue(self.w.contains(u(2026, 1, 6, 14)))
        self.assertTrue(self.w.contains(u(2026, 1, 6, 16, 59, 59)))
        self.assertFalse(self.w.contains(u(2026, 1, 6, 17)))


class TestLondonNewYorkGap(unittest.TestCase):
    """US moves to summer time three weeks before the UK (2026-03-08 vs 03-29)."""

    def test_offset_between_cities_changes(self):
        w = load_windows(str(CONFIG))
        london, ny = w["london"], w["new_york"]
        # Normal: London 08:00 is 5 hours before New York 08:00.
        self.assertEqual(ny.bounds(date(2026, 3, 2))[0] - london.bounds(date(2026, 3, 2))[0], timedelta(hours=5))
        # In the gap: only 4 hours.
        self.assertEqual(ny.bounds(date(2026, 3, 16))[0] - london.bounds(date(2026, 3, 16))[0], timedelta(hours=4))
        # After the UK changes too: back to 5.
        self.assertEqual(ny.bounds(date(2026, 3, 30))[0] - london.bounds(date(2026, 3, 30))[0], timedelta(hours=5))

    def test_uk_fall_back(self):  # UK clocks change 2026-10-25
        london = load_windows(str(CONFIG))["london"]
        self.assertEqual(london.bounds(date(2026, 10, 23))[0], u(2026, 10, 23, 7))
        self.assertEqual(london.bounds(date(2026, 10, 26))[0], u(2026, 10, 26, 8))


class TestTokyo(unittest.TestCase):
    def test_no_dst(self):
        asia = load_windows(str(CONFIG))["asia"]
        self.assertEqual(asia.bounds(date(2026, 1, 6))[0], u(2026, 1, 6, 0))
        self.assertEqual(asia.bounds(date(2026, 7, 7))[0], u(2026, 7, 7, 0))


class TestMidnightCrossing(unittest.TestCase):
    def test_window_crossing_midnight(self):
        w = Window("late", "America/New_York", time(20, 0), time(2, 0))
        self.assertEqual(w.bounds(date(2026, 1, 6)), (u(2026, 1, 7, 1), u(2026, 1, 7, 7)))
        self.assertTrue(w.contains(u(2026, 1, 7, 6)))  # 01:00 New York, opened the day before
        self.assertFalse(w.contains(u(2026, 1, 7, 8)))


class TestDstEdgeTimes(unittest.TestCase):
    def test_nonexistent_local_time_moves_forward(self):
        # 02:30 does not exist in New York on 2026-03-08; it maps to 03:30 EDT.
        w = Window("x", "America/New_York", time(2, 30), time(4, 0))
        self.assertEqual(w.bounds(date(2026, 3, 8))[0], u(2026, 3, 8, 7, 30))

    def test_ambiguous_local_time_uses_first(self):
        # 01:30 happens twice in New York on 2026-11-01; the first is EDT.
        w = Window("x", "America/New_York", time(1, 30), time(4, 0))
        self.assertEqual(w.bounds(date(2026, 11, 1))[0], u(2026, 11, 1, 5, 30))


class TestTradingDay(unittest.TestCase):
    def test_rolls_at_17_new_york(self):
        self.assertEqual(trading_day(u(2026, 1, 6, 21, 59)), date(2026, 1, 6))  # 16:59 EST
        self.assertEqual(trading_day(u(2026, 1, 6, 22, 0)), date(2026, 1, 7))  # 17:00 EST

    def test_summer_boundary(self):
        self.assertEqual(trading_day(u(2026, 7, 7, 20, 59)), date(2026, 7, 7))  # 16:59 EDT
        self.assertEqual(trading_day(u(2026, 7, 7, 21, 0)), date(2026, 7, 8))

    def test_sunday_open_belongs_to_monday(self):
        self.assertEqual(trading_day(u(2026, 1, 4, 22, 0)), date(2026, 1, 5))

    def test_bounds_are_24h_on_normal_days(self):
        s, e = trading_day_bounds(date(2026, 1, 6))
        self.assertEqual((s, e), (u(2026, 1, 5, 22), u(2026, 1, 6, 22)))

    def test_bounds_around_fall_back(self):
        # Monday 2026-11-02 starts Sunday 17:00 EST, after the change.
        s, e = trading_day_bounds(date(2026, 11, 2))
        self.assertEqual((s, e), (u(2026, 11, 1, 22), u(2026, 11, 2, 22)))
        # The weekend day that contains the change is 25 hours long.
        s, e = trading_day_bounds(date(2026, 11, 1))
        self.assertEqual(e - s, timedelta(hours=25))

    def test_every_moment_falls_in_its_own_day(self):
        t = u(2026, 2, 25)
        while t < u(2026, 4, 5):  # spans the US and UK spring changes
            s, e = trading_day_bounds(trading_day(t))
            self.assertTrue(s <= t < e, t)
            t += timedelta(minutes=37)


class TestMarketOpen(unittest.TestCase):
    def test_week(self):
        self.assertFalse(market_open(u(2026, 1, 4, 21, 59)))  # Sun 16:59 EST
        self.assertTrue(market_open(u(2026, 1, 4, 22, 0)))  # Sun 17:00 EST
        self.assertTrue(market_open(u(2026, 1, 9, 21, 59)))  # Fri 16:59 EST
        self.assertFalse(market_open(u(2026, 1, 9, 22, 0)))  # Fri 17:00 EST
        self.assertFalse(market_open(u(2026, 1, 10, 12, 0)))  # Saturday


if __name__ == "__main__":
    unittest.main()
