import copy
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STRATEGY_DIR = ROOT / "strategy_"
if str(STRATEGY_DIR) not in sys.path:
    sys.path.insert(0, str(STRATEGY_DIR))

from candles import CandleStore
from config_loader import get_instrument, get_quantity
from exit_manager import should_exit
import indicators
from market_data import (
    _history_instrument_type_candidates,
    _parse_intraday_data,
    _security_id_for_quote,
    create_dhan,
)


@dataclass
class Trade:
    side: str
    entry_time: datetime
    entry_price: float
    exit_time: datetime | None
    exit_price: float | None
    quantity: int
    reason: str | None = None

    @property
    def points(self):
        if self.exit_price is None:
            return 0.0
        if self.side == "LONG":
            return self.exit_price - self.entry_price
        return self.entry_price - self.exit_price

    @property
    def pnl(self):
        return self.points * self.quantity


def load_config():
    with open(STRATEGY_DIR / "strategy_config.json", "r", encoding="utf-8") as config_file:
        return json.load(config_file)


def parse_datetime(prompt):
    while True:
        value = input(prompt).strip()
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(value, fmt)
                if fmt == "%Y-%m-%d":
                    return parsed.replace(hour=0, minute=0, second=0)
                return parsed
            except ValueError:
                continue
        print("Use format YYYY-MM-DD HH:MM, for example 2026-07-01 09:00")


def ask_int(prompt, default):
    value = input(f"{prompt} [{default}]: ").strip()
    if not value:
        return int(default)
    return int(value)


def validate_timeframe(name, timeframe):
    timeframe = int(timeframe)
    if timeframe < 1:
        raise ValueError(f"{name} timeframe must be at least 1 minute")
    return timeframe


def ask_mode():
    value = input("Mode: replay or instant [instant]: ").strip().lower()
    if not value:
        return "instant"
    if value in {"replay", "live", "live-like", "1"}:
        return "replay"
    return "instant"


def floor_time(dt, timeframe_minutes):
    day_start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    minutes_since_start = dt.hour * 60 + dt.minute
    candle_start = minutes_since_start - (minutes_since_start % int(timeframe_minutes))
    return day_start + timedelta(minutes=candle_start)


def aggregate_candles(candles, timeframe_minutes):
    if int(timeframe_minutes) <= 1:
        return list(candles)
    aggregated = []
    current = None
    for candle in candles:
        bucket_time = floor_time(candle["time"], timeframe_minutes)
        if current is None or current["time"] != bucket_time:
            if current is not None:
                aggregated.append(current)
            current = {
                "time": bucket_time,
                "open": candle["open"],
                "high": candle["high"],
                "low": candle["low"],
                "close": candle["close"],
            }
            continue
        current["high"] = max(current["high"], candle["high"])
        current["low"] = min(current["low"], candle["low"])
        current["close"] = candle["close"]
    if current is not None:
        aggregated.append(current)
    return aggregated


def fetch_1min_history(dhan, instrument, start_dt, end_dt, warmup_days=10):
    from_date = (start_dt - timedelta(days=warmup_days)).date().isoformat()
    to_date = end_dt.date().isoformat()
    security_id = _security_id_for_quote(instrument["security_id"])
    exchange_segment = instrument["exchange_segment"]
    last_error = None

    for instrument_type in _history_instrument_type_candidates(instrument):
        print(f"Fetching 1-minute history: {exchange_segment} {instrument_type} {from_date} to {to_date}")
        response = dhan.intraday_minute_data(
            security_id,
            exchange_segment,
            instrument_type,
            from_date,
            to_date,
            interval=1,
        )
        if not isinstance(response, dict) or response.get("status") != "success":
            last_error = response.get("remarks") if isinstance(response, dict) else response
            print(f"History attempt failed for {instrument_type}: {last_error}")
            continue
        candles = _parse_intraday_data(response.get("data"))
        candles = [candle for candle in candles if start_dt - timedelta(days=warmup_days) <= candle["time"] <= end_dt]
        if candles:
            return candles
        last_error = "Dhan returned no candles in requested range"
    raise RuntimeError(f"Could not fetch historical candles: {last_error}")


