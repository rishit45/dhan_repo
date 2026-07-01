from collections import deque
from datetime import datetime, timedelta


class CandleStore:
    def __init__(self, timeframe_minutes=5, history_len=300):
        self.timeframe_minutes = int(timeframe_minutes)
        self.closed_candles = deque(maxlen=int(history_len))
        self.current_time = None
        self.current_candle = None
        self.current_last_price = None

    def update(self, price, tick_time=None):
        tick_time = tick_time or datetime.now()
        price = float(price)
        candle_time = self._floor_time(tick_time)

        if self.current_candle is None:
            self.current_time = candle_time
            self.current_candle = self._new_candle(candle_time, price)
            self.current_last_price = price
            return None

        if candle_time == self.current_time:
            self.current_candle["high"] = max(self.current_candle["high"], price)
            self.current_candle["low"] = min(self.current_candle["low"], price)
            self.current_last_price = price
            return None

        self.current_candle["close"] = self.current_last_price
        completed = self.current_candle
        self.closed_candles.append(completed)
        self.current_time = candle_time
        self.current_candle = self._new_candle(candle_time, price)
        self.current_last_price = price
        return completed

    def closes(self):
        return [candle["close"] for candle in self.closed_candles if candle["close"] is not None]

    def opens(self):
        return [candle["open"] for candle in self.closed_candles]

    def _floor_time(self, tick_time):
        day_start = tick_time.replace(hour=0, minute=0, second=0, microsecond=0)
        minutes_since_start = tick_time.hour * 60 + tick_time.minute
        candle_start = minutes_since_start - (minutes_since_start % self.timeframe_minutes)
        return day_start + timedelta(minutes=candle_start)

    def _new_candle(self, candle_time, price):
        return {
            "time": candle_time,
            "open": price,
            "high": price,
            "low": price,
            "close": None
        }
