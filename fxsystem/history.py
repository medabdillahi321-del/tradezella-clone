"""Historical 1-minute bars from OANDA, saved locally.

- Fetches OANDA's 1-minute bid and ask candles (read-only), up to 5000 per request.
- Saves one CSV per trading day: data/history/<INSTRUMENT>/M1/<trading day>.csv
  (trading days roll over at 17:00 New York, like the rest of the system).
- A day that had already ended when it was fetched is marked final in a manifest
  with its SHA-256, and is never fetched again. Unfinished days are re-fetched.
- Only complete candles are saved.
"""

from __future__ import annotations

import csv
import hashlib
import json
import time as _time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from fxsystem.clock import parse_oanda_time, require_utc, trading_day, trading_day_bounds

UTC = timezone.utc
FIELDS = ["start_utc", "bid_o", "bid_h", "bid_l", "bid_c", "ask_o", "ask_h", "ask_l", "ask_c", "volume"]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class OandaHistory:
    def __init__(
        self,
        reader,
        data_dir: str | Path,
        instrument: str = "EUR_USD",
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        pause_s: float = 0.2,
        sleep: Callable[[float], None] = _time.sleep,
    ) -> None:
        self.reader = reader
        self.instrument = instrument
        self.dir = Path(data_dir) / "history" / instrument / "M1"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.dir / "manifest.json"
        self.now = now
        self.pause_s = pause_s
        self.sleep = sleep
        self.requests = 0

    # ---- manifest ----
    def _manifest(self) -> dict:
        if self.manifest_path.exists():
            return json.loads(self.manifest_path.read_text())
        return {}

    def _save_manifest(self, m: dict) -> None:
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(m, indent=1, sort_keys=True))
        tmp.replace(self.manifest_path)

    def day_path(self, day: date) -> Path:
        return self.dir / f"{day.isoformat()}.csv"

    # ---- fetching ----
    def _fetch(self, start: datetime, end: datetime) -> list[dict]:
        """All complete candles with start <= time < end, paging 5000 at a time."""
        out: list[dict] = []
        cursor = start
        while cursor < end:
            if self.requests:
                self.sleep(self.pause_s)
            self.requests += 1
            resp = self.reader.get(
                f"/v3/instruments/{self.instrument}/candles",
                {"granularity": "M1", "price": "BA", "from": _iso(cursor), "count": "5000"},
            )
            candles = resp.get("candles", [])
            if not candles:
                break
            last = cursor
            for c in candles:
                t = parse_oanda_time(c["time"])
                last = max(last, t)
                if t >= end:
                    return out
                if c.get("complete"):
                    out.append(c)
            if len(candles) < 5000:
                break
            cursor = last + timedelta(minutes=1)
        return out

    def load(self, first_day: date, last_day: date) -> dict:
        """Make sure every trading day from first_day to last_day is saved. Returns a summary."""
        manifest = self._manifest()
        days = []
        d = first_day
        while d <= last_day:
            if d.weekday() != 5:  # no trading day is named after a Saturday
                days.append(d)
            d += timedelta(days=1)
        todo = [d for d in days if not manifest.get(d.isoformat(), {}).get("final")]
        fetched = 0
        if todo:
            start = trading_day_bounds(todo[0])[0]
            end = min(trading_day_bounds(todo[-1])[1], self.now())
            by_day: dict[str, list[dict]] = {}
            for c in self._fetch(start, end):
                by_day.setdefault(trading_day(parse_oanda_time(c["time"])).isoformat(), []).append(c)
            now = self.now()
            for d in todo:
                rows = by_day.get(d.isoformat(), [])
                p = self.day_path(d)
                tmp = p.with_suffix(".tmp")
                with open(tmp, "w", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    w.writerow(FIELDS)
                    for c in rows:
                        b, a = c["bid"], c["ask"]
                        w.writerow([_iso(parse_oanda_time(c["time"])), b["o"], b["h"], b["l"], b["c"],
                                    a["o"], a["h"], a["l"], a["c"], c.get("volume", 0)])
                tmp.replace(p)
                manifest[d.isoformat()] = {
                    "rows": len(rows),
                    "final": trading_day_bounds(d)[1] <= now,
                    "sha256": sha256_file(p),
                    "fetched_utc": _iso(now),
                    "source": "oanda_practice_candles_M1_BA",
                }
                fetched += 1
            self._save_manifest(manifest)
        return {
            "days": len(days),
            "fetched": fetched,
            "already_saved": len(days) - fetched,
            "bars": sum(manifest[d.isoformat()]["rows"] for d in days),
            "requests": self.requests,
        }

    def verify(self) -> list[str]:
        """Files whose contents no longer match the manifest (changed after saving)."""
        bad = []
        for day, info in self._manifest().items():
            p = self.day_path(date.fromisoformat(day))
            if not p.exists() or sha256_file(p) != info["sha256"]:
                bad.append(day)
        return bad


def read_bars(data_dir: str | Path, start: datetime, end: datetime, instrument: str = "EUR_USD") -> list[dict]:
    """Saved bars with start <= bar start < end, oldest first, prices as Decimals."""
    start, end = require_utc(start), require_utc(end)
    d = trading_day(start)
    last = trading_day(end - timedelta(microseconds=1))
    base = Path(data_dir) / "history" / instrument / "M1"
    out = []
    while d <= last:
        p = base / f"{d.isoformat()}.csv"
        if p.exists():
            with open(p, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    t = datetime.fromisoformat(r["start_utc"].replace("Z", "+00:00"))
                    if start <= t < end:
                        out.append({
                            "start": t,
                            "bid": {k: Decimal(r[f"bid_{k}"]) for k in "ohlc"},
                            "ask": {k: Decimal(r[f"ask_{k}"]) for k in "ohlc"},
                            "volume": int(r["volume"]),
                        })
        d += timedelta(days=1)
    out.sort(key=lambda b: b["start"])
    return out
