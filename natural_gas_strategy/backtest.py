import copy
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
natural_gas_strategyDIR = ROOT / "natural_gas_strategy"
if str(natural_gas_strategyDIR) not in sys.path:
    sys.path.insert(0, str(natural_gas_strategyDIR))

from config_loader import get_instrument, get_quantity
from exit_manager import should_exit
import indicators
from market_data import (
    _history_instrument_type_candidates,
    _parse_intraday_data,
    _security_id_for_quote,
    create_dhan,
)


DERIVATIVE_SEGMENTS = {"NSE_FNO", "BSE_FNO", "MCX_COMM", "NSE_CURRENCY", "BSE_CURRENCY"}


@dataclass
class Trade:
    side: str
    entry_time: datetime
    entry_price: float
    exit_time: datetime | None
    exit_price: float | None
    quantity: int
    reason: str | None = None
    margin_quantity: int | None = None
    margin_required: float | None = None
    margin_response: object | None = None
    trend_quantity_reason: str | None = None
    trend_mac_high: float | None = None
    trend_mac_low: float | None = None
    trend_ltp: float | None = None
    base_quantity: int | None = None
    raw_quantity: int | None = None

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
    with open(natural_gas_strategyDIR / "strategy_config.json", "r", encoding="utf-8") as config_file:
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


def parse_trade_time(value, setting_name):
    """Parse a daily backtest-window time such as ``09:15`` or ``23:30``."""
    if value in (None, ""):
        return None
    value = str(value).strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"backtest.{setting_name} must use HH:MM, for example 09:15")


def get_backtest_trade_window(config):
    settings = config.get("backtest", {})
    start = parse_trade_time(settings.get("trade_start_time"), "trade_start_time")
    stop = parse_trade_time(settings.get("trade_stop_time"), "trade_stop_time")
    if (start is None) != (stop is None):
        raise ValueError("Set both backtest.trade_start_time and backtest.trade_stop_time, or neither")
    if start == stop and start is not None:
        raise ValueError("backtest trade start and stop times must be different")
    return start, stop


def is_in_backtest_trade_window(value, start, stop):
    """Return whether a timestamp is inside the daily entry window.

    The stop time is exclusive so a position is closed at that boundary.  A
    start later than stop represents an overnight MCX-style trading window.
    """
    if start is None:
        return True
    current = value.time()
    if start < stop:
        return start <= current < stop
    return current >= start or current < stop


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


class HistoricalCandleAggregator:
    """Build complete timeframe candles from Dhan's 1-minute OHLC candles.

    Dhan timestamps a minute candle at its start.  Its close is only known at
    the end of that minute, so this aggregator completes a timeframe candle
    only after the final source minute has closed.
    """

    def __init__(self, timeframe_minutes):
        self.timeframe_minutes = int(timeframe_minutes)
        self.current = None

    def update(self, source_candle):
        source_time = source_candle["time"]
        bucket = floor_time(source_time, self.timeframe_minutes)
        completed = None

        if self.current is not None and self.current["time"] != bucket:
            # Handle a gap in the source history without silently discarding
            # the previous partial candle.
            completed = self.current
            self.current = None

        if self.current is None:
            self.current = {
                "time": bucket,
                "open": float(source_candle["open"]),
                "high": float(source_candle["high"]),
                "low": float(source_candle["low"]),
                "close": float(source_candle["close"]),
            }
        else:
            self.current["high"] = max(self.current["high"], float(source_candle["high"]))
            self.current["low"] = min(self.current["low"], float(source_candle["low"]))
            self.current["close"] = float(source_candle["close"])

        source_close_time = source_time + timedelta(minutes=1)
        if floor_time(source_close_time, self.timeframe_minutes) != bucket:
            completed = self.current
            self.current = None
        return completed


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


def jumped_above(previous_close, current_open, level):
    return previous_close is not None and float(previous_close) < float(level) and float(current_open) > float(level)


def jumped_below(previous_close, current_open, level):
    return previous_close is not None and float(previous_close) > float(level) and float(current_open) < float(level)


