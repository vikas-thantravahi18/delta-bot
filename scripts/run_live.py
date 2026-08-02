"""Run the bot live (or in paper/dry-run mode).

  py scripts/run_live.py              # dry-run: logs intended trades, no orders
  py scripts/run_live.py --live       # REAL orders (asks for typed confirmation)
  py scripts/run_live.py --once       # evaluate a single tick and exit

Real trading requires DELTA_API_KEY / DELTA_API_SECRET in your .env and
`live.dry_run: false` in config.yaml.  Start on testnet and tiny size.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Config            # noqa: E402
from src.live import LiveTrader          # noqa: E402
from src.utils import setup_logging      # noqa: E402


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="Run the Delta trading bot.")
    parser.add_argument("--live", action="store_true",
                        help="Place REAL orders (otherwise dry-run).")
    parser.add_argument("--once", action="store_true",
                        help="Run a single evaluation tick and exit.")
    parser.add_argument("--yes", action="store_true",
                        help="Skip the typed live-trading confirmation "
                             "(used by run_portfolio.py, which confirms once).")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    cfg = Config.load(args.config)

    live = args.live
    if live and cfg.live.dry_run:
        print("config.yaml has live.dry_run=true -> staying in dry-run. "
              "Set it to false to trade for real.")
        live = False

    if live:
        if not (cfg.exchange.api_key and cfg.exchange.api_secret):
            print("ERROR: DELTA_API_KEY / DELTA_API_SECRET missing in .env.")
            sys.exit(1)
        print("\n*** LIVE TRADING ***")
        # Show the sizing that is ACTUALLY used. In margin mode risk_per_trade_usd is
        # ignored (dollar risk = notional x stop%), so printing "$3/trade" would lie.
        if cfg.risk.sizing_mode == "margin":
            mult = cfg.risk.capital_allocation_pct * cfg.risk.max_leverage
            sizing = (f"MARGIN sizing ~{mult:g}x wallet notional/trade "
                      f"(dollar risk = notional x stop%, NOT a fixed $)")
        elif cfg.risk.risk_per_trade_pct is not None:
            sizing = f"risk {cfg.risk.risk_per_trade_pct:.1%} of wallet/trade"
        else:
            sizing = f"risk ${cfg.risk.risk_per_trade_usd:g}/trade"
        print(f"Symbol {cfg.market.symbol} @ {cfg.market.resolution} | "
              f"{sizing} | "
              f"R:R 1:{cfg.risk.reward_risk_ratio:g} | "
              f"max {cfg.risk.max_trades_per_day} trades/day")
        if not args.yes:
            confirm = input("Type 'I UNDERSTAND' to place real orders: ").strip()
            if confirm != "I UNDERSTAND":
                print("Confirmation not given. Exiting.")
                sys.exit(0)

    trader = LiveTrader(cfg, live=live)
    if args.once:
        trader.setup()
        trader.tick()
    else:
        trader.run_forever()


if __name__ == "__main__":
    main()
