"""Shadow simulator: virtual trades filled at OANDA's real bid and ask.

Flow for each closed 1-minute bar (decision time = bar start + 1 minute):
  1. Manage the open virtual trade with this bar: stop, partial exits, target.
  2. Data check on the bar (fxsystem.replay.bar_check). Bad data: no new trade.
  3. Ask the strategy. Each idea goes to the risk engine with the bar's closing
     bid/ask and the virtual account. Every decision is logged.
  4. An approved idea becomes a pending entry, filled at the NEXT bar's open:
     buys at the ask, sells at the bid. If the next bar is not usable (gap, wide
     spread, market closed), the entry is cancelled, never filled late.

Fill rules (conservative on purpose):
- A long stops out when the bid trades at or below the stop; a short when the ask
  trades at or above it. If the bar opens beyond the stop, the fill is at that
  worse opening price.
- Targets and partial exits fill at their exact price, never better.
- If one bar touches both the stop and a profit level, the stop is assumed to
  come first: a 1-minute bar cannot show the order.
- 1R is the risk the risk engine approved. Results are in R and in dollars.

This is a simulation. Shadow results are not proof of an edge.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Sequence

from fxsystem.clock import NEW_YORK, trading_day
from fxsystem.quality import Limits
from fxsystem.replay import Strategy, bar_check
from fxsystem.risk import AccountSnapshot, Proposal, Quote, RiskEngine
from fxsystem.storage import LocalLog, canonical_json

UTC = timezone.utc
PIP = Decimal("0.0001")
SHADOW_ACCOUNT = "101-SHADOW-0000000-001"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Partial:
    """Close `fraction` of the original position when profit reaches `at_r` R.
    Optionally move the stop on the rest to the entry price (break-even)."""

    at_r: Decimal
    fraction: Decimal
    move_stop_to_entry: bool = False


@dataclass
class VirtualTrade:
    intent_id: str
    side: str
    units: int                  # original size
    remaining: int
    entry_price: Decimal
    entry_time: datetime
    stop: Decimal
    target: Decimal | None
    risk_usd: Decimal           # 1R in dollars, as approved by the risk engine
    partials: list[Partial]
    done_partials: int = 0
    realized: Decimal = Decimal(0)
    exits: list = field(default_factory=list)
    ambiguous_bars: int = 0

    def r_price(self, r: Decimal) -> Decimal:
        dist = abs(self.entry_price - self._initial_stop) * r
        return self.entry_price + dist if self.side == "buy" else self.entry_price - dist

    def __post_init__(self):
        self._initial_stop = self.stop


class ShadowAccount:
    def __init__(self, start_equity: Decimal, margin_rate: Decimal = Decimal("0.02")) -> None:
        self.balance = Decimal(start_equity)
        self.margin_rate = margin_rate
        self.trade: VirtualTrade | None = None

    def unrealized(self, bid: Decimal, ask: Decimal) -> Decimal:
        t = self.trade
        if not t:
            return Decimal(0)
        return (bid - t.entry_price) * t.remaining if t.side == "buy" else (t.entry_price - ask) * t.remaining

    def snapshot(self, bid: Decimal, ask: Decimal, now: datetime) -> AccountSnapshot:
        t = self.trade
        equity = self.balance + self.unrealized(bid, ask)
        units, open_risk, margin_used = 0, Decimal(0), Decimal(0)
        if t:
            units = t.remaining if t.side == "buy" else -t.remaining
            mark = bid if t.side == "buy" else ask
            loss_to_stop = (mark - t.stop) if t.side == "buy" else (t.stop - mark)
            open_risk = max(Decimal(0), loss_to_stop * t.remaining)
            margin_used = t.remaining * mark * self.margin_rate
        return AccountSnapshot(SHADOW_ACCOUNT, "USD", equity, equity - margin_used, units, open_risk, now)


@dataclass
class ShadowResult:
    run_id: str
    bars: int = 0
    ideas: int = 0
    approved: int = 0
    refused: dict = field(default_factory=dict)
    cancelled_entries: int = 0
    trades: list = field(default_factory=list)
    final_equity: Decimal = Decimal(0)
    fingerprint: str = ""
    log_path: str = ""

    def summary(self) -> dict:
        rs = [t["r"] for t in self.trades]
        wins = [r for r in rs if r > 0]
        peak = dd = cum = Decimal(0)
        for r in rs:
            cum += r
            peak = max(peak, cum)
            dd = max(dd, peak - cum)
        n = len(rs)
        return {
            "trades": n,
            "wins": len(wins),
            "win_rate": f"{Decimal(len(wins)) / n:.2f}" if n else None,
            "total_r": f"{sum(rs, Decimal(0)):.2f}",
            "avg_r": f"{sum(rs, Decimal(0)) / n:.3f}" if n else None,
            "max_drawdown_r": f"{dd:.2f}",
            "ambiguous_trades": sum(1 for t in self.trades if t["ambiguous_bars"]),
            "final_equity": f"{self.final_equity:.2f}",
        }


def _reason_key(r: str) -> str:
    for word, key in (("kill", "kill_switch"), ("window", "outside_window"), ("worst case", "worst_case"),
                      ("new trades today", "daily_cap"), ("shutdown", "daily_shutdown"),
                      ("reached the limit", "daily_loss"), ("one at a time", "trade_open"),
                      ("stop", "stop"), ("margin", "margin"), ("data quality", "data_quality"),
                      ("duplicate", "duplicate")):
        if word in r:
            return key
    return "other"


def run_shadow(
    bars: list[dict],
    strategy: Strategy,
    engine: RiskEngine,
    out_dir: str | Path,
    start_equity: Decimal = Decimal(10000),
    limits: Limits = Limits(),
    partials: Sequence[Partial] = (),
    started: datetime | None = None,
    inputs: dict | None = None,
    flat_at: time | None = None,
) -> ShadowResult:
    """flat_at: if set, an open trade is closed by this New York time on its entry day, at the
    close of the first bar ending at or after it (closing bid for longs, ask for shorts)."""
    started = started or datetime.now(UTC)
    config = {"strategy": strategy.name, "strategy_version": strategy.version,
              "risk_config": engine.cfg.config_hash, "start_equity": str(start_equity),
              "partials": [[str(p.at_r), str(p.fraction), p.move_stop_to_entry] for p in partials],
              "bars": len(bars), "first_bar": _iso(bars[0]["start"]) if bars else None,
              "flat_at_new_york": flat_at.isoformat() if flat_at else None,
              "inputs": inputs or {}}
    run_id = f"{started:%Y%m%dT%H%M%SZ}-shadow-{hashlib.sha256(canonical_json(config).encode()).hexdigest()[:12]}"
    path = Path(out_dir) / "replays" / f"{run_id}.sqlite"
    acct = ShadowAccount(start_equity, engine.cfg.margin_rate)
    res = ShadowResult(run_id, log_path=str(path))
    fp = hashlib.sha256()

    with LocalLog(path) as log:
        engine.log = log
        engine.allowed_accounts = {SHADOW_ACCOUNT}
        log.append("system.shadow_start", started, {"run_id": run_id, **config})
        pending = None  # (decision, proposal, decided_at, risk_usd)

        def emit(kind: str, now: datetime, payload: dict) -> None:
            log.append(kind, now, {"run_id": run_id, **payload})
            # The fingerprint ignores the run's own name, so identical runs match.
            fp.update(canonical_json({"kind": kind, **payload}).replace(run_id, "RUN").encode())

        def close_part(t: VirtualTrade, units: int, price: Decimal, when: datetime, why: str) -> None:
            pnl = (price - t.entry_price) * units if t.side == "buy" else (t.entry_price - price) * units
            t.remaining -= units
            t.realized += pnl
            acct.balance += pnl
            t.exits.append({"time": _iso(when), "units": units, "price": str(price), "why": why})
            emit("shadow.fill", when, {"intent_id": t.intent_id, "action": why, "units": units,
                                       "price": str(price), "pnl": f"{pnl:.2f}"})
            if t.remaining == 0:
                r = (t.realized / t.risk_usd).quantize(Decimal("0.001")) if t.risk_usd else Decimal(0)
                summary = {"intent_id": t.intent_id, "side": t.side, "units": t.units,
                           "entry_time": _iso(t.entry_time), "entry_price": str(t.entry_price),
                           "exit_time": _iso(when), "pnl": f"{t.realized:.2f}", "r": str(r),
                           "ambiguous_bars": t.ambiguous_bars, "exits": t.exits}
                emit("shadow.trade_closed", when, summary)
                res.trades.append({**summary, "r": r})
                acct.trade = None
                engine.trade_closed(t.intent_id, when)

        def manage(t: VirtualTrade, b: dict, when: datetime, entry_bar: bool) -> None:
            """Apply one bar to the open trade, conservatively."""
            if t.side == "buy":
                worst_hit = b["bid"]["l"] <= t.stop
                gap_price = b["bid"]["o"] if b["bid"]["o"] <= t.stop else None
                best = b["bid"]["h"]
                reached = lambda px: best >= px  # noqa: E731
            else:
                worst_hit = b["ask"]["h"] >= t.stop
                gap_price = b["ask"]["o"] if b["ask"]["o"] >= t.stop else None
                best = b["ask"]["l"]
                reached = lambda px: best <= px  # noqa: E731
            levels = []
            if t.done_partials < len(t.partials):
                levels.append(t.r_price(t.partials[t.done_partials].at_r))
            if t.target is not None:
                levels.append(t.target)
            profit_touch = any(reached(px) for px in levels)
            if worst_hit:
                if profit_touch:
                    t.ambiguous_bars += 1  # cannot tell the order: assume the stop came first
                close_part(t, t.remaining, gap_price if gap_price is not None else t.stop, when,
                           "stop" if t.stop != t.entry_price else "stop_at_entry")
                return
            while t.done_partials < len(t.partials) and t.remaining:
                p = t.partials[t.done_partials]
                px = t.r_price(p.at_r)
                if not reached(px):
                    break
                units = int((Decimal(t.units) * p.fraction).to_integral_value(rounding=ROUND_DOWN))
                units = min(units, t.remaining)
                t.done_partials += 1
                if units:
                    close_part(t, units, px, when, f"partial_{p.at_r}R")
                if p.move_stop_to_entry and t.remaining:
                    t.stop = t.entry_price
                    emit("shadow.stop_moved", when, {"intent_id": t.intent_id, "stop": str(t.stop)})
            if t.remaining and t.target is not None and reached(t.target):
                close_part(t, t.remaining, t.target, when, "target")

        for i, b in enumerate(bars):
            res.bars += 1
            now = b["start"] + timedelta(minutes=1)
            prev = bars[i - 1] if i else None
            reasons = bar_check(b, prev, limits)

            # 4 (from the previous bar). Fill or cancel the pending entry at this bar's open.
            if pending:
                dec, prop, decided_at, risk_usd, target = pending
                pending = None
                if reasons or b["start"] != decided_at:
                    res.cancelled_entries += 1
                    emit("shadow.entry_cancelled", now, {"intent_id": prop.intent_id,
                                                         "why": "; ".join(reasons) or "next bar missing"})
                    engine.trade_closed(prop.intent_id, now)
                else:
                    price = b["ask"]["o"] if prop.side == "buy" else b["bid"]["o"]
                    t = VirtualTrade(prop.intent_id, prop.side, dec.units, dec.units, price, b["start"],
                                     prop.stop_price, target, risk_usd, list(partials))
                    acct.trade = t
                    emit("shadow.fill", b["start"], {"intent_id": t.intent_id, "action": "entry",
                                                     "side": t.side, "units": t.units, "price": str(price),
                                                     "stop": str(t.stop), "target": str(target) if target else None})
                    manage(t, b, now, entry_bar=True)

            # 1. Manage an open trade (stops are honoured even on bad-data bars).
            elif acct.trade:
                manage(acct.trade, b, now, entry_bar=False)

            # Optional time exit: flat by flat_at (New York) on the entry day.
            t = acct.trade
            if t and flat_at is not None:
                cutoff = datetime.combine(t.entry_time.astimezone(NEW_YORK).date(), flat_at, tzinfo=NEW_YORK)
                if b["start"] + timedelta(minutes=1) >= cutoff:  # this bar ends at or after the cutoff
                    close_part(t, t.remaining, b["bid"]["c"] if t.side == "buy" else b["ask"]["c"], now, "time_exit")

            # Start each trading day's risk state with that moment's equity.
            day = trading_day(now)
            if engine.state is None or engine.state.day != day:
                engine.start_day(day, acct.snapshot(b["bid"]["c"], b["ask"]["c"], now).equity, now)

            # 2. Data check. 3. Strategy and risk.
            if reasons:
                continue
            for intent in strategy.on_bar(bars[: i + 1], now):
                res.ideas += 1
                bid, ask = b["bid"]["c"], b["ask"]["c"]
                ref = ask if intent.side == "buy" else bid
                sign = -1 if intent.side == "buy" else 1
                stop = ref + sign * intent.stop_pips * PIP
                target = ref - sign * intent.target_pips * PIP if intent.target_pips else None
                prop = Proposal(f"{run_id}:{_iso(b['start'])}:{intent.side}", "EUR_USD", intent.side, stop, target)
                dec = engine.evaluate(prop, Quote(bid, ask, True), acct.snapshot(bid, ask, now), now)
                if dec.approved:
                    res.approved += 1
                    pending = (dec, prop, b["start"] + timedelta(minutes=1), Decimal(dec.details["trade_risk"]), target)
                else:
                    for k in {_reason_key(r) for r in dec.reasons}:  # each decision counted once per kind
                        res.refused[k] = res.refused.get(k, 0) + 1

        last = bars[-1] if bars else None
        if acct.trade and last:
            emit("shadow.open_at_end", last["start"], {"intent_id": acct.trade.intent_id,
                                                       "unrealized": f"{acct.unrealized(last['bid']['c'], last['ask']['c']):.2f}"})
        res.final_equity = acct.balance + (acct.unrealized(last["bid"]["c"], last["ask"]["c"]) if last else 0)
        res.fingerprint = fp.hexdigest()
        log.append("system.shadow_stop", started, {"run_id": run_id, **res.summary(),
                                                   "fingerprint": res.fingerprint, "refused": res.refused})
    return res
