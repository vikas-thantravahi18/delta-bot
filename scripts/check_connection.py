"""Smoke-test the exchange connection & API keys (no orders placed).

  py scripts/check_connection.py

Reads config.yaml + .env exactly like the live bot, then verifies:
  * which base URL you're pointed at (loudly flags PRODUCTION),
  * your API keys authenticate (fetches wallet balance),
  * the product resolves (id + contract size),
  * the current leverage, and that setting it to risk.max_leverage works.

This is the safe first step before running the live bot on the demo account.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Config          # noqa: E402
from src.exchange import DeltaClient   # noqa: E402
from src.utils import setup_logging    # noqa: E402


def main() -> None:
    setup_logging()
    cfg = Config.load()

    base = cfg.exchange.base_url
    is_demo = "testnet" in base or "demo" in base
    print(f"\nBase URL : {base}  ({'DEMO/TESTNET' if is_demo else '*** PRODUCTION ***'})")
    if not is_demo:
        print("  WARNING: this is a PRODUCTION endpoint — real money. Ctrl-C to abort.")

    if not (cfg.exchange.api_key and cfg.exchange.api_secret):
        print("  ERROR: DELTA_API_KEY / DELTA_API_SECRET missing in .env.")
        sys.exit(1)

    client = DeltaClient(
        base_url=base, api_key=cfg.exchange.api_key, api_secret=cfg.exchange.api_secret
    )

    # 1) Product resolves (public).
    product = client.get_product(cfg.market.symbol)
    pid = product.get("id")
    cv = product.get("contract_value")
    print(f"Product  : {cfg.market.symbol} id={pid} contract_value={cv}")

    # 2) Keys authenticate (signed).
    try:
        balances = client.get_balances()
        usd = next(
            (b for b in balances
             if str(b.get("asset_symbol", "")).upper() in ("USD", "USDT", "USDC")),
            balances[0] if balances else {},
        )
        print(f"Auth OK  : available balance ~ ${float(usd.get('available_balance', 0)):.2f} "
              f"({usd.get('asset_symbol', '?')})")
    except Exception as exc:
        print(f"  ERROR: signed request failed - check your keys/permissions ({exc}).")
        sys.exit(1)

    # 3) Leverage read + set (the liquidation fix).
    try:
        cur = client.get_leverage(pid)
        cur_lev = cur.get("leverage") if isinstance(cur, dict) else cur
        print(f"Leverage : currently {cur_lev}x")
        client.set_leverage(pid, cfg.risk.max_leverage)
        print(f"           set to {cfg.risk.max_leverage:g}x OK (matches risk.max_leverage).")
    except Exception as exc:
        print(f"  WARNING: leverage read/set failed ({exc}). "
              f"Set it to {cfg.risk.max_leverage:g}x manually in the UI before trading.")

    print("\nConnection check complete. No orders were placed.\n")


if __name__ == "__main__":
    main()
