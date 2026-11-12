"""Read-only OANDA client for the practice environment.

An OANDA API token can place orders, so this client limits what the data
side of the system can do with it:
- only the practice hosts can be reached, never the live ones;
- only GET requests are allowed;
- only the read endpoints listed in ALLOWED_PATHS can be called.

The token is read from the OANDA_TOKEN environment variable and is never
printed, logged or stored by this module.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from typing import Callable

PRACTICE_REST = "https://api-fxpractice.oanda.com"
PRACTICE_STREAM = "https://stream-fxpractice.oanda.com"
ALLOWED_HOSTS = {PRACTICE_REST, PRACTICE_STREAM}

# Read endpoints only. Anything that changes an account is absent on purpose.
ALLOWED_PATHS = [
    re.compile(r"^/v3/accounts$"),
    re.compile(r"^/v3/accounts/[A-Za-z0-9-]+/summary$"),
    re.compile(r"^/v3/accounts/[A-Za-z0-9-]+/instruments$"),
    re.compile(r"^/v3/accounts/[A-Za-z0-9-]+/pricing$"),
    re.compile(r"^/v3/accounts/[A-Za-z0-9-]+/pricing/stream$"),
    re.compile(r"^/v3/instruments/[A-Z]{3}_[A-Z]{3}/candles$"),
]


class NotAllowed(PermissionError):
    """Raised when code asks for something outside the read-only allowlist."""


Transport = Callable[[urllib.request.Request, float], bytes]


def _default_transport(req: urllib.request.Request, timeout: float) -> bytes:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


class OandaReader:
    def __init__(
        self,
        token: str | None = None,
        host: str = PRACTICE_REST,
        transport: Transport = _default_transport,
        timeout: float = 10.0,
    ) -> None:
        if host not in ALLOWED_HOSTS:
            raise NotAllowed(f"Only the OANDA practice hosts are allowed, not {host}")
        token = token or os.environ.get("OANDA_TOKEN", "")
        if not token:
            raise NotAllowed("OANDA_TOKEN is not set")
        self._token = token
        self.host = host
        self._transport = transport
        self.timeout = timeout

    def __repr__(self) -> str:  # never show the token
        return f"OandaReader(host={self.host!r})"

    def get(self, path: str, params: dict[str, str] | None = None) -> dict:
        if not any(p.match(path) for p in ALLOWED_PATHS):
            raise NotAllowed(f"Endpoint is not on the read-only list: {path}")
        query = ""
        if params:
            query = "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            f"{self.host}{path}{query}",
            method="GET",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept-Datetime-Format": "RFC3339",
            },
        )
        return json.loads(self._transport(req, self.timeout))

    def accounts(self) -> list[dict]:
        return self.get("/v3/accounts").get("accounts", [])

