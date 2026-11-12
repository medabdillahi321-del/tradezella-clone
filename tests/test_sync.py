import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fxsystem.storage import LocalLog
from fxsystem.sync import SupabaseSync

UTC = timezone.utc


class FakeSupabase:
    """Stands in for Supabase: stores rows by id and ignores duplicates, like the real table."""

    def __init__(self):
        self.rows = {}
        self.down = False
        self.fail_after = None  # crash after receiving this many batches
        self.requests = []

    def __call__(self, req, timeout):
        if self.down:
            raise OSError("network is unreachable")
        self.requests.append(req)
        for row in json.loads(req.data):
            self.rows.setdefault(row["id"], row)
        if self.fail_after is not None and len(self.requests) >= self.fail_after:
            # Supabase stored the rows but the answer never arrived.
            raise TimeoutError("connection reset")
        return 201


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.log = LocalLog(Path(self.dir.name) / "events.sqlite")
        self.sb = FakeSupabase()
        self.sync = SupabaseSync(self.log, url="https://example.supabase.co", key="secret-key", transport=self.sb, batch_size=3)

    def tearDown(self):
        self.log.close()
        self.dir.cleanup()

    def add(self, n):
        for i in range(n):
            self.log.append("market.bar", datetime(2026, 1, 6, 14, i, tzinfo=UTC), {"i": i})

    def test_everything_copied_in_order(self):
        self.add(7)
        r = self.sync.run_once()
        self.assertEqual((r.sent, r.remaining, r.error), (7, 0, None))
        self.assertEqual(len(self.sb.requests), 3)  # batches of 3, 3, 1
        seqs = [row["seq"] for req in self.sb.requests for row in json.loads(req.data)]
        self.assertEqual(seqs, list(range(1, 8)))

    def test_supabase_down_loses_nothing(self):
        self.add(4)
        self.sb.down = True
        r = self.sync.run_once()
        self.assertEqual((r.sent, r.remaining), (0, 4))
        self.assertIn("unreachable", r.error)
        self.add(2)  # the system keeps writing locally meanwhile
        self.sb.down = False
        r = self.sync.run_once()
        self.assertEqual((r.sent, r.remaining), (6, 0))
        self.assertEqual(len(self.sb.rows), 6)

    def test_lost_answer_is_resent_without_duplicates(self):
        self.add(5)
        self.sb.fail_after = 1
        r = self.sync.run_once()
        self.assertEqual(r.sent, 0)  # first batch arrived, but we could not know
        self.sb.fail_after = None
        r = self.sync.run_once()
        self.assertEqual((r.sent, r.remaining), (5, 0))
        self.assertEqual(len(self.sb.rows), 5)  # resent batch did not duplicate

    def test_http_error_stops_and_keeps_events(self):
        self.add(2)
        sync = SupabaseSync(self.log, url="https://example.supabase.co", key="k", transport=lambda req, t: 401)
        r = sync.run_once()
        self.assertEqual((r.sent, r.remaining), (0, 2))
        self.assertIn("401", r.error)

    def test_request_shape(self):
        self.add(1)
        self.sync.run_once()
        req = self.sb.requests[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.full_url, "https://example.supabase.co/rest/v1/events?on_conflict=id")
        self.assertIn("ignore-duplicates", req.get_header("Prefer"))

    def test_key_not_in_repr(self):
        self.assertNotIn("secret-key", repr(self.sync))

    def test_refuses_without_settings(self):
        with self.assertRaises(ValueError):
            SupabaseSync(self.log, url="", key="")
        with self.assertRaises(ValueError):
            SupabaseSync(self.log, url="http://insecure.example", key="k")


if __name__ == "__main__":
    unittest.main()
