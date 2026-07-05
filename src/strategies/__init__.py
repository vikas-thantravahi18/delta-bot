from .base import Strategy, Signal
from .ema_rsi_atr import EmaRsiAtrStrategy
from .ut_stc import UtStcStrategy
from .v2_dualtrend import V2DualTrendStrategy

# Registry so config can select a strategy by name.
STRATEGIES = {
    "ema_rsi_atr": EmaRsiAtrStrategy,
    "ut_stc": UtStcStrategy,
    "v2_dualtrend": V2DualTrendStrategy,
}


def build_strategy(name: str, params: dict) -> Strategy:
    if name not in STRATEGIES:
        raise ValueError(
            f"Unknown strategy '{name}'. Available: {list(STRATEGIES)}"
        )
    return STRATEGIES[name](**(params or {}))


__all__ = [
    "Strategy", "Signal", "EmaRsiAtrStrategy", "UtStcStrategy",
    "V2DualTrendStrategy", "build_strategy", "STRATEGIES",
]
