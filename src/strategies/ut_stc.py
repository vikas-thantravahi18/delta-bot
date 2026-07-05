"""ut_stc — UT Bot (ATR trailing-stop flip) + Schaff Trend Cycle.

Validated on ETHUSD 4h, RR 1:3. Two-sided: longs and shorts both profitable
independently in the stage gauntlet (beat 200/200 random-entry seeds). ETH-only —
dead on BTC. Ported byte-for-byte from the stage `UtStc` strategy.

Long  : UT-Bot flips up within `flip_window` bars AND STC <= zone AND STC rising.
Short : mirror (UT flips down, STC >= 100-zone, STC falling). Stop = swing lo/hi.
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from ._ta import arrays, cross_dn, cross_up, rma, roll_max, roll_min, stc, true_range
from .base import Signal, Strategy


class UtStcStrategy(Strategy):
    name = "ut_stc"

    def __init__(self, ut_key: float = 2.0, atr_period: int = 1, stc_len: int = 80,
                 macd_fast: int = 27, macd_slow: int = 50, zone: float = 25.0,
                 flip_window: int = 2, swing: int = 10) -> None:
        self.ut_key = ut_key
        self.atr_period = atr_period
        self.stc_len = stc_len
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.zone = zone
        self.flip_window = flip_window
        self.swing = swing

    @property
    def warmup(self) -> int:
        return self.stc_len + 60  # STC needs `stc_len`, +buffer for stable state

    def prepare(self, df):
        df = df.copy()
        o, h, l, c, _v = arrays(df)
        n = len(c)

        # UT Bot: ATR trailing stop, flip when price crosses it.
        nloss = self.ut_key * rma(true_range(h, l, c), self.atr_period)
        xstop = np.empty(n)
        xstop[0] = c[0]
        for i in range(1, n):
            p = xstop[i - 1]
            if c[i] > p and c[i - 1] > p:
                xstop[i] = max(p, c[i] - nloss[i])
            elif c[i] < p and c[i - 1] < p:
                xstop[i] = min(p, c[i] + nloss[i])
            elif c[i] > p:
                xstop[i] = c[i] - nloss[i]
            else:
                xstop[i] = c[i] + nloss[i]
        ut_buy = cross_up(c, xstop)
        ut_sell = cross_dn(c, xstop)

        st = stc(c, self.macd_fast, self.macd_slow, self.stc_len)
        st1 = np.concatenate(([np.nan], st[:-1]))
        slo = roll_min(l, self.swing)
        shi = roll_max(h, self.swing)

        ubw = ut_buy.copy()
        usw = ut_sell.copy()
        for k in range(1, self.flip_window + 1):
            ubw[k:] |= ut_buy[:-k]
            usw[k:] |= ut_sell[:-k]

        long = ubw & (st <= self.zone) & (st > st1)
        short = usw & (st >= 100 - self.zone) & (st < st1)
        df["sig_side"] = np.where(long, 1, np.where(short, -1, 0)).astype(np.int8)
        df["sig_stop"] = np.where(long, slo, np.where(short, shi, np.nan))
        return df

    def signal(self, df, i: int) -> Optional[Signal]:
        if i < self.warmup:
            return None
        row = df.iloc[i]
        side = int(row["sig_side"])
        if side == 0:
            return None
        stop = float(row["sig_stop"])
        close = float(row["close"])
        if not np.isfinite(stop):
            return None
        if side == 1 and stop < close:
            return Signal("long", close, stop, "UT flip up + STC oversold turn")
        if side == -1 and stop > close:
            return Signal("short", close, stop, "UT flip down + STC overbought turn")
        return None
