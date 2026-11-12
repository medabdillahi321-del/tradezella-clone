"""Health monitor: checks the running system and raises alerts.

check() looks at the files the system writes and returns findings, each with a
level: INFO, WARN or CRITICAL. It reads only; it never changes recorded data.

Alerter decides what to send: a new problem is sent once, repeated every
resend_after_min while it lasts, and followed by an "all clear" when it goes away.
Alerts go to the screen, the event log ("system.alert") and, if ALERT_WEBHOOK_URL
is set, to Discord, Slack or Telegram. The webhook address is never logged.

A CRITICAL finding listed in auto_kill, during the operating window, turns the
kill switch on. The monitor never turns it off.
"""

from __future__ import annotations

import json
import os
import shutil
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from fxsystem.clock import Window, market_open, parse_oanda_time, require_utc, trading_day
from fxsystem.storage import LocalLog

UTC = timezone.utc
LEVELS = {"INFO": 0, "WARN": 1, "CRITICAL": 2}


@dataclass(frozen=True)
class MonitorConfig:
    bar_stale_after_s: float = 180
    tick_stale_after_s: float = 30
    max_latency_s: float = 5
    stream_errors_warn: int = 3
    disk_warn_gb: float = 2.0
    disk_critical_gb: float = 0.5
    sync_backlog_warn: int = 1000
    resend_after_min: float = 30
    auto_kill: tuple[str, ...] = ("recorder_stopped", "prices_stale", "log_tampered")

    @classmethod
    def load(cls, path: str | Path) -> "MonitorConfig":
        import tomllib

        with open(path, "rb") as f:
            raw = tomllib.load(f)["monitor"]
        raw["auto_kill"] = tuple(raw.get("auto_kill", ()))
        return cls(**raw)


@dataclass(frozen=True)
class Finding:
    level: str
    code: str
    message: str


def _to_dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def last_tick(data_dir: Path, instrument: str, now: datetime) -> tuple[datetime, datetime] | None:
    """(OANDA time, received time) of the last price written, from the end of today's tick file."""
    p = data_dir / "ticks" / instrument / f"{trading_day(now).isoformat()}.csv"
    if not p.exists():
        return None
    with open(p, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 4096))
        lines = [ln for ln in f.read().decode("utf-8", "replace").splitlines() if ln and not ln.startswith("time_utc")]
    if not lines:
        return None
    parts = lines[-1].split(",")
    try:
        return parse_oanda_time(parts[0]), parse_oanda_time(parts[4])
    except (IndexError, ValueError):
        return None


def check(
    data_dir: str | Path,
    now: datetime,
    window: Window,
    cfg: MonitorConfig = MonitorConfig(),
    instrument: str = "EUR_USD",
    verify_chain: bool = False,
    disk_free_gb: Callable[[Path], float] | None = None,
) -> list[Finding]:
    now = require_utc(now)
    data = Path(data_dir)
    out: list[Finding] = []
    in_window = window.contains(now)
    expect = market_open(now) and in_window   # recording must be running now
    urgent = "CRITICAL" if expect else "INFO"

    log_path = data / "events.sqlite"
    if not log_path.exists():
        out.append(Finding(urgent, "recorder_stopped", "No event log yet: the recorder has never run here."))
        return out

    with LocalLog(log_path) as log:
        # Recorder producing bars?
        bar = log.last("market.bar_m1")
        if bar is None:
            out.append(Finding(urgent, "recorder_stopped", "No 1-minute bars recorded yet."))
        else:
            age = (now - (_to_dt(bar.ts_utc) + timedelta(minutes=1))).total_seconds()
            if age > cfg.bar_stale_after_s and market_open(now):
                out.append(Finding(urgent, "recorder_stopped",
                                   f"No new 1-minute bar for {age / 60:.0f} minutes (last one ended "
                                   f"{(_to_dt(bar.ts_utc) + timedelta(minutes=1)):%H:%M} UTC)."))

        # Prices arriving, and on time?
        lt = last_tick(data, instrument, now)
        if lt and market_open(now):
            t, received = lt
            age = (now - t).total_seconds()
            if age > cfg.tick_stale_after_s:
                out.append(Finding(urgent, "prices_stale", f"No new price for {age:.0f} seconds."))
            lag = (received - t).total_seconds()
            if lag > cfg.max_latency_s:
                out.append(Finding("WARN", "prices_late",
                                   f"Last price arrived {lag:.1f} s after its OANDA time (slow link or wrong clock)."))
        elif expect and bar is not None:
            out.append(Finding("CRITICAL", "prices_stale", "No price file for today's trading day."))

        # Connection trouble.
        errors = log.count_since("system.stream_error", now - timedelta(minutes=10))
        if errors >= cfg.stream_errors_warn:
            out.append(Finding("WARN", "stream_errors", f"{errors} price-stream errors in the last 10 minutes."))
        day_start = _day_start(now)
        gaps = log.count_since("market.gap", day_start)
        if gaps:
            out.append(Finding("WARN", "stream_gaps", f"{gaps} gap(s) in the price stream today."))

        # Trading stopped by the system?
        shut = log.last("risk.shutdown")
        if shut and shut.payload.get("day") == trading_day(now).isoformat():
            out.append(Finding("WARN", "risk_shutdown", f"Daily shutdown is on: {shut.payload.get('reason', '')}"))

        # Supabase copy falling behind (only if it is set up).
        if os.environ.get("SUPABASE_URL"):
            backlog = log.unsynced_count()
            if backlog > cfg.sync_backlog_warn:
                out.append(Finding("WARN", "sync_backlog", f"{backlog} events waiting to be copied to Supabase."))

        if verify_chain:
            ok, msg = log.verify()
            if not ok:
                out.append(Finding("CRITICAL", "log_tampered", f"Event log check failed: {msg}"))

    # Kill switch state.
    if (data / "KILL").exists():
        out.append(Finding("WARN", "kill_switch_on", "The kill switch is on: no new trades."))

    # Disk space.
    free = (disk_free_gb or (lambda p: shutil.disk_usage(p).free / 1e9))(data)
    if free < cfg.disk_critical_gb:
        out.append(Finding("CRITICAL", "disk_low", f"Only {free:.1f} GB of disk space left."))
    elif free < cfg.disk_warn_gb:
        out.append(Finding("WARN", "disk_low", f"Only {free:.1f} GB of disk space left."))
    return out


