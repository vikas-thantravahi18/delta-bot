"""LIVE ETH OPTION SELECTION — does the trader actually pick a valid contract?

Exercises the real code path with the real ETH chain: the same OptionsTrader,
the same _pick_option, the same strike scorer — only the config differs. Places
no orders.

Checks the things that would cost money if wrong:
  * it returns a pick at all (the chain fetch is now asset-parametrised)
  * the contract is ETH, not BTC
  * expiry is the next 12:00 UTC
  * call for long, put for short
  * lots x premium x 0.01 actually equals the stake
  * the score is positive on a full 30-point move

Run:  py scripts/test_eth_pick.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import Config
from src.live.options_trader import OptionsTrader

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{('  — ' + detail) if detail else ''}")
    return cond


def main() -> int:
    cfg = Config.load(str(ROOT / "config.ut_stc_eth_options.yaml"))
    t = OptionsTrader(cfg, live=False)

    print("=" * 84)
    print("LIVE ETH OPTION SELECTION")
    print("=" * 84)
    spot = t._spot()
    hrs = t._hours_to_settlement()
    bal = t._balance()
    stake = bal * t.stake_pct
    print(f"\n  ETH spot ${spot:,.2f} | {hrs:.1f}h to settlement | "
          f"wallet ${bal:,.2f} | stake ${stake:.2f}")
    print(f"  lot {t.lot_size} ETH | target +{t.target_points:.0f} | "
          f"span +/-{t.strike_span_points:.0f}\n")

    check("spot fetched for ETHUSD", spot is not None and spot > 100,
          f"${spot:,.2f}" if spot else "none")

    now = datetime.now(timezone.utc)
    tgt = now.replace(hour=12, minute=0, second=0, microsecond=0)
    if tgt <= now:
        tgt += timedelta(days=1)
    tag = tgt.strftime("%d%m%y")

    for side in ("long", "short"):
        print(f"\n  --- {side.upper()} ({'CALL' if side == 'long' else 'PUT'}) ---")
        p = t._pick_option(side, hrs, stake)
        if not check(f"{side}: returned a pick", p is not None):
            continue
        cost = p["scored_lots"] * p["ask"] * t.lot_size
        off = p["strike"] - p["spot"]
        print(f"      {p['symbol']}")
        print(f"      strike {p['strike']:,.0f} ({off:+,.1f} from spot) | "
              f"{p['scored_lots']} lots @ {p['ask']:.2f} = ${cost:.2f}")
        print(f"      IV {100*p['iv']:.1f}% | spread {p['spread_pct']:.2f}% | "
              f"score ${p['score']:+.2f} | best of {p['considered']}")
        check(f"{side}: it is an ETH contract", "-ETH-" in p["symbol"],
              p["symbol"])
        check(f"{side}: next 12:00 UTC expiry",
              p["symbol"].endswith(tag), f"{p['symbol'][-6:]} vs {tag}")
        check(f"{side}: correct option type",
              p["symbol"].startswith("C-") == (side == "long"))
        check(f"{side}: cost uses the stake", stake * 0.80 <= cost <= stake + 1e-6,
              f"${cost:.2f} of ${stake:.2f}")
        check(f"{side}: positive score on a {t.target_points:.0f}-pt move",
              p["score"] > 0, f"${p['score']:+.2f}")
        check(f"{side}: strike inside the search span",
              abs(off) <= t.strike_span_points, f"{off:+,.1f}")
        check(f"{side}: book is tradeable",
              p["spread_pct"] <= t.max_spread_pct,
              f"{p['spread_pct']:.2f}% <= {t.max_spread_pct}%")

    print(f"\n  --- BTC config still works (no regression) ---")
    tb = OptionsTrader(Config.load(str(ROOT / "config.options_btc.yaml")), live=False)
    pb = tb._pick_option("long", tb._hours_to_settlement(),
                         tb._balance() * tb.stake_pct)
    check("BTC still picks a contract", pb is not None,
          pb["symbol"] if pb else "none")
    if pb:
        check("BTC contract is still BTC", "-BTC-" in pb["symbol"], pb["symbol"])
        check("BTC lot size unchanged", abs(tb.lot_size - 0.001) < 1e-9,
              str(tb.lot_size))

    print("\n" + "=" * 84)
    print(f"{len(OK)} passed, {len(BAD)} failed")
    for b in BAD:
        print(f"  FAILED: {b}")
    print("=" * 84)
    return 1 if BAD else 0


if __name__ == "__main__":
    raise SystemExit(main())
