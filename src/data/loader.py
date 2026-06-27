"""Historical OHLC loading from Delta Exchange, with pagination + local caching."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from ..config import PROJECT_ROOT
from ..exchange import DeltaClient

CACHE_DIR = PROJECT_ROOT / "data" / "cache"

# Delta resolution -> seconds per candle (used for pagination + day math).
RESOLUTION_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200,
    "1d": 86400, "1w": 604800,
}

# Named lookback periods -> approximate days.
PERIOD_DAYS = {
    "1m": 30, "1mo": 30, "1month": 30,
    "3m": 90, "3mo": 90,
    "6m": 180, "6mo": 180,
    "1y": 365, "1yr": 365,
    "2y": 730, "2yr": 730,
    "3y": 1095, "3yr": 1095,
}

# Delta caps each candles call; stay safely under it.
MAX_CANDLES_PER_CALL = 1800


def period_to_start_end(period: str, end: Optional[int] = None) -> tuple[int, int]:
    """Map a named period ('1m','3m','6m','1y','2y') to (start, end) unix seconds."""
    key = period.lower().strip()
    if key not in PERIOD_DAYS:
        raise ValueError(f"Unknown period '{period}'. Choose from: {sorted(PERIOD_DAYS)}")
    end_ts = end if end is not None else int(time.time())
    start_ts = int((datetime.fromtimestamp(end_ts, tz=timezone.utc)
                    - timedelta(days=PERIOD_DAYS[key])).timestamp())
    return start_ts, end_ts


def load_candles(
    client: DeltaClient,
    symbol: str,
    resolution: str,
    start: int,
    end: int,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Fetch OHLC candles for [start, end], paginating around Delta's per-call cap.

    Returns a DataFrame indexed by UTC datetime with
    columns: open, high, low, close, volume.
    """
    if resolution not in RESOLUTION_SECONDS:
        raise ValueError(f"Unsupported resolution '{resolution}'.")

    cache_file = CACHE_DIR / f"{symbol}_{resolution}_{start}_{end}.pkl"
    if use_cache and cache_file.exists():
        return pd.read_pickle(cache_file)

    step = RESOLUTION_SECONDS[resolution]
    window = step * MAX_CANDLES_PER_CALL

    rows: list[dict] = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + window, end)
        candles = client.get_candles(symbol, resolution, cursor, chunk_end)
        if candles:
            rows.extend(candles)
        cursor = chunk_end
        time.sleep(0.25)  # be polite to the API / avoid rate limits

    if not rows:
        raise RuntimeError(
            f"No candles returned for {symbol} {resolution} "
            f"({datetime.utcfromtimestamp(start)} -> {datetime.utcfromtimestamp(end)})."
        )

    df = pd.DataFrame(rows)
    df = df.drop_duplicates(subset="time").sort_values("time")
    df["datetime"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("datetime")[["open", "high", "low", "close", "volume"]].astype(float)

    if use_cache:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        df.to_pickle(cache_file)
    return df


def attach_higher_tf_trend(
    df: pd.DataFrame, rule: str, ema_period: int = 50
) -> pd.DataFrame:
    """Add an 'htf_trend' column (1 up / -1 down) from a coarser timeframe.

    Resamples close to `rule`, takes an EMA, and marks up/down trend. Uses only
    *completed* higher-timeframe bars (shift(1)) then forward-fills onto the base
    index to avoid look-ahead bias.
    """
    rule_map = {
        "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h", "12h": "12h",
        "1d": "1D", "1w": "1W",
    }
    pandas_rule = rule_map.get(rule, rule)

    htf_close = df["close"].resample(pandas_rule, label="right", closed="right").last().dropna()
    htf_ema = htf_close.ewm(span=ema_period, adjust=False).mean()
    trend = (htf_close > htf_ema).astype(int) - (htf_close < htf_ema).astype(int)
    trend = trend.shift(1)  # only use the previous completed HTF bar

    out = df.copy()
    out["htf_trend"] = trend.reindex(df.index, method="ffill").fillna(0).astype(int)
    return out
