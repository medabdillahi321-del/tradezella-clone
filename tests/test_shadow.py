"""Shadow simulator tests. Bars have a constant 1-pip spread: ask = bid + 0.0001.
Account $10,000, so 1R is about $25 (0.25%), sized with a 0.5-pip slippage allowance."""

import tempfile
import unittest
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fxsystem.clock import Window
from fxsystem.replay import Intent
from fxsystem.risk import RiskConfig, RiskEngine
from fxsystem.shadow import Partial, run_shadow
from fxsystem.storage import LocalLog

UTC = timezone.utc
T0 = datetime(2026, 1, 6, 14, 0, tzinfo=UTC)  # Tuesday 09:00 New York
WINDOW = Window("operating", "America/New_York", time(9), time(12), "approved")
CONFIG = Path(__file__).resolve().parent.parent / "config" / "risk.toml"
D = Decimal
SPREAD = D("0.0001")


def bar(minute, o, h=None, l=None, c=None):
    o = D(o)
    h, l, c = D(h or o), D(l or o), D(c or o)
    return {"start": T0 + timedelta(minutes=minute),
            "bid": {"o": o, "h": h, "l": l, "c": c},
            "ask": {"o": o + SPREAD, "h": h + SPREAD, "l": l + SPREAD, "c": c + SPREAD}, "volume": 1}


class Scripted:
    """Proposes given trades at given bar numbers."""
    name, version = "scripted", "1"

    def __init__(self, plan):
        self.plan = plan

    def on_bar(self, bars, now):
        i = len(bars) - 1
        if i in self.plan:
            side, stop, target = self.plan[i]
            return [Intent(side, D(stop), D(target) if target else D(0), "test")]
        return []


class ShadowTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def run_it(self, bars, plan, partials=(), started=None, flat_at=None):
        engine = RiskEngine(RiskConfig.load(CONFIG), WINDOW, self.root)
        return run_shadow(bars, Scripted(plan), engine, self.root, partials=partials, started=started,
                          flat_at=flat_at)

    def events(self, r, kind):
        with LocalLog(r.log_path) as log:
            return [e.payload for e in log.events(kind)]


class TestFills(ShadowTest):
    def test_buy_hits_target(self):
        # Idea on bar 0 (ask close 1.10010): stop 1.09910, target 1.10210. Entry at bar 1's ask open 1.10010.
        bars = [bar(0, "1.10000"), bar(1, "1.10000"), bar(2, "1.10050", h="1.10300")]
        r = self.run_it(bars, {0: ("buy", "10", "20")})
        self.assertEqual(len(r.trades), 1)
        t = r.trades[0]
        self.assertEqual(t["entry_price"], "1.10010")
        self.assertEqual(t["exits"][0]["price"], "1.10210")  # exactly the target, never better
        self.assertEqual(t["units"], 23809)
        self.assertEqual(t["r"], D("1.905"))  # 20 pips / (10 + 0.5) pips of approved risk

    def test_buy_hits_stop(self):
        bars = [bar(0, "1.10000"), bar(1, "1.10000"), bar(2, "1.09950", l="1.09800")]
        t = self.run_it(bars, {0: ("buy", "10", "20")}).trades[0]
        self.assertEqual(t["exits"][0]["price"], "1.09910")
        self.assertEqual(t["exits"][0]["why"], "stop")
        self.assertEqual(t["r"], D("-0.952"))

    def test_gap_through_stop_fills_at_worse_open(self):
        bars = [bar(0, "1.10000"), bar(1, "1.10000"), bar(2, "1.09850", l="1.09800")]
        t = self.run_it(bars, {0: ("buy", "10", "20")}).trades[0]
        self.assertEqual(t["exits"][0]["price"], "1.09850")
        self.assertLess(t["r"], D("-1"))

    def test_stop_and_target_in_same_bar_assumes_stop(self):
        bars = [bar(0, "1.10000"), bar(1, "1.10000"), bar(2, "1.10000", h="1.10300", l="1.09800")]
        t = self.run_it(bars, {0: ("buy", "10", "20")}).trades[0]
        self.assertEqual(t["exits"][0]["why"], "stop")
        self.assertEqual(t["ambiguous_bars"], 1)

    def test_sell_uses_ask_for_stop_and_target(self):
        # Sell idea: bid close 1.10000, stop 1.10100, target 1.09800. Entry at bid open 1.10000.
        # The ask must reach 1.09800 for the target, so the bid must reach 1.09790.
        bars = [bar(0, "1.10000"), bar(1, "1.10000"), bar(2, "1.09900", l="1.09795"), bar(3, "1.09850", l="1.09790")]
        t = self.run_it(bars, {0: ("sell", "10", "20")}).trades[0]
        self.assertEqual(t["entry_price"], "1.10000")
        self.assertEqual(t["exits"][0]["why"], "target")
        self.assertEqual(t["exit_time"], "2026-01-06T14:04:00Z")  # bar 3 closes at 14:04

    def test_partial_then_break_even(self):
        # Close half at +1R (10 pips), move the stop to entry, then price returns to entry.
        bars = [bar(0, "1.10000"), bar(1, "1.10000"), bar(2, "1.10050", h="1.10120"),
                bar(3, "1.10050", l="1.10000")]
        r = self.run_it(bars, {0: ("buy", "10", "20")}, partials=[Partial(D(1), D("0.5"), True)])
        t = r.trades[0]
        self.assertEqual([x["why"] for x in t["exits"]], ["partial_1R", "stop_at_entry"])
        self.assertEqual([x["units"] for x in t["exits"]], [11904, 11905])
        self.assertEqual(t["exits"][0]["price"], "1.10110")
        self.assertEqual(t["exits"][1]["price"], "1.10010")
        self.assertEqual(D(t["pnl"]), D("11.90"))  # 11,904 units x 10 pips; the rest at break-even
        self.assertEqual(len(self.events(r, "shadow.stop_moved")), 1)

    def test_stop_before_partial_in_same_bar(self):
        bars = [bar(0, "1.10000"), bar(1, "1.10000"), bar(2, "1.10000", h="1.10120", l="1.09900")]
        t = self.run_it(bars, {0: ("buy", "10", "20")}, partials=[Partial(D(1), D("0.5"), True)]).trades[0]
        self.assertEqual([x["why"] for x in t["exits"]], ["stop"])


