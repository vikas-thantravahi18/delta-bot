"""Run the BTC OPTIONS strategy (Trend Dip Sniper on the Delta option chain).

  py scripts/run_options.py                 # dry-run: logs intended trades only
  py scripts/run_options.py --once          # a single evaluation tick, then exit
  py scripts/run_options.py --live          # REAL orders (typed confirmation)

Real trading needs DELTA_API_KEY / DELTA_API_SECRET in .env AND
`live.dry_run: false` in config.options_btc.yaml.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import Config                       # noqa: E402
from src.live.options_trader import OptionsTrader   # noqa: E402
from src.utils import setup_logging                 # noqa: E402

DEFAULT_CFG = ROOT / "config.options_btc.yaml"


def main() -> None:
    setup_logging()
    ap = argparse.ArgumentParser(description="Run the BTC options strategy.")
    ap.add_argument("--live", action="store_true", help="Place REAL orders.")
    ap.add_argument("--once", action="store_true", help="Single tick, then exit.")
    ap.add_argument("--yes", action="store_true", help="Skip typed confirmation.")
    ap.add_argument("--config", default=str(DEFAULT_CFG))
    a = ap.parse_args()

    cfg = Config.load(a.config)
    live = a.live
    if live and cfg.live.dry_run:
        print(f"{Path(a.config).name} has live.dry_run=true -> staying in dry-run. "
              "Set it to false to trade for real.")
        live = False

    if live:
        if not (cfg.exchange.api_key and cfg.exchange.api_secret):
            print("ERROR: DELTA_API_KEY / DELTA_API_SECRET missing in .env.")
            sys.exit(1)
        p = cfg.strategy.params or {}
        print("\n*** LIVE BTC OPTIONS TRADING ***")
        print(f"  target  +{p.get('target_points', 400):g} BTC points")
        print(f"  stake   {100*p.get('stake_pct', 0.10):.0f}% of wallet per trade")
        print(f"  expiry  today's, ATM strike (skip if <{p.get('min_hours_to_expiry',3)}h "
              f"to 12:00 UTC)")
        print("  stop    NONE — the option premium is the maximum loss")
        if not a.yes:
            if input("Type 'I UNDERSTAND' to place real option orders: ").strip() != "I UNDERSTAND":
                print("Confirmation not given. Exiting.")
                sys.exit(0)

    trader = OptionsTrader(cfg, live=live)
    if a.once:
        trader.setup()
        trader.tick()
    else:
        trader.run_forever()


if __name__ == "__main__":
    main()
