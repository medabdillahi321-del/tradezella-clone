"""Local event log: the system writes here first, always.

- One SQLite file on this computer (Python's built-in sqlite3, nothing to install).
- Append-only: the database itself refuses edits and deletions of events.
- Tamper-evident: each event carries a SHA-256 hash of itself and of the event
  before it, so any later change breaks the chain and verify() reports where.
- Idempotent: an event written twice with the same idem_key is stored once.
- Event kinds are namespaced, so shadow (virtual) trades can never be mistaken
  for anything else.

Copying to Supabase is a separate step (fxsystem/sync.py). If Supabase is down,
nothing is lost: events wait here until the copy succeeds.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fxsystem.clock import require_utc, trading_day

GENESIS = "0" * 64

# Allowed event kinds: a namespace, a dot, then lowercase words, e.g. "shadow.fill".
# Add a namespace here (and in supabase/migrations) before using it.
NAMESPACES = (
    "market.",   # prices, bars, data-quality flags
    "shadow.",   # virtual trade intents, virtual fills: never real orders
    "risk.",     # risk decisions, including every NO TRADE
    "review.",   # human or AI review notes
    "system.",   # start, stop, errors, kill switch
)
KIND_RE = re.compile(r"^(market|shadow|risk|review|system)\.[a-z0-9_.]+$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    id           TEXT NOT NULL UNIQUE,
    kind         TEXT NOT NULL,
    ts_utc       TEXT NOT NULL,
    trading_day  TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    idem_key     TEXT UNIQUE,
    prev_hash    TEXT NOT NULL,
    hash         TEXT NOT NULL,
    recorded_utc TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TABLE IF NOT EXISTS synced (
    seq       INTEGER PRIMARY KEY REFERENCES events(seq),
    synced_at TEXT NOT NULL
);
"""


class StorageError(RuntimeError):
    pass


def _iso(dt: datetime) -> str:
    return require_utc(dt).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical_json(payload: dict) -> str:
    """One exact text form per payload, so hashes are reproducible."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def event_hash(prev_hash: str, id_: str, kind: str, ts_utc: str, payload_json: str) -> str:
    text = "|".join((prev_hash, id_, kind, ts_utc, payload_json))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Event:
    seq: int
    id: str
    kind: str
    ts_utc: str
    trading_day: str
    payload_json: str
    idem_key: str | None
    prev_hash: str
    hash: str
    recorded_utc: str

    @property
    def payload(self) -> dict:
        return json.loads(self.payload_json)

    def as_row(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


class LocalLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")  # survive power loss, at some speed cost
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "LocalLog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def append(
        self,
        kind: str,
        ts: datetime,
        payload: dict,
        idem_key: str | None = None,
        now: datetime | None = None,
    ) -> Event:
        """Write one event. Returns the stored event (the existing one if idem_key was seen before)."""
        if not KIND_RE.match(kind):
            raise StorageError(f"Unknown event kind {kind!r}; it must start with one of {NAMESPACES}")
        ts_utc = _iso(ts)
        payload_json = canonical_json(payload)
        recorded = _iso(now or datetime.now(timezone.utc))
        id_ = str(uuid.uuid4())
        # BEGIN IMMEDIATE takes the write lock first, so two writers cannot fork the hash chain.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if idem_key is not None:
                existing = self._one("WHERE idem_key = ?", (idem_key,))
                if existing:
                    self.db.execute("COMMIT")
                    if existing.kind != kind or existing.payload_json != payload_json:
                        raise StorageError(f"idem_key {idem_key!r} was already used for a different event")
                    return existing
            last = self.db.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            prev = last[0] if last else GENESIS
            h = event_hash(prev, id_, kind, ts_utc, payload_json)
            cur = self.db.execute(
                "INSERT INTO events (id, kind, ts_utc, trading_day, payload_json, idem_key, prev_hash, hash, recorded_utc)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (id_, kind, ts_utc, trading_day(ts).isoformat(), payload_json, idem_key, prev, h, recorded),
            )
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise
        return self._one("WHERE seq = ?", (cur.lastrowid,))

    def _one(self, where: str, args: tuple) -> Event | None:
        row = self.db.execute(f"SELECT * FROM events {where}", args).fetchone()
        return Event(*row) if row else None

    def events(self, kind_prefix: str | None = None, after_seq: int = 0) -> list[Event]:
        sql, args = "SELECT * FROM events WHERE seq > ?", [after_seq]
        if kind_prefix:
            sql += " AND kind LIKE ?"
            args.append(kind_prefix.replace("%", r"\%") + "%")
        rows = self.db.execute(sql + " ORDER BY seq", args).fetchall()
        return [Event(*r) for r in rows]

    def last(self, kind_prefix: str) -> Event | None:
        """The most recent event whose kind starts with kind_prefix."""
        return self._one("WHERE kind LIKE ? ORDER BY seq DESC LIMIT 1", (kind_prefix.replace("%", r"\%") + "%",))

    def count_since(self, kind_prefix: str, since: datetime) -> int:
        """How many events of this kind were written at or after `since`."""
        return self.db.execute(
            "SELECT COUNT(*) FROM events WHERE kind LIKE ? AND ts_utc >= ?",
            (kind_prefix.replace("%", r"\%") + "%", _iso(since)),
        ).fetchone()[0]

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def verify(self) -> tuple[bool, str]:
        """Recompute the hash chain. Returns (ok, message naming the first broken event)."""
        prev = GENESIS
        n = 0
        for e in self.events():
            n += 1
            if e.prev_hash != prev:
                return False, f"Event {e.seq} does not follow event before it (chain broken)"
            if event_hash(e.prev_hash, e.id, e.kind, e.ts_utc, e.payload_json) != e.hash:
                return False, f"Event {e.seq} was changed after it was written"
            prev = e.hash
        return True, f"{n} events, chain intact"

    # ---- sync bookkeeping (used by fxsystem/sync.py) ----
    def unsynced(self, limit: int = 500) -> list[Event]:
        rows = self.db.execute(
            "SELECT e.* FROM events e LEFT JOIN synced s ON s.seq = e.seq"
            " WHERE s.seq IS NULL ORDER BY e.seq LIMIT ?",
            (limit,),
        ).fetchall()
        return [Event(*r) for r in rows]

    def mark_synced(self, seqs: list[int], now: datetime | None = None) -> None:
        at = _iso(now or datetime.now(timezone.utc))
        self.db.execute("BEGIN IMMEDIATE")
        self.db.executemany("INSERT OR IGNORE INTO synced (seq, synced_at) VALUES (?, ?)", [(s, at) for s in seqs])
        self.db.execute("COMMIT")

    def unsynced_count(self) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM events e LEFT JOIN synced s ON s.seq = e.seq WHERE s.seq IS NULL"
        ).fetchone()[0]
