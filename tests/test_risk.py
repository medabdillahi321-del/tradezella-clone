"""Risk engine tests. Example account: $10,000, so 0.25% = $25 and 1.00% = $100."""

import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fxsystem.clock import Window
from fxsystem.risk import AccountSnapshot, Proposal, Quote, RiskConfig, RiskEngine
from fxsystem.storage import LocalLog

UTC = timezone.utc
CONFIG = Path(__file__).resolve().parent.parent / "config" / "risk.toml"
NOW = datetime(2026, 1, 6, 15, 0, tzinfo=UTC)  # Tuesday 10:00 New York
DAY = date(2026, 1, 6)
ACCT = "101-001-1234567-001"
WINDOW = Window("operating", "America/New_York", time(9), time(12), "approved")
D = Decimal


def quote(bid="1.10000", ask="1.00000", ok=True, reasons=()):
    ask = "1.10012" if ask == "1.00000" else ask
    return Quote(D(bid), D(ask), ok, tuple(reasons))


def acct(equity="10000", units=0, open_risk="0", age_s=1, account_id=ACCT, currency="USD", margin="10000"):
    return AccountSnapshot(account_id, currency, D(equity), D(margin), units, D(open_risk), NOW - timedelta(seconds=age_s))


class RiskTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.log = LocalLog(self.root / "events.sqlite")
        self.cfg = RiskConfig.load(CONFIG)
        self.eng = RiskEngine(self.cfg, WINDOW, self.root, allowed_accounts={ACCT}, log=self.log)
        self.eng.start_day(DAY, D("10000"))
        self.n = 0

    def tearDown(self):
        self.log.close()
        self.dir.cleanup()

    def buy(self, stop="1.09912", target=None, intent=None):
        # ask 1.10012 - stop 1.09912 = 10.0 pips
        self.n += 1
        return Proposal(intent or f"i{self.n}", "EUR_USD", "buy", D(stop), D(target) if target else None)

    def ev(self, p=None, q=None, a="default", now=NOW):
        return self.eng.evaluate(p or self.buy(), q or quote(), acct() if a == "default" else a, now)


class TestSizing(RiskTest):
    def test_basic_size(self):
        d = self.ev()
        self.assertTrue(d.approved, str(d))
        # $25 / (10 pips + 0.5 pip slippage) = 25 / 0.00105 = 23,809.5 -> 23,809 units
        self.assertEqual(d.units, 23809)
        self.assertEqual(d.details["entry_price"], "1.10012")  # buys enter at the ask
        self.assertEqual(d.details["trade_risk"], "25.00")

    def test_sell_enters_at_bid(self):
        d = self.ev(Proposal("s1", "EUR_USD", "sell", D("1.10100")))
        self.assertTrue(d.approved, str(d))
        self.assertEqual(d.details["entry_price"], "1.10000")
        self.assertEqual(d.details["stop_pips"], "10.0")

    def test_smaller_size_after_losses(self):
        d = self.ev(a=acct(equity="9950"))  # basis is the lower equity
        self.assertTrue(d.approved, str(d))
        self.assertEqual(d.details["equity_basis"], "9950")
        self.assertEqual(d.units, 23690)  # 24.875 / 0.00105 = 23,690.5, rounded down

    def test_near_limit_refused_even_with_trades_left(self):
        d = self.ev(a=acct(equity="9900.01"))  # down $99.99: one more trade could pass $100
        self.assertFalse(d.approved)
        self.assertTrue(any("worst case" in r for r in d.reasons))

    def test_not_bigger_after_gains(self):
        d = self.ev(a=acct(equity="11000"))
        self.assertEqual(d.units, 23809)  # still sized on the $10,000 start

    def test_units_round_down_never_up(self):
        d = self.ev(self.buy(stop="1.09909"))  # 10.3 + 0.5 = 10.8 pips -> 23,148.1
        self.assertEqual(d.units, 23148)


