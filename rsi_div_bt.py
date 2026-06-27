#!/usr/bin/env python3
# =============================================================================
# Faithful port + honest test of the Pine "RSI Divergence + EMA Filter" strategy.
#
#   Signal : regular RSI divergence confirmed by pivots (pivothigh/pivotlow,
#            left=right=5). Bullish div (price lower-low, RSI higher-low) -> long;
#            bearish div (price higher-high, RSI lower-high) -> short.
#   Filter : EMA200 trend (long only above, short only below).
#   Exit   : two modes, both tested ---
#            (A) use_rr=true : fixed 1.5xATR stop + RR target (1:2, 1:3).
#            (B) use_trail=true (THE PINE DEFAULT): NO hard stop; a 2xATR trailing
#                stop that only ARMS after +2xATR profit. Unarmed trades have no
#                exit and ride to the end.  <-- the script's real risk profile.
#
# Real Delta BTCUSD candles (cached), net of fee 0.05% + slippage + funding.
# Full period + out-of-sample (recent-half) split.   Run:  py rsi_div_bt.py
# =============================================================================
import glob
from pathlib import Path

import numpy as np
import pandas as pd

FEE = 0.0005          # 0.05% taker, matches the Pine commission
SLIP = 0.0002
FUNDING_8H = 0.0001   # ~0.01%/8h, charged on holding time
RISK_PCT = 0.01       # mode A: 1% risk/trade
ALLOC = 0.50          # mode B: deploy 50% equity notional (no defined stop)
START_BAL = 1000.0
PIVOT_L = PIVOT_R = 5
RSI_LEN = 14
EMA_LEN = 200
ATR_LEN = 14


