from collections import deque
from datetime import datetime, timedelta
import re


FIELD_NAMES = {"time", "open", "high", "low", "close"}


class InstrumentCandles:
    def __init__(self, name, maxlen=200, timeframe_minutes=1):
        self.name = name
        self.maxlen = maxlen
        self.timeframe_minutes = timeframe_minutes
        self.current_time = None
        self.current_candle = None
        self.current_last_price = None
        self.closed_candles = deque(maxlen=maxlen)
        self.completed_candle = None

    def update(self, price, tick_time=None):
        tick_time = tick_time or datetime.now()
        candle_time = self._floor_time(tick_time)
        price = float(price)
        self.completed_candle = None

        if self.current_candle is None:
            self.current_time = candle_time
            self.current_candle = self._new_candle(candle_time, price)
            self.current_last_price = price
            return self

        if candle_time == self.current_time:
            self.current_candle["high"] = max(self.current_candle["high"], price)
            self.current_candle["low"] = min(self.current_candle["low"], price)
            self.current_last_price = price
            return self

        self.current_candle["close"] = self.current_last_price
        self.closed_candles.append(self.current_candle)
        self.completed_candle = self.current_candle
        self.current_time = candle_time
        self.current_candle = self._new_candle(candle_time, price)
        self.current_last_price = price
        return self

    def consume_completed(self):
        candle = self.completed_candle
        self.completed_candle = None
        return candle

    def closed(self, field=None, candle_time=None, index=-1):
        candle = self._find_closed_candle(candle_time, index)
        if candle is None:
            return None
        if field is None:
            return candle
        return candle.get(field)

    def as_dict(self):
        return {
            "name": self.name,
            "current": self.current_candle,
            "closed": list(self.closed_candles),
        }

    def __getitem__(self, key):
        if key in FIELD_NAMES:
            if self.current_candle is None:
                return None
            return self.current_candle.get(key)

        field, candle_time = self._parse_closed_key(key)
        if field is not None:
            return self.closed(field=field, candle_time=candle_time)

        raise KeyError(key)

    def _floor_time(self, tick_time):
        day_start = tick_time.replace(hour=0, minute=0, second=0, microsecond=0)
        minutes_since_start = tick_time.hour * 60 + tick_time.minute
        candle_start_minutes = minutes_since_start - (minutes_since_start % self.timeframe_minutes)
        return day_start + timedelta(minutes=candle_start_minutes)

    def _new_candle(self, candle_time, price):
        price = float(price)
        return {
            "time": candle_time,
            "open": price,
            "high": price,
            "low": price,
            "close": None,
        }

    def _find_closed_candle(self, candle_time, index):
        if not self.closed_candles:
            return None

        if candle_time is None:
            return self.closed_candles[index]

        wanted = parse_candle_time(candle_time)
        for candle in reversed(self.closed_candles):
            if candle["time"] == wanted:
                return candle
        return None

    def _parse_closed_key(self, key):
        if not isinstance(key, str):
            return None, None

        for field in FIELD_NAMES - {"time"}:
            prefix = field + "-"
            if key.startswith(prefix):
                return field, key[len(prefix):]

        return None, None


class CandleStore:
    def __init__(self, maxlen=200, timeframe_minutes=1):
        self.maxlen = maxlen
        self.timeframe_minutes = timeframe_minutes
        self.instruments = {}

    def configure_timeframe(self, timeframe):
        timeframe_minutes = parse_timeframe_minutes(timeframe)
        if timeframe_minutes == self.timeframe_minutes:
            return

        self.timeframe_minutes = timeframe_minutes
        self.instruments.clear()

    def update(self, instrument_key, price, tick_time=None):
        instrument = self.get(instrument_key)
        return instrument.update(price, tick_time=tick_time)

    def get(self, instrument_key):
        if instrument_key not in self.instruments:
            self.instruments[instrument_key] = InstrumentCandles(
                instrument_key,
                maxlen=self.maxlen,
                timeframe_minutes=self.timeframe_minutes,
            )
        return self.instruments[instrument_key]

    def current(self, instrument_key):
        return self.get(instrument_key).current_candle

    def closed(self, instrument_key, field=None, candle_time=None, index=-1):
        return self.get(instrument_key).closed(field=field, candle_time=candle_time, index=index)


def parse_timeframe_minutes(value):
    if isinstance(value, int):
        minutes = value
    elif isinstance(value, float):
        minutes = int(value)
    else:
        text = str(value or "1min").strip().lower()
        text = text.replace(" ", "")
        text = text.replace("hours", "hr").replace("hour", "hr").replace("hrs", "hr")
        text = text.replace("minutes", "min").replace("minute", "min").replace("mins", "min")

        hours = 0
        minutes = 0
        matches = re.findall(r"(\d+)(hr|h|min|m)", text)

        if not matches and text.isdigit():
            minutes = int(text)
        else:
            for amount, unit in matches:
                if unit in {"hr", "h"}:
                    hours += int(amount)
                else:
                    minutes += int(amount)

        minutes += hours * 60

    if minutes < 1:
        raise ValueError("timeframe must be at least 1 minute")

    return minutes


def format_timeframe(minutes):
    minutes = int(minutes)
    hours = minutes // 60
    remaining_minutes = minutes % 60

    parts = []
    if hours:
        parts.append(f"{hours}HR")
    if remaining_minutes:
        parts.append(f"{remaining_minutes}MIN")
    return "-".join(parts) or "1MIN"


def parse_candle_time(value):
    if isinstance(value, datetime):
        return value.replace(second=0, microsecond=0)

    text = str(value).strip()
    formats = ("%Y-%m-%d %H:%M", "%H:%M")

    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
            if fmt == "%H:%M":
                now = datetime.now()
                parsed = parsed.replace(year=now.year, month=now.month, day=now.day)
            return parsed.replace(second=0, microsecond=0)
        except ValueError:
            pass

    raise ValueError(f"Unsupported candle time '{value}'. Use 'HH:MM' or 'YYYY-MM-DD HH:MM'.")


def previous_candle_time(minutes=1, now=None):
    now = now or datetime.now()
    return (now - timedelta(minutes=minutes)).replace(second=0, microsecond=0)


default_store = CandleStore(maxlen=200, timeframe_minutes=1)
instrument_data = default_store.instruments


def configure_timeframe(timeframe):
    default_store.configure_timeframe(timeframe)


def get_timeframe_label():
    return format_timeframe(default_store.timeframe_minutes)


def process_tick(symbol, price, tick_time=None):
    return default_store.update(symbol, price, tick_time=tick_time)


def get_instrument(symbol):
    return default_store.get(symbol)


def get_closed(symbol, field=None, candle_time=None, index=-1):
    return default_store.closed(symbol, field=field, candle_time=candle_time, index=index)
