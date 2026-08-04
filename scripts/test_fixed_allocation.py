"""VERIFY THE $20 FIXED ALLOCATION — and that it stays $20 as the wallet moves.

The point of allocation_usd is that it does NOT scale with the balance. This
sizes a trade at several wallet sizes and checks the margin stays flat, then
confirms the old percentage behaviour still works when allocation_usd is absent.

Also prints what $20 of margin actually buys, since at 10x that is $200 of
notional and the dollar RISK depends on where the strategy's stop sits.

Read-only. Run:  py scripts/test_fixed_allocation.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import Config
from src.risk import RiskManager

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{('  — ' + detail) if detail else ''}")
    return cond


def main() -> int:
    print("=" * 88)
    print("FIXED $20 ALLOCATION — BTC perp legs")
    print("=" * 88)

    for name in ("config.v2_btc.yaml", "config.ema_rsi_btc.yaml"):
        cfg = Config.load(str(ROOT / name))
        rm = RiskManager(cfg.risk, lot_size=cfg.market.lot_size,
                         min_lots=cfg.market.min_lots)
        print(f"\n## {name}")
        print(f"   sizing_mode={cfg.risk.sizing_mode}  "
              f"allocation_usd={cfg.risk.allocation_usd}  "
              f"max_leverage={cfg.risk.max_leverage}")
        check(f"{name}: allocation_usd is 20",
              cfg.risk.allocation_usd == 20.0, str(cfg.risk.allocation_usd))

        print(f"\n   {'wallet':>10}{'margin':>10}{'notional':>11}{'lots':>7}"
              f"{'stop $':>9}{'risk $':>9}{'risk %':>8}")
        print("   " + "-" * 64)
        entry, stop = 63500.0, 62200.0     # ~2% stop, typical for these strategies
        margins = []
        for bal in (67.0, 150.0, 500.0, 2000.0):
            plan = rm.build_plan("long", entry, stop, bal)
            if plan is None:
                print(f"   {bal:>10,.0f}{'too small to size':>37}")
                continue
            margins.append(round(plan.margin_usd, 2))
            print(f"   {bal:>10,.0f}{plan.margin_usd:>10.2f}{plan.notional:>11.2f}"
                  f"{plan.lots:>7}{abs(entry-stop):>9,.0f}"
                  f"{plan.risk_usd:>9.2f}{100*plan.risk_usd/bal:>7.1f}%")
        if len(margins) >= 2:
            check(f"{name}: margin does NOT scale with the wallet",
                  max(margins) - min(margins) <= 2.0,
                  f"{min(margins)} - {max(margins)} across wallets")
            check(f"{name}: margin is about $20",
                  all(abs(m - 20.0) <= 2.5 for m in margins),
                  f"{margins}")

    # ---- the percentage path still works -------------------------------- #
    print("\n\n## REGRESSION — percentage sizing when allocation_usd is absent")
    cfg = Config.load(str(ROOT / "config.v2_btc.yaml"))
    cfg.risk.allocation_usd = None
    rm = RiskManager(cfg.risk, lot_size=cfg.market.lot_size,
                     min_lots=cfg.market.min_lots)
    a1 = rm.allocation(100.0)
    a2 = rm.allocation(400.0)
    print(f"   allocation(100) = {a1:.2f}   allocation(400) = {a2:.2f}")
    check("falls back to capital_allocation_pct", abs(a2 - 4 * a1) < 1e-6,
          f"{a1:.2f} -> {a2:.2f} scales with balance")

    # ---- never post margin you don't have ------------------------------- #
    cfg2 = Config.load(str(ROOT / "config.v2_btc.yaml"))
    rm2 = RiskManager(cfg2.risk, lot_size=cfg2.market.lot_size,
                      min_lots=cfg2.market.min_lots)
    check("capped at the balance when the wallet is under $20",
          rm2.allocation(8.0) == 8.0, f"allocation(8) = {rm2.allocation(8.0):.2f}")

    print("\n" + "=" * 88)
    print(f"{len(OK)} passed, {len(BAD)} failed")
    for b in BAD:
        print(f"  FAILED: {b}")
    print("=" * 88)
    return 1 if BAD else 0


if __name__ == "__main__":
    raise SystemExit(main())
