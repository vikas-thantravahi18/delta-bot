"""EMA-crossover + RSI confirmation + ATR stop trend strategy.

Idea (a classic momentum/trend approach):
  * Trade only with the trend: price on the correct side of a long EMA, and
    (optionally) aligned with a higher-timeframe trend.
  * Enter when the fast EMA crosses the slow EMA in the trend direction.
  * Require RSI confirmation, but avoid chasing overbought/oversold extremes.
  * Place the stop an ATR multiple away; the take-profit is derived from the
    configured reward:risk ratio by the risk manager / backtester.

With a 1:2 reward:risk the break-even win rate is ~33%, so a >35% win rate is
profitable. This is a *framework* — tune params via backtesting before going live.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from ..indicators import atr, ema, rsi
from .base import Signal, Strategy


class EmaRsiAtrStrategy(Strategy):
    name = "ema_rsi_atr"

    def __init__(
        self,
        ema_fast: int = 9,
        ema_slow: int = 21,
        ema_trend: int = 200,
        rsi_period: int = 14,
        rsi_long_min: float = 50.0,
        rsi_short_max: float = 50.0,
        rsi_overbought: float = 75.0,
        rsi_oversold: float = 25.0,
        atr_period: int = 14,
        atr_stop_mult: float = 1.5,
        use_higher_tf_filter: bool = True,
    ) -> None:
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.ema_trend = ema_trend
        self.rsi_period = rsi_period
        self.rsi_long_min = rsi_long_min
        self.rsi_short_max = rsi_short_max
        self.rsi_overbought = rsi_overbought
        self.rsi_oversold = rsi_oversold
        self.atr_period = atr_period
        self.atr_stop_mult = atr_stop_mult
        self.use_higher_tf_filter = use_higher_tf_filter

    @property
    def warmup(self) -> int:
        return max(self.ema_trend, self.atr_period, self.rsi_period) + 5

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["ema_fast"] = ema(df["close"], self.ema_fast)
        df["ema_slow"] = ema(df["close"], self.ema_slow)
        df["ema_trend"] = ema(df["close"], self.ema_trend)
        df["rsi"] = rsi(df["close"], self.rsi_period)
        df["atr"] = atr(df, self.atr_period)
        # htf_trend may be attached upstream (1 up / -1 down). Default neutral.
        if "htf_trend" not in df.columns:
            df["htf_trend"] = 0
        return df

    def signal(self, df: pd.DataFrame, i: int) -> Optional[Signal]:
        if i < self.warmup:
            return None

        row = df.iloc[i]
        prev = df.iloc[i - 1]
        close = float(row["close"])
        atr_val = float(row["atr"])
        if atr_val <= 0 or pd.isna(atr_val):
            return None

        crossed_up = prev["ema_fast"] <= prev["ema_slow"] and row["ema_fast"] > row["ema_slow"]
        crossed_down = prev["ema_fast"] >= prev["ema_slow"] and row["ema_fast"] < row["ema_slow"]

        htf = int(row["htf_trend"]) if not pd.isna(row["htf_trend"]) else 0
        htf_ok_long = (htf >= 0) if self.use_higher_tf_filter else True
        htf_ok_short = (htf <= 0) if self.use_higher_tf_filter else True

        # ---- Long setup ----
        if (
            crossed_up
            and close > row["ema_trend"]
            and self.rsi_long_min < row["rsi"] < self.rsi_overbought
            and htf_ok_long
        ):
            stop = close - self.atr_stop_mult * atr_val
            if stop < close:
                return Signal(
                    side="long",
                    entry=close,
                    stop=stop,
                    reason=f"EMA{self.ema_fast}>{self.ema_slow} cross up, "
                    f"close>EMA{self.ema_trend}, RSI={row['rsi']:.1f}",
                )

        # ---- Short setup ----
        if (
            crossed_down
            and close < row["ema_trend"]
            and self.rsi_oversold < row["rsi"] < self.rsi_short_max
            and htf_ok_short
        ):
            stop = close + self.atr_stop_mult * atr_val
            if stop > close:
                return Signal(
                    side="short",
                    entry=close,
                    stop=stop,
                    reason=f"EMA{self.ema_fast}<{self.ema_slow} cross down, "
                    f"close<EMA{self.ema_trend}, RSI={row['rsi']:.1f}",
                )

        return None