def entry_price_confirms_signal(side, price, channel):
    if side == "LONG":
        return float(price) > float(channel["high"])
    return float(price) < float(channel["low"])


def entry_price_confirmation_blocks(entry_config):
    mode = str(entry_config.get("price_confirmation", "warn")).lower()
    return mode in {"block", "strict", "true", "yes", "1"}


def _number_from(data, keys):
    if not isinstance(data, dict):
        return None
    for key in keys:
        if key in data and data[key] not in (None, ""):
            try:
                return float(data[key])
            except (TypeError, ValueError):
                return None
    return None


def _margin_quantity(config, instrument, quantity):
    safety = config.get("live_safety", {})
    mode = str(safety.get("margin_quantity_mode", "auto")).lower()
    segment = str(instrument.get("exchange_segment", "")).upper()
    lot_size = int(instrument.get("lot_size", 1))
    quantity = int(quantity)

    if mode == "order_quantity":
        return quantity
    if mode == "lots" or (mode == "auto" and segment in DERIVATIVE_SEGMENTS):
        if lot_size <= 0 or quantity % lot_size != 0:
            raise ValueError(f"Cannot convert quantity {quantity} to lots using lot_size {lot_size}")
        return max(quantity // lot_size, 1)
    return quantity


def calculate_entry_margin(dhan, config, instrument, side, quantity, price):
    margin_quantity = _margin_quantity(config, instrument, quantity)
    if dhan is None or not hasattr(dhan, "margin_calculator"):
        return {
            "margin_quantity": int(margin_quantity),
            "margin_required": None,
            "margin_response": "Dhan margin_calculator unavailable",
        }

    transaction_type = "BUY" if side == "LONG" else "SELL"
    try:
        response = dhan.margin_calculator(
            security_id=instrument["security_id"],
            exchange_segment=instrument["exchange_segment"],
            transaction_type=transaction_type,
            quantity=int(margin_quantity),
            product_type=instrument.get("product_type", "INTRADAY"),
            price=float(price),
        )
        data = response.get("data", response) if isinstance(response, dict) else response
        margin_required = _number_from(
            data,
            ("totalMargin", "total_margin", "requiredMargin", "required_margin"),
        )
        return {
            "margin_quantity": int(margin_quantity),
            "margin_required": margin_required,
            "margin_response": response,
        }
    except Exception as exc:
        return {
            "margin_quantity": int(margin_quantity),
            "margin_required": None,
            "margin_response": f"Margin calculator error: {exc}",
        }


def _round_to_tradable_quantity(quantity, instrument):
    lot_size = int(instrument.get("lot_size", 1))
    quantity = int(quantity)
    if lot_size <= 1:
        return max(quantity, 1)
    rounded = (quantity // lot_size) * lot_size
    return max(rounded, lot_size)


def trend_quantity_decision(config, instrument, side, ltp, trend_channel, trend_config):
    base_quantity = get_quantity(config, ltp=ltp)
    if not trend_config.get("enabled", True) or trend_channel is None:
        return {
            "quantity": base_quantity,
            "base_quantity": base_quantity,
            "raw_quantity": base_quantity,
            "trend_quantity_reason": "disabled_or_mac_not_ready",
            "trend_mac_high": None if trend_channel is None else trend_channel["high"],
            "trend_mac_low": None if trend_channel is None else trend_channel["low"],
            "trend_ltp": float(ltp),
        }

    ratio = float(trend_config.get("half_quantity_ratio", 0.5))
    opposite_mode = str(trend_config.get("opposite_quantity_mode", "half")).lower()
    raw_quantity = base_quantity
    reason = "normal_between_or_same_direction"
    if float(ltp) > float(trend_channel["high"]) and side == "SHORT":
        if opposite_mode in {"zero", "skip", "none", "0"}:
            raw_quantity = 0
            reason = "one_hour_ltp_above_mac_high_short_zero"
        else:
            raw_quantity = int(base_quantity * ratio)
            reason = "one_hour_ltp_above_mac_high_short_half"
    elif float(ltp) < float(trend_channel["low"]) and side == "LONG":
        if opposite_mode in {"zero", "skip", "none", "0"}:
            raw_quantity = 0
            reason = "one_hour_ltp_below_mac_low_long_zero"
        else:
            raw_quantity = int(base_quantity * ratio)
            reason = "one_hour_ltp_below_mac_low_long_half"
    elif float(ltp) > float(trend_channel["high"]) and side == "LONG":
        reason = "one_hour_ltp_above_mac_high_long_normal"
    elif float(ltp) < float(trend_channel["low"]) and side == "SHORT":
        reason = "one_hour_ltp_below_mac_low_short_normal"

    return {
        "quantity": 0 if raw_quantity <= 0 else _round_to_tradable_quantity(raw_quantity, instrument),
        "base_quantity": base_quantity,
        "raw_quantity": raw_quantity,
        "trend_quantity_reason": reason,
        "trend_mac_high": trend_channel["high"],
        "trend_mac_low": trend_channel["low"],
        "trend_ltp": float(ltp),
    }


def quantity_with_trend_filter(config, instrument, side, ltp, trend_channel, trend_config):
    return trend_quantity_decision(
        config,
        instrument,
        side,
        ltp,
        trend_channel,
        trend_config,
    )["quantity"]


def close_position(position, exit_time, exit_price, reason):
    return Trade(
        side=position["side"],
        entry_time=position["entry_time"],
        entry_price=position["entry_price"],
        exit_time=exit_time,
        exit_price=float(exit_price),
        quantity=position["quantity"],
        reason=reason,
        margin_quantity=position.get("margin_quantity"),
        margin_required=position.get("margin_required"),
        margin_response=position.get("margin_response"),
        trend_quantity_reason=position.get("trend_quantity_reason"),
        trend_mac_high=position.get("trend_mac_high"),
        trend_mac_low=position.get("trend_mac_low"),
        trend_ltp=position.get("trend_ltp"),
        base_quantity=position.get("base_quantity"),
        raw_quantity=position.get("raw_quantity"),
    )


def position_pnl(position, price):
    if position is None or price is None:
        return 0.0
    points = float(price) - float(position["entry_price"])
    if position["side"] == "SHORT":
        points = -points
    return points * int(position["quantity"])


def daily_target_limit(daily_target_config):
    if not daily_target_config.get("enabled", False):
        return 0.0
    return float(daily_target_config.get("target_pnl", 0) or 0)


def daily_target_status(realized_pnl, position, ltp, daily_target_config):
    target = daily_target_limit(daily_target_config)
    unrealized_pnl = 0.0
    if daily_target_config.get("include_unrealized", True):
        unrealized_pnl = position_pnl(position, ltp)
    total_pnl = float(realized_pnl) + unrealized_pnl
    return {
        "enabled": bool(daily_target_config.get("enabled", False)),
        "target_pnl": target,
        "realized_pnl": float(realized_pnl),
        "unrealized_pnl": unrealized_pnl,
        "total_pnl": total_pnl,
        "hit": target > 0 and total_pnl >= target,
    }


def record_closed_trade(trades, trade, realized_pnl_by_day, daily_target_config):
    trades.append(trade)
    if trade.exit_time is None:
        return {"hit": False}
    day = trade.exit_time.date()
    realized_pnl_by_day[day] = realized_pnl_by_day.get(day, 0.0) + trade.pnl
    return daily_target_status(realized_pnl_by_day[day], None, trade.exit_price, daily_target_config)


def run_backtest(config, one_min_candles, start_dt, end_dt, long_timeframe, short_timeframe, mode, dhan=None):
    long_timeframe = validate_timeframe("Long", long_timeframe)
    short_timeframe = validate_timeframe("Short", short_timeframe)
    config = copy.deepcopy(config)
    config["live_orders"] = False
    config.setdefault("candles", {})["timeframe_minutes"] = int(long_timeframe)
    config.setdefault("candles", {})["short_timeframe_minutes"] = int(short_timeframe)

    indicators.MA_PERIOD = int(config.get("moving_average_channel", {}).get("length", 20))
    instrument = get_instrument(config)
    entry_config = config.get("entry", {})
    enabled_sides = set(config.get("entry", {}).get("enabled_sides", ["LONG", "SHORT"]))
    exit_config = config.get("exit", {})
    trend_config = config.get("long_short_quantity_checker", {})
    daily_target_config = config.get("daily_target", {})
    trend_timeframe = int(trend_config.get("timeframe_minutes", 60))
    trade_start_time, trade_stop_time = get_backtest_trade_window(config)

    long_store = HistoricalCandleAggregator(long_timeframe)
    short_store = HistoricalCandleAggregator(short_timeframe)
    trend_store = HistoricalCandleAggregator(trend_timeframe)
    long_rows = []
    short_rows = []
    trend_rows = []
    trend_channel = None
    trades = []
    position = None
    current_trade_day = None
    realized_pnl_by_day = {}
    daily_target_reached = False
    daily_target_skipped_signals = 0
    pending_entry = None

    replay_delay = 0.0
    if mode == "replay":
        replay_delay = float(input("Replay delay per historical minute in seconds [0.05]: ").strip() or "0.05")

    for tick in one_min_candles:
        source_time = tick["time"]
        # The close of a Dhan minute candle is available only one minute after
        # its start timestamp.  All close-based decisions use this time.
        tick_time = source_time + timedelta(minutes=1)
        ltp = float(tick["close"])

        # A MAC signal generated on the preceding completed candle can first
        # be executed at this minute's open.  This prevents look-ahead fills
        # at a candle close that was not known when the order would be sent.
        if (
            pending_entry is not None
            and position is None
            and is_in_backtest_trade_window(source_time, trade_start_time, trade_stop_time)
        ):
            pending = pending_entry
            pending_entry = None
            fill_price = float(tick["open"])
            if (
                not daily_target_reached
                and entry_price_confirms_signal(pending["side"], fill_price, pending["channel"])
            ):
                trend_decision = trend_quantity_decision(
                    config,
                    instrument,
                    pending["side"],
                    fill_price,
                    trend_channel,
                    trend_config,
                )
                quantity = trend_decision["quantity"]
                if quantity > 0:
                    margin = calculate_entry_margin(
                        dhan, config, instrument, pending["side"], quantity, fill_price
                    )
                    position = {
                        "side": pending["side"],
                        "entry_time": source_time,
                        "entry_price": fill_price,
                        "quantity": quantity,
                        **trend_decision,
                        **margin,
                    }
                    if mode == "replay":
                        print(
                            f"[{source_time}] ENTRY {pending['side']} price={fill_price} "
                            f"qty={quantity} signal_time={pending['signal_time']}"
                        )
        elif pending_entry is not None:
            # Do not carry an unfilled signal into the next trading window.
            pending_entry = None

        completed_long = long_store.update(tick)
        completed_short = short_store.update(tick)
        completed_trend = trend_store.update(tick)

        if tick_time >= start_dt and current_trade_day != tick_time.date():
            current_trade_day = tick_time.date()
            daily_status = daily_target_status(
                realized_pnl_by_day.get(current_trade_day, 0.0),
                None,
                ltp,
                daily_target_config,
            )
            daily_target_reached = daily_status["hit"]
            if mode == "replay" and daily_target_limit(daily_target_config) > 0:
                print(f"[{tick_time}] DAILY TARGET RESET/STATUS {daily_status}")

        in_trade_window = is_in_backtest_trade_window(tick_time, trade_start_time, trade_stop_time)
        if tick_time >= start_dt and position is not None and not in_trade_window:
            trade = close_position(position, tick_time, ltp, "BACKTEST_TIME_STOP")
            record_closed_trade(trades, trade, realized_pnl_by_day, daily_target_config)
            if mode == "replay":
                print_trade("EXIT", trade)
            position = None

        if completed_trend is not None:
            append_candle(trend_rows, completed_trend)
            trend_channel = mac_channel(trend_rows)
            if mode == "replay" and tick_time >= start_dt:
                print(f"[{tick_time}] TREND TF CLOSE {completed_trend['close']} MAC={trend_channel}")

        if tick_time >= start_dt and position is not None:
            daily_status = daily_target_status(
                realized_pnl_by_day.get(tick_time.date(), 0.0),
                position,
                ltp,
                daily_target_config,
            )
            if daily_status["hit"]:
                daily_target_reached = True
                if daily_target_config.get("close_position_when_hit", True):
                    trade = close_position(position, tick_time, ltp, "DAILY_TARGET")
                    record_closed_trade(trades, trade, realized_pnl_by_day, daily_target_config)
                    if mode == "replay":
                        print_trade("EXIT", trade)
                    position = None

        if position is not None:
            exit_reason = should_exit(position, ltp, exit_config)
            if exit_reason and tick_time >= start_dt:
                trade = close_position(position, tick_time, ltp, exit_reason)
                daily_status = record_closed_trade(trades, trade, realized_pnl_by_day, daily_target_config)
                daily_target_reached = daily_target_reached or daily_status["hit"]
                if mode == "replay":
                    print_trade("EXIT", trade)
                position = None

        if completed_long is not None:
            previous = last_close(long_rows)
            append_candle(long_rows, completed_long)
            channel = mac_channel(long_rows)
            current_open = float(completed_long["open"])
            current = float(completed_long["close"])
            if mode == "replay" and tick_time >= start_dt:
                print(f"[{tick_time}] LONG TF CLOSE {current} MAC={channel}")
            if tick_time >= start_dt and channel and position is not None and position["side"] == "LONG":
                if current < channel["low"]:
                    trade = close_position(position, tick_time, current, "MAC_BREAK_LOWER")
                    daily_status = record_closed_trade(trades, trade, realized_pnl_by_day, daily_target_config)
                    daily_target_reached = daily_target_reached or daily_status["hit"]
                    if mode == "replay":
                        print_trade("EXIT", trade)
                    position = None
            if (
                tick_time >= start_dt
                and in_trade_window
                and channel
                and position is None
                and pending_entry is None
            ):
                signal = (
                    crossed_above(previous, current, channel["high"])
                    or jumped_above(previous, current_open, channel["high"])
                ) and "LONG" in enabled_sides
                if signal and not entry_price_confirms_signal("LONG", ltp, channel) and entry_price_confirmation_blocks(entry_config):
                    signal = False
                if signal:
                    if daily_target_reached and daily_target_config.get("block_new_entries_after_hit", True):
                        daily_target_skipped_signals += 1
                        if mode == "replay":
                            print(f"[{tick_time}] SKIP LONG daily target reached")
                    else:
                        pending_entry = {
                            "side": "LONG",
                            "channel": channel,
                            "signal_time": tick_time,
                        }
                        if mode == "replay":
                            print(f"[{tick_time}] SIGNAL LONG; filling at next 1-minute open")

        if completed_short is not None:
            previous = last_close(short_rows)
            append_candle(short_rows, completed_short)
            channel = mac_channel(short_rows)
            current_open = float(completed_short["open"])
            current = float(completed_short["close"])
            if mode == "replay" and tick_time >= start_dt:
                print(f"[{tick_time}] SHORT TF CLOSE {current} MAC={channel}")
            if tick_time >= start_dt and channel and position is not None and position["side"] == "SHORT":
                if current > channel["high"]:
                    trade = close_position(position, tick_time, current, "MAC_BREAK_UPPER")
                    daily_status = record_closed_trade(trades, trade, realized_pnl_by_day, daily_target_config)
                    daily_target_reached = daily_target_reached or daily_status["hit"]
                    if mode == "replay":
                        print_trade("EXIT", trade)
                    position = None
            if (
                tick_time >= start_dt
                and in_trade_window
                and channel
                and position is None
                and pending_entry is None
            ):
                signal = (
                    crossed_below(previous, current, channel["low"])
                    or jumped_below(previous, current_open, channel["low"])
                ) and "SHORT" in enabled_sides
                if signal and not entry_price_confirms_signal("SHORT", ltp, channel) and entry_price_confirmation_blocks(entry_config):
                    signal = False
                if signal:
                    if daily_target_reached and daily_target_config.get("block_new_entries_after_hit", True):
                        daily_target_skipped_signals += 1
                        if mode == "replay":
                            print(f"[{tick_time}] SKIP SHORT daily target reached")
                    else:
                        pending_entry = {
                            "side": "SHORT",
                            "channel": channel,
                            "signal_time": tick_time,
                        }
                        if mode == "replay":
                            print(f"[{tick_time}] SIGNAL SHORT; filling at next 1-minute open")

        if mode == "replay" and tick_time >= start_dt:
            time.sleep(replay_delay)

    if position is not None:
        final_price = float(one_min_candles[-1]["close"])
        final_time = one_min_candles[-1]["time"] + timedelta(minutes=1)
        trade = close_position(position, final_time, final_price, "END_OF_BACKTEST")
        record_closed_trade(trades, trade, realized_pnl_by_day, daily_target_config)
        if mode == "replay":
            print_trade("EXIT", trade)

    return {
        "instrument": instrument,
        "start": start_dt,
        "end": end_dt,
        "long_timeframe": long_timeframe,
        "short_timeframe": short_timeframe,
        "trend_timeframe": trend_timeframe,
        "trade_start_time": None if trade_start_time is None else trade_start_time.isoformat(),
        "trade_stop_time": None if trade_stop_time is None else trade_stop_time.isoformat(),
        "daily_target": daily_target_config,
        "daily_target_skipped_signals": daily_target_skipped_signals,
        "realized_pnl_by_day": realized_pnl_by_day,
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
    margins = [trade.margin_required for trade in trades if trade.margin_required is not None]

    print("\n===== BACKTEST SUMMARY =====")
    print(f"Instrument: {result['instrument'].get('tradingsymbol', result['instrument']['security_id'])}")
    print(f"Range: {result['start']} -> {result['end']}")
    print(f"Long timeframe: {result['long_timeframe']} min")
    print(f"Short timeframe: {result['short_timeframe']} min")
    print(f"Trend quantity timeframe: {result['trend_timeframe']} min")
    if result.get("trade_start_time") is not None:
        print(f"Daily trade window: {result['trade_start_time']} to {result['trade_stop_time']} (stop exclusive)")
    daily_target = result.get("daily_target", {})
    daily_target_value = daily_target_limit(daily_target)
    if daily_target.get("enabled", False) and daily_target_value > 0:
        print(f"Daily target PnL: {daily_target_value:.2f}")
        print(f"Signals skipped after daily target: {result.get('daily_target_skipped_signals', 0)}")
    print(f"Trades: {len(trades)}")
    print(f"Winners: {len(winners)}")
    print(f"Losers: {len(losers)}")
    print(f"Total PnL: {total_pnl:.2f}")
    if margins:
        print(f"Max Dhan margin required: {max(margins):.2f}")
    realized_pnl_by_day = result.get("realized_pnl_by_day", {})
    if realized_pnl_by_day:
        print("\nDaily realized PnL:")
        for day in sorted(realized_pnl_by_day):
            print(f"{day}: {realized_pnl_by_day[day]:.2f}")
    if trades:
        print("\nTrades:")
        for idx, trade in enumerate(trades, 1):
            print(
                f"{idx}. {trade.side} entry_time={trade.entry_time} entry={trade.entry_price} "
                f"exit_time={trade.exit_time} exit={trade.exit_price} "
                f"points={trade.points:.2f} pnl={trade.pnl:.2f} "
                f"base_qty={trade.base_quantity} raw_qty={trade.raw_quantity} "
                f"margin_qty={trade.margin_quantity} margin_required={trade.margin_required} "
                f"trend_ltp={trade.trend_ltp} trend_high={trade.trend_mac_high} "
                f"trend_low={trade.trend_mac_low} trend_reason={trade.trend_quantity_reason} "
                f"reason={trade.reason}"
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
    result = run_backtest(config, candles, start_dt, end_dt, long_timeframe, short_timeframe, mode, dhan=dhan)
    print_summary(result)


if __name__ == "__main__":
    main()
