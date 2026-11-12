import json
import unittest

from fxsystem.oanda_read import NotAllowed, OandaReader


class FakeTransport:
    def __init__(self, body):
        self.body = body
        self.requests = []

    def __call__(self, req, timeout):
        self.requests.append(req)
        return json.dumps(self.body).encode()


class TestReadOnly(unittest.TestCase):
    def test_live_host_refused(self):
        with self.assertRaises(NotAllowed):
            OandaReader(token="t", host="https://api-fxtrade.oanda.com")

    def test_missing_token_refused(self):
        with self.assertRaises(NotAllowed):
            OandaReader(token="")

    def test_order_endpoints_refused(self):
        r = OandaReader(token="t", transport=FakeTransport({}))
        for path in (
            "/v3/accounts/101-001-1-001/orders",
            "/v3/accounts/101-001-1-001/trades/1/close",
            "/v3/accounts/101-001-1-001/positions/EUR_USD/close",
            "/v3/accounts/101-001-1-001/configuration",
        ):
            with self.assertRaises(NotAllowed, msg=path):
                r.get(path)

    def test_allowed_read_uses_get_and_bearer(self):
        fake = FakeTransport({"accounts": [{"id": "101-001-1-001"}]})
        r = OandaReader(token="secret-token", transport=fake)
        self.assertEqual(r.accounts(), [{"id": "101-001-1-001"}])
        req = fake.requests[0]
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(req.full_url, "https://api-fxpractice.oanda.com/v3/accounts")
        self.assertEqual(req.get_header("Authorization"), "Bearer secret-token")

    def test_candles_with_params(self):
        fake = FakeTransport({"candles": []})
        r = OandaReader(token="t", transport=fake)
        r.get("/v3/instruments/EUR_USD/candles", {"granularity": "M1", "price": "BA", "count": "5"})
        self.assertIn("granularity=M1", fake.requests[0].full_url)

    def test_token_not_in_repr(self):
        self.assertNotIn("secret-token", repr(OandaReader(token="secret-token")))


if __name__ == "__main__":
    unittest.main()