def load(period="2y", res="1h"):
    # Read the widest cached 1h dataset directly (no project config / yaml needed).
    cands = glob.glob(str(Path(__file__).parent / "data/cache/BTCUSD_1h_*.pkl"))
    best = max(cands, key=lambda f: len(pd.read_pickle(f)))
    raw = pd.read_pickle(best)                       # DatetimeIndex, ohlcv
    if res == "4h":                                  # derive 4h by aggregation
        raw = raw.resample("4h").agg({"open": "first", "high": "max", "low": "min",
                                      "close": "last", "volume": "sum"}).dropna()
    dt = pd.to_datetime(raw.index)
    df = raw.reset_index(drop=True)
    df.columns = [c.lower() for c in df.columns]
    df["dt"] = dt.tz_localize(None).values
    cl, hi, lo = df["close"], df["high"], df["low"]
    df["ema"] = cl.ewm(span=EMA_LEN, adjust=False).mean()
    d = cl.diff()
    g = d.clip(lower=0).ewm(alpha=1/RSI_LEN, adjust=False).mean()
    l_ = (-d).clip(lower=0).ewm(alpha=1/RSI_LEN, adjust=False).mean()
    df["rsi"] = 100 - 100/(1 + g/l_)
    tr = pd.concat([hi-lo, (hi-cl.shift()).abs(), (lo-cl.shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1/ATR_LEN, adjust=False).mean()
    return df


def find_signals(df):
    """Replicate the Pine var-state divergence tracking. Returns long_sig/short_sig
    boolean arrays indexed by the CONFIRMATION bar (pivot_bar + PIVOT_R)."""
    h, l, rsi = df.high.values, df.low.values, df.rsi.values
    n = len(df)
    long_sig = np.zeros(n, bool); short_sig = np.zeros(n, bool)
    # last/prev pivot trackers
    pph_p = pph_r = lph_p = lph_r = np.nan
    ppl_p = ppl_r = lpl_p = lpl_r = np.nan
    for t in range(PIVOT_L + PIVOT_R, n):
        p = t - PIVOT_R                       # candidate pivot bar
        win_l = slice(p - PIVOT_L, p)
        win_r = slice(p + 1, p + PIVOT_R + 1)
        is_ph = h[p] > h[win_l].max() and h[p] > h[win_r].max()
        is_pl = l[p] < l[win_l].min() and l[p] < l[win_r].min()
        if is_ph:
            pph_p, pph_r = lph_p, lph_r
            lph_p, lph_r = h[p], rsi[p]
            if not np.isnan(pph_p) and lph_p > pph_p and lph_r < pph_r:
                short_sig[t] = True
        if is_pl:
            ppl_p, ppl_r = lpl_p, lpl_r
            lpl_p, lpl_r = l[p], rsi[p]
            if not np.isnan(ppl_p) and lpl_p < ppl_p and lpl_r > ppl_r:
                long_sig[t] = True
    return long_sig, short_sig


def backtest(df, long_sig, short_sig, mode, rr=2.0, atr_sl=1.5, trail=2.0,
             use_ema=True, lev=10, start=None, end=None):
    o, h, l, cl = df.open.values, df.high.values, df.low.values, df.close.values
    ema, atr = df.ema.values, df.atr.values
    n = len(df); start = start or (EMA_LEN + PIVOT_L + PIVOT_R + 2); end = end or n
    bal = START_BAL; peak_eq = bal; max_dd = 0.0; pos = None; trades = []
    rode_to_end = 0
    for i in range(start, end):
        # ---- manage open position ----
        if pos:
            ex = reason = None
            if mode == "rr":
                if pos["dir"] == "long":
                    if l[i] <= pos["sl"]: ex, reason = pos["sl"]*(1-SLIP), "stop"
                    elif h[i] >= pos["tp"]: ex, reason = pos["tp"], "tp"
                else:
                    if h[i] >= pos["sl"]: ex, reason = pos["sl"]*(1+SLIP), "stop"
                    elif l[i] <= pos["tp"]: ex, reason = pos["tp"], "tp"
            else:  # trailing-only (the Pine default)
                off = pos["off"]
                if pos["dir"] == "long":
                    pos["xt"] = max(pos["xt"], h[i])
                    if not pos["armed"] and h[i] >= pos["e"] + pos["arm"]: pos["armed"] = True
                    if pos["armed"]:
                        stop = pos["xt"] - off
                        if l[i] <= stop: ex, reason = stop*(1-SLIP), "trail"
                else:
                    pos["xt"] = min(pos["xt"], l[i])
                    if not pos["armed"] and l[i] <= pos["e"] - pos["arm"]: pos["armed"] = True
                    if pos["armed"]:
                        stop = pos["xt"] + off
                        if h[i] >= stop: ex, reason = stop*(1+SLIP), "trail"
            if ex is not None:
                _book(pos, ex, i, trades, reason)
                bal = START_BAL + sum(t["net"] for t in trades)
                peak_eq = max(peak_eq, bal); max_dd = max(max_dd, (peak_eq-bal)/peak_eq*100)
                pos = None
            else:
                continue  # stay in position; one position at a time (pyramiding=0)
        # ---- look for entry (signal on prev bar -> enter at this open) ----
        if pos is None and i-1 >= 0:
            if np.isnan(atr[i-1]) or atr[i-1] <= 0:
                pass
            else:
                long_ok = long_sig[i-1] and (not use_ema or cl[i-1] > ema[i-1])
                short_ok = short_sig[i-1] and (not use_ema or cl[i-1] < ema[i-1])
                a = atr[i-1]
                if long_ok:
                    e = o[i]*(1+SLIP)
                    if mode == "rr":
                        sl = e - atr_sl*a; tp = e + rr*atr_sl*a
                        units = min((bal*RISK_PCT)/(e-sl), bal*lev/e)
                        pos = dict(dir="long", e=e, sl=sl, tp=tp, units=units, i=i)
                    else:
                        units = (bal*ALLOC)/e
                        pos = dict(dir="long", e=e, units=units, i=i,
                                   arm=trail*a, off=trail*a, armed=False, xt=e)
                elif short_ok:
                    e = o[i]*(1-SLIP)
                    if mode == "rr":
                        sl = e + atr_sl*a; tp = e - rr*atr_sl*a
                        units = min((bal*RISK_PCT)/(sl-e), bal*lev/e)
                        pos = dict(dir="short", e=e, sl=sl, tp=tp, units=units, i=i)
                    else:
                        units = (bal*ALLOC)/e
                        pos = dict(dir="short", e=e, units=units, i=i,
                                   arm=trail*a, off=trail*a, armed=False, xt=e)
    # close any still-open position at final close (the "rode to end" case)
    if pos is not None:
        _book(pos, cl[end-1], end-1, trades, "eod")
        rode_to_end += 1
        bal = START_BAL + sum(t["net"] for t in trades)
    if not trades:
        return dict(n=0, win=0, pf=0, ret=0, dd=max_dd, ride=rode_to_end)
    nets = [t["net"] for t in trades]
    w = [x for x in nets if x > 0]; ls = [x for x in nets if x <= 0]
    pf = sum(w)/abs(sum(ls)) if ls and sum(ls) != 0 else 99.0
    return dict(n=len(trades), win=len(w)/len(trades)*100, pf=pf,
                ret=(bal/START_BAL-1)*100, dd=max_dd, ride=rode_to_end,
                avgwin=np.mean(w) if w else 0, avgloss=np.mean(ls) if ls else 0)


def _book(pos, ex, i, trades, reason):
    g = (ex - pos["e"]) if pos["dir"] == "long" else (pos["e"] - ex)
    held = i - pos["i"]
    fee = (pos["e"] + ex) * pos["units"] * FEE
    fund = pos["e"] * pos["units"] * FUNDING_8H * max(held, 0) / 8.0
    trades.append(dict(net=pos["units"]*g - fee - fund, reason=reason))


if __name__ == "__main__":
    for res in ("1h", "4h"):
        df = load("2y", res)
        ls_, ss_ = find_signals(df)
        n = len(df); mid = n//2
        d0 = df.dt.iloc[0].date()
        d1 = df.dt.iloc[-1].date()
        bh = (df.close.iloc[-1]/df.close.iloc[EMA_LEN]-1)*100
        print(f"\n{'='*78}\nRSI Divergence + EMA200 | {res} | {d0}->{d1} | {n} candles | "
              f"net of costs | B&H {bh:+.1f}%")
        print(f"raw signals: {ls_.sum()} long / {ss_.sum()} short")
        print('='*78)
        print("MODE A  (use_rr=true: fixed 1.5xATR stop + RR target, 1% risk/trade)")
        for rr in (2.0, 3.0):
            f = backtest(df, ls_, ss_, "rr", rr=rr)
            h2 = backtest(df, ls_, ss_, "rr", rr=rr, start=mid, end=n)
            print(f"  RR 1:{rr:g}  full n={f['n']:3d} win={f['win']:4.1f}% PF={f['pf']:.2f} "
                  f"ret={f['ret']:+7.1f}% maxDD=-{f['dd']:.1f}% | OOS PF={h2['pf']:.2f} "
                  f"ret={h2['ret']:+.1f}% n={h2['n']}")
        print("MODE B  (THE PINE DEFAULT: 2xATR trailing stop, arms at +2xATR, NO hard stop)")
        f = backtest(df, ls_, ss_, "trail")
        h2 = backtest(df, ls_, ss_, "trail", start=mid, end=n)
        print(f"  trail   full n={f['n']:3d} win={f['win']:4.1f}% PF={f['pf']:.2f} "
              f"ret={f['ret']:+7.1f}% maxDD=-{f['dd']:.1f}% | OOS PF={h2['pf']:.2f} "
              f"ret={h2['ret']:+.1f}% n={h2['n']}")
        if f['n']:
            print(f"          avg win=${f['avgwin']:.1f}  avg loss=${f['avgloss']:.1f}  "
                  f"(unbounded-loser tell: |avgloss| vs avgwin)")