class TestTimeExit(ShadowTest):
    def test_flat_at_closes_open_trade(self):
        # Entry 09:01 New York; flat at 09:03, so the trade closes at the bar ending 09:03 (bid close).
        bars = [bar(0, "1.10000"), bar(1, "1.10000"), bar(2, "1.10020", c="1.10030"), bar(3, "1.10040")]
        t = self.run_it(bars, {0: ("buy", "10", "20")}, flat_at=time(9, 3)).trades[0]
        self.assertEqual(t["exits"][0]["why"], "time_exit")
        self.assertEqual(t["exits"][0]["price"], "1.10030")
        self.assertEqual(t["exit_time"], "2026-01-06T14:03:00Z")

    def test_without_flat_at_trade_stays_open(self):
        bars = [bar(0, "1.10000"), bar(1, "1.10000"), bar(2, "1.10020"), bar(3, "1.10040")]
        self.assertEqual(self.run_it(bars, {0: ("buy", "10", "20")}).trades, [])


class TestPipeline(ShadowTest):
    def test_entry_cancelled_if_next_bar_missing(self):
        bars = [bar(0, "1.10000"), bar(7, "1.10000"), bar(8, "1.10000")]  # 7-minute gap after the idea
        r = self.run_it(bars, {0: ("buy", "10", "20"), 2: ("buy", "10", "20")})
        # The first entry is cancelled because the bar after the idea is missing, which also
        # frees the slot. Bar 2 has good data, so its idea is approved.
        self.assertEqual(r.cancelled_entries, 1)
        self.assertEqual(r.approved, 2)
        self.assertEqual(len(self.events(r, "shadow.entry_cancelled")), 1)

    def test_one_trade_at_a_time_and_slot_freed_after_close(self):
        bars = [bar(0, "1.10000"), bar(1, "1.10000"), bar(2, "1.10000"),
                bar(3, "1.10050", h="1.10300"), bar(4, "1.10000"), bar(5, "1.10000")]
        r = self.run_it(bars, {0: ("buy", "10", "20"), 2: ("buy", "10", "20"), 4: ("buy", "10", "20")})
        self.assertEqual(r.refused.get("trade_open"), 1)  # bar 2: first trade still open
        self.assertEqual(r.approved, 2)                    # bar 4: free again after the target

    def test_kill_switch_blocks_everything(self):
        (self.root / "KILL").write_text("on")
        r = self.run_it([bar(0, "1.10000"), bar(1, "1.10000")], {0: ("buy", "10", "20")})
        self.assertEqual((r.approved, r.refused.get("kill_switch")), (0, 1))
        self.assertEqual(r.trades, [])

    def test_every_risk_decision_logged(self):
        r = self.run_it([bar(0, "1.10000"), bar(1, "1.10000")], {0: ("buy", "10", "20")})
        self.assertEqual(len(self.events(r, "risk.decision")), 1)
        self.assertEqual(len(self.events(r, "risk.day_start")), 1)
        with LocalLog(r.log_path) as log:
            self.assertTrue(log.verify()[0])

    def test_open_trade_reported_at_end(self):
        r = self.run_it([bar(0, "1.10000"), bar(1, "1.10000"), bar(2, "1.10050")], {0: ("buy", "10", "20")})
        self.assertEqual(len(self.events(r, "shadow.open_at_end")), 1)
        self.assertEqual(r.trades, [])
        self.assertGreater(r.final_equity, D(10000))

    def test_repeatable(self):
        bars = [bar(0, "1.10000"), bar(1, "1.10000"), bar(2, "1.10050", h="1.10300")]
        a = self.run_it(bars, {0: ("buy", "10", "20")}, started=datetime(2026, 2, 1, tzinfo=UTC))
        b = self.run_it(bars, {0: ("buy", "10", "20")}, started=datetime(2026, 2, 2, tzinfo=UTC))
        self.assertEqual(a.fingerprint, b.fingerprint)
        self.assertNotEqual(a.log_path, b.log_path)

    def test_summary(self):
        bars = [bar(0, "1.10000"), bar(1, "1.10000"), bar(2, "1.09950", l="1.09800"),
                bar(3, "1.10000"), bar(4, "1.10000"), bar(5, "1.10050", h="1.10300")]
        s = self.run_it(bars, {0: ("buy", "10", "20"), 3: ("buy", "10", "20")}).summary()
        self.assertEqual((s["trades"], s["wins"]), (2, 1))
        self.assertEqual(s["max_drawdown_r"], "0.95")


if __name__ == "__main__":
    unittest.main()
