"""Strategy interface shared by the backtester and the live trader."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd


@dataclass
class Signal:
    """A trade intent emitted for the *most recently closed* candle.

    side       : "long" or "short"
    entry      : reference entry price (close of the signal candle)
    stop        : stop-loss price
    reason     : human-readable explanation (for logs)
    """
    side: str
    entry: float
    stop: float
    reason: str = ""


class Strategy:
    """Base class. Subclasses implement `prepare` and `signal`."""

    name: str = "base"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        """Return df with any indicator columns the strategy needs."""
        raise NotImplementedError

    def signal(self, df: pd.DataFrame, i: int) -> Optional[Signal]:
        """Decide whether to enter at the close of bar `i`.

        `df` is the output of `prepare`. Only data up to and including bar `i`
        may be used (no look-ahead). Returns a Signal or None.
        """
        raise NotImplementedError

    @property
    def warmup(self) -> int:
        """Number of leading bars to skip while indicators stabilise."""
        return 200
