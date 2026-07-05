"""v2_dualtrend — two-sided trend system built from two validated edges.

LONG  = Dynamic Swing Anchored VWAP (Zeiierman), cross mode: in a Donchian
        uptrend, enter when close crosses back above the re-anchored adaptive VWAP.
SHORT = ChartPrime Swing ZigZag: Donchian(200) breakdown — short when price
        prints a fresh 200-bar low (trend flips down).
Both use a 2xATR14 stop; take-profit comes from the config reward:risk (RR 1:3).
Long takes precedence if both fire on the same bar (rare). Ported from the stage
`DynSwingVwap(prd=30, apt=20, mode='cross')` long side + `SwingZigzag(length=200)`
short side (the built 'v2' portfolio, validated on BTC 1h and holds on ETH).
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from ._ta import arrays, atr_np, roll_max, roll_min
from .base import Signal, Strategy


def _vwap_pullback_long(o, h, l, c, v, prd, apt, stop_mult):
    """DynSwingVwap cross-mode, long signals only. Returns (side, stop_ref)."""
    n = len(c)
    hlc3 = (h + l + c) / 3.0
    rmax = roll_max(h, prd)
    rmin = roll_min(l, prd)
    a = atr_np(h, l, c, 14)
    alpha = 1.0 - np.exp(-np.log(2.0) / max(1.0, apt))
    vwap = np.full(n, np.nan)
    side = np.zeros(n, np.int8)
    ref = np.full(n, np.nan)
    p_e = v_e = np.nan
    dir_ = 0
    phL = plL = 0
    ph = pl = np.nan
    for i in range(n):
        if np.isfinite(rmax[i]) and h[i] >= rmax[i]:
            phL = i; ph = h[i]
        if np.isfinite(rmin[i]) and l[i] <= rmin[i]:
            plL = i; pl = l[i]
        d = 1 if phL > plL else -1
        if np.isnan(p_e):
            p_e = hlc3[i] * v[i]; v_e = v[i]
        elif d != dir_ and dir_ != 0:
            # flip: re-anchor at the opposite pivot, re-run the EWMA forward
            anch_bar = plL if d > 0 else phL
            anch_px = pl if d > 0 else ph
            bb = min(i - anch_bar, 300)
            j0 = i - bb
            pe = anch_px * v[j0]; ve = v[j0]
            for j in range(j0 + 1, i + 1):
                pe = (1 - alpha) * pe + alpha * (hlc3[j] * v[j])
                ve = (1 - alpha) * ve + alpha * v[j]
            p_e, v_e = pe, ve
        else:
            p_e = (1 - alpha) * p_e + alpha * (hlc3[i] * v[i])
            v_e = (1 - alpha) * v_e + alpha * v[i]
        vwap[i] = p_e / v_e if v_e > 0 else np.nan
        dir_ = d
        if i > 0 and np.isfinite(vwap[i]) and np.isfinite(a[i]):
            if d > 0 and c[i] > vwap[i] and c[i - 1] <= vwap[i - 1]:
                side[i] = 1
                ref[i] = c[i] - stop_mult * a[i]
    return side, ref


def _donchian_breakdown_short(h, l, c, length, stop_mult):
    """SwingZigzag Donchian trend-flip, short signals only. Returns (side, stop_ref)."""
    n = len(c)
    upper = roll_max(h, length)
    lower = roll_min(l, length)
    a = atr_np(h, l, c, 14)
    trend = np.zeros(n, np.int8)
    cur = 0
    for i in range(n):
        if np.isfinite(upper[i]) and h[i] >= upper[i]:
            cur = 1
        if np.isfinite(lower[i]) and l[i] <= lower[i]:
            cur = -1
        trend[i] = cur
    side = np.zeros(n, np.int8)
    ref = np.full(n, np.nan)
    for i in range(1, n):
        if trend[i] == -1 and trend[i - 1] != -1 and np.isfinite(a[i]):
            side[i] = -1
            ref[i] = c[i] + stop_mult * a[i]
    return side, ref


class V2DualTrendStrategy(Strategy):
    name = "v2_dualtrend"

    def __init__(self, vwap_prd: int = 30, vwap_apt: float = 20.0,
                 zigzag_len: int = 200, stop_mult: float = 2.0) -> None:
        self.vwap_prd = vwap_prd
        self.vwap_apt = vwap_apt
        self.zigzag_len = zigzag_len
        self.stop_mult = stop_mult

    @property
    def warmup(self) -> int:
        # Donchian(200) needs its full window to exist plus room for the trend
        # state and VWAP re-anchor to settle before the acting bar.
        return self.zigzag_len + 40

    def prepare(self, df):
        df = df.copy()
        o, h, l, c, v = arrays(df)
        n = len(c)
        ls, lr = _vwap_pullback_long(o, h, l, c, v, self.vwap_prd, self.vwap_apt, self.stop_mult)
        ss, sr = _donchian_breakdown_short(h, l, c, self.zigzag_len, self.stop_mult)
        side = np.zeros(n, np.int8)
        ref = np.full(n, np.nan)
        m = ss == -1
        side[m] = -1; ref[m] = sr[m]
        m = ls == 1               # long precedence on same-bar clash
        side[m] = 1; ref[m] = lr[m]
        df["sig_side"] = side
        df["sig_stop"] = ref
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
            return Signal("long", close, stop, "uptrend pullback: close reclaimed anchored VWAP")
        if side == -1 and stop > close:
            return Signal("short", close, stop, "Donchian-200 breakdown (fresh 200-bar low)")
        return None
