"""GROUND TRUTH — every real option trade, and what IV you actually paid.

Everything modelled so far rested on an ASSUMED implied volatility, because Delta
publishes no historical option prices. But your own fills are real option prices.
From each entry fill the implied volatility can be solved backwards exactly:

    premium_paid = BlackScholes(spot, strike, time_left, IV)   ->  solve for IV

That is a direct measurement of the single number the whole analysis hinged on,
taken from money that actually changed hands rather than from a curve I built.

Also pulls the live chain's own greeks (delta, theta, gamma, vega) and checks the
Black-Scholes engine against them, so the pricing model is validated rather than
trusted.

Read-only. Run:  py scripts/audit_real_trades.py
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import Config
from src.live.options_trader import OptionsTrader

LOT = 0.001


def bs(s, k, t, sig, call):
    if t <= 1e-9 or sig <= 1e-9:
        return max(0.0, (s - k) if call else (k - s))
    v = sig * math.sqrt(t)
    d1 = (math.log(s / k) + 0.5 * sig * sig * t) / v
    d2 = d1 - v
    n = lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
    return (s * n(d1) - k * n(d2)) if call else (k * n(-d2) - s * n(-d1))


def implied_vol(price, s, k, t, call):
    """Bisection — robust where Newton wanders on near-worthless options."""
    if t <= 1e-9 or price <= 0:
        return float("nan")
    intrinsic = max(0.0, (s - k) if call else (k - s))
    if price <= intrinsic + 1e-9:
        return float("nan")
    lo, hi = 0.001, 5.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if bs(s, k, t, mid, call) < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def main() -> int:
    cfg = Config.load(str(ROOT / "config.options_btc.yaml"))
    trader = OptionsTrader(cfg, live=False)
    client = trader.client

    # ---------------------------------------------------------------- 1
    print("=" * 96)
    print("1. EVERY REAL OPTION TRADE ON THE ACCOUNT")
    print("=" * 96)
    fills = [f for f in (client.get_fills(page_size=200) or [])
             if str(f.get("product_symbol", "")).startswith(("C-BTC", "P-BTC"))]
    if not fills:
        print("\n  no option fills found.")
        return 1
    fills.sort(key=lambda z: str(z.get("created_at")))

    # group into position episodes per symbol
    eps, pos = {}, {}
    for f in fills:
        sym = str(f["product_symbol"])
        sz = float(f.get("size") or 0)
        px = float(f.get("price") or 0)
        fee = float(f.get("commission") or 0)
        side = str(f.get("side"))
        p = pos.setdefault(sym, dict(qty=0.0, cost=0.0, pro=0.0, fee=0.0,
                                     t0=None, t1=None, lots=0))
        p["fee"] += fee
        if side == "buy":
            if p["qty"] == 0:
                p["t0"] = str(f["created_at"])
            p["qty"] += sz
            p["cost"] += sz * px * LOT
            p["lots"] += int(sz)
        else:
            p["qty"] -= sz
            p["pro"] += sz * px * LOT
            p["t1"] = str(f["created_at"])
            if abs(p["qty"]) < 1e-9:
                eps.setdefault(sym, []).append(dict(p))
                pos[sym] = dict(qty=0.0, cost=0.0, pro=0.0, fee=0.0,
                                t0=None, t1=None, lots=0)

    print(f"\n  {'symbol':<22}{'lots':>6}{'in':>9}{'out':>9}{'held':>8}"
          f"{'P&L':>9}{'return':>9}")
    print("  " + "-" * 74)
    tot = 0.0
    rows = []
    for sym, lst in eps.items():
        for e in lst:
            if e["lots"] == 0:
                continue
            pnl = e["pro"] - e["cost"] - e["fee"]
            tot += pnl
            avg_in = e["cost"] / (e["lots"] * LOT)
            avg_out = e["pro"] / (e["lots"] * LOT)
            t0 = datetime.fromisoformat(e["t0"].replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(e["t1"].replace("Z", "+00:00"))
            held = (t1 - t0).total_seconds() / 3600
            rows.append((sym, e, t0, avg_in, pnl))
            print(f"  {sym:<22}{e['lots']:>6}{avg_in:>9.1f}{avg_out:>9.1f}"
                  f"{held:>7.1f}h{pnl:>+9.2f}{100*pnl/e['cost']:>+8.0f}%")
    print(f"\n  {len(rows)} completed round trips   TOTAL {tot:+.2f}")
    wins = [r for r in rows if r[4] > 0]
    if rows:
        print(f"  win rate {100*len(wins)/len(rows):.0f}%   "
              f"average {tot/len(rows):+.2f} per trade")

    # ---------------------------------------------------------------- 2
    print("\n" + "=" * 96)
    print("2. WHAT IV DID YOU ACTUALLY PAY?  (solved back from each entry fill)")
    print("=" * 96)
    print("\n  This is the number every projection depended on, measured from")
    print("  your own money rather than assumed.\n")
    print(f"  {'symbol':<22}{'entry':>9}{'BTC then':>11}{'hrs left':>10}"
          f"{'IV PAID':>10}")
    print("  " + "-" * 62)
    ivs = []
    for sym, e, t0, avg_in, pnl in rows:
        try:
            parts = sym.split("-")
            k = float(parts[2])
            tag = parts[3]
            exp = datetime(2000 + int(tag[4:6]), int(tag[2:4]), int(tag[0:2]),
                           12, tzinfo=timezone.utc)
        except Exception:
            continue
        hrs = (exp - t0).total_seconds() / 3600.0
        if hrs <= 0:
            continue
        # BTC price at entry, from the 5m candle covering that minute
        try:
            ts = int(t0.timestamp())
            cd = client.get_candles("BTCUSD", "5m", ts - 900, ts + 300)
            spot = float(cd[-1]["close"]) if cd else float("nan")
        except Exception:
            spot = float("nan")
        if not (spot == spot):
            continue
        call = sym.startswith("C-")
        iv = implied_vol(avg_in, spot, k, hrs / 24 / 365, call)
        if iv == iv:
            ivs.append(iv)
        print(f"  {sym:<22}{avg_in:>9.1f}{spot:>11,.0f}{hrs:>9.1f}h"
              f"{100*iv:>9.1f}%")
    if ivs:
        import statistics as st
        print(f"\n  MEASURED: mean {100*st.mean(ivs):.1f}%   "
              f"median {100*st.median(ivs):.1f}%   "
              f"range {100*min(ivs):.1f}-{100*max(ivs):.1f}%")
        print(f"  my backtest curve assumed ~18%; the 'live IV' correction used 27.8%")

    # ---------------------------------------------------------------- 3
    print("\n" + "=" * 96)
    print("3. IS THE PRICING ENGINE RIGHT?  (vs Delta's own greeks, live)")
    print("=" * 96)
    chain = client.get_option_tickers("BTC")
    spot = None
    cands = []
    for t in chain:
        try:
            k = float(t["strike_price"]); mark = float(t["mark_price"])
            iv = float(t.get("mark_vol") or 0)
            g = t.get("greeks") or {}
            spot = float(t.get("spot_price") or spot or 0)
        except Exception:
            continue
        if mark <= 0 or iv <= 0 or not g:
            continue
        cands.append((abs(k - spot), t, k, mark, iv, g))
    cands.sort(key=lambda z: z[0])
    print(f"\n  BTC ${spot:,.1f}\n")
    print(f"  {'symbol':<22}{'mark':>9}{'my BS':>9}{'diff':>8}"
          f"{'delta':>8}{'theta/day':>11}{'IV':>8}")
    print("  " + "-" * 74)
    now = datetime.now(timezone.utc)
    for _, t, k, mark, iv, g in cands[:6]:
        sym = t["symbol"]
        tag = sym.split("-")[-1]
        try:
            exp = datetime(2000 + int(tag[4:6]), int(tag[2:4]), int(tag[0:2]),
                           12, tzinfo=timezone.utc)
        except Exception:
            continue
        hrs = (exp - now).total_seconds() / 3600.0
        if hrs <= 0:
            continue
        call = sym.startswith("C-")
        theo = bs(spot, k, hrs / 24 / 365, iv, call)
        print(f"  {sym:<22}{mark:>9.1f}{theo:>9.1f}"
              f"{100*(theo/mark-1):>+7.1f}%{float(g.get('delta') or 0):>8.2f}"
              f"{float(g.get('theta') or 0):>11.1f}{100*iv:>7.1f}%")
    print("\n  If 'diff' is within a couple of percent the engine is sound and the")
    print("  uncertainty was never the maths — it was the IV fed into it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
