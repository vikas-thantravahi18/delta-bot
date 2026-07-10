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
  6. Skip the trade if the stop is wide enough that the exchange's own
     liquidation could plausibly trigger before it (see `liquidation_buffer_ok`).
     The caller must actually set `max_leverage` on the exchange for this
     assumption to hold (see DeltaClient.set_leverage).

Take-profit is placed at `reward_risk_ratio` x the stop distance (1:2 / 1:3).
The stop then trails `trail_distance_r` x the stop distance behind the best
price reached (native exchange-side trailing via `bracket_trail_amount`),
so profit is locked in progressively instead of requiring the full target.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .config import RiskConfig

# Conservative, fixed safety knobs for the liquidation guard below. Delta's
# real maintenance-margin tables are tiered and not exposed here, so we
# assume a small constant maintenance margin and demand a healthy multiple
# of headroom between our stop and the estimated liquidation price. This is
# what stops an exchange-side liquidation firing before our own stop-loss.
EST_MAINTENANCE_MARGIN_PCT = 0.01
LIQUIDATION_SAFETY_MULT = 1.5


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
    trail_amount: Optional[float] = None  # price-distance to trail the stop by (None = off)


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

    def liquidation_buffer_ok(self, entry: float, stop: float) -> bool:
        """Guard against the exchange's isolated-margin liquidation firing
        before our own stop-loss does.

        Approximates the liquidation distance as
        ``1/leverage - estimated_maintenance_margin`` and requires the stop
        to sit safely (with a margin-of-safety multiple) inside that buffer.
        Caller is expected to actually set this same leverage on the
        exchange (see DeltaClient.set_leverage) so the assumption holds.
        """
        if entry <= 0:
            return False
        stop_distance_pct = abs(entry - stop) / entry
        liq_buffer_pct = (1.0 / self.cfg.max_leverage) - EST_MAINTENANCE_MARGIN_PCT
        return stop_distance_pct * LIQUIDATION_SAFETY_MULT <= liq_buffer_pct

    def build_plan(
        self, side: str, entry: float, stop: float, balance: float
    ) -> TradePlan | None:
        """Turn an entry/stop into a fully sized, lot-rounded plan (or None)."""
        stop_distance = abs(entry - stop)
        if stop_distance <= 0 or entry <= 0:
            return None

        rr = self.cfg.reward_risk_ratio
        take_profit = entry + rr * stop_distance if side == "long" else entry - rr * stop_distance

        # Allocation = the share of wallet we're willing to deploy as margin.
        allocated = balance * self.cfg.capital_allocation_pct
        risk_budget = self.risk_amount(balance)

        # Two ways to size, both in base units (BTC/ETH):
        qty_by_risk = risk_budget / stop_distance                    # lose <= risk budget
        qty_by_margin = (allocated * self.cfg.max_leverage) / entry  # deploy `allocated` as margin

        if self.cfg.sizing_mode == "margin":
            # MARGIN mode: deploy `capital_allocation_pct` of the wallet as margin
            # every trade -> big, fixed-notional positions. The realised dollar
            # risk (below) then scales with the stop distance and can be large;
            # the liquidation-buffer guard in the caller still protects against
            # an exchange liquidation firing before the stop.
            qty = qty_by_margin
        else:
            # RISK mode (default): size so hitting the stop loses <= the risk
            # budget, but never exceed what the margin allocation can afford.
            qty = min(qty_by_risk, qty_by_margin)

        # Round DOWN to whole lots (exchange only accepts integer lots).
        lots = int(math.floor(qty / self.lot_size))
        if lots < self.min_lots:
            return None  # too small to trade safely within the constraints

        qty = lots * self.lot_size
        notional = qty * entry
        realised_risk = qty * stop_distance                    # <= risk_budget
        margin_usd = notional / self.cfg.max_leverage

        trail_amount = None
        if self.cfg.trail_distance_r:
            trail_amount = stop_distance * float(self.cfg.trail_distance_r)

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
            trail_amount=trail_amount,
        )
