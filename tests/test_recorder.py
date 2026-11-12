import csv
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fxsystem.oanda_read import NotAllowed, OandaReader
from fxsystem.recorder import Recorder
from fxsystem.storage import LocalLog

UTC = timezone.utc
T0 = datetime(2026, 1, 6, 14, 0, tzinfo=UTC)  # 09:00 New York


def ts(seconds):
    return (T0 + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%f000Z")


def price(sec, bid, ask, tradeable=True, inst="EUR_USD"):
    return {"type": "PRICE", "instrument": inst, "time": ts(sec), "tradeable": tradeable,
            "bids": [{"price": bid, "liquidity": 1000000}], "asks": [{"price": ask, "liquidity": 1000000}]}


def hb(sec):
    return {"type": "HEARTBEAT", "time": ts(sec)}


class RecTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.log = LocalLog(self.root / "events.sqlite")
        self.sleeps = []

    def tearDown(self):
        self.log.close()
        self.dir.cleanup()

    def make(self, streams):
        """streams: one message list per connection; an Exception in a list is raised there.

        The recorder is told to stop once the last connection's messages are used up.
        """
        it = iter(enumerate(streams))
        state = {"done": False}

        def factory():
            i, msgs = next(it)

            def gen():
                for m in msgs:
                    if isinstance(m, Exception):
                        if i == len(streams) - 1:
                            state["done"] = True
                        raise m
                    yield m
                if i == len(streams) - 1:
                    state["done"] = True
            return gen()

        rec = Recorder(self.log, self.root, factory, now=lambda: T0 + timedelta(hours=1),
                       sleep=self.sleeps.append)
        return rec, lambda: state["done"]

    def bars(self):
        return [e.payload for e in self.log.events("market.bar_m1")]

    def tick_rows(self, day="2026-01-06"):
        with open(self.root / "ticks" / "EUR_USD" / f"{day}.csv", newline="") as f:
            return list(csv.reader(f))


class TestBars(RecTest):
    def test_bar_ohlc_from_ticks(self):
        rec, stop = self.make([[
            price(1, "1.10000", "1.10015"),
            price(20, "1.10030", "1.10040"),
            price(40, "1.09990", "1.10010"),
            price(59, "1.10010", "1.10020"),
            price(61, "1.10020", "1.10030"),  # next minute: closes the first bar
        ]])
        rec.run(stop)
        bars = self.bars()
        self.assertEqual(len(bars), 1)  # the second minute is unfinished, so not written
        b = bars[0]
        self.assertEqual(b["start_utc"], "2026-01-06T14:00:00.000000Z")
        self.assertEqual(b["bid"], {"o": "1.10000", "h": "1.10030", "l": "1.09990", "c": "1.10010"})
        self.assertEqual(b["ask"], {"o": "1.10015", "h": "1.10040", "l": "1.10010", "c": "1.10020"})
        self.assertEqual(b["ticks"], 4)
        self.assertEqual((b["spread_min"], b["spread_max"]), ("0.00010", "0.00020"))

    def test_heartbeat_closes_bar_when_market_is_quiet(self):
        rec, stop = self.make([[price(5, "1.1", "1.1001"), hb(30), hb(60)]])
        rec.run(stop)
        self.assertEqual(len(self.bars()), 1)

    def test_bar_not_closed_early(self):
        rec, stop = self.make([[price(5, "1.1", "1.1001"), hb(59)]])
        rec.run(stop)
        self.assertEqual(self.bars(), [])

    def test_minute_without_prices_has_no_bar(self):
        rec, stop = self.make([[price(5, "1.1", "1.1001"), price(185, "1.2", "1.2001"), hb(240)]])
        rec.run(stop)
        self.assertEqual([b["start_utc"][11:16] for b in self.bars()], ["14:00", "14:03"])

    def test_untradeable_prices_counted(self):
        rec, stop = self.make([[price(5, "1.1", "1.1001", tradeable=False), hb(60)]])
        rec.run(stop)
        self.assertEqual(self.bars()[0]["untradeable_ticks"], 1)

    def test_other_instruments_ignored(self):
        rec, stop = self.make([[price(5, "150.1", "150.2", inst="USD_JPY"), hb(60)]])
        rec.run(stop)
        self.assertEqual(self.bars(), [])


class TestPartialBars(RecTest):
    def test_first_minute_after_start_is_partial(self):
        rec, stop = self.make([[price(25, "1.1", "1.1001"), price(65, "1.1", "1.1001"), hb(120)]])
        rec.run(stop)
        self.assertEqual([b["partial"] for b in self.bars()], [True, False])

    def test_minute_spanning_reconnect_is_partial(self):
        rec, stop = self.make([
            [price(65, "1.1", "1.1001"), price(125, "1.1", "1.1001"), OSError("drop")],
            [price(150, "1.1", "1.1001"), price(185, "1.1", "1.1001"), hb(240)],
        ])
        rec.run(stop)
        self.assertEqual([(b["start_utc"][14:16], b["partial"]) for b in self.bars()],
                         [("01", True), ("02", True), ("03", False)])

    def test_reconnect_in_a_new_minute_marks_that_minute(self):
        rec, stop = self.make([
            [price(65, "1.1", "1.1001"), hb(120), OSError("drop")],
            [price(200, "1.1", "1.1001"), hb(240)],
        ])
        rec.run(stop)
        self.assertEqual([(b["start_utc"][14:16], b["partial"]) for b in self.bars()],
                         [("01", True), ("03", True)])


class TestTickFile(RecTest):
    def test_every_tick_written(self):
        rec, stop = self.make([[price(1, "1.10000", "1.10015"), price(2, "1.10001", "1.10016")]])
        rec.run(stop)
        rows = self.tick_rows()
        self.assertEqual(rows[0], ["time_utc", "bid", "ask", "tradeable", "received_utc"])
        self.assertEqual(rows[1][:4], ["2026-01-06T14:00:01.000000Z", "1.10000", "1.10015", "1"])
        self.assertEqual(len(rows), 3)

    def test_new_file_at_17_new_york(self):
        # 21:59:59 and 22:00:00 UTC are 16:59:59 and 17:00:00 New York in January.
        rec, stop = self.make([[price(7 * 3600 + 3599, "1.1", "1.1001"), price(8 * 3600, "1.1", "1.1001")]])
        rec.run(stop)
        self.assertEqual(len(self.tick_rows("2026-01-06")), 2)
        self.assertEqual(len(self.tick_rows("2026-01-07")), 2)


class TestReconnect(RecTest):
    def test_reconnects_logs_gap_and_skips_repeats(self):
        rec, stop = self.make([
            [price(1, "1.1", "1.1001"), price(2, "1.1", "1.1001"), TimeoutError("read timed out")],
            [price(2, "1.1", "1.1001"), price(3, "1.1", "1.1001"), hb(60)],  # 2 is a repeat
        ])
        rec.run(stop)
        self.assertEqual(rec.stats["ticks"], 3)
        self.assertEqual(rec.stats["duplicates"], 1)
        self.assertEqual(rec.stats["reconnects"], 1)
        self.assertEqual(len(self.log.events("system.stream_error")), 1)
        gaps = self.log.events("market.gap")
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].payload["from_utc"], "2026-01-06T14:00:02.000000Z")
        self.assertEqual(self.bars()[0]["ticks"], 3)

    def test_backoff_doubles_and_resets(self):
        rec, stop = self.make([[OSError("down")], [OSError("down")], [OSError("down")], [hb(1), OSError("x")], [hb(2)]])
        rec.run(stop)
        self.assertEqual(self.sleeps, [1.0, 2.0, 4.0, 1.0])

    def test_start_and_stop_logged_with_stats(self):
        rec, stop = self.make([[price(1, "1.1", "1.1001")]])
        rec.run(stop)
        self.assertEqual(len(self.log.events("system.recorder_start")), 1)
        last = self.log.events("system.recorder_stop")[0].payload
        self.assertEqual(last["ticks"], 1)
        self.assertTrue(self.log.verify()[0])

    def test_restart_does_not_duplicate_bars(self):
        msgs = [price(5, "1.1", "1.1001"), hb(60)]
        rec, stop = self.make([msgs])
        rec.run(stop)
        rec, stop = self.make([msgs])  # the same minute recorded again after a restart
        rec.run(stop)
        self.assertEqual(len(self.bars()), 1)


class TestStreamClient(unittest.TestCase):
    def test_stream_uses_stream_host_and_parses_lines(self):
        seen = []

        def fake(req, timeout):
            seen.append(req)
            return [b'{"type":"HEARTBEAT","time":"2026-01-06T14:00:00.000000000Z"}\n', b"\n"]

        r = OandaReader(token="t", stream_transport=fake)
        msgs = list(r.stream("/v3/accounts/101-001-1-001/pricing/stream", {"instruments": "EUR_USD"}))
        self.assertEqual(msgs[0]["type"], "HEARTBEAT")
        self.assertTrue(seen[0].full_url.startswith("https://stream-fxpractice.oanda.com/v3/accounts/"))
        self.assertEqual(seen[0].get_method(), "GET")

    def test_stream_and_get_not_mixed(self):
        r = OandaReader(token="t")
        with self.assertRaises(NotAllowed):
            r.get("/v3/accounts/101-001-1-001/pricing/stream")
        with self.assertRaises(NotAllowed):
            list(r.stream("/v3/accounts"))


if __name__ == "__main__":
    unittest.main()
