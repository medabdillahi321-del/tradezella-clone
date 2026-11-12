"""Risk engine: the last check before any trade, real or virtual.

It does not know or care which strategy proposed the trade. It answers APPROVED
(with a position size) or NO TRADE (with every reason), and it fails closed:
anything missing, stale, inconsistent or unknown is NO TRADE.

Rules:
- Size: risk 0.25% of the equity basis. The basis is the LOWER of start-of-day
  equity and current equity. Per-unit risk is the distance from the real entry
  price (ask for a buy, bid for a sell) to the stop, plus a slippage allowance.
  Units are rounded down. Less than 1 unit means NO TRADE.
- At most 4 new trades per trading day (resets 17:00 New York).
- One trade at a time: no new trade while a position or approved trade is open.
- Daily loss: loss so far = start-of-day equity minus current equity (current
  equity already includes open profit or loss and costs). If it reaches 1.00%,
  the day is shut down and stays shut down until the next trading day.
- Worst case: loss so far + risk of open positions + risk of the new trade must
  stay within 1.00%. So after a full loss, a further full-size trade that could
  breach the limit is refused even if the trade count allows it.
- Also NO TRADE on: kill switch, data-quality gate failing, outside the operating
  window, instrument or account not allowed, stale or mismatched account data,
  unreconciled positions, stop on the wrong side or too close or too far, a target
  on the wrong side, not enough margin, a repeated intent, or settings changed
  since the day started.

The engine never changes its own limits. It cannot place orders.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from fxsystem.clock import Window, require_utc, trading_day
from fxsystem.storage import LocalLog

UTC = timezone.utc
PIP = Decimal("0.0001")
HUNDRED = Decimal(100)


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade_pct: Decimal
    max_new_trades_per_day: int
    daily_loss_limit_pct: Decimal
    equity_basis: str
    slippage_allowance_pips: Decimal
    min_stop_pips: Decimal
    max_stop_pips: Decimal
    margin_rate: Decimal
    max_margin_use: Decimal
    max_snapshot_age_s: float
    account_currency: str
    allowed_instruments: tuple[str, ...]
    kill_switch_file: str
    config_hash: str

    @classmethod
    def load(cls, path: str | Path) -> "RiskConfig":
        import tomllib

        raw_bytes = Path(path).read_bytes()
        r = tomllib.loads(raw_bytes.decode("utf-8"))["risk"]
        if r["equity_basis"] != "lower_of_day_start_and_current":
            raise ValueError(f"Unknown equity_basis {r['equity_basis']!r}")
        return cls(
            risk_per_trade_pct=Decimal(r["risk_per_trade_pct"]),
            max_new_trades_per_day=int(r["max_new_trades_per_day"]),
            daily_loss_limit_pct=Decimal(r["daily_loss_limit_pct"]),
            equity_basis=r["equity_basis"],
            slippage_allowance_pips=Decimal(r["slippage_allowance_pips"]),
            min_stop_pips=Decimal(r["min_stop_pips"]),
            max_stop_pips=Decimal(r["max_stop_pips"]),
            margin_rate=Decimal(r["margin_rate"]),
            max_margin_use=Decimal(r["max_margin_use"]),
            max_snapshot_age_s=float(r["max_snapshot_age_s"]),
            account_currency=r["account_currency"],
            allowed_instruments=tuple(r["allowed_instruments"]),
            kill_switch_file=r["kill_switch_file"],
            config_hash=hashlib.sha256(raw_bytes).hexdigest()[:16],
        )


@dataclass(frozen=True)
class AccountSnapshot:
    """What the broker says right now. For shadow trading this is a virtual account."""

    account_id: str
    currency: str
    equity: Decimal               # balance + open profit/loss (OANDA "NAV")
    margin_available: Decimal
    open_position_units: int      # signed: positive long, negative short, 0 flat
    open_position_risk: Decimal   # money lost if open positions hit their stops (0 if flat)
    taken_at: datetime


@dataclass(frozen=True)
class Proposal:
    intent_id: str
    instrument: str
    side: str                     # "buy" or "sell"
    stop_price: Decimal
    target_price: Decimal | None = None


@dataclass(frozen=True)
class Quote:
    bid: Decimal
    ask: Decimal
    gate_ok: bool
    gate_reasons: tuple[str, ...] = ()


@dataclass
class Decision:
    approved: bool
    units: int = 0
    reasons: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return f"APPROVED {self.units} units" if self.approved else "NO TRADE: " + "; ".join(self.reasons)


@dataclass
class DayState:
    day: date
    start_equity: Decimal
    config_hash: str
    approved_today: int = 0
    shut_down: bool = False
    shutdown_reason: str = ""
    trade_open: bool = False          # an approved trade not yet closed
    unreconciled: bool = False        # broker and system disagree about positions
    seen_intents: set = field(default_factory=set)


class RiskEngine:
    def __init__(
        self,
        config: RiskConfig,
        window: Window,
        data_dir: str | Path,
        allowed_accounts: set[str] | None = None,
        log: LocalLog | None = None,
    ) -> None:
        self.cfg = config
        self.window = window
        self.data_dir = Path(data_dir)
        if allowed_accounts is None:
            allowed_accounts = {a.strip() for a in os.environ.get("FX_ALLOWED_ACCOUNTS", "").split(",") if a.strip()}
        self.allowed_accounts = allowed_accounts
        self.log = log
        self.state: DayState | None = None

    # ---- day handling ----
    def start_day(self, day: date, start_equity: Decimal, now: datetime | None = None) -> DayState:
        self.state = DayState(day, Decimal(start_equity), self.cfg.config_hash)
        self._log("risk.day_start", now or datetime.now(UTC), {
            "day": day.isoformat(), "start_equity": str(start_equity), "config_hash": self.cfg.config_hash})
        return self.state

    def restore(self, log: LocalLog, day: date) -> DayState | None:
        """Rebuild today's state from the event log after a restart. Unknown history means NO TRADE."""
        st = None
        for e in log.events("risk."):
            p = e.payload
            # day_start and shutdown carry their trading day; other events are dated by when they happened.
            event_day = p.get("day", e.trading_day)
            if event_day != day.isoformat():
                continue
            if e.kind == "risk.day_start":
                st = DayState(day, Decimal(p["start_equity"]), p["config_hash"])
            elif st is None:
                continue
            elif e.kind == "risk.decision":
                st.seen_intents.add(p["intent_id"])
                if p["approved"]:
                    st.approved_today += 1
                    st.trade_open = True
            elif e.kind == "risk.trade_closed":
                st.trade_open = False
            elif e.kind == "risk.shutdown":
                st.shut_down, st.shutdown_reason = True, p["reason"]
        self.state = st
        return st

    def trade_closed(self, intent_id: str, now: datetime) -> None:
        if self.state:
            self.state.trade_open = False
        self._log("risk.trade_closed", now, {"intent_id": intent_id})

    def mark_unreconciled(self, why: str, now: datetime) -> None:
        if self.state:
            self.state.unreconciled = True
        self._log("risk.unreconciled", now, {"reason": why})

    def _shutdown(self, reason: str, now: datetime) -> None:
        st = self.state
        if st and not st.shut_down:
            st.shut_down, st.shutdown_reason = True, reason
            self._log("risk.shutdown", now, {"reason": reason, "day": st.day.isoformat()})

    def _log(self, kind: str, now: datetime, payload: dict) -> None:
        if self.log is not None:
            self.log.append(kind, now, payload)

    # ---- the decision ----
    def evaluate(self, p: Proposal, quote: Quote, acct: AccountSnapshot | None, now: datetime) -> Decision:
        now = require_utc(now)
        c, st = self.cfg, self.state
        reasons: list[str] = []
        d: dict = {"intent_id": p.intent_id, "config_hash": c.config_hash, "side": p.side,
                   "stop_price": str(p.stop_price), "bid": str(quote.bid), "ask": str(quote.ask)}

        # -- hard stops: checked first, and always all reported --
        if (self.data_dir / c.kill_switch_file).exists():
            reasons.append("kill switch is on")
        if st is None:
            reasons.append("risk day not started")
        elif st.day != trading_day(now):
            reasons.append(f"risk state is for {st.day}, but it is now trading day {trading_day(now)}")
        else:
            if st.config_hash != c.config_hash:
                reasons.append("risk settings changed since the day started")
            if st.shut_down:
                reasons.append(f"daily shutdown: {st.shutdown_reason}")
            if st.unreconciled:
                reasons.append("positions not reconciled with the broker")
            if p.intent_id in st.seen_intents:
                reasons.append("this trade idea was already decided (duplicate)")
            if st.trade_open:
                reasons.append("a trade is already open (one at a time)")
            if st.approved_today >= c.max_new_trades_per_day:
                reasons.append(f"already {st.approved_today} new trades today (max {c.max_new_trades_per_day})")

        if not quote.gate_ok:
            reasons.append("data quality: " + ("; ".join(quote.gate_reasons) or "not OK"))
        if not self.window.contains(now):
            reasons.append("outside the operating window")
        if p.instrument not in c.allowed_instruments:
            reasons.append(f"instrument {p.instrument} not allowed")
        if p.side not in ("buy", "sell"):
            reasons.append(f"unknown side {p.side!r}")

        # -- account --
        if acct is None:
            reasons.append("no account data")
        else:
            age = (now - require_utc(acct.taken_at)).total_seconds()
            if age > c.max_snapshot_age_s or age < -2:
                reasons.append(f"account data is {age:.0f}s old")
            if not acct.account_id.startswith("101-"):
                reasons.append("account is not an OANDA practice account")
            if acct.account_id not in self.allowed_accounts:
                reasons.append("account is not on the allowed list")
            if acct.currency != c.account_currency:
                reasons.append(f"account currency {acct.currency} is not supported")
            if acct.open_position_units != 0:
                reasons.append("a position is already open at the broker (one at a time)")
            if acct.equity <= 0:
                reasons.append("equity is zero or negative")

        # -- prices and stop --
        entry = quote.ask if p.side == "buy" else quote.bid
        if quote.ask <= quote.bid:
            reasons.append("crossed quote")
        stop_dist = (entry - p.stop_price) if p.side == "buy" else (p.stop_price - entry)
        d["entry_price"] = str(entry)
        d["stop_pips"] = f"{stop_dist / PIP:.1f}"
        if stop_dist <= 0:
            reasons.append("stop is on the wrong side of the entry price")
        else:
            if stop_dist / PIP < c.min_stop_pips:
                reasons.append(f"stop {stop_dist / PIP:.1f} pips is tighter than {c.min_stop_pips}")
            if stop_dist / PIP > c.max_stop_pips:
                reasons.append(f"stop {stop_dist / PIP:.1f} pips is wider than {c.max_stop_pips}")
        if p.target_price is not None:
            if (p.side == "buy" and p.target_price <= entry) or (p.side == "sell" and p.target_price >= entry):
                reasons.append("target is on the wrong side of the entry price")

        # -- money: only meaningful with a started day and account data --
        if st is not None and acct is not None and acct.equity > 0 and stop_dist > 0:
            limit = st.start_equity * c.daily_loss_limit_pct / HUNDRED
            loss_so_far = max(Decimal(0), st.start_equity - acct.equity)
            d["loss_so_far"] = str(loss_so_far.quantize(Decimal("0.01")))
            d["daily_limit"] = str(limit.quantize(Decimal("0.01")))
            if st.day == trading_day(now) and loss_so_far >= limit:
                self._shutdown(f"daily loss {loss_so_far:.2f} reached the limit {limit:.2f}", now)
                reasons.append(f"daily loss {loss_so_far:.2f} reached the limit {limit:.2f}")

            basis = min(st.start_equity, acct.equity)
            risk_amount = basis * c.risk_per_trade_pct / HUNDRED
            per_unit = stop_dist + c.slippage_allowance_pips * PIP  # USD per unit on EUR/USD
            units = int((risk_amount / per_unit).to_integral_value(rounding=ROUND_DOWN))
            trade_risk = units * per_unit
            worst = loss_so_far + acct.open_position_risk + trade_risk
            margin_needed = units * entry * c.margin_rate
            d.update({
                "equity_basis": str(basis), "risk_amount": str(risk_amount.quantize(Decimal("0.01"))),
                "per_unit_risk": str(per_unit), "units": units,
                "trade_risk": str(trade_risk.quantize(Decimal("0.01"))),
                "worst_case_day_loss": str(worst.quantize(Decimal("0.01"))),
                "margin_needed": str(margin_needed.quantize(Decimal("0.01"))),
            })
            if units < 1:
                reasons.append("position size rounds down to zero units")
            if worst > limit:
                reasons.append(f"worst case for the day {worst:.2f} would exceed the limit {limit:.2f}")
            if margin_needed > acct.margin_available * c.max_margin_use:
                reasons.append("not enough margin")
        else:
            units = 0

        approved = not reasons
        decision = Decision(approved, units if approved else 0, reasons, d)
        if st is not None and st.day == trading_day(now) and p.intent_id not in st.seen_intents:
            st.seen_intents.add(p.intent_id)
            if approved:
                st.approved_today += 1
                st.trade_open = True
        self._log("risk.decision", now, {**d, "approved": approved, "units": decision.units, "reasons": reasons})
        return decision
