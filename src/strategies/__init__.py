from .base import Strategy, Signal
from .ema_rsi_atr import EmaRsiAtrStrategy

# Registry so config can select a strategy by name.
STRATEGIES = {
    "ema_rsi_atr": EmaRsiAtrStrategy,
}


def build_strategy(name: str, params: dict) -> Strategy:
    if name not in STRATEGIES:
        raise ValueError(
            f"Unknown strategy '{name}'. Available: {list(STRATEGIES)}"
        )
    return STRATEGIES[name](**(params or {}))


__all__ = ["Strategy", "Signal", "EmaRsiAtrStrategy", "build_strategy", "STRATEGIES"]
