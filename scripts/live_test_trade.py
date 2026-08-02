"""LIVE TEST — one real 1-lot round trip through the production code path.

Places a REAL order with REAL money. Deliberately minimal size: exactly one
contract (0.001 BTC of premium, roughly $0.20-0.25 at current prices).

It does NOT hand-roll the orders. It drives the same `_enter` / `_close` the bot
uses in production, so what gets tested is the real path:

    strike scorer -> market buy -> fill confirmation -> state file ->
    position on the exchange -> market sell (reduce_only) -> flat

Sizing is forced to 1 lot by temporarily overriding stake_pct, and there is a hard
gate that aborts if the computed size is anything other than 1.

Run:  py scripts/live_test_trade.py --yes
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import Config
from src.live.options_trader import OptionsTrader, STATE_PATH
from src.strategies.base import Signal

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{('  — ' + detail) if detail else ''}")
    return cond


def positions_for(client, symbol):
    try:
        for p in (client.get_option_positions() or []):
            if str(p.get("product_symbol")) == symbol:
                return int(float(p.get("size") or 0))
    except Exception as exc:
        print(f"      (position read failed: {exc})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="skip the typed confirmation")
    ap.add_argument("--side", default="long", choices=["long", "short"])
    a = ap.parse_args()

    cfg = Config.load(str(ROOT / "config.options_btc.yaml"))
    trader = OptionsTrader(cfg, live=True)
    client = trader.client

    print("=" * 84)
    print(f"LIVE 1-LOT ROUND TRIP — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
    print("=" * 84)

    check("trader is in LIVE mode", trader.dry_run is False,
          "dry_run=False" if not trader.dry_run else "STILL DRY-RUN — nothing will trade")
    if trader.dry_run:
        return 1

    bal = trader._balance()
    print(f"\n  wallet ${bal:.2f}")
    if not check("wallet has funds", bal > 1.0, f"${bal:.2f}"):
        return 1

    # existing positions must be clear, or we cannot attribute the result
    try:
        existing = client.get_option_positions() or []
    except Exception as exc:
        print(f"  could not read positions: {exc}")
        existing = []
    if not check("no option position open before the test", len(existing) == 0,
                 f"{len(existing)} open" if existing else "flat"):
        for p in existing:
            print(f"      {p.get('product_symbol')} size={p.get('size')}")
        return 1

    hrs = trader._hours_to_settlement()
    if not check("enough time to expiry", hrs >= trader.min_hours_to_expiry,
                 f"{hrs:.1f}h"):
        return 1

    # ---- the pick, at full size, purely to read the premium --------------- #
    full = trader._pick_option(a.side, hrs, bal * trader.stake_pct)
    if not check("scorer returned a contract", full is not None):
        return 1
    print(f"\n  scorer picks  {full['symbol']}")
    print(f"                strike {full['strike']:,.0f} "
          f"({full['strike']-full['spot']:+,.0f} from spot)  ask {full['ask']:.1f}  "
          f"spread {full['spread_pct']:.2f}%")
    print(f"                at your real size that would be "
          f"{full['scored_lots']} lots = ${full['scored_lots']*full['ask']*0.001:.2f}")

    # ---- force EXACTLY one lot -------------------------------------------- #
    one_lot_cost = full["ask"] * 0.001
    trader.stake_pct = (one_lot_cost * 1.4) / bal        # 1.4x -> int() gives 1
    pick = trader._pick_option(a.side, hrs, bal * trader.stake_pct)
    if not check("re-priced at test size", pick is not None):
        return 1
    lots = int((bal * trader.stake_pct) / (pick["ask"] * 0.001))
    print(f"\n  TEST SIZE: {lots} lot of {pick['symbol']} "
          f"@ {pick['ask']:.1f} = ${lots*pick['ask']*0.001:.3f} at risk")
    if not check("HARD GATE: exactly 1 lot", lots == 1, f"computed {lots}"):
        return 1

    if not a.yes:
        if input("\n  Type 'GO' to place a REAL order: ").strip() != "GO":
            print("  aborted.")
            return 1

    backup = STATE_PATH.read_text() if STATE_PATH.exists() else None
    entry_sym = pick["symbol"]

    try:
        # ---- ENTRY -------------------------------------------------------- #
        print("\n## ENTRY")
        sig = Signal(side=a.side, entry=pick["spot"],
                     stop=pick["spot"] - trader.target_points,
                     reason="LIVE TEST — 1 lot")
        t0 = time.time()
        trader._enter(sig, pick, bal)
        print(f"      _enter returned in {time.time()-t0:.1f}s")

        if not check("bot recorded an open trade", trader.open_trade is not None):
            return 1
        ot = trader.open_trade
        print(f"      recorded: {ot.symbol} x{ot.lots} @ {ot.entry_premium:.1f}, "
              f"target BTC {ot.btc_target:,.1f}")
        check("recorded symbol matches the pick", ot.symbol == entry_sym)
        check("recorded 1 lot", ot.lots == 1, f"{ot.lots}")
        check("state file written", STATE_PATH.exists())

        time.sleep(3)
        on_ex = positions_for(client, entry_sym)
        check("EXCHANGE shows the position", on_ex == 1, f"size={on_ex}")

        # ---- EXIT ---------------------------------------------------------- #
        print("\n## EXIT")
        t0 = time.time()
        trader._close("LIVE TEST — closing immediately")
        print(f"      _close returned in {time.time()-t0:.1f}s")
        check("bot cleared the open trade", trader.open_trade is None)

        time.sleep(4)
        after = positions_for(client, entry_sym)
        check("EXCHANGE shows flat", after == 0, f"size={after}")

    finally:
        if backup is not None:
            STATE_PATH.write_text(backup)
        elif STATE_PATH.exists():
            STATE_PATH.unlink()
        print("\n  (state file restored)")

    # ---- FILLS ------------------------------------------------------------- #
    print("\n## WHAT THE EXCHANGE ACTUALLY FILLED")
    time.sleep(3)
    try:
        fills = [f for f in (client.get_fills(page_size=50) or [])
                 if str(f.get("product_symbol")) == entry_sym]
    except Exception as exc:
        print(f"  could not read fills: {exc}")
        fills = []
    if fills:
        print(f"\n      {'time':<22}{'side':>6}{'size':>7}{'price':>10}{'fee':>9}")
        print("      " + "-" * 54)
        buy = sell = fee = 0.0
        for f in sorted(fills, key=lambda z: str(z.get("created_at")))[-8:]:
            sz = float(f.get("size") or 0)
            px = float(f.get("price") or 0)
            fe = float(f.get("commission") or 0)
            fee += fe
            if str(f.get("side")) == "buy":
                buy += sz * px * 0.001
            else:
                sell += sz * px * 0.001
            print(f"      {str(f.get('created_at'))[:19]:<22}{str(f.get('side')):>6}"
                  f"{sz:>7.0f}{px:>10.1f}{fe:>9.4f}")
        print(f"\n      bought ${buy:.4f}   sold ${sell:.4f}   fees ${fee:.4f}")
        print(f"      NET on the round trip: ${sell - buy - fee:+.4f}")
        check("both legs filled", buy > 0 and sell > 0)
        check("round-trip cost is small", abs(sell - buy - fee) < 0.20,
              f"${sell-buy-fee:+.4f}")
    else:
        check("fills visible via the API", False, "none returned for this symbol")

    print("\n" + "=" * 84)
    print(f"{len(OK)} passed, {len(BAD)} failed")
    for b in BAD:
        print(f"  FAILED: {b}")
    print("=" * 84)
    return 1 if BAD else 0


if __name__ == "__main__":
    raise SystemExit(main())