def mac_channel(candles):
    high = indicators.compute_sma_high(candles)
    low = indicators.compute_sma_low(candles)
    if high is None or low is None:
        return None
    return {"high": high, "low": low}


def append_candle(rows, candle):
    rows.append({
        "open": float(candle["open"]),
        "high": float(candle["high"]),
        "low": float(candle["low"]),
        "close": float(candle["close"]),
    })


def last_close(rows):
    if not rows:
        return None
    return float(rows[-1]["close"])


def crossed_above(previous_close, current_close, level):
    return previous_close is not None and float(previous_close) <= float(level) and float(current_close) > float(level)


def crossed_below(previous_close, current_close, level):
    return previous_close is not None and float(previous_close) >= float(level) and float(current_close) < float(level)


def close_position(position, exit_time, exit_price, reason):
    return Trade(
        side=position["side"],
        entry_time=position["entry_time"],
        entry_price=position["entry_price"],
        exit_time=exit_time,
        exit_price=float(exit_price),
        quantity=position["quantity"],
        reason=reason,
    )


def run_backtest(config, one_min_candles, start_dt, end_dt, long_timeframe, short_timeframe, mode):
    long_timeframe = validate_timeframe("Long", long_timeframe)
    short_timeframe = validate_timeframe("Short", short_timeframe)
    config = copy.deepcopy(config)
    config["live_orders"] = False
    config.setdefault("candles", {})["timeframe_minutes"] = int(long_timeframe)
    config.setdefault("candles", {})["short_timeframe_minutes"] = int(short_timeframe)

    indicators.MA_PERIOD = int(config.get("moving_average_channel", {}).get("length", 20))
    instrument = get_instrument(config)
    enabled_sides = set(config.get("entry", {}).get("enabled_sides", ["LONG", "SHORT"]))
    exit_config = config.get("exit", {})

    long_store = CandleStore(timeframe_minutes=long_timeframe, history_len=1000)
    short_store = CandleStore(timeframe_minutes=short_timeframe, history_len=1000)
    long_rows = []
    short_rows = []
    trades = []
    position = None

    replay_delay = 0.0
    if mode == "replay":
        replay_delay = float(input("Replay delay per historical minute in seconds [0.05]: ").strip() or "0.05")

    for tick in one_min_candles:
        tick_time = tick["time"]
        ltp = float(tick["close"])
        completed_long = long_store.update(ltp, tick_time=tick_time)
        completed_short = short_store.update(ltp, tick_time=tick_time)

        if position is not None:
            exit_reason = should_exit(position, ltp, exit_config)
            if exit_reason and tick_time >= start_dt:
                trade = close_position(position, tick_time, ltp, exit_reason)
                trades.append(trade)
                if mode == "replay":
                    print_trade("EXIT", trade)
                position = None

        if completed_long is not None:
            previous = last_close(long_rows)
            append_candle(long_rows, completed_long)
            channel = mac_channel(long_rows)
            current = float(completed_long["close"])
            if mode == "replay" and tick_time >= start_dt:
                print(f"[{tick_time}] LONG TF CLOSE {current} MAC={channel}")
            if tick_time >= start_dt and channel and position is not None and position["side"] == "LONG":
                if current < channel["low"]:
                    trade = close_position(position, tick_time, current, "MAC_BREAK_LOWER")
                    trades.append(trade)
                    if mode == "replay":
                        print_trade("EXIT", trade)
                    position = None
            if tick_time >= start_dt and channel and position is None:
                if crossed_above(previous, current, channel["high"]) and "LONG" in enabled_sides:
                    quantity = get_quantity(config, ltp=ltp)
                    position = {
                        "side": "LONG",
                        "entry_time": tick_time,
                        "entry_price": ltp,
                        "quantity": quantity,
                    }
                    if mode == "replay":
                        print(f"[{tick_time}] ENTRY LONG price={ltp} qty={quantity}")

        if completed_short is not None:
            previous = last_close(short_rows)
            append_candle(short_rows, completed_short)
            channel = mac_channel(short_rows)
            current = float(completed_short["close"])
            if mode == "replay" and tick_time >= start_dt:
                print(f"[{tick_time}] SHORT TF CLOSE {current} MAC={channel}")
            if tick_time >= start_dt and channel and position is not None and position["side"] == "SHORT":
                if current > channel["high"]:
                    trade = close_position(position, tick_time, current, "MAC_BREAK_UPPER")
                    trades.append(trade)
                    if mode == "replay":
                        print_trade("EXIT", trade)
                    position = None
            if tick_time >= start_dt and channel and position is None:
                if crossed_below(previous, current, channel["low"]) and "SHORT" in enabled_sides:
                    quantity = get_quantity(config, ltp=ltp)
                    position = {
                        "side": "SHORT",
                        "entry_time": tick_time,
                        "entry_price": ltp,
                        "quantity": quantity,
                    }
                    if mode == "replay":
                        print(f"[{tick_time}] ENTRY SHORT price={ltp} qty={quantity}")

        if mode == "replay" and tick_time >= start_dt:
            time.sleep(replay_delay)

    if position is not None:
        final_price = float(one_min_candles[-1]["close"])
        trade = close_position(position, end_dt, final_price, "END_OF_BACKTEST")
        trades.append(trade)
        if mode == "replay":
            print_trade("EXIT", trade)

    return {
        "instrument": instrument,
        "start": start_dt,
        "end": end_dt,
        "long_timeframe": long_timeframe,
        "short_timeframe": short_timeframe,
        "trades": trades,
    }


