"""Position sizing and risk management.

Mirrors how you size an order in the Delta order panel:

  1. Deploy only `capital_allocation_pct` of the wallet (e.g. 50%) as margin.
  2. Risk budget per trade = `risk_per_trade_pct` of that allocation
     (e.g. 10% of the 50% = 5% of the wallet => $5 on a $100 wallet),
     or a fixed `risk_per_trade_usd` if you prefer.
  3. Size from BOTH constraints and take the smaller:
       - risk-based : qty so that hitting the stop loses <= the risk budget
       - margin-based: qty affordable with (allocation * leverage) of notional
  4. Round DOWN to whole lots (Delta trades integer lots; 1 lot = `lot_size` BTC).
  5. If you can't afford the minimum lot within the risk budget -> no trade.

Take-profit is placed at `reward_risk_ratio` x the stop distance (1:2 / 1:3).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .config import RiskConfig


@dataclass
class TradePlan:
    side: str            # "long" / "short"
    entry: float
    stop: float
    take_profit: float
    lots: int            # whole lots to send to Delta (1 lot = lot_size BTC)
    qty: float           # position size in base units (lots * lot_size)
    risk_usd: float      # realised dollars at risk if the stop is hit
    notional: float      # qty * entry
    margin_usd: float    # margin this position will tie up (notional / leverage)


class RiskManager:
    def __init__(self, cfg: RiskConfig, lot_size: float = 0.001, min_lots: int = 1) -> None:
        self.cfg = cfg
        self.lot_size = lot_size      # BTC per lot/contract (Delta BTCUSD = 0.001)
        self.min_lots = max(1, int(min_lots))

    def risk_amount(self, balance: float) -> float:
        """Dollar risk budget for a single trade."""
        if self.cfg.risk_per_trade_usd is not None:
            return float(self.cfg.risk_per_trade_usd)
        allocated = balance * self.cfg.capital_allocation_pct
        return allocated * float(self.cfg.risk_per_trade_pct)

    def build_plan(
        self, side: str, entry: float, stop: float, balance: float
    ) -> TradePlan | None:
        """Turn an entry/stop into a fully sized, lot-rounded plan (or None)."""
        stop_distance = abs(entry - stop)
        if stop_distance <= 0 or entry <= 0:
            return None

        rr = self.cfg.reward_risk_ratio
        take_profit = entry + rr * stop_distance if side == "long" else entry - rr * stop_distance

        # Allocation = 50% of wallet is the margin we're willing to deploy.
        allocated = balance * self.cfg.capital_allocation_pct
        risk_budget = self.risk_amount(balance)

        # Two independent caps on size, in BTC.
        qty_by_risk = risk_budget / stop_distance              # don't lose > budget
        qty_by_margin = (allocated * self.cfg.max_leverage) / entry  # must be affordable
        qty = min(qty_by_risk, qty_by_margin)

        # Round DOWN to whole lots (exchange only accepts integer lots).
        lots = int(math.floor(qty / self.lot_size))
        if lots < self.min_lots:
            return None  # too small to trade safely within the constraints

        qty = lots * self.lot_size
        notional = qty * entry
        realised_risk = qty * stop_distance                    # <= risk_budget
        margin_usd = notional / self.cfg.max_leverage

        return TradePlan(
            side=side,
            entry=entry,
            stop=stop,
            take_profit=take_profit,
            lots=lots,
            qty=qty,
            risk_usd=realised_risk,
            notional=notional,
            margin_usd=margin_usd,
        )
