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
from typing import Callable, Iterable, Iterator

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
StreamTransport = Callable[[urllib.request.Request, float], Iterable[bytes]]


def _default_transport(req: urllib.request.Request, timeout: float) -> bytes:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _default_stream_transport(req: urllib.request.Request, timeout: float) -> Iterator[bytes]:
    # timeout applies to each read: OANDA sends a heartbeat every 5 seconds, so a
    # read that waits longer than `timeout` means the stream has gone quiet.
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        yield from resp


class OandaReader:
    def __init__(
        self,
        token: str | None = None,
        host: str = PRACTICE_REST,
        transport: Transport = _default_transport,
        timeout: float = 10.0,
        stream_transport: StreamTransport = _default_stream_transport,
    ) -> None:
        if host not in ALLOWED_HOSTS:
            raise NotAllowed(f"Only the OANDA practice hosts are allowed, not {host}")
        token = token or os.environ.get("OANDA_TOKEN", "")
        if not token:
            raise NotAllowed("OANDA_TOKEN is not set")
        self._token = token
        self.host = host
        self._transport = transport
        self._stream_transport = stream_transport
        self.timeout = timeout

    def __repr__(self) -> str:  # never show the token
        return f"OandaReader(host={self.host!r})"

    def _request(self, host: str, path: str, params: dict[str, str] | None) -> urllib.request.Request:
        if not any(p.match(path) for p in ALLOWED_PATHS):
            raise NotAllowed(f"Endpoint is not on the read-only list: {path}")
        query = ""
        if params:
            query = "?" + urllib.parse.urlencode(params)
        return urllib.request.Request(
            f"{host}{path}{query}",
            method="GET",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept-Datetime-Format": "RFC3339",
            },
        )

    def get(self, path: str, params: dict[str, str] | None = None) -> dict:
        if path.endswith("/stream"):
            raise NotAllowed("Use stream() for streaming endpoints")
        return json.loads(self._transport(self._request(self.host, path, params), self.timeout))

    def stream(self, path: str, params: dict[str, str] | None = None) -> Iterator[dict]:
        """Yield messages from a streaming endpoint, one per line, from the practice stream host."""
        if not path.endswith("/stream"):
            raise NotAllowed("stream() is only for streaming endpoints")
        req = self._request(PRACTICE_STREAM, path, params)
        for line in self._stream_transport(req, self.timeout):
            line = line.strip()
            if line:
                yield json.loads(line)

    def accounts(self) -> list[dict]:
        return self.get("/v3/accounts").get("accounts", [])

