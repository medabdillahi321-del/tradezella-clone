"""Copy events from the local log to Supabase.

- Sends events in order, in batches.
- Safe to retry: Supabase ignores an event it already has (same id), so a batch
  that was half-sent before a crash can simply be sent again.
- Marks events as copied only after Supabase confirms.
- Never deletes or changes anything locally.

Settings come from environment variables: SUPABASE_URL (https://<project>.supabase.co)
and SUPABASE_KEY. The key is never printed or stored by this module.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from fxsystem.storage import LocalLog

Transport = Callable[[urllib.request.Request, float], int]


def _default_transport(req: urllib.request.Request, timeout: float) -> int:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


@dataclass
class SyncResult:
    sent: int
    remaining: int
    error: str | None = None


class SupabaseSync:
    TABLE = "events"

    def __init__(
        self,
        log: LocalLog,
        url: str | None = None,
        key: str | None = None,
        transport: Transport = _default_transport,
        timeout: float = 15.0,
        batch_size: int = 200,
    ) -> None:
        self.log = log
        self.url = (url or os.environ.get("SUPABASE_URL", "")).rstrip("/")
        self._key = key or os.environ.get("SUPABASE_KEY", "")
        if not self.url.startswith("https://") or not self._key:
            raise ValueError("Set SUPABASE_URL (https://...) and SUPABASE_KEY to copy events to Supabase")
        self._transport = transport
        self.timeout = timeout
        self.batch_size = batch_size

    def __repr__(self) -> str:  # never show the key
        return f"SupabaseSync(url={self.url!r})"

    def _post(self, rows: list[dict]) -> int:
        req = urllib.request.Request(
            f"{self.url}/rest/v1/{self.TABLE}?on_conflict=id",
            method="POST",
            data=json.dumps(rows).encode("utf-8"),
            headers={
                "apikey": self._key,
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
                "Prefer": "resolution=ignore-duplicates,return=minimal",
            },
        )
        return self._transport(req, self.timeout)

    def run_once(self) -> SyncResult:
        """Send everything waiting, batch by batch. Stops at the first failure."""
        sent = 0
        while True:
            batch = self.log.unsynced(self.batch_size)
            if not batch:
                return SyncResult(sent, 0)
            try:
                status = self._post([e.as_row() for e in batch])
            except Exception as e:  # noqa: BLE001  (network down, timeout, DNS ...)
                return SyncResult(sent, self.log.unsynced_count(), f"{type(e).__name__}: {e}")
            if not 200 <= status < 300:
                return SyncResult(sent, self.log.unsynced_count(), f"Supabase answered HTTP {status}")
            self.log.mark_synced([e.seq for e in batch])
            sent += len(batch)
