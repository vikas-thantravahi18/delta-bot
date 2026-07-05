"""Numpy TA helpers for the ported portfolio strategies.

These mirror the exact math validated in the stage backtester so the live/bot
signals match the research. Vectorised where possible; a couple of genuinely
stateful constructs (UT-Bot trailing stop, adaptive VWAP re-anchor) are loops.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def true_range(h, l, c):
    pc = np.concatenate(([c[0]], c[:-1]))
    return np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))


def rma(x, n):
    return pd.Series(x).ewm(alpha=1.0 / n, adjust=False).mean().to_numpy()


def ema_np(x, n):
    return pd.Series(x).ewm(span=n, adjust=False).mean().to_numpy()


def atr_np(h, l, c, n=14):
    return rma(true_range(h, l, c), n)


def roll_max(x, n):
    return pd.Series(x).rolling(n).max().to_numpy()


def roll_min(x, n):
    return pd.Series(x).rolling(n).min().to_numpy()


def cross_up(a, b):
    out = np.zeros(len(a), bool)
    out[1:] = (a[1:] > b[1:]) & (a[:-1] <= b[:-1])
    return out


def cross_dn(a, b):
    out = np.zeros(len(a), bool)
    out[1:] = (a[1:] < b[1:]) & (a[:-1] >= b[:-1])
    return out


def stc(c, fast, slow, length):
    """Schaff Trend Cycle (double-smoothed stochastic of MACD)."""
    macd = ema_np(c, fast) - ema_np(c, slow)
    n = len(macd)
    mser = pd.Series(macd)
    v1 = mser.rolling(length).min().to_numpy()
    v2 = (mser.rolling(length).max() - mser.rolling(length).min()).to_numpy()
    pf = np.full(n, np.nan)
    for i in range(n):
        f1 = ((macd[i] - v1[i]) / v2[i] * 100
              if v2[i] and np.isfinite(v2[i]) and v2[i] > 0 else (pf[i - 1] if i else np.nan))
        pf[i] = f1 if (i == 0 or np.isnan(pf[i - 1])) else pf[i - 1] + 0.5 * (f1 - pf[i - 1])
    pser = pd.Series(pf)
    v3 = pser.rolling(length).min().to_numpy()
    v4 = (pser.rolling(length).max() - pser.rolling(length).min()).to_numpy()
    out = np.full(n, np.nan)
    for i in range(n):
        f2 = ((pf[i] - v3[i]) / v4[i] * 100
              if v4[i] and np.isfinite(v4[i]) and v4[i] > 0 else (out[i - 1] if i else np.nan))
        out[i] = f2 if (i == 0 or np.isnan(out[i - 1])) else out[i - 1] + 0.5 * (f2 - out[i - 1])
    return out


def arrays(df):
    return (df["open"].to_numpy(float), df["high"].to_numpy(float),
            df["low"].to_numpy(float), df["close"].to_numpy(float),
            df["volume"].to_numpy(float))
