"""Robust multi-timeframe parameter search for the ema_rsi_atr strategy.

Goal: find a config that is profitable across ALL trailing windows
(3m / 6m / 1y / 2y), trades ~1-3x per week, and survives an out-of-sample
check — not one that's curve-fit to the recent regime.

Method
------
* For each timeframe in TIMEFRAMES, load 2y of candles once and attach the
  higher-timeframe trend once.
* For each parameter combo, run ONE full-2y backtest (using the live risk
  config from config.yaml -> lot sizing, % risk, leverage), then derive:
    - trailing returns at 3m / 6m / 1y / 2y from the equity curve,
    - out-of-sample average R: split trades at 75% of time, require the edge
      to be positive on BOTH the older (train) and recent (test) halves,
    - trade frequency (per week).
* Keep only combos positive on EVERY window with a positive OOS edge and a
  sane trade count, then rank by 2y return.

Run:  py scripts/optimize.py     (writes results/optimize_robust.csv)
Then plug the winner's timeframe + params into config.yaml and run
`py scripts/run_backtest.py` to confirm the per-period breakdown.
"""
from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backtest import Backtester                       # noqa: E402
from src.config import Config, PROJECT_ROOT               # noqa: E402
from src.data.loader import (                             # noqa: E402
    attach_higher_tf_trend, load_candles, period_to_start_end,
)
from src.exchange import DeltaClient                      # noqa: E402
from src.strategies import build_strategy                 # noqa: E402
from src.utils import setup_logging                       # noqa: E402

# --- search space -----------------------------------------------------------
TIMEFRAMES = ["30m", "1h"]          # 30m ~2x the trades of 1h (toward ~2/week)
TREND_TF = "4h"                     # higher-timeframe filter resolution
GRID = {
    "ema_fast": [9, 13, 21],
    "ema_slow": [34, 55, 100],
    "ema_trend": [200],
    "atr_stop_mult": [2.0, 3.0],
    "reward_risk_ratio": [2.0, 3.0],
    "use_higher_tf_filter": [True, False],
}
TRAIN_FRAC = 0.75
MIN_TRADES, MAX_TRADES = 80, 600    # ~0.75-5.5 trades/week over 2y
WINDOWS = {"r_3m": 90, "r_6m": 180, "r_1y": 365, "r_2y": 730}


def trailing_return_pct(equity: pd.Series, days: int) -> float:
    if len(equity) < 2:
        return 0.0
    t0 = equity.index[-1] - pd.Timedelta(days=days)
    idx = max(0, equity.index.searchsorted(t0, side="right") - 1)
    base = float(equity.iloc[idx])
    return (float(equity.iloc[-1]) / base - 1.0) * 100 if base > 0 else 0.0


def avg_r(trades: pd.DataFrame) -> float:
    return float(trades["r_multiple"].mean()) if len(trades) else 0.0


