import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fxsystem.storage import GENESIS, LocalLog, StorageError

UTC = timezone.utc


def t(h, m=0):
    return datetime(2026, 1, 6, h, m, tzinfo=UTC)


class LogTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "events.sqlite"
        self.log = LocalLog(self.path)

    def tearDown(self):
        self.log.close()
        self.dir.cleanup()


class TestAppend(LogTest):
    def test_append_and_read_back(self):
        e = self.log.append("market.bar", t(14), {"close": 1.1092, "symbol": "EUR_USD"})
        self.assertEqual(e.seq, 1)
        self.assertEqual(e.prev_hash, GENESIS)
        self.assertEqual(e.payload, {"close": 1.1092, "symbol": "EUR_USD"})
        self.assertEqual(e.ts_utc, "2026-01-06T14:00:00.000000Z")
        self.assertEqual(e.trading_day, "2026-01-06")

    def test_trading_day_rolls_at_17_new_york(self):
        e = self.log.append("market.bar", t(22), {})  # 17:00 EST
        self.assertEqual(e.trading_day, "2026-01-07")

    def test_chain_links(self):
        a = self.log.append("system.start", t(14), {})
        b = self.log.append("system.stop", t(15), {})
        self.assertEqual(b.prev_hash, a.hash)

    def test_unknown_kinds_refused(self):
        for kind in ("trade.order", "shadow.", "Shadow.fill", "shadow fill", "market"):
            with self.assertRaises(StorageError, msg=kind):
                self.log.append(kind, t(14), {})
        self.assertEqual(self.log.count(), 0)

    def test_naive_time_refused(self):
        with self.assertRaises(ValueError):
            self.log.append("system.start", datetime(2026, 1, 6, 14), {})

    def test_nan_refused(self):
        with self.assertRaises(ValueError):
            self.log.append("market.bar", t(14), {"close": float("nan")})
        self.assertEqual(self.log.count(), 0)

    def test_payload_key_order_does_not_matter(self):
        a = self.log.append("review.note", t(14), {"b": 1, "a": 2})
        self.assertEqual(a.payload_json, '{"a":2,"b":1}')

    def test_filter_by_namespace(self):
        self.log.append("shadow.intent", t(14), {})
        self.log.append("market.bar", t(14), {})
        self.log.append("shadow.fill", t(14), {})
        self.assertEqual([e.kind for e in self.log.events("shadow.")], ["shadow.intent", "shadow.fill"])


class TestIdempotency(LogTest):
    def test_same_key_stored_once(self):
        a = self.log.append("shadow.intent", t(14), {"side": "buy"}, idem_key="intent-1")
        b = self.log.append("shadow.intent", t(14), {"side": "buy"}, idem_key="intent-1")
        self.assertEqual(a.id, b.id)
        self.assertEqual(self.log.count(), 1)

    def test_same_key_different_content_refused(self):
        self.log.append("shadow.intent", t(14), {"side": "buy"}, idem_key="intent-1")
        with self.assertRaises(StorageError):
            self.log.append("shadow.intent", t(14), {"side": "sell"}, idem_key="intent-1")
        self.assertEqual(self.log.count(), 1)


class TestAppendOnly(LogTest):
    def test_update_and_delete_refused_by_database(self):
        self.log.append("risk.decision", t(14), {"result": "NO TRADE"})
        with self.assertRaises(sqlite3.IntegrityError):
            self.log.db.execute("UPDATE events SET payload_json = '{}'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.log.db.execute("DELETE FROM events")
        self.assertEqual(self.log.count(), 1)

    def test_tampering_detected(self):
        for i in range(5):
            self.log.append("market.bar", t(14, i), {"i": i})
        self.assertTrue(self.log.verify()[0])
        # Someone removes the protection and edits an event directly.
        self.log.db.execute("DROP TRIGGER events_no_update")
        self.log.db.execute("""UPDATE events SET payload_json = '{"i":99}' WHERE seq = 3""")
        ok, msg = self.log.verify()
        self.assertFalse(ok)
        self.assertIn("Event 3", msg)


class TestRestart(LogTest):
    def test_survives_close_and_reopen(self):
        self.log.append("system.start", t(14), {})
        last = self.log.append("market.bar", t(14, 1), {"close": 1.1})
        self.log.close()
        self.log = LocalLog(self.path)
        nxt = self.log.append("market.bar", t(14, 2), {"close": 1.2})
        self.assertEqual(nxt.seq, 3)
        self.assertEqual(nxt.prev_hash, last.hash)
        self.assertEqual(self.log.verify(), (True, "3 events, chain intact"))

    def test_two_connections_do_not_fork_the_chain(self):
        other = LocalLog(self.path)
        try:
            for i in range(20):
                (self.log if i % 2 else other).append("market.bar", t(14, i), {"i": i})
        finally:
            other.close()
        self.assertTrue(self.log.verify()[0])
        self.assertEqual(self.log.count(), 20)


if __name__ == "__main__":
    unittest.main()