def _day_start(now: datetime) -> datetime:
    from fxsystem.clock import trading_day_bounds

    return trading_day_bounds(trading_day(now))[0]


# ---------- sending alerts ----------

Send = Callable[[str, str], None]


def webhook_sender(url: str, timeout: float = 10.0) -> Send:
    """Post to Discord, Slack or Telegram (sendMessage URL with chat_id), or any JSON webhook."""
    def send(level: str, text: str) -> None:
        host = urllib.parse.urlparse(url).hostname or ""
        msg = f"[{level}] FX system: {text}"
        body = {"content": msg} if "discord" in host else {"text": msg} if ("slack" in host or "telegram" in host) \
            else {"text": msg, "content": msg, "level": level}
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout):
            pass
    return send



class Alerter:
    def __init__(
        self,
        data_dir: str | Path,
        window: Window,
        cfg: MonitorConfig = MonitorConfig(),
        send: Send | None = None,
        echo: Callable[[str], None] = print,
    ) -> None:
        self.data = Path(data_dir)
        self.window = window
        self.cfg = cfg
        self.send = send
        self.echo = echo
        self.state_path = self.data / "monitor_state.json"

    def _state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def handle(self, findings: list[Finding], now: datetime) -> list[str]:
        """Send what needs sending. Returns the messages sent."""
        now = require_utc(now)
        state = self._state()
        current = {f.code: f for f in findings if f.level != "INFO"}
        sent: list[str] = []

        def out(level: str, text: str, code: str) -> None:
            self.echo(f"{now:%Y-%m-%d %H:%M:%S} UTC  {level:<8} {text}")
            with LocalLog(self.data / "events.sqlite") as log:
                log.append("system.alert", now, {"level": level, "code": code, "message": text})
            if self.send:
                try:
                    self.send(level, text)
                except Exception as e:  # noqa: BLE001  an alert channel failing must not stop the monitor
                    self.echo(f"  (could not send the alert message: {type(e).__name__})")
            sent.append(f"{level} {text}")

        for code, f in current.items():
            prev = state.get(code)
            due = prev is None or LEVELS[f.level] > LEVELS[prev["level"]] or \
                now - _to_dt(prev["last_sent"]) >= timedelta(minutes=self.cfg.resend_after_min)
            if due:
                out(f.level, f.message, code)
                state[code] = {"level": f.level, "first_seen": prev["first_seen"] if prev else now.isoformat(),
                               "last_sent": now.isoformat()}
            if f.level == "CRITICAL" and code in self.cfg.auto_kill and self.window.contains(now):
                self._kill(f, now, out)

        for code in [c for c in state if c not in current]:
            out("RESOLVED", f"All clear: {code.replace('_', ' ')}.", code)
            del state[code]

        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(self.state_path)
        return sent

    def _kill(self, f: Finding, now: datetime, out) -> None:
        flag = self.data / "KILL"
        if flag.exists():
            return
        flag.write_text(f"Turned on by the health monitor at {now.isoformat()}: {f.message}\n")
        with LocalLog(self.data / "events.sqlite") as log:
            log.append("system.kill_switch", now, {"state": "on", "by": "monitor", "reason": f.code})
        out("CRITICAL", f"Kill switch turned ON automatically ({f.code}). Turn it off yourself once checked: "
                        "python scripts/kill.py off", "kill_switch_auto")
