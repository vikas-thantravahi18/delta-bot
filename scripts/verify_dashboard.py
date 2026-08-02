"""Verify the dashboard's P&L maths against the real account.

Delta's /v2/fills returns realized_pnl: null, so the dashboard computes P&L by
FIFO-matching fills. This script runs that exact function on your live fills and
prints what the dashboard will show, so the numbers can be checked by hand.

  py scripts/verify_dashboard.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import Config          # noqa: E402
from src.exchange import DeltaClient   # noqa: E402


def load_dashboard_fns():
    """Import the pure helpers from dashboard.py without starting streamlit."""
    import types
    from collections import defaultdict, deque

    src = (ROOT / "dashboard.py").read_text(encoding="utf-8")
    mod = types.ModuleType("dash_fns")
    mod.__dict__.update({"defaultdict": defaultdict, "deque": deque})

    def grab(start_marker, end_marker):
        a = src.index(start_marker)
        b = src.index(end_marker, a)
        return src[a:b]

    exec("BTC_CONTRACT = 0.001\nETH_CONTRACT = 0.01\n", mod.__dict__)
    exec(grab("def contract_size", "@st.cache_data"), mod.__dict__)
    exec(grab("def realised_trades", "def load_baseline"), mod.__dict__)
    return mod


def main() -> None:
    cfg = Config.load()
    if not (cfg.exchange.api_key and cfg.exchange.api_secret):
        print("No API keys in .env — cannot verify against the live account.")
        return
    c = DeltaClient(base_url=cfg.exchange.base_url,
                    api_key=cfg.exchange.api_key,
                    api_secret=cfg.exchange.api_secret)
    fns = load_dashboard_fns()

    fills = c.get_fills(200) or []
    trades = fns.realised_trades(fills)
    total = sum(t["pnl"] for t in trades)
    wins = [t for t in trades if t["pnl"] > 0]
    gl = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    gw = sum(t["pnl"] for t in wins)

    print("=" * 92)
    print("DASHBOARD P&L VERIFICATION")
    print("=" * 92)
    print(f"\n  fills fetched         {len(fills)}")
    print(f"  closed round trips    {len(trades)}")
    print(f"  realised P&L          ${total:+,.2f}")
    print(f"  win rate              {100*len(wins)/max(len(trades),1):.0f}%")
    print(f"  avg per trade         ${total/max(len(trades),1):+,.2f}")
    print(f"  profit factor         {(gw/gl):.2f}" if gl > 0 else "  profit factor         —")

    if trades:
        print(f"\n  {'closed':<21}{'instrument':<23}{'side':<6}{'size':>6}"
              f"{'entry':>9}{'exit':>9}{'fees':>8}{'P&L':>10}")
        print("  " + "-" * 90)
        for t in sorted(trades, key=lambda z: str(z["closed"]), reverse=True)[:12]:
            print(f"  {str(t['closed'])[:19]:<21}{t['symbol']:<23}{t['side']:<6}"
                  f"{t['size']:>6g}{t['entry']:>9,.1f}{t['exit']:>9,.1f}"
                  f"{t['fees']:>8.2f}{t['pnl']:>+10.2f}")

    bal = None
    for b in c.get_balances():
        if str(b.get("asset_symbol", "")).upper() in ("USD", "USDT", "USDC"):
            bal = float(b.get("balance", 0) or 0)
            break
    if bal is not None:
        base = bal - total
        print(f"\n  balance               ${bal:,.2f}")
        print(f"  inferred baseline     ${base:,.2f}   (balance - realised P&L)")
        if base > 0:
            print(f"  TOTAL RETURN          {100*total/base:+.2f}%")
        print("\n  (the dashboard writes this baseline to data/dashboard_baseline.json "
              "on first run;\n   edit it after any deposit or withdrawal)")


if __name__ == "__main__":
    main()
