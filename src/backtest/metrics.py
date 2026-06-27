"""Performance metrics computed from a list of closed trades + equity curve."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def compute_metrics(
    trades: pd.DataFrame, equity: pd.Series, starting_balance: float
) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {
            "trades": 0,
            "win_rate": 0.0,
            "note": "No trades were generated for this period.",
            "starting_balance": starting_balance,
            "final_balance": float(equity.iloc[-1]) if len(equity) else starting_balance,
        }

    wins = trades[trades["pnl"] > 0]
    losses = trades[trades["pnl"] <= 0]

    gross_profit = float(wins["pnl"].sum())
    gross_loss = float(-losses["pnl"].sum())
    net_pnl = float(trades["pnl"].sum())

    win_rate = len(wins) / n
    avg_win = float(wins["pnl"].mean()) if len(wins) else 0.0
    avg_loss = float(losses["pnl"].mean()) if len(losses) else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
    expectancy = net_pnl / n
    avg_r = float(trades["r_multiple"].mean())

    final_balance = float(equity.iloc[-1])
    total_return_pct = (final_balance / starting_balance - 1) * 100

    # Max drawdown on the equity curve.
    running_max = equity.cummax()
    drawdown = (equity - running_max) / running_max
    max_dd_pct = float(drawdown.min() * 100)

    # Streaks.
    win_flags = (trades["pnl"] > 0).tolist()
    max_win_streak = _max_streak(win_flags, True)
    max_loss_streak = _max_streak(win_flags, False)

    # Annualised Sharpe from per-trade returns (rough, trade-based).
    r = trades["r_multiple"]
    sharpe = float(r.mean() / r.std() * np.sqrt(len(r))) if r.std() > 0 else 0.0

    return {
        "trades": n,
        "wins": int(len(wins)),
        "losses": int(len(losses)),
        "win_rate": win_rate,
        "win_rate_pct": win_rate * 100,
        "profit_factor": profit_factor,
        "expectancy_usd": expectancy,
        "avg_r_multiple": avg_r,
        "avg_win_usd": avg_win,
        "avg_loss_usd": avg_loss,
        "gross_profit_usd": gross_profit,
        "gross_loss_usd": gross_loss,
        "net_pnl_usd": net_pnl,
        "starting_balance": starting_balance,
        "final_balance": final_balance,
        "total_return_pct": total_return_pct,
        "max_drawdown_pct": max_dd_pct,
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "sharpe_trades": sharpe,
    }


def _max_streak(flags: list[bool], value: bool) -> int:
    best = cur = 0
    for f in flags:
        if f == value:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def format_metrics(metrics: dict[str, Any]) -> str:
    """Pretty one-block summary for the console / log."""
    if metrics.get("trades", 0) == 0:
        return f"  No trades. {metrics.get('note', '')}"

    lines = [
        f"  Trades              : {metrics['trades']}  "
        f"(W {metrics['wins']} / L {metrics['losses']})",
        f"  Win rate            : {metrics['win_rate_pct']:.1f}%",
        f"  Profit factor       : {metrics['profit_factor']:.2f}",
        f"  Expectancy / trade  : ${metrics['expectancy_usd']:.2f}  "
        f"({metrics['avg_r_multiple']:+.2f} R)",
        f"  Avg win / avg loss  : ${metrics['avg_win_usd']:.2f} / ${metrics['avg_loss_usd']:.2f}",
        f"  Net PnL             : ${metrics['net_pnl_usd']:.2f}",
        f"  Balance             : ${metrics['starting_balance']:.2f} -> "
        f"${metrics['final_balance']:.2f}  ({metrics['total_return_pct']:+.1f}%)",
        f"  Max drawdown        : {metrics['max_drawdown_pct']:.1f}%",
        f"  Longest win/loss run: {metrics['max_win_streak']} / {metrics['max_loss_streak']}",
        f"  Sharpe (per-trade)  : {metrics['sharpe_trades']:.2f}",
    ]
    return "\n".join(lines)
