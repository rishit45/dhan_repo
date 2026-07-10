import csv
from collections import deque
from datetime import datetime, timedelta


class CandleStore:
    def __init__(self, timeframe_minutes=5, history_len=200):
        self.timeframe_minutes = int(timeframe_minutes)
        self.closed_candles = deque(maxlen=int(history_len))
        self.current_time = None
        self.current_candle = None
        self.current_last_price = None
        self.completed_candle = None

    def update(self, price, tick_time=None):
        tick_time = tick_time or datetime.now()
        price = float(price)
        candle_time = self._floor_time(tick_time)
        self.completed_candle = None

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
        self.closed_candles.append(self.current_candle)
        self.completed_candle = self.current_candle
        self.current_time = candle_time
        self.current_candle = self._new_candle(candle_time, price)
        self.current_last_price = price
        return self.completed_candle

    def closes(self):
        return [candle["close"] for candle in self.closed_candles if candle["close"] is not None]

    def opens(self):
        return [candle["open"] for candle in self.closed_candles]

    def load_history(self, candles):
        if not isinstance(candles, (list, tuple)):
            return
        parsed = []
        for candle in candles:
            if not isinstance(candle, dict):
                continue
            try:
                time_value = candle.get("time") or candle.get("datetime")
                if isinstance(time_value, str):
                    parsed_time = self._parse_time(time_value)
                elif isinstance(time_value, datetime):
                    parsed_time = time_value
                else:
                    continue
                parsed.append({
                    "time": parsed_time,
                    "open": float(candle["open"]),
                    "high": float(candle["high"]),
                    "low": float(candle["low"]),
                    "close": float(candle["close"]),
                })
            except Exception:
                continue

        parsed.sort(key=lambda c: c["time"])
        for candle in parsed:
            self.closed_candles.append(candle)

    def load_history_from_csv(self, path, time_format=None):
        try:
            with open(path, newline="", encoding="utf-8") as csvfile:
                reader = csv.DictReader(csvfile)
                candles = []
                for row in reader:
                    if not row:
                        continue
                    time_value = row.get("time") or row.get("datetime") or row.get("timestamp")
                    if time_value is None:
                        continue
                    if time_format:
                        parsed_time = datetime.strptime(time_value, time_format)
                    else:
                        parsed_time = self._parse_time(time_value)
                    candles.append({
                        "time": parsed_time,
                        "open": float(row.get("open", row.get("Open", 0))),
                        "high": float(row.get("high", row.get("High", 0))),
                        "low": float(row.get("low", row.get("Low", 0))),
                        "close": float(row.get("close", row.get("Close", 0))),
                    })
                self.load_history(candles)
        except FileNotFoundError:
            raise
        except Exception:
            raise

    def _parse_time(self, value):
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(value)
        except Exception:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M"):
            try:
                return datetime.strptime(value, fmt)
            except Exception:
                continue
        raise ValueError(f"Unsupported time format: {value}")

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
