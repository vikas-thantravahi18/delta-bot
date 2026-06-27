"""Offline tests: no network needed. Run with `py tests/test_strategy.py` or pytest."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backtest import Backtester
from src.config import Config
from src.indicators import atr, ema, rsi
from src.risk import RiskManager
from src.strategies import build_strategy


def _synthetic_df(n: int = 1500, seed: int = 7) -> pd.DataFrame:
    """Random-walk OHLC with mild trend + noise so signals can fire."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0, 1, n).cumsum()
    trend = np.sin(np.linspace(0, 8 * np.pi, n)) * 40  # oscillating trend
    close = 30000 + steps * 25 + trend * 20
    idx = pd.date_range("2023-01-01", periods=n, freq="1h", tz="UTC")
    high = close + rng.uniform(5, 60, n)
    low = close - rng.uniform(5, 60, n)
    open_ = close + rng.uniform(-30, 30, n)
    vol = rng.uniform(1, 10, n)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


def test_indicators():
    df = _synthetic_df(300)
    e = ema(df["close"], 21)
    r = rsi(df["close"], 14)
    a = atr(df, 14)
    assert len(e) == len(df)
    assert r.between(0, 100).all()
    assert (a.dropna() >= 0).all()
    print("test_indicators: OK")


def test_risk_manager():
    cfg = Config.load()
    cfg.risk.risk_per_trade_usd = 5.0
    cfg.risk.reward_risk_ratio = 2.0
    rm = RiskManager(cfg.risk, lot_size=0.001, min_lots=1)
    plan = rm.build_plan("long", entry=30000.0, stop=29700.0, balance=100.0)
    assert plan is not None
    # Whole lots only, and realised risk must be <= the $5 budget (lot rounding
    # makes it slightly under, within one lot's worth of risk).
    assert plan.lots >= 1
    assert abs(plan.qty - plan.lots * 0.001) < 1e-12
    one_lot_risk = 0.001 * (plan.entry - plan.stop)
    assert plan.risk_usd <= 5.0 + 1e-9
    assert plan.risk_usd > 5.0 - one_lot_risk
    # 1:2 reward: tp distance == 2x stop distance
    assert abs((plan.take_profit - plan.entry) - 2 * (plan.entry - plan.stop)) < 1e-6

    # Tiny wallet can't afford a lot within budget -> no trade.
    assert rm.build_plan("long", entry=60000.0, stop=58200.0, balance=3.2) is None
    print("test_risk_manager: OK")


def test_backtest_runs():
    cfg = Config.load()
    cfg.strategy.params["use_higher_tf_filter"] = False  # keep synthetic test self-contained
    strat = build_strategy(cfg.strategy.name, cfg.strategy.params)
    bt = Backtester(cfg, strat)
    result = bt.run(_synthetic_df(1500), starting_balance=100.0)
    assert isinstance(result.trades, pd.DataFrame)
    assert "win_rate" in result.metrics or result.metrics.get("trades") == 0
    print(f"test_backtest_runs: OK ({result.metrics.get('trades', 0)} trades, "
          f"win_rate={result.metrics.get('win_rate_pct', 0):.1f}%)")


if __name__ == "__main__":
    test_indicators()
    test_risk_manager()
    test_backtest_runs()
    print("\nAll tests passed.")
