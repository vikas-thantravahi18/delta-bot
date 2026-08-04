"""Why did the last completed option trade pay what it paid?

Takes the real entry and exit fills, confirms what BTC actually did in between, and
decomposes the result against what the strike scorer projected. The scorer prices a
target-sized move happening after `score_hold_hours`; if the move takes longer, theta
is charged on the difference and the trade pays less. This quantifies that gap.

Run:  py scripts/explain_pnl.py
"""
from __future__ import annotations

import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

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


def main() -> int:
    cfg = Config.load(str(ROOT / "config.options_btc.yaml"))
    trader = OptionsTrader(cfg, live=False)
    client = trader.client

    fills = [f for f in (client.get_fills(page_size=100) or [])
             if str(f.get("product_symbol", "")).startswith(("C-BTC", "P-BTC"))]
    fills.sort(key=lambda z: str(z.get("created_at")))
    if not fills:
        print("no option fills")
        return 1

    sym = str(fills[-1]["product_symbol"])
    legs = [f for f in fills if str(f["product_symbol"]) == sym]
    buys = [f for f in legs if str(f["side"]) == "buy"]
    sells = [f for f in legs if str(f["side"]) == "sell"]
    if not buys or not sells:
        print(f"{sym} is not a completed round trip yet.")
        return 1
    # the last complete episode: take the largest buy/sell pair
    buy = max(buys, key=lambda z: float(z["size"]))
    sell = max(sells, key=lambda z: float(z["size"]))

    lots = int(float(buy["size"]))
    p_in, p_out = float(buy["price"]), float(sell["price"])
    fee = float(buy.get("commission") or 0) + float(sell.get("commission") or 0)
    t_in = datetime.fromisoformat(str(buy["created_at"]).replace("Z", "+00:00"))
    t_out = datetime.fromisoformat(str(sell["created_at"]).replace("Z", "+00:00"))
    held = (t_out - t_in).total_seconds() / 3600.0
    is_put = sym.startswith("P-")
    strike = float(sym.split("-")[2])

    cost = lots * p_in * LOT
    gross = lots * (p_out - p_in) * LOT
    net = gross - fee

    print("=" * 80)
    print(f"P&L BREAKDOWN — {sym}")
    print("=" * 80)
    print(f"\n  bought  {t_in:%Y-%m-%d %H:%M} UTC   {lots} lots @ {p_in:.1f}"
          f"   = ${cost:.2f}")
    print(f"  sold    {t_out:%Y-%m-%d %H:%M} UTC   {lots} lots @ {p_out:.1f}"
          f"   = ${lots*p_out*LOT:.2f}")
    print(f"  held    {held:.1f} hours")
    print(f"\n  gross            ${gross:+.2f}")
    print(f"  fees             ${-fee:+.2f}   ({100*fee/cost:.1f}% of the position)")
    print(f"  NET              ${net:+.2f}   ({100*net/cost:+.1f}% on ${cost:.2f})")

    # ---- did BTC reach the target? ---------------------------------------- #
    end = int(time.time())
    df = pd.DataFrame(client.get_candles("BTCUSD", "5m", int(t_in.timestamp()) - 3600, end))
    df["t"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.sort_values("t").set_index("t")
    win = df[(df.index >= t_in) & (df.index <= t_out)]
    entry_spot = float(df[df.index <= t_in]["close"].iloc[-1])
    target = entry_spot - trader.target_points if is_put else entry_spot + trader.target_points

    print(f"\n## DID BTC GET THERE?")
    print(f"\n  BTC at entry      {entry_spot:,.1f}")
    print(f"  target            {target:,.1f}  ({'down' if is_put else 'up'} "
          f"{trader.target_points:.0f})")
    if not win.empty:
        lo, hi = float(win["low"].min()), float(win["high"].max())
        best = lo if is_put else hi
        hit = (lo <= target) if is_put else (hi >= target)
        print(f"  best it reached   {best:,.1f}   ->  target "
              f"{'HIT' if hit else 'MISSED'}")
        print(f"  range while held  {lo:,.1f} - {hi:,.1f}")

    # ---- why not more? ---------------------------------------------------- #
    settle = t_in.replace(hour=12, minute=0, second=0, microsecond=0)
    if settle <= t_in:
        settle = settle.replace(day=settle.day + 1)
    life_h = (settle - t_in).total_seconds() / 3600.0
    iv = 0.288
    scored_h = trader.score_hold_hours

    print(f"\n## WHAT THE SCORER EXPECTED vs WHAT HAPPENED")
    print(f"\n  The scorer prices the move landing after {scored_h:.0f}h. It took "
          f"{held:.1f}h.")
    print(f"  Every extra hour is theta charged on the premium.\n")
    print(f"  {'move lands after':>18}{'time left':>12}{'option worth':>14}"
          f"{'your P&L':>11}")
    print("  " + "-" * 56)
    for h in sorted({2.0, 4.0, scored_h, 8.0, round(held, 1), 16.0}):
        if h >= life_h:
            continue
        t1 = max((life_h - h) / 24 / 365, 1e-9)
        px = bs(target, strike, t1, iv, not is_put)
        pnl = lots * (px - p_in) * LOT - fee
        star = "  <- what happened" if abs(h - held) < 0.15 else (
            "  <- what was scored" if abs(h - scored_h) < 0.15 else "")
        print(f"  {h:>17.1f}h{life_h-h:>11.1f}h{px:>14.1f}{pnl:>+11.2f}{star}")

    print(f"\n  The option had {life_h:.1f}h of life at entry. You held it for "
          f"{held:.1f}h,")
    print(f"  so {100*held/life_h:.0f}% of its remaining time value was spent getting there.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
