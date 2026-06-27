"""Quick demo: show how the bot sizes a trade in whole lots for various wallets.

  py scripts/demo_sizing.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Config
from src.risk import RiskManager

cfg = Config.load()
rm = RiskManager(cfg.risk, lot_size=cfg.market.lot_size, min_lots=cfg.market.min_lots)

entry = 60280.0
stop = entry - 900.0  # ~$900 stop distance (a representative ATR*3 on BTC 1h)

print(f"Rules: deploy {cfg.risk.capital_allocation_pct:.0%} of wallet, "
      f"risk {cfg.risk.risk_per_trade_pct:.0%} of that, "
      f"{cfg.risk.max_leverage:g}x cap, 1 lot = {cfg.market.lot_size} BTC")
print(f"Signal: LONG entry={entry:.0f} stop={stop:.0f} (stop distance ${entry-stop:.0f})\n")

for bal in (200.0, 100.0, 50.0, 20.0, 3.2):
    plan = rm.build_plan("long", entry, stop, bal)
    if plan is None:
        print(f"  wallet ${bal:>7.2f} -> NO TRADE (can't afford 1 lot within risk budget)")
    else:
        print(f"  wallet ${bal:>7.2f} -> {plan.lots:>3d} lots "
              f"({plan.qty:.4f} BTC) | notional ${plan.notional:>8.2f} | "
              f"margin ${plan.margin_usd:>6.2f} | risk ${plan.risk_usd:>5.2f} "
              f"| TP {plan.take_profit:.0f}")
