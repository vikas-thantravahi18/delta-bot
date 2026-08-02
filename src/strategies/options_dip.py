"""Trend Dip Sniper — the signal behind the BTC options strategy.

    regime   EMA50 > EMA200 on 5m  -> longs only  (mirror for shorts)
    trigger  RSI(14) CROSSES down through 40 in an uptrend
             (up through 60 in a downtrend). The CROSS, not the state — the raw
             state fires ~38x/day, the cross fires ~1x/day.
    exit     handled by the options trader: BTC +400 points, or option expiry.
             There is NO stop-loss: when you BUY an option the premium already
             caps the loss, and back-testing showed an underlying stop only
             closed trades that would have recovered (168 of them, 0 the other way).

Measured on 2 years of real Delta 5m candles, traded as same-day ATM options:
    ~41 trades/month | 69-70% win | +$18.43 per $50 staked (mid fills, 17.7% IV)

The trader enforces a 12h cooldown between entries and skips any signal with
under 3 hours to the 12:00 UTC settlement.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from .base import Signal, Strategy


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0.0).ewm(alpha=1.0 / n, adjust=False).mean()
    dn = (-d.clip(upper=0.0)).ewm(alpha=1.0 / n, adjust=False).mean()
    rs = up / dn.replace(0.0, pd.NA)
    out = 100.0 - 100.0 / (1.0 + rs)
    return out.fillna(50.0)


class OptionsDipStrategy(Strategy):
    name = "options_dip"

    def __init__(self, ema_fast: int = 50, ema_slow: int = 200, rsi_len: int = 14,
                 rsi_buy: float = 40.0, rsi_sell: float = 60.0,
                 target_points: float = 400.0, both_sides: bool = True) -> None:
        self.ema_fast = int(ema_fast)
        self.ema_slow = int(ema_slow)
        self.rsi_len = int(rsi_len)
        self.rsi_buy = float(rsi_buy)
        self.rsi_sell = float(rsi_sell)
        self.target_points = float(target_points)
        self.both_sides = bool(both_sides)

    @property
    def warmup(self) -> int:
        return self.ema_slow + 10

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["ema_fast"] = _ema(out["close"], self.ema_fast)
        out["ema_slow"] = _ema(out["close"], self.ema_slow)
        out["rsi"] = _rsi(out["close"], self.rsi_len)
        return out

    def signal(self, df: pd.DataFrame, i: int) -> Optional[Signal]:
        if i < 1:
            return None
        row, prev = df.iloc[i], df.iloc[i - 1]
        for c in ("ema_fast", "ema_slow", "rsi"):
            if pd.isna(row[c]) or pd.isna(prev[c]):
                return None

        up = row["ema_fast"] > row["ema_slow"]
        dn = row["ema_fast"] < row["ema_slow"]
        close = float(row["close"])

        # the CROSS into the pullback zone, not the state
        crossed_down = row["rsi"] < self.rsi_buy <= prev["rsi"]
        crossed_up = row["rsi"] > self.rsi_sell >= prev["rsi"]

        if up and crossed_down:
            return Signal(
                side="long", entry=close,
                stop=close - self.target_points,   # informational only; options have no stop
                reason=(f"uptrend (EMA{self.ema_fast}>EMA{self.ema_slow}), "
                        f"RSI crossed below {self.rsi_buy:g} "
                        f"({prev['rsi']:.1f}->{row['rsi']:.1f}) -> BUY CALL"),
            )
        if self.both_sides and dn and crossed_up:
            return Signal(
                side="short", entry=close,
                stop=close + self.target_points,
                reason=(f"downtrend (EMA{self.ema_fast}<EMA{self.ema_slow}), "
                        f"RSI crossed above {self.rsi_sell:g} "
                        f"({prev['rsi']:.1f}->{row['rsi']:.1f}) -> BUY PUT"),
            )
        return None
