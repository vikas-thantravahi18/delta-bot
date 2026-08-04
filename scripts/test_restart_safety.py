"""RESTART SAFETY — what each bot does to positions it did not open.

Two failure modes this checks for, both live right now because there are
hand-placed BTC option positions on the account:

  1. CROSS-ASSET ADOPTION. get_option_positions() returns every option position
     on the account. Without a filter the ETH bot would adopt BTC positions and
     the BTC bot would adopt ETH ones, and the two would fight over the same
     trade.
  2. ADOPTING MANUAL TRADES. Adoption means the bot manages the position to ITS
     target and closes it before ITS expiry window. For a hand-placed trade that
     is the bot taking your position off you.

Calls _reconcile() directly with dry_run bypassed for the read, but places no
orders — reconcile only reads and updates local state.

Run:  py scripts/test_restart_safety.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import Config
from src.live.options_trader import OptionsTrader, STATE_PATH

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{('  — ' + detail) if detail else ''}")
    return cond


def main() -> int:
    backup = STATE_PATH.read_text() if STATE_PATH.exists() else None
    try:
        print("=" * 84)
        print("RESTART SAFETY")
        print("=" * 84)

        cfg = Config.load(str(ROOT / "config.options_btc.yaml"))
        probe = OptionsTrader(cfg, live=False)
        positions = [p for p in (probe.client.get_option_positions() or [])
                     if abs(float(p.get("size") or 0)) > 0]
        print(f"\n  option positions on the account right now: {len(positions)}")
        for p in positions:
            print(f"      {p.get('product_symbol')}  size {p.get('size')}  "
                  f"entry {p.get('entry_price')}")
        if not positions:
            print("\n  Nothing open — restart is trivially safe. Re-run when a "
                  "position exists to exercise this properly.")
            return 0

        for name, conf in (("BTC", "config.options_btc.yaml"),
                           ("ETH", "config.ut_stc_eth_options.yaml")):
            print(f"\n## {name} leg — simulating a restart")
            t = OptionsTrader(Config.load(str(ROOT / conf)), live=True)
            if STATE_PATH.exists():
                STATE_PATH.unlink()          # worst case: no memory of anything
            t.open_trade = None
            print(f"      adopt_untracked = {t.adopt_untracked}")
            t._reconcile()
            adopted = t.open_trade.symbol if t.open_trade else None
            check(f"{name}: did not adopt anything", adopted is None,
                  f"adopted {adopted}" if adopted else "left all positions alone")
            mine = [p for p in positions if t._mine(p.get("product_symbol"))]
            others = [p for p in positions if not t._mine(p.get("product_symbol"))]
            check(f"{name}: correctly identifies its own asset",
                  all(f"-{t.option_asset}-" in str(p.get('product_symbol'))
                      for p in mine),
                  f"{len(mine)} on {t.option_asset}, {len(others)} on other assets")

        print("\n## WHAT THIS MEANS FOR A RESTART")
        print(f"""
      Your {len(positions)} open position(s) are untouched. Each bot now:
        * ignores option positions on the other asset entirely
        * ignores untracked positions on its OWN asset (adopt_untracked: false)
        * manages only what it opened itself, tracked in options_state.json

      So restarting does NOT close, modify or take over your manual trades.

      The trade-off: if the bot is killed mid-trade, its own position also
      becomes untracked and it will NOT resume managing it — you would close
      that one by hand. Set adopt_untracked: true to get the old behaviour back,
      but only when no hand-placed positions are open.""")
    finally:
        if backup is not None:
            STATE_PATH.write_text(backup)
        elif STATE_PATH.exists():
            STATE_PATH.unlink()
        print("\n  (state file restored)")

    print("\n" + "=" * 84)
    print(f"{len(OK)} passed, {len(BAD)} failed")
    for b in BAD:
        print(f"  FAILED: {b}")
    print("=" * 84)
    return 1 if BAD else 0


if __name__ == "__main__":
    raise SystemExit(main())
