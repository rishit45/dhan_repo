from collections import deque
from datetime import datetime


class CandleAggregator:
    """
    Builds 1-minute OHLC candles from live tick data for any number of symbols.

    Usage:
        agg = CandleAggregator(watchlist=["RELIANCE", "TCS"], max_candles=20)

        while True:
            prices = tsl.get_ltp_data(names=agg.watchlist)
            for symbol in agg.watchlist:
                if symbol not in prices:
                    continue
                ltp = prices[symbol]["ltp"]
                agg.process_tick(symbol, float(ltp))
            time.sleep(1)
    """

    def __init__(self, watchlist=None, max_candles=20):
        """
        Args:
            watchlist     (list[str]|None) - symbols to track, optional convenience storage
            max_candles   (int)            - how many completed candles to retain per symbol
        """
        self.watchlist        = watchlist or []   # symbols this instance is meant to track
        self.max_candles      = max_candles        # rolling history length per symbol
        self.instrument_data  = {}                  # symbol -> {"current_minute", "current_candle", "candles"}

    def _ensure_symbol(self, symbol):
        """
        Creates the tracking state for a symbol the first time it's seen.
        """
        if symbol not in self.instrument_data:
            self.instrument_data[symbol] = {
                "current_minute": None,                        # datetime, start of the in-progress candle
                "current_candle": None,                         # dict: time/open/high/low/close, in progress
                "candles":        deque(maxlen=self.max_candles),  # completed candles, oldest first
            }

    def process_tick(self, symbol, price):
        """
        Feeds one live price tick into the aggregator for a symbol.
        Updates the in-progress candle, or finalizes it and starts a new
        one if the minute has rolled over.

        Args:
            symbol (str)   - instrument identifier
            price  (float) - latest traded price
        """
        self._ensure_symbol(symbol)
        stock = self.instrument_data[symbol]   # this symbol's tracking state

        minute = datetime.now().replace(second=0, microsecond=0)   # start-of-minute timestamp for this tick

        if stock["current_minute"] is None:
            stock["current_minute"] = minute
            stock["current_candle"] = {
                "time": minute, "open": price,
                "high": price,  "low": price, "close": price,
            }
            return

        if minute == stock["current_minute"]:
            candle          = stock["current_candle"]   # the candle still being built
            candle["high"]  = max(candle["high"], price)
            candle["low"]   = min(candle["low"],  price)
            candle["close"] = price
        else:
            completed = stock["current_candle"]   # the candle that just finished
            stock["candles"].append(completed)

            stock["current_minute"] = minute
            stock["current_candle"] = {
                "time": minute, "open": price,
                "high": price,  "low": price, "close": price,
            }

    def get_candles(self, symbol):
        """
        Returns the completed candle history for one symbol.

        Args:
            symbol (str) - instrument identifier

        Returns:
            deque[dict] - completed candles, oldest first, or empty deque if symbol unseen
        """
        if symbol not in self.instrument_data:
            return deque()
        return self.instrument_data[symbol]["candles"]

    def get_current_candle(self, symbol):
        """
        Returns the in-progress (not yet completed) candle for one symbol.

        Args:
            symbol (str) - instrument identifier

        Returns:
            dict|None - {"time","open","high","low","close"}, or None if symbol unseen
        """
        if symbol not in self.instrument_data:
            return None
        return self.instrument_data[symbol]["current_candle"]

    def get_closes(self, symbol):
        """
        Returns just the close prices from completed candles for one symbol.

        Args:
            symbol (str) - instrument identifier

        Returns:
            list[float] - close prices, oldest first
        """
        return [c["close"] for c in self.get_candles(symbol)]