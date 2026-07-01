"""Event-driven backtester.

Conventions to avoid look-ahead bias:
  * A signal is decided on the CLOSE of bar j (using data <= j).
  * The trade is entered at the OPEN of bar j+1.
  * Stops/targets are then evaluated bar-by-bar using each bar's high/low.
  * If a bar could hit BOTH stop and target, we assume the stop hit first
    (conservative).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

import pandas as pd

from ..config import Config
from ..risk import RiskManager, TradePlan
from ..strategies.base import Strategy
from .metrics import compute_metrics


@dataclass
class Trade:
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    side: str
    entry: float
    exit: float
    qty: float
    pnl: float
    r_multiple: float
    reason: str           # entry reason
    exit_reason: str      # tp | stop | eod
    balance_after: float


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity: pd.Series
    metrics: dict
    starting_balance: float
    final_balance: float


class Backtester:
    def __init__(self, cfg: Config, strategy: Strategy) -> None:
        self.cfg = cfg
        self.strategy = strategy
        self.risk = RiskManager(
            cfg.risk, lot_size=cfg.market.lot_size, min_lots=cfg.market.min_lots
        )
        self.fee = cfg.backtest.fee_rate
        self.slip = cfg.backtest.slippage_rate

    def run(self, df: pd.DataFrame, starting_balance: Optional[float] = None) -> BacktestResult:
        start_bal = starting_balance if starting_balance is not None else self.cfg.starting_balance
        balance = start_bal
        df = self.strategy.prepare(df)
        n = len(df)

        position: Optional[dict] = None
        trades: list[Trade] = []
        equity_times: list[pd.Timestamp] = []
        equity_values: list[float] = []
        day_trades: dict = {}

        start_i = self.strategy.warmup + 1
        for i in range(start_i, n):
            bar = df.iloc[i]
            ts = df.index[i]

            # 1) Manage an existing position on this bar.
            if position is not None:
                closed, balance = self._maybe_exit(position, bar, ts, balance, trades)
                if closed:
                    position = None

            # 2) Consider a new entry from the previous bar's signal.
            if position is None:
                day = ts.date()
                if day_trades.get(day, 0) < self.cfg.risk.max_trades_per_day:
                    sig = self.strategy.signal(df, i - 1)
                    if sig is not None:
                        position = self._try_open(sig, bar, balance)
                        if position is not None:
                            day_trades[day] = day_trades.get(day, 0) + 1
                            # Allow same-bar stop/target resolution.
                            closed, balance = self._maybe_exit(
                                position, bar, ts, balance, trades
                            )
                            if closed:
                                position = None

            unreal = self._unrealised(position, float(bar["close"])) if position else 0.0
            equity_times.append(ts)
            equity_values.append(balance + unreal)

        # Close any still-open position at the final close.
        if position is not None:
            last_ts = df.index[-1]
            last_close = float(df.iloc[-1]["close"])
            balance = self._record_exit(
                position, last_close, last_ts, "eod", balance, trades
            )

        trades_df = pd.DataFrame([asdict(t) for t in trades])
        equity = pd.Series(equity_values, index=pd.DatetimeIndex(equity_times), name="equity")
        if equity.empty:
            equity = pd.Series([balance], name="equity")
        metrics = compute_metrics(trades_df, equity, start_bal)

        return BacktestResult(
            trades=trades_df,
            equity=equity,
            metrics=metrics,
            starting_balance=start_bal,
            final_balance=float(equity.iloc[-1]),
        )

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _try_open(self, sig, bar, balance) -> Optional[dict]:
        open_px = float(bar["open"])
        if sig.side == "long":
            entry = open_px * (1 + self.slip)
            if entry <= sig.stop:        # gapped through the stop already
                return None
        else:
            entry = open_px * (1 - self.slip)
            if entry >= sig.stop:
                return None

        # Same guard the live trader applies: skip if the stop is wide
        # enough that the exchange's own liquidation (at risk.max_leverage)
        # could plausibly fire before this stop does.
        if not self.risk.liquidation_buffer_ok(entry, sig.stop):
            return None

        plan: TradePlan | None = self.risk.build_plan(sig.side, entry, sig.stop, balance)
        if plan is None or plan.qty <= 0:
            return None

        return {
            "side": plan.side,
            "entry": plan.entry,
            "stop": plan.stop,
            "initial_stop": plan.stop,
            "tp": plan.take_profit,
            "qty": plan.qty,
            "risk_usd": plan.risk_usd,
            "entry_time": bar.name,
            "reason": sig.reason,
            "trail_amount": plan.trail_amount,
            "high_water": plan.entry,
            "low_water": plan.entry,
        }

    def _maybe_exit(self, pos, bar, ts, balance, trades) -> tuple[bool, float]:
        high, low = float(bar["high"]), float(bar["low"])

        # Ratchet the stop toward the best price reached, mirroring Delta's
        # native bracket_trail_amount trailing stop-loss (only ever tightens).
        if pos.get("trail_amount"):
            if pos["side"] == "long":
                pos["high_water"] = max(pos["high_water"], high)
                candidate = pos["high_water"] - pos["trail_amount"]
                if candidate > pos["stop"]:
                    pos["stop"] = candidate
            else:
                pos["low_water"] = min(pos["low_water"], low)
                candidate = pos["low_water"] + pos["trail_amount"]
                if candidate < pos["stop"]:
                    pos["stop"] = candidate

        exit_price = None
        exit_reason = ""

        if pos["side"] == "long":
            if low <= pos["stop"]:                       # stop first (conservative)
                exit_price = pos["stop"] * (1 - self.slip)
                exit_reason = "trail" if pos["stop"] > pos["initial_stop"] else "stop"
            elif high >= pos["tp"]:
                exit_price, exit_reason = pos["tp"], "tp"
        else:
            if high >= pos["stop"]:
                exit_price = pos["stop"] * (1 + self.slip)
                exit_reason = "trail" if pos["stop"] < pos["initial_stop"] else "stop"
            elif low <= pos["tp"]:
                exit_price, exit_reason = pos["tp"], "tp"

        if exit_price is None:
            return False, balance
        balance = self._record_exit(pos, exit_price, ts, exit_reason, balance, trades)
        return True, balance

    def _record_exit(self, pos, exit_price, ts, exit_reason, balance, trades) -> float:
        qty = pos["qty"]
        if pos["side"] == "long":
            gross = qty * (exit_price - pos["entry"])
        else:
            gross = qty * (pos["entry"] - exit_price)
        fees = self.fee * qty * (pos["entry"] + exit_price)
        pnl = gross - fees
        balance += pnl
        r_multiple = pnl / pos["risk_usd"] if pos["risk_usd"] > 0 else 0.0

        trades.append(
            Trade(
                entry_time=pos["entry_time"],
                exit_time=ts,
                side=pos["side"],
                entry=round(pos["entry"], 2),
                exit=round(exit_price, 2),
                qty=round(qty, 8),
                pnl=round(pnl, 4),
                r_multiple=round(r_multiple, 3),
                reason=pos["reason"],
                exit_reason=exit_reason,
                balance_after=round(balance, 2),
            )
        )
        return balance

    def _unrealised(self, pos, price) -> float:
        if pos is None:
            return 0.0
        if pos["side"] == "long":
            return pos["qty"] * (price - pos["entry"])
        return pos["qty"] * (pos["entry"] - price)
