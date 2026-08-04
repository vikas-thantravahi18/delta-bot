"""LIVE ETH TEST — one real 1-lot round trip through the production code path.

Places a REAL order with REAL money. One contract = 0.01 ETH of premium, which
at current quotes is a few cents.

Drives the same `_enter` / `_close` the bot uses, so what gets tested is the ETH
path end to end: ut_stc signal object -> strike scorer -> market buy -> fill
confirmation -> state file -> exchange position -> market sell -> flat.

The ETH config ships with dry_run: true. This script overrides that flag in
memory ONLY for the duration of the test; the file is never modified.

Sizing is forced to one lot and there is a hard gate that aborts on anything else.

Run:  py scripts/live_test_eth.py --yes
"""
from __future__ import annotations

import argparse
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


def size_on(client, symbol):
    try:
        for p in (client.get_option_positions() or []):
            if str(p.get("product_symbol")) == symbol:
                return int(float(p.get("size") or 0))
    except Exception as exc:
        print(f"      (position read failed: {exc})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--side", default="long", choices=["long", "short"])
    a = ap.parse_args()

    cfg = Config.load(str(ROOT / "config.ut_stc_eth_options.yaml"))
    tr = OptionsTrader(cfg, live=True)
    # The config is deliberately dry_run: true. Override in memory only.
    tr.dry_run = False
    client = tr.client

    print("=" * 86)
    print(f"LIVE ETH 1-LOT ROUND TRIP — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
    print("=" * 86)

    check("trader is ETH", tr.option_asset == "ETH" and tr.underlying == "ETHUSD",
          f"{tr.underlying} / {tr.option_asset}")
    check("lot size 0.01 ETH", abs(tr.lot_size - 0.01) < 1e-9, str(tr.lot_size))
    check("LIVE mode forced for the test", tr.dry_run is False)

    bal = tr._balance()
    spot = tr._spot()
    hrs = tr._hours_to_settlement()
    print(f"\n  wallet ${bal:,.2f} | ETH ${spot:,.2f} | {hrs:.1f}h to settlement")
    if not check("wallet has funds", bal > 1.0, f"${bal:.2f}"):
        return 1
    if not check("enough time to expiry", hrs >= tr.min_hours_to_expiry,
                 f"{hrs:.1f}h"):
        return 1

    full = tr._pick_option(a.side, hrs, bal * tr.stake_pct)
    if not check("scorer returned a contract", full is not None):
        return 1
    print(f"\n  at full size it would buy {full['scored_lots']} lots of "
          f"{full['symbol']} @ {full['ask']:.2f}")

    # ---- force exactly one lot ------------------------------------------ #
    tr.stake_pct = (full["ask"] * tr.lot_size * 1.4) / bal
    pick = tr._pick_option(a.side, hrs, bal * tr.stake_pct)
    if not check("re-priced at test size", pick is not None):
        return 1
    lots = int((bal * tr.stake_pct) / (pick["ask"] * tr.lot_size))
    cost = lots * pick["ask"] * tr.lot_size
    print(f"\n  TEST: {lots} lot of {pick['symbol']} @ {pick['ask']:.2f} "
          f"= ${cost:.4f} at risk")
    if not check("HARD GATE: exactly 1 lot", lots == 1, f"computed {lots}"):
        return 1

    if not a.yes and input("\n  Type 'GO' for a REAL order: ").strip() != "GO":
        print("  aborted.")
        return 1

    backup = STATE_PATH.read_text() if STATE_PATH.exists() else None
    sym = pick["symbol"]
    try:
        print("\n## ENTRY")
        sig = Signal(side=a.side, entry=pick["spot"],
                     stop=pick["spot"] - tr.target_points,
                     reason="LIVE ETH TEST — 1 lot")
        t0 = time.time()
        tr._enter(sig, pick, bal)
        print(f"      _enter returned in {time.time()-t0:.1f}s")
        if not check("bot recorded an open trade", tr.open_trade is not None):
            return 1
        ot = tr.open_trade
        print(f"      recorded {ot.symbol} x{ot.lots} @ {ot.entry_premium:.2f}, "
              f"target ETH {ot.btc_target:,.1f}")
        check("recorded 1 lot", ot.lots == 1, str(ot.lots))
        check("symbol matches the pick", ot.symbol == sym)
        check("state file written", STATE_PATH.exists())

        time.sleep(3)
        check("EXCHANGE shows the position", size_on(client, sym) == 1,
              f"size={size_on(client, sym)}")

        print("\n## EXIT")
        t0 = time.time()
        tr._close("LIVE ETH TEST — closing immediately")
        print(f"      _close returned in {time.time()-t0:.1f}s")
        check("bot cleared the trade", tr.open_trade is None)
        time.sleep(4)
        check("EXCHANGE shows flat", size_on(client, sym) == 0,
              f"size={size_on(client, sym)}")
    finally:
        if backup is not None:
            STATE_PATH.write_text(backup)
        elif STATE_PATH.exists():
            STATE_PATH.unlink()
        print("\n  (state file restored)")

    print("\n## FILLS")
    time.sleep(3)
    try:
        fills = [f for f in (client.get_fills(page_size=40) or [])
                 if str(f.get("product_symbol")) == sym]
    except Exception as exc:
        fills = []
        print(f"  could not read fills: {exc}")
    if fills:
        print(f"\n      {'time':<22}{'side':>6}{'size':>7}{'price':>10}{'fee':>10}")
        print("      " + "-" * 55)
        buy = sell = fee = 0.0
        for f in sorted(fills, key=lambda z: str(z.get("created_at")))[-6:]:
            sz, px = float(f.get("size") or 0), float(f.get("price") or 0)
            fe = float(f.get("commission") or 0)
            fee += fe
            if str(f.get("side")) == "buy":
                buy += sz * px * tr.lot_size
            else:
                sell += sz * px * tr.lot_size
            print(f"      {str(f.get('created_at'))[:19]:<22}{str(f.get('side')):>6}"
                  f"{sz:>7.0f}{px:>10.2f}{fe:>10.4f}")
        print(f"\n      bought ${buy:.4f}   sold ${sell:.4f}   fees ${fee:.4f}")
        print(f"      NET ${sell - buy - fee:+.4f}")
        check("both legs filled", buy > 0 and sell > 0)
        if buy > 0:
            print(f"      fee was {100*fee/2/buy:.1f}% of premium per side "
                  f"(BTC measured 2.4%; model assumed 4.1%)")
    else:
        check("fills visible", False, "none returned")

    print("\n" + "=" * 86)
    print(f"{len(OK)} passed, {len(BAD)} failed")
    for b in BAD:
        print(f"  FAILED: {b}")
    print("=" * 86)
    return 1 if BAD else 0


if __name__ == "__main__":
    raise SystemExit(main())
