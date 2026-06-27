"""Load and validate bot configuration from YAML + environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

REGION_URLS = {
    "global": "https://api.delta.exchange",
    "india": "https://api.india.delta.exchange",
}


@dataclass
class ExchangeConfig:
    region: str = "india"
    base_url: str = ""
    api_key: str = ""
    api_secret: str = ""


@dataclass
class MarketConfig:
    symbol: str = "BTCUSD"
    resolution: str = "1h"
    trend_resolution: str = "4h"
    lot_size: float = 0.001   # BTC per lot/contract (Delta BTCUSD = 0.001)
    min_lots: int = 1         # smallest order the bot will place


@dataclass
class RiskConfig:
    capital_allocation_pct: float = 0.50
    risk_per_trade_usd: Optional[float] = 5.0
    risk_per_trade_pct: Optional[float] = None
    reward_risk_ratio: float = 2.0
    max_trades_per_day: int = 2
    max_open_positions: int = 1
    max_leverage: float = 10.0


@dataclass
class StrategyConfig:
    name: str = "ema_rsi_atr"
    params: dict = field(default_factory=dict)


@dataclass
class BacktestConfig:
    fee_rate: float = 0.0005
    slippage_rate: float = 0.0002
    results_dir: str = "results"


@dataclass
class LiveConfig:
    dry_run: bool = True
    poll_seconds: int = 60
    order_type: str = "market_order"


@dataclass
class Config:
    exchange: ExchangeConfig
    market: MarketConfig
    risk: RiskConfig
    strategy: StrategyConfig
    backtest: BacktestConfig
    live: LiveConfig
    starting_balance: float = 100.0

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        load_dotenv(PROJECT_ROOT / ".env")

        # Pick config.yaml if present, else fall back to the example.
        if path is None:
            candidate = PROJECT_ROOT / "config.yaml"
            path = candidate if candidate.exists() else PROJECT_ROOT / "config.example.yaml"
        raw: dict[str, Any] = yaml.safe_load(Path(path).read_text()) or {}

        ex = raw.get("exchange", {})
        region = ex.get("region", "india")
        base_url = os.getenv("DELTA_BASE_URL") or REGION_URLS.get(region, REGION_URLS["india"])

        exchange = ExchangeConfig(
            region=region,
            base_url=base_url.rstrip("/"),
            api_key=os.getenv("DELTA_API_KEY", ""),
            api_secret=os.getenv("DELTA_API_SECRET", ""),
        )

        m = raw.get("market", {})
        market = MarketConfig(
            symbol=m.get("symbol", "BTCUSD"),
            resolution=str(m.get("resolution", "1h")),
            trend_resolution=str(m.get("trend_resolution", "4h")),
            lot_size=float(m.get("lot_size", 0.001)),
            min_lots=int(m.get("min_lots", 1)),
        )

        r = raw.get("risk", {})
        risk = RiskConfig(
            capital_allocation_pct=float(r.get("capital_allocation_pct", 0.50)),
            risk_per_trade_usd=_opt_float(r.get("risk_per_trade_usd", 5.0)),
            risk_per_trade_pct=_opt_float(r.get("risk_per_trade_pct", None)),
            reward_risk_ratio=float(r.get("reward_risk_ratio", 2.0)),
            max_trades_per_day=int(r.get("max_trades_per_day", 2)),
            max_open_positions=int(r.get("max_open_positions", 1)),
            max_leverage=float(r.get("max_leverage", 10.0)),
        )

        s = raw.get("strategy", {})
        strategy = StrategyConfig(
            name=s.get("name", "ema_rsi_atr"),
            params=s.get("params", {}) or {},
        )

        b = raw.get("backtest", {})
        backtest = BacktestConfig(
            fee_rate=float(b.get("fee_rate", 0.0005)),
            slippage_rate=float(b.get("slippage_rate", 0.0002)),
            results_dir=b.get("results_dir", "results"),
        )

        lv = raw.get("live", {})
        live = LiveConfig(
            dry_run=bool(lv.get("dry_run", True)),
            poll_seconds=int(lv.get("poll_seconds", 60)),
            order_type=lv.get("order_type", "market_order"),
        )

        starting_balance = float(raw.get("account", {}).get("starting_balance", 100.0))

        cfg = cls(
            exchange=exchange,
            market=market,
            risk=risk,
            strategy=strategy,
            backtest=backtest,
            live=live,
            starting_balance=starting_balance,
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not 0 < self.risk.capital_allocation_pct <= 1:
            raise ValueError("risk.capital_allocation_pct must be in (0, 1].")
        if self.risk.reward_risk_ratio <= 0:
            raise ValueError("risk.reward_risk_ratio must be > 0.")
        if self.risk.risk_per_trade_usd is None and self.risk.risk_per_trade_pct is None:
            raise ValueError("Set risk_per_trade_usd or risk_per_trade_pct.")
        if self.risk.max_trades_per_day < 1:
            raise ValueError("risk.max_trades_per_day must be >= 1.")


def _opt_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    return float(value)
