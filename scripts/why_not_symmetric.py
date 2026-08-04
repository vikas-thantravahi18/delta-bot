"""Is a -$8 option loss symmetric with a +$8 gain? Measured on the LIVE position.

The intuition is that a position down $8 can just as easily be up $8. For a
directional option BUYER it cannot, for two reasons that compound:

  THETA IS ONE-WAY   part of the $8 is time value that has already been spent.
                     It does not come back if price returns — only price can be
                     recovered, not the clock.
  DELTA SHRINKS      as the option moves out of the money it becomes LESS
                     sensitive to BTC. So the move needed to recover is bigger
                     than the move that caused the loss. Losing makes the next
                     recovery harder, which is the opposite of symmetric.

This prices the live position at a range of BTC levels and shows exactly how far
price has to come back.

Run:  py scripts/why_not_symmetric.py
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


def delta_of(s, k, t, sig, call):
    if t <= 1e-9 or sig <= 1e-9:
        return (1.0 if s > k else 0.0) if call else (-1.0 if s < k else 0.0)
    v = sig * math.sqrt(t)
    d1 = (math.log(s / k) + 0.5 * sig * sig * t) / v
    n = lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
    return n(d1) if call else n(d1) - 1.0


def main() -> int:
    cfg = Config.load(str(ROOT / "config.options_btc.yaml"))
    trader = OptionsTrader(cfg, live=False)
    client = trader.client

    pos = [p for p in (client.get_option_positions() or [])
           if abs(float(p.get("size") or 0)) > 0]
    if not pos:
        print("No option position open right now.")
        return 1
    p = pos[0]
    sym = str(p["product_symbol"])
    lots = int(abs(float(p["size"])))
    entry = float(p.get("entry_price") or 0)
    is_put = sym.startswith("P-")
    strike = float(sym.split("-")[2])

    tick = client.get_ticker(sym)
    mark = float(tick.get("mark_price") or 0)
    iv = float(tick.get("mark_vol") or 0.28)
    spot = float(tick.get("spot_price") or 0) or trader._spot()
    prod = client.get_product(sym)
    settle = datetime.fromisoformat(str(prod["settlement_time"]).replace("Z", "+00:00"))
    hrs_left = (settle - datetime.now(timezone.utc)).total_seconds() / 3600.0
    T = max(hrs_left / 24 / 365, 1e-9)

    cost = lots * entry * LOT
    now_val = lots * mark * LOT
    pnl = now_val - cost

    print("=" * 84)
    print(f"LIVE POSITION — {sym}")
    print("=" * 84)
    print(f"\n  {lots} lots, entry {entry:.1f}, now {mark:.1f}")
    print(f"  paid ${cost:.2f}  ->  worth ${now_val:.2f}   P&L ${pnl:+.2f} "
          f"({100*pnl/cost:+.1f}%)")
    print(f"  BTC {spot:,.1f}   strike {strike:,.0f}   {hrs_left:.1f}h to settlement"
          f"   IV {100*iv:.1f}%")

    # ---- 1. how much of the loss is TIME, not direction? ----------------- #
    # what would it be worth if BTC were back at the entry spot, right now?
    entry_spot = strike + (spot - strike)     # placeholder, refined below
    # recover the entry spot from the entry premium
    lo, hi = spot - 5000, spot + 5000
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        v = bs(mid, strike, T, iv, not is_put)
        if (v < entry) == (not is_put):
            lo = mid
        else:
            hi = mid
    print("\n" + "=" * 84)
    print("### 1. HOW MUCH OF THE LOSS IS THE CLOCK, NOT THE PRICE?")
    print("=" * 84)
    # value at the ORIGINAL spot but at TODAY's remaining time
    # (approximate original spot: strike offset at entry is unknown, so use the
    #  breakeven solve above which is the price that restores the entry premium)
    be_now = 0.5 * (lo + hi)
    print(f"\n  BTC must reach {be_now:,.1f} for the option to be worth {entry:.1f} again.")
    print(f"  That is {abs(be_now - spot):,.0f} points from here "
          f"({'down' if be_now < spot else 'up'}).")

    # ---- 2. the asymmetry, priced -------------------------------------- #
    print("\n" + "=" * 84)
    print("### 2. WHAT EACH BTC MOVE IS WORTH FROM HERE")
    print("=" * 84)
    dl = delta_of(spot, strike, T, iv, not is_put)
    print(f"\n  delta now: {dl:+.3f}   (it was near {'-0.5' if is_put else '0.5'} "
          f"at entry — that shrinkage IS the asymmetry)\n")
    print(f"  {'BTC move':>10}{'BTC price':>12}{'premium':>10}{'P&L':>10}{'vs now':>10}")
    print("  " + "-" * 52)
    for mv in (-800, -600, -400, -200, 0, 200, 400, 600, 800):
        s_ = spot + mv
        v = bs(s_, strike, T, iv, not is_put)
        pl = lots * v * LOT - cost
        star = ""
        if mv == 0:
            star = "  <- now"
        print(f"  {mv:>+10.0f}{s_:>12,.0f}{v:>10.1f}{pl:>+10.2f}"
              f"{pl - pnl:>+10.2f}{star}")

    # ---- 3. the direct answer ------------------------------------------- #
    print("\n" + "=" * 84)
    print("### 3. IS -$8 SYMMETRIC WITH +$8?")
    print("=" * 84)
    fav = -1 if is_put else 1
    lost = -pnl

    # Scan outward in the favourable direction rather than bisecting — the sign
    # handling on a bisection flips with put/call and silently returns the bound.
    def pl_at(mv):
        return lots * bs(spot + mv, strike, T, iv, not is_put) * LOT - cost

    be_mv = tgt_mv = None
    for step in range(0, 4001, 5):
        mv = fav * step
        v = pl_at(mv)
        if be_mv is None and v >= 0:
            be_mv = mv
        if tgt_mv is None and v >= lost:
            tgt_mv = mv
            break

    print(f"\n  you are down ${lost:.2f} on a ${cost:.2f} position\n")
    if be_mv is not None:
        print(f"  just to BREAK EVEN   BTC must move {abs(be_mv):,.0f} points "
              f"{'down' if fav < 0 else 'up'}  (-> {spot+be_mv:,.0f})")
    if tgt_mv is not None:
        print(f"  to be UP ${lost:.2f}      BTC must move {abs(tgt_mv):,.0f} points "
              f"{'down' if fav < 0 else 'up'}  (-> {spot+tgt_mv:,.0f})")
    else:
        print(f"  to be UP ${lost:.2f}      not reachable within 4,000 points")

    print(f"\n  Compare the two directions from here, same 400-point move:")
    print(f"    400 points YOUR way      {pl_at(fav*400) - pnl:>+8.2f}")
    print(f"    400 points AGAINST you   {pl_at(-fav*400) - pnl:>+8.2f}")
    print(f"\n  delta is {dl:+.3f} now versus about "
          f"{'-0.50' if is_put else '+0.50'} at entry — the option responds to BTC")
    print(f"  {100*(1-abs(dl)/0.5):.0f}% LESS than when you bought it. That is why the move")
    print(f"  that lost the money does not win it back.")
    print(f"\n  Floor: the most you can still lose is ${cost + pnl:.2f} — the option")
    print(f"  goes to zero, never negative. From here the payoff is skewed your way;")
    print(f"  it is the ENTRY that was symmetric, and that symmetry is now spent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