class TestDailyLimits(RiskTest):
    def close(self, intent):
        self.eng.trade_closed(intent, NOW)

    def test_four_trades_then_stop(self):
        for i in range(4):
            d = self.ev(self.buy(intent=f"t{i}"), a=acct(equity="10050"))  # winning day
            self.assertTrue(d.approved, f"trade {i}: {d}")
            self.close(f"t{i}")
        d = self.ev(self.buy(intent="t5"), a=acct(equity="10050"))
        self.assertFalse(d.approved)
        self.assertIn("already 4 new trades today (max 4)", d.reasons)

    def test_one_at_a_time(self):
        self.assertTrue(self.ev(self.buy(intent="a")).approved)
        d = self.ev(self.buy(intent="b"))
        self.assertIn("a trade is already open (one at a time)", d.reasons)

    def test_broker_position_blocks(self):
        d = self.ev(a=acct(units=1000))
        self.assertIn("a position is already open at the broker (one at a time)", d.reasons)

    def test_daily_loss_shuts_down_and_stays_down(self):
        d = self.ev(a=acct(equity="9900"))  # exactly 1% down
        self.assertFalse(d.approved)
        self.assertTrue(any("reached the limit 100.00" in r for r in d.reasons))
        d = self.ev(a=acct(equity="10200"))  # even after recovering
        self.assertTrue(any("daily shutdown" in r for r in d.reasons))
        self.assertEqual(len(self.log.events("risk.shutdown")), 1)

    def test_worst_case_blocks_trade_that_could_breach(self):
        # Down $80 already: a $25 trade could take the day to -$105.
        d = self.ev(a=acct(equity="9920"))
        self.assertTrue(any("worst case for the day" in r for r in d.reasons), str(d))
        # Down $74: worst case 74 + 24.99 stays under 100.
        self.assertTrue(self.ev(a=acct(equity="9926")).approved)

    def test_open_risk_counts(self):
        d = self.ev(a=acct(equity="10000", open_risk="80"))
        self.assertTrue(any("worst case" in r for r in d.reasons))

    def test_new_day_needs_new_start(self):
        tomorrow = NOW + timedelta(days=1)
        d = self.eng.evaluate(self.buy(), quote(), acct(), tomorrow)
        self.assertTrue(any("but it is now trading day 2026-01-07" in r for r in d.reasons))


