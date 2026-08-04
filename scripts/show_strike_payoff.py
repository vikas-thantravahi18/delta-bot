"""WHAT THE STRIKE SCORER IS ACTUALLY MAXIMISING — and what it is not.

The scorer prices every strike at spot +/- target_points after the average hold,
and buys whichever returns the most dollars for the stake:

    s1    = spot + target_points
    fair  = BlackScholes(s1, strike, time_left_after_hold, iv)
    score = lots * (fair - half_spread) * contract - cost      <- maximised

So the answer to "does it pick the one that gains most if the trade works" is yes
— but strictly at the TARGET. It is indifferent to what happens at +50% of the
target, or at double it. This prints the whole payoff curve for every live strike
so that trade-off is visible rather than assumed.

Read-only. Run:  py scripts/show_strike_payoff.py [--config ...] [--side long]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import Config
from src.live.options_trader import OptionsTrader


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.ut_stc_eth_options.yaml")
    ap.add_argument("--side", default="long", choices=["long", "short"])
    a = ap.parse_args()

    cfg = Config.load(str(ROOT / a.config))
    t = OptionsTrader(cfg, live=False)
    call = a.side == "long"
    spot = t._spot()
    hrs = t._hours_to_settlement()
    stake = t._balance() * t.stake_pct
    tgt = t.target_points

    print("=" * 96)
    print(f"STRIKE PAYOFF CURVE — {t.option_asset} {a.side.upper()}, "
          f"target +{tgt:.0f}")
    print(f"spot {spot:,.2f} | {hrs:.1f}h to settlement | stake ${stake:.2f} | "
          f"lot {t.lot_size} {t.option_asset}")
    print("=" * 96)

    chain = t.client.get_option_tickers(t.option_asset)
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    exp = now.replace(hour=12, minute=0, second=0, microsecond=0)
    if exp <= now:
        exp += timedelta(days=1)
    tag = exp.strftime("%d%m%y")

    hold = min(t.score_hold_hours, max(hrs - 0.5, 0.1))
    t1 = max((hrs - hold) / 24.0 / 365.0, 1e-9)
    moves = [0.0, tgt * 0.5, tgt * 0.75, tgt, tgt * 1.5, tgt * 2.0]

    rows = []
    for tk in chain:
        sym = str(tk.get("symbol", ""))
        if not sym.endswith(tag) or sym.startswith("C-") != call:
            continue
        try:
            k = float(tk["strike_price"]); mark = float(tk["mark_price"])
            iv = float(tk.get("mark_vol") or 0)
        except Exception:
            continue
        if mark <= 0 or iv <= 0 or abs(k - spot) > t.strike_span_points:
            continue
        sc = t._score_strike(tk, k, mark, spot, hrs, call, stake)
        if sc is None:
            continue
        pnl = []
        for mv in moves:
            s1 = spot + (mv if call else -mv)
            fair = t._bs(s1, k, t1, sc["iv"], call)
            ex = max(fair - (sc["ask"] - sc["bid"]) / 2.0, 0.0)
            pnl.append(sc["lots"] * ex * t.lot_size - sc["cost"])
        rows.append((k, sc, pnl))

    if not rows:
        print("\n  no priceable strikes.")
        return 1
    rows.sort(key=lambda z: z[0])
    best_at_target = max(rows, key=lambda z: z[1]["pnl"])

    print(f"\n  {'strike':>8}{'offset':>8}{'ask':>7}{'lots':>7}{'spr%':>7}"
          + "".join(f"{('+' + format(m, '.0f')):>9}" for m in moves) + "   pick")
    print("  " + "-" * (44 + 9 * len(moves)))
    for k, sc, pnl in rows:
        mark = " <-- SCORER" if k == best_at_target[0] else ""
        print(f"  {k:>8,.0f}{k-spot:>+8.0f}{sc['ask']:>7.2f}{sc['lots']:>7}"
              f"{sc['spread_pct']:>6.1f}%"
              + "".join(f"{v:>+9.2f}" for v in pnl) + mark)

    print("\n" + "=" * 96)
    print("### WHAT THIS SHOWS")
    print("=" * 96)
    for i, mv in enumerate(moves):
        b = max(rows, key=lambda z: z[2][i])
        same = "  (same as scorer)" if b[0] == best_at_target[0] else ""
        print(f"  best strike if it moves {mv:>5.0f}: {b[0]:>8,.0f} "
              f"({b[0]-spot:+.0f})  ->  {b[2][i]:+.2f}{same}")

    i_t = moves.index(tgt)
    sc = best_at_target[1]
    print(f"""
  The scorer maximises the '+{tgt:.0f}' column and nothing else. That is the right
  column IF the move lands near the target — which is what the strategy is built
  to produce ({100 * 0.774:.0f}% of ut_stc entries reach it inside 24h).

  What it gives up: at half the target it may pay nothing, and at double it a
  further-out strike would have paid more. It is a bet on the SIZE of the move,
  not just the direction.""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