def print_trade(label, trade):
    print(
        f"{label} {trade.side} entry={trade.entry_price} exit={trade.exit_price} "
        f"points={trade.points:.2f} pnl={trade.pnl:.2f} reason={trade.reason}"
    )


def print_summary(result):
    trades = result["trades"]
    total_pnl = sum(trade.pnl for trade in trades)
    winners = [trade for trade in trades if trade.pnl > 0]
    losers = [trade for trade in trades if trade.pnl < 0]

    print("\n===== BACKTEST SUMMARY =====")
    print(f"Instrument: {result['instrument'].get('tradingsymbol', result['instrument']['security_id'])}")
    print(f"Range: {result['start']} -> {result['end']}")
    print(f"Long timeframe: {result['long_timeframe']} min")
    print(f"Short timeframe: {result['short_timeframe']} min")
    print(f"Trades: {len(trades)}")
    print(f"Winners: {len(winners)}")
    print(f"Losers: {len(losers)}")
    print(f"Total PnL: {total_pnl:.2f}")
    if trades:
        print("\nTrades:")
        for idx, trade in enumerate(trades, 1):
            print(
                f"{idx}. {trade.side} entry_time={trade.entry_time} entry={trade.entry_price} "
                f"exit_time={trade.exit_time} exit={trade.exit_price} "
                f"points={trade.points:.2f} pnl={trade.pnl:.2f} reason={trade.reason}"
            )


def main():
    config = load_config()
    start_dt = parse_datetime("Start date/time: ")
    end_dt = parse_datetime("End date/time: ")
    if end_dt <= start_dt:
        raise ValueError("End date/time must be after start date/time")

    candle_config = config.get("candles", {})
    long_timeframe = ask_int("Long timeframe minutes", candle_config.get("timeframe_minutes", 5))
    short_timeframe = ask_int("Short timeframe minutes", candle_config.get("short_timeframe_minutes", 3))
    long_timeframe = validate_timeframe("Long", long_timeframe)
    short_timeframe = validate_timeframe("Short", short_timeframe)
    mode = ask_mode()

    dhan = create_dhan(config)
    instrument = get_instrument(config)
    candles = fetch_1min_history(dhan, instrument, start_dt, end_dt)
    if not candles:
        raise RuntimeError("No historical candles found")

    print(f"Loaded {len(candles)} one-minute candles")
    print(f"Building {long_timeframe}-minute long candles and {short_timeframe}-minute short candles from 1-minute history")
    result = run_backtest(config, candles, start_dt, end_dt, long_timeframe, short_timeframe, mode)
    print_summary(result)


if __name__ == "__main__":
    main()