class TestGuards(RiskTest):
    def assertRefused(self, d, text):
        self.assertFalse(d.approved)
        self.assertTrue(any(text in r for r in d.reasons), f"{text!r} not in {d.reasons}")
        self.assertEqual(d.units, 0)

    def test_kill_switch(self):
        (self.root / "KILL").write_text("on")
        self.assertRefused(self.ev(), "kill switch is on")

    def test_data_quality_failure(self):
        self.assertRefused(self.ev(q=quote(ok=False, reasons=["feed stale: nothing from OANDA for 12s"])), "feed stale")

    def test_outside_window(self):
        self.assertRefused(self.ev(now=NOW + timedelta(hours=3)), "outside the operating window")

    def test_no_account_data(self):
        self.assertRefused(self.ev(a=None), "no account data")

    def test_stale_account_data(self):
        self.assertRefused(self.ev(a=acct(age_s=31)), "account data is 31s old")

    def test_live_account_refused(self):
        self.assertRefused(self.ev(a=acct(account_id="001-001-1234567-001")), "not an OANDA practice account")

    def test_unlisted_account_refused(self):
        self.assertRefused(self.ev(a=acct(account_id="101-001-9999999-001")), "not on the allowed list")

    def test_no_allowed_accounts_means_no_trade(self):
        self.eng.allowed_accounts = set()
        self.assertRefused(self.ev(), "not on the allowed list")

    def test_wrong_currency(self):
        self.assertRefused(self.ev(a=acct(currency="CAD")), "currency CAD is not supported")

    def test_other_instrument(self):
        self.assertRefused(self.ev(Proposal("x", "GBP_USD", "buy", D("1.2"))), "instrument GBP_USD not allowed")

    def test_stop_wrong_side(self):
        self.assertRefused(self.ev(self.buy(stop="1.10100")), "stop is on the wrong side")

    def test_stop_too_tight_and_too_wide(self):
        self.assertRefused(self.ev(self.buy(stop="1.09992")), "tighter than 3")   # 2 pips
        self.assertRefused(self.ev(self.buy(stop="1.09412")), "wider than 50")    # 60 pips

    def test_target_wrong_side(self):
        self.assertRefused(self.ev(self.buy(target="1.09900")), "target is on the wrong side")

    def test_crossed_quote(self):
        self.assertRefused(self.ev(q=quote(bid="1.10020", ask="1.10010")), "crossed quote")

    def test_not_enough_margin(self):
        # 23,809 units x 1.10012 x 2% = $523.86 needed; only half of $1,000 may be used.
        self.assertRefused(self.ev(a=acct(margin="1000")), "not enough margin")

    def test_duplicate_intent(self):
        self.ev(self.buy(intent="same"), q=quote(ok=False))
        self.assertRefused(self.ev(self.buy(intent="same")), "already decided (duplicate)")

    def test_settings_changed_mid_day(self):
        self.eng.state.config_hash = "something-else"
        self.assertRefused(self.ev(), "risk settings changed since the day started")

    def test_unreconciled(self):
        self.eng.mark_unreconciled("broker shows a position we did not open", NOW)
        self.assertRefused(self.ev(), "not reconciled")

    def test_day_not_started(self):
        self.eng.state = None
        self.assertRefused(self.ev(), "risk day not started")

    def test_all_reasons_reported_together(self):
        (self.root / "KILL").write_text("on")
        d = self.ev(self.buy(stop="1.10100"), q=quote(ok=False), a=None, now=NOW + timedelta(hours=3))
        self.assertGreaterEqual(len(d.reasons), 5)


class TestLoggingAndRestart(RiskTest):
    def test_every_decision_logged(self):
        self.ev()
        self.ev(q=quote(ok=False))
        decisions = [e.payload for e in self.log.events("risk.decision")]
        self.assertEqual([x["approved"] for x in decisions], [True, False])
        self.assertTrue(decisions[1]["reasons"])
        self.assertEqual(decisions[0]["config_hash"], self.cfg.config_hash)

    def test_restart_restores_counts_and_shutdown(self):
        for i in range(2):
            self.ev(self.buy(intent=f"t{i}"))
            self.eng.trade_closed(f"t{i}", NOW)
        self.ev(self.buy(intent="t2"))  # left open
        self.ev(a=acct(equity="9899"))  # triggers shutdown
        fresh = RiskEngine(self.cfg, WINDOW, self.root, allowed_accounts={ACCT}, log=self.log)
        st = fresh.restore(self.log, DAY)
        self.assertEqual(st.approved_today, 3)
        self.assertTrue(st.trade_open)
        self.assertTrue(st.shut_down)
        self.assertIn("t0", st.seen_intents)

    def test_restart_without_history_is_no_trade(self):
        fresh = RiskEngine(self.cfg, WINDOW, self.root, allowed_accounts={ACCT})
        fresh.restore(self.log, date(2026, 1, 7))
        d = fresh.evaluate(self.buy(), quote(), acct(), NOW + timedelta(days=1))
        self.assertIn("risk day not started", d.reasons)

    def test_allowed_accounts_from_environment(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"FX_ALLOWED_ACCOUNTS": f" {ACCT} , 101-x "}):
            e = RiskEngine(self.cfg, WINDOW, self.root)
        self.assertEqual(e.allowed_accounts, {ACCT, "101-x"})


if __name__ == "__main__":
    unittest.main()
