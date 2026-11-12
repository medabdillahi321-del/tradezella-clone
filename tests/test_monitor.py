import os
import tempfile
import unittest
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from unittest import mock

from fxsystem.clock import Window
from fxsystem.monitor import Alerter, Finding, MonitorConfig, check
from fxsystem.storage import LocalLog

UTC = timezone.utc
NOW = datetime(2026, 1, 6, 15, 0, tzinfo=UTC)  # Tuesday 10:00 New York, inside the window
EVENING = datetime(2026, 1, 6, 23, 0, tzinfo=UTC)  # 18:00 New York, market open, outside the window
WINDOW = Window("operating", "America/New_York", time(9), time(12), "approved")


def plenty(_):
    return 50.0


class MonTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def add_bar(self, ended_ago_s, now=NOW):
        start = now - timedelta(seconds=ended_ago_s) - timedelta(minutes=1)
        with LocalLog(self.root / "events.sqlite") as log:
            log.append("market.bar_m1", start, {"instrument": "EUR_USD"})

    def add_tick(self, age_s, lag_s=0.2, now=NOW):
        t = now - timedelta(seconds=age_s)
        p = self.root / "ticks" / "EUR_USD" / "2026-01-06.csv"
        p.parent.mkdir(parents=True, exist_ok=True)
        f = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.%fZ")  # noqa: E731
        p.write_text(f"time_utc,bid,ask,tradeable,received_utc\n{f(t)},1.1,1.1001,1,{f(t + timedelta(seconds=lag_s))}\n")

    def codes(self, findings, level=None):
        return {f.code for f in findings if level is None or f.level == level}

    def run_check(self, now=NOW, **kw):
        return check(self.root, now, WINDOW, disk_free_gb=kw.pop("disk", plenty), **kw)


class TestChecks(MonTest):
    def test_healthy(self):
        self.add_bar(10)
        self.add_tick(1)
        self.assertEqual(self.run_check(), [])

    def test_never_recorded_in_window_is_critical(self):
        f = self.run_check()
        self.assertEqual(f[0].level, "CRITICAL")
        self.assertEqual(f[0].code, "recorder_stopped")

    def test_bars_stopped(self):
        self.add_bar(240)
        self.add_tick(1)
        self.assertIn("recorder_stopped", self.codes(self.run_check(), "CRITICAL"))

    def test_bars_stopped_outside_window_is_info(self):
        self.add_bar(3600, now=EVENING)
        f = self.run_check(now=EVENING)
        self.assertIn("recorder_stopped", self.codes(f, "INFO"))
        self.assertNotIn("CRITICAL", {x.level for x in f})

    def test_weekend_is_quiet(self):
        sat = datetime(2026, 1, 10, 15, 0, tzinfo=UTC)
        self.add_bar(86400, now=sat)
        self.assertEqual(self.run_check(now=sat), [])

    def test_prices_stale(self):
        self.add_bar(10)
        self.add_tick(45)
        self.assertIn("prices_stale", self.codes(self.run_check(), "CRITICAL"))

    def test_prices_late(self):
        self.add_bar(10)
        self.add_tick(1, lag_s=8)
        self.assertIn("prices_late", self.codes(self.run_check(), "WARN"))

    def test_stream_errors_and_gaps(self):
        self.add_bar(10)
        self.add_tick(1)
        with LocalLog(self.root / "events.sqlite") as log:
            for i in range(3):
                log.append("system.stream_error", NOW - timedelta(minutes=i), {"error": "x"})
            log.append("market.gap", NOW - timedelta(minutes=30), {"from_utc": "a", "to_utc": "b"})
        self.assertTrue({"stream_errors", "stream_gaps"} <= self.codes(self.run_check(), "WARN"))

    def test_risk_shutdown_and_kill_switch_reported(self):
        self.add_bar(10)
        self.add_tick(1)
        with LocalLog(self.root / "events.sqlite") as log:
            log.append("risk.shutdown", NOW, {"reason": "daily loss", "day": "2026-01-06"})
        (self.root / "KILL").write_text("on")
        self.assertTrue({"risk_shutdown", "kill_switch_on"} <= self.codes(self.run_check(), "WARN"))

    def test_disk(self):
        self.add_bar(10)
        self.add_tick(1)
        self.assertIn("disk_low", self.codes(self.run_check(disk=lambda p: 1.5), "WARN"))
        self.assertIn("disk_low", self.codes(self.run_check(disk=lambda p: 0.2), "CRITICAL"))

    def test_tampered_log(self):
        self.add_bar(10)
        self.add_tick(1)
        with LocalLog(self.root / "events.sqlite") as log:
            log.db.execute("DROP TRIGGER events_no_update")
            log.db.execute("UPDATE events SET payload_json = '{}'")
        self.assertIn("log_tampered", self.codes(self.run_check(verify_chain=True), "CRITICAL"))

    def test_sync_backlog_only_when_configured(self):
        self.add_bar(10)
        self.add_tick(1)
        cfg = MonitorConfig(sync_backlog_warn=0)
        self.assertNotIn("sync_backlog", self.codes(check(self.root, NOW, WINDOW, cfg, disk_free_gb=plenty)))
        with mock.patch.dict(os.environ, {"SUPABASE_URL": "https://x.supabase.co"}):
            self.assertIn("sync_backlog", self.codes(check(self.root, NOW, WINDOW, cfg, disk_free_gb=plenty)))


