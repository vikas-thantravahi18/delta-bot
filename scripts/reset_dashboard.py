"""Restart the dashboard's counters from NOW.

The dashboard reports only the active strategy: option fills at/after
`strategy_start`, measured against `starting_capital`. Both live in
data/dashboard_baseline.json. This script rewrites them to "now" and your current
balance, so the win rate, P&L and return % begin from zero.

Run it once when you start the strategy live, and again any time you want a clean
slate (after a deposit, a config change, or a break in trading).

  py scripts/reset_dashboard.py            # show what it would do, then confirm
  py scripts/reset_dashboard.py --yes      # no prompt
  py scripts/reset_dashboard.py --capital 200   # override the capital baseline
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import Config          # noqa: E402
from src.exchange import DeltaClient   # noqa: E402

BASELINE = ROOT / "data" / "dashboard_baseline.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="skip the confirmation")
    ap.add_argument("--capital", type=float, default=None,
                    help="capital baseline (default: current wallet balance)")
    a = ap.parse_args()

    cfg = Config.load()
    bal = None
    if cfg.exchange.api_key and cfg.exchange.api_secret:
        try:
            c = DeltaClient(base_url=cfg.exchange.base_url,
                            api_key=cfg.exchange.api_key,
                            api_secret=cfg.exchange.api_secret)
            for b in c.get_balances():
                if str(b.get("asset_symbol", "")).upper() in ("USD", "USDT", "USDC"):
                    bal = float(b.get("balance", 0) or 0)
                    break
        except Exception as exc:
            print(f"Could not read balance ({exc}).")

    cap = a.capital if a.capital is not None else bal
    if cap is None:
        print("No balance available and no --capital given. Aborting.")
        sys.exit(1)

    if BASELINE.exists():
        try:
            old = json.loads(BASELINE.read_text())
            print(f"current baseline: ${float(old['starting_capital']):,.2f} "
                  f"from {str(old['strategy_start'])[:19]}")
        except Exception:
            print("current baseline: unreadable")
    else:
        print("current baseline: none")

    now = dt.datetime.now(dt.timezone.utc)
    print(f"new baseline:     ${cap:,.2f} from {now:%Y-%m-%d %H:%M:%S} UTC")
    print("\nEvery trade before that timestamp — perp legs and any manual option "
          "trades — stops counting.")
    if not a.yes:
        if input("Reset? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return

    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE.write_text(json.dumps({
        "starting_capital": round(float(cap), 4),
        "strategy_start": now.isoformat(),
        "note": ("Dashboard counts only option fills at/after strategy_start. "
                 "Re-run scripts/reset_dashboard.py to restart the counter."),
    }, indent=1))
    print(f"\nWritten to {BASELINE}")
    print("Refresh the dashboard (press R) to see it reset.")


if __name__ == "__main__":
    main()
