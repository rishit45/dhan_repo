"""Simple SMA-based helpers for seeding and updating MAC-like SMA.

This module provides a DataFrame-friendly `update_sma` function (per user's
request) and lightweight fallbacks if `pandas` is not available.

Behaviour:
- `MA_PERIOD` is the length used for the SMA (default 20 = 19 historical + current).
- `seed_candles_from_ltps` will populate a candle container with 1-row candles
  created from simple LTP values (open=high=low=close=ltp).
- `update_sma` appends a new candle (open, high, low, close) and returns the
  updated container plus the current SMA computed over the mean of high/low.
"""

MA_PERIOD = 55

try:
    import pandas as pd
except Exception:
    pd = None


def update_sma_high(candles, open_price, high_price, low_price, close_price):
    """Append a candle and return (candles, current_high_sma).

    Computes SMA of the `high` column over the last `MA_PERIOD` rows.
    """
    if pd is not None and hasattr(candles, "loc"):
        idx = len(candles)
        candles.loc[idx] = [open_price, high_price, low_price, close_price]
        recent = candles["high"].tail(MA_PERIOD)
        if len(recent) < MA_PERIOD:
            return candles, None
        return candles, float(recent.mean())

    if not isinstance(candles, list):
        raise TypeError("candles must be a pandas.DataFrame or a list of dicts")

    candles.append({"open": float(open_price), "high": float(high_price), "low": float(low_price), "close": float(close_price)})
    highs = [c["high"] for c in candles[-MA_PERIOD:]]
    if len(highs) < MA_PERIOD:
        return candles, None
    return candles, sum(highs) / len(highs)


def update_sma_low(candles, open_price, high_price, low_price, close_price):
    """Append a candle and return (candles, current_low_sma).

    Computes SMA of the `low` column over the last `MA_PERIOD` rows.
    """
    if pd is not None and hasattr(candles, "loc"):
        idx = len(candles)
        candles.loc[idx] = [open_price, high_price, low_price, close_price]
        recent = candles["low"].tail(MA_PERIOD)
        if len(recent) < MA_PERIOD:
            return candles, None
        return candles, float(recent.mean())

    if not isinstance(candles, list):
        raise TypeError("candles must be a pandas.DataFrame or a list of dicts")

    candles.append({"open": float(open_price), "high": float(high_price), "low": float(low_price), "close": float(close_price)})
    lows = [c["low"] for c in candles[-MA_PERIOD:]]
    if len(lows) < MA_PERIOD:
        return candles, None
    return candles, sum(lows) / len(lows)


def append_candle(candles, open_price, high_price, low_price, close_price):
    """Append a candle without computing SMAs. Returns candles."""
    if pd is not None and hasattr(candles, "loc"):
        idx = len(candles)
        candles.loc[idx] = [open_price, high_price, low_price, close_price]
        return candles

    if not isinstance(candles, list):
        raise TypeError("candles must be a pandas.DataFrame or a list of dicts")

    candles.append({"open": float(open_price), "high": float(high_price), "low": float(low_price), "close": float(close_price)})
    return candles


def compute_sma_high(candles):
    """Compute SMA of `high` over last MA_PERIOD rows; return float or None."""
    if pd is not None and hasattr(candles, "loc"):
        recent = candles["high"].tail(MA_PERIOD)
        if len(recent) < MA_PERIOD:
            return None
        return float(recent.mean())

    if not isinstance(candles, list):
        raise TypeError("candles must be a pandas.DataFrame or a list of dicts")

    highs = [c["high"] for c in candles[-MA_PERIOD:]]
    if len(highs) < MA_PERIOD:
        return None
    return sum(highs) / len(highs)


def compute_sma_low(candles):
    """Compute SMA of `low` over last MA_PERIOD rows; return float or None."""
    if pd is not None and hasattr(candles, "loc"):
        recent = candles["low"].tail(MA_PERIOD)
        if len(recent) < MA_PERIOD:
            return None
        return float(recent.mean())

    if not isinstance(candles, list):
        raise TypeError("candles must be a pandas.DataFrame or a list of dicts")

    lows = [c["low"] for c in candles[-MA_PERIOD:]]
    if len(lows) < MA_PERIOD:
        return None
    return sum(lows) / len(lows)


def seed_candles_from_ltps(candles, ltps):
    """Seed `candles` from a list of LTP values.

    - `ltps` is an iterable of numeric LTPs (oldest first).
    - Each LTP becomes a 5-min candle with open=high=low=close=ltp.
    - Works with a pandas DataFrame or a list of dicts (in-place).
    """
    if pd is not None and hasattr(candles, "loc"):
        start = len(candles)
        for i, v in enumerate(ltps):
            candles.loc[start + i] = [v, v, v, v]
        return candles

    if not isinstance(candles, list):
        raise TypeError("candles must be a pandas.DataFrame or a list of dicts")

    for v in ltps:
        candles.append({"open": float(v), "high": float(v), "low": float(v), "close": float(v)})
    return candles