class TestAlerter(MonTest):
    def setUp(self):
        super().setUp()
        self.sent, self.printed = [], []
        self.alerter = Alerter(self.root, WINDOW, MonitorConfig(),
                               send=lambda lvl, txt: self.sent.append((lvl, txt)), echo=self.printed.append)

    def test_sent_once_then_repeated_then_resolved(self):
        f = [Finding("WARN", "prices_late", "late")]
        self.alerter.handle(f, NOW)
        self.alerter.handle(f, NOW + timedelta(minutes=10))
        self.assertEqual(len(self.sent), 1)
        self.alerter.handle(f, NOW + timedelta(minutes=31))
        self.assertEqual(len(self.sent), 2)
        self.alerter.handle([], NOW + timedelta(minutes=32))
        self.assertEqual(self.sent[-1][0], "RESOLVED")
        with LocalLog(self.root / "events.sqlite") as log:
            self.assertEqual(len(log.events("system.alert")), 3)

    def test_escalation_sent_immediately(self):
        self.alerter.handle([Finding("WARN", "disk_low", "1.5 GB")], NOW)
        self.alerter.handle([Finding("CRITICAL", "disk_low", "0.2 GB")], NOW + timedelta(minutes=1))
        self.assertEqual([s[0] for s in self.sent], ["WARN", "CRITICAL"])

    def test_info_not_sent(self):
        self.alerter.handle([Finding("INFO", "recorder_stopped", "off")], NOW)
        self.assertEqual(self.sent, [])

    def test_critical_in_window_turns_kill_switch_on(self):
        self.alerter.handle([Finding("CRITICAL", "prices_stale", "no price for 45 s")], NOW)
        self.assertTrue((self.root / "KILL").exists())
        self.assertTrue(any("Kill switch turned ON" in t for _, t in self.sent))
        with LocalLog(self.root / "events.sqlite") as log:
            self.assertEqual(log.last("system.kill_switch").payload["by"], "monitor")

    def test_kill_switch_never_turned_off_by_monitor(self):
        self.alerter.handle([Finding("CRITICAL", "prices_stale", "x")], NOW)
        self.alerter.handle([], NOW + timedelta(minutes=5))  # problem resolved
        self.assertTrue((self.root / "KILL").exists())

    def test_no_auto_kill_outside_window_or_for_other_codes(self):
        self.alerter.handle([Finding("CRITICAL", "prices_stale", "x")], EVENING)
        self.alerter.handle([Finding("CRITICAL", "disk_low", "x")], NOW)
        self.assertFalse((self.root / "KILL").exists())

    def test_failing_channel_does_not_stop_monitor(self):
        def boom(lvl, txt):
            raise OSError("network down")
        a = Alerter(self.root, WINDOW, send=boom, echo=self.printed.append)
        a.handle([Finding("WARN", "prices_late", "late")], NOW)
        self.assertTrue(any("could not send" in p for p in self.printed))

    def test_state_survives_restart(self):
        self.alerter.handle([Finding("WARN", "prices_late", "late")], NOW)
        fresh = Alerter(self.root, WINDOW, send=lambda l, t: self.sent.append((l, t)), echo=self.printed.append)
        fresh.handle([Finding("WARN", "prices_late", "late")], NOW + timedelta(minutes=5))
        self.assertEqual(len(self.sent), 1)  # not re-sent after a restart


class TestWebhookShape(unittest.TestCase):
    def test_discord_and_slack_bodies(self):
        import json
        from fxsystem import monitor
        seen = []

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake(req, timeout):
            seen.append((req.full_url, json.loads(req.data)))
            return Resp()
        with mock.patch.object(monitor.urllib.request, "urlopen", side_effect=fake):
            monitor.webhook_sender("https://discord.com/api/webhooks/1/abc")("WARN", "late")
            monitor.webhook_sender("https://hooks.slack.com/services/x")("CRITICAL", "down")
        self.assertEqual(seen[0][1], {"content": "[WARN] FX system: late"})
        self.assertEqual(seen[1][1], {"text": "[CRITICAL] FX system: down"})


if __name__ == "__main__":
    unittest.main()