def main() -> None:
    setup_logging()
    base_cfg = Config.load()
    client = DeltaClient(base_url=base_cfg.exchange.base_url)
    start, end = period_to_start_end("2y")
    weeks_2y = 730 / 7.0

    keys = [k for k in GRID]
    raw_combos = [dict(zip(keys, v)) for v in itertools.product(*GRID.values())]
    combos = [c for c in raw_combos if c["ema_fast"] < c["ema_slow"]]

    rows = []
    for tf in TIMEFRAMES:
        print(f"\n=== timeframe {tf} : loading 2y candles ===")
        df = load_candles(client, base_cfg.market.symbol, tf, start, end)
        df = attach_higher_tf_trend(df, TREND_TF, ema_period=50)
        cutoff = df.index[int(len(df) * TRAIN_FRAC)]
        print(f"{len(df)} bars | evaluating {len(combos)} combos "
              f"(TRAIN < {cutoff.date()} <= TEST)")

        for n, combo in enumerate(combos, 1):
            params = dict(combo)
            rr = params.pop("reward_risk_ratio")
            cfg = Config.load()
            cfg.risk.reward_risk_ratio = rr
            cfg.market.resolution = tf
            sp = dict(base_cfg.strategy.params)
            sp.update(params)
            strat = build_strategy(cfg.strategy.name, sp)

            result = Backtester(cfg, strat).run(df, starting_balance=100.0)
            tr = result.trades
            if tr.empty or not (MIN_TRADES <= len(tr) <= MAX_TRADES):
                continue
            eq = result.equity
            rets = {k: trailing_return_pct(eq, d) for k, d in WINDOWS.items()}
            train_r = avg_r(tr[tr["entry_time"] < cutoff])
            test_r = avg_r(tr[tr["entry_time"] >= cutoff])

            rows.append({
                "tf": tf,
                "ema_fast": combo["ema_fast"], "ema_slow": combo["ema_slow"],
                "ema_trend": combo["ema_trend"], "atr": combo["atr_stop_mult"],
                "rr": rr, "htf": combo["use_higher_tf_filter"],
                "trades": len(tr), "tr/wk": round(len(tr) / weeks_2y, 2),
                "win%": round(result.metrics.get("win_rate_pct", 0), 1),
                "PF": round(result.metrics.get("profit_factor", 0), 2),
                **{k: round(v, 1) for k, v in rets.items()},
                "train_R": round(train_r, 3), "test_R": round(test_r, 3),
            })
            if n % 24 == 0:
                print(f"  ...{n}/{len(combos)}")

    res = pd.DataFrame(rows)
    out = PROJECT_ROOT / base_cfg.backtest.results_dir / "optimize_robust.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    res.to_csv(out, index=False)
    print(f"\nSaved {len(res)} evaluated combos -> {out}")

    if res.empty:
        print("No combos in the trade-count band. Widen MIN_TRADES/MAX_TRADES.")
        return

    robust = res[
        (res["r_3m"] > 0) & (res["r_6m"] > 0) & (res["r_1y"] > 0) & (res["r_2y"] > 0)
        & (res["train_R"] > 0) & (res["test_R"] > 0)
    ].sort_values("r_2y", ascending=False)

    print("\n" + "=" * 96)
    print("ROBUST COMBOS — positive on 3m/6m/1y/2y AND positive in/out-of-sample, "
          "ranked by 2y return")
    print("=" * 96)
    if robust.empty:
        print("None passed the full-robustness filter. Closest (positive 1y AND 2y):")
        near = res[(res["r_1y"] > 0) & (res["r_2y"] > 0)].sort_values("r_2y", ascending=False)
        _show(near.head(15))
        print("\nHonest read: a config that's solidly positive across ALL windows may")
        print("not exist for this strategy on BTC. Levers left: a different strategy")
        print("type, regime filters, or accepting that only some windows are positive.")
    else:
        _show(robust.head(15))
        b = robust.iloc[0]
        print(f"\nBEST: tf={b['tf']} ema_fast={int(b['ema_fast'])} ema_slow={int(b['ema_slow'])} "
              f"ema_trend={int(b['ema_trend'])} atr_stop_mult={b['atr']} rr={b['rr']} "
              f"htf={b['htf']}")
        print(f"  returns 3m {b['r_3m']}%  6m {b['r_6m']}%  1y {b['r_1y']}%  2y {b['r_2y']}%  "
              f"| {b['trades']} trades ({b['tr/wk']}/wk) win {b['win%']}% "
              f"| OOS R {b['test_R']}")


def _show(frame: pd.DataFrame) -> None:
    cols = ["tf", "ema_fast", "ema_slow", "atr", "rr", "htf", "trades", "tr/wk",
            "win%", "PF", "r_3m", "r_6m", "r_1y", "r_2y", "train_R", "test_R"]
    try:
        from tabulate import tabulate
        print(tabulate(frame[cols], headers="keys", tablefmt="github", showindex=False))
    except ImportError:
        print(frame[cols].to_string(index=False))


if __name__ == "__main__":
    main()
