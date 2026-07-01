"""Backtest the configured strategy over one or more lookback periods.

Examples
--------
  py scripts/run_backtest.py                       # all periods (1m,3m,6m,1y,2y)
  py scripts/run_backtest.py --periods 1m 6m       # just these
  py scripts/run_backtest.py --symbol ETHUSD --resolution 15m --plot
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make `import src...` work when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backtest import Backtester                       # noqa: E402
from src.backtest.metrics import format_metrics           # noqa: E402
from src.config import Config, PROJECT_ROOT, REGION_URLS  # noqa: E402
from src.data.loader import (                             # noqa: E402
    attach_higher_tf_trend, load_candles, period_to_start_end,
)
from src.exchange import DeltaClient                      # noqa: E402
from src.strategies import build_strategy                 # noqa: E402
from src.utils import setup_logging                       # noqa: E402

ALL_PERIODS = ["1m", "3m", "6m", "1y", "2y", "3y"]


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="Backtest the Delta trading strategy.")
    parser.add_argument("--periods", nargs="+", default=ALL_PERIODS,
                        help=f"Lookback windows (default: {' '.join(ALL_PERIODS)})")
    parser.add_argument("--symbol", default=None, help="Override config market.symbol")
    parser.add_argument("--resolution", default=None, help="Override candle timeframe")
    parser.add_argument("--balance", type=float, default=None, help="Override starting balance")
    parser.add_argument("--plot", action="store_true", help="Save equity-curve PNGs")
    parser.add_argument("--no-cache", action="store_true", help="Bypass the candle cache")
    parser.add_argument("--config", default=None, help="Path to a config.yaml")
    parser.add_argument(
        "--base-url", default=None,
        help="Data source for historical candles. Defaults to the PRODUCTION URL "
             "for your region — backtests deliberately IGNORE DELTA_BASE_URL so a "
             "testnet/demo live endpoint (which has little history) can't starve "
             "the backtest and collapse all long periods to the same short window.",
    )
    args = parser.parse_args()

    cfg = Config.load(args.config)
    if args.symbol:
        cfg.market.symbol = args.symbol
    if args.resolution:
        cfg.market.resolution = args.resolution
    balance = args.balance if args.balance is not None else cfg.starting_balance

    # Historical candles are public; always fetch them from a data-rich
    # production endpoint (not the possibly-testnet live-trading DELTA_BASE_URL).
    data_url = args.base_url or REGION_URLS.get(cfg.exchange.region, REGION_URLS["india"])
    client = DeltaClient(base_url=data_url)
    strategy = build_strategy(cfg.strategy.name, cfg.strategy.params)
    use_htf = bool(cfg.strategy.params.get("use_higher_tf_filter", False))

    results_dir = PROJECT_ROOT / cfg.backtest.results_dir
    results_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nStrategy : {cfg.strategy.name}")
    print(f"Data src : {data_url}")
    print(f"Symbol   : {cfg.market.symbol} @ {cfg.market.resolution} "
          f"(trend filter: {cfg.market.trend_resolution if use_htf else 'off'})")
    print(f"Balance  : ${balance:.2f} | allocation {cfg.risk.capital_allocation_pct:.0%} | "
          f"risk ${cfg.risk.risk_per_trade_usd}/trade | R:R 1:{cfg.risk.reward_risk_ratio:g}")

    summary = []
    for period in args.periods:
        print(f"\n{'='*64}\nPERIOD: {period}\n{'='*64}")
        try:
            start, end = period_to_start_end(period)
            df = load_candles(client, cfg.market.symbol, cfg.market.resolution,
                              start, end, use_cache=not args.no_cache)
            if use_htf:
                df = attach_higher_tf_trend(
                    df, cfg.market.trend_resolution,
                    ema_period=cfg.strategy.params.get("ema_trend", 50),
                )
        except Exception as exc:
            print(f"  ! Failed to load data: {exc}")
            continue

        bt = Backtester(cfg, build_strategy(cfg.strategy.name, cfg.strategy.params))
        result = bt.run(df, starting_balance=balance)
        print(f"  Candles loaded      : {len(df)}  "
              f"({df.index[0].date()} -> {df.index[-1].date()})")
        print(format_metrics(result.metrics))

        # Save artefacts.
        tag = f"{cfg.market.symbol}_{cfg.market.resolution}_{period}"
        if not result.trades.empty:
            result.trades.to_csv(results_dir / f"trades_{tag}.csv", index=False)
        result.equity.to_csv(results_dir / f"equity_{tag}.csv")
        if args.plot:
            _plot_equity(result.equity, results_dir / f"equity_{tag}.png", tag)

        m = result.metrics
        summary.append({
            "period": period,
            "trades": m.get("trades", 0),
            "win%": m.get("win_rate_pct", 0.0),
            "PF": m.get("profit_factor", 0.0),
            "return%": m.get("total_return_pct", 0.0),
            "maxDD%": m.get("max_drawdown_pct", 0.0),
            "final$": m.get("final_balance", balance),
        })

    _print_summary(summary)


def _print_summary(summary: list[dict]) -> None:
    if not summary:
        return
    print(f"\n{'='*64}\nSUMMARY\n{'='*64}")
    try:
        from tabulate import tabulate
        rows = [[s["period"], s["trades"], f"{s['win%']:.1f}", f"{s['PF']:.2f}",
                 f"{s['return%']:+.1f}", f"{s['maxDD%']:.1f}", f"{s['final$']:.2f}"]
                for s in summary]
        print(tabulate(rows,
                       headers=["period", "trades", "win%", "PF", "return%", "maxDD%", "final$"],
                       tablefmt="github"))
    except ImportError:
        print(f"{'period':>7} {'trades':>7} {'win%':>6} {'PF':>6} "
              f"{'return%':>8} {'maxDD%':>7} {'final$':>9}")
        for s in summary:
            print(f"{s['period']:>7} {s['trades']:>7} {s['win%']:>6.1f} {s['PF']:>6.2f} "
                  f"{s['return%']:>+8.1f} {s['maxDD%']:>7.1f} {s['final$']:>9.2f}")
    print("\nNote: a >35% win rate at 1:2 R:R is break-even-positive (BE ~= 33%).")
    print("Past backtest performance does NOT guarantee future results.")


def _plot_equity(equity, path, title) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(10, 4))
        equity.plot(ax=ax)
        ax.set_title(f"Equity curve — {title}")
        ax.set_ylabel("Balance ($)")
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(path, dpi=110)
        plt.close(fig)
        print(f"  Saved equity plot   : {path.name}")
    except Exception as exc:
        print(f"  (plot skipped: {exc})")


if __name__ == "__main__":
    main()
