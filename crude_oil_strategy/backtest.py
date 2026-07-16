import copy
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


STRATEGY_DIR = Path(__file__).resolve().parent
if str(STRATEGY_DIR) not in sys.path:
    sys.path.insert(0, str(STRATEGY_DIR))

from candles import CandleStore
from config_loader import get_instrument, get_quantity
from exit_manager import daily_pnl_status, should_exit
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
                    return parsed.replace(hour=0, minute=0, second=0), True
                return parsed, False
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
    # MCX evening-session candles can require the next API date to appear in
    # Dhan's intraday response, even when their candle timestamps are same-day.
    to_date = (end_dt + timedelta(days=1)).date().isoformat()
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
    )


def candle_stop_loss(side, candles, entry_candle_index, current_candle_index, buffer_points):
    if entry_candle_index is None or current_candle_index is None or not candles:
        return None

    entry_candle_index = int(entry_candle_index)
    current_candle_index = int(current_candle_index)
    candle_number = current_candle_index - entry_candle_index + 1
    if candle_number <= 0:
        return None

    if candle_number <= 3:
        reference_index = entry_candle_index
    else:
        reference_index = current_candle_index - 2

    if reference_index < 0 or reference_index >= len(candles):
        return None

    reference_candle = candles[reference_index]
    if side == "LONG":
        return float(reference_candle["low"]) - float(buffer_points)
    return float(reference_candle["high"]) + float(buffer_points)


def update_candle_stop(position, candles, current_candle_index, buffer_points):
    if position is None:
        return None
    stop_loss = candle_stop_loss(
        position["side"],
        candles,
        position.get("entry_candle_index"),
        current_candle_index,
        buffer_points,
    )
    if stop_loss is not None:
        position["stop_loss"] = stop_loss
    return stop_loss


def candle_stop_exit(position, close_price):
    if position is None or close_price is None or position.get("stop_loss") is None:
        return None
    if position["side"] == "LONG" and float(close_price) <= float(position["stop_loss"]):
        return "CANDLE_STOP_LOWER"
    if position["side"] == "SHORT" and float(close_price) >= float(position["stop_loss"]):
        return "CANDLE_STOP_UPPER"
    return None


def parse_trade_time(value, name):
    if value in (None, ""):
        return None
    try:
        return datetime.strptime(str(value), "%H:%M").time()
    except ValueError as exc:
        raise ValueError(f"backtest.{name} must use HH:MM, for example 09:00") from exc


def in_trade_window(moment, start_time, stop_time):
    """Return whether a timestamp is inside a normal or overnight trade window."""
    if start_time is None or stop_time is None:
        return True
    current_time = moment.time()
    if start_time <= stop_time:
        return start_time <= current_time < stop_time
    return current_time >= start_time or current_time < stop_time


def build_daily_stats(trades):
    daily = {}
    for trade in trades:
        trade_day = trade.exit_time.date().isoformat()
        stats = daily.setdefault(trade_day, {"pnl": 0.0, "trades": 0, "winners": 0, "losers": 0, "breakeven": 0})
        stats["pnl"] += trade.pnl
        stats["trades"] += 1
        if trade.pnl > 0:
            stats["winners"] += 1
        elif trade.pnl < 0:
            stats["losers"] += 1
        else:
            stats["breakeven"] += 1
    return daily


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
    enabled_sides = set(entry_config.get("enabled_sides", ["LONG", "SHORT"]))
    exit_config = config.get("exit", {})
    daily_pnl_config = config.get("daily_pnl", {})
    backtest_config = config.get("backtest", {})
    trade_start_time = parse_trade_time(backtest_config.get("trade_start_time"), "trade_start_time")
    trade_stop_time = parse_trade_time(backtest_config.get("trade_stop_time"), "trade_stop_time")
    candle_stop_points = float(exit_config.get("candle_stop_points", 3))

    long_store = CandleStore(timeframe_minutes=long_timeframe, history_len=1000)
    short_store = CandleStore(timeframe_minutes=short_timeframe, history_len=1000)
    long_rows = []
    short_rows = []
    trades = []
    position = None
    realized_pnl_by_day = {}
    blocked_days = set()

    def close_and_record(open_position, exit_time, exit_price, reason):
        trade = close_position(open_position, exit_time, exit_price, reason)
        trades.append(trade)
        day_key = exit_time.date().isoformat()
        realized_pnl_by_day[day_key] = realized_pnl_by_day.get(day_key, 0.0) + trade.pnl
        status = daily_pnl_status(realized_pnl_by_day[day_key], None, None, daily_pnl_config)
        if status["hit"]:
            blocked_days.add(day_key)
        if mode == "replay":
            print_trade("EXIT", trade)
        return trade

    replay_delay = 0.0
    if mode == "replay":
        replay_delay = float(input("Replay delay per historical minute in seconds [0.05]: ").strip() or "0.05")

    for tick in one_min_candles:
        tick_time = tick["time"]
        ltp = float(tick["close"])
        day_key = tick_time.date().isoformat()
        trade_window_open = in_trade_window(tick_time, trade_start_time, trade_stop_time)

        if tick_time >= start_dt and position is not None:
            daily_status = daily_pnl_status(
                realized_pnl_by_day.get(day_key, 0.0), position, ltp, daily_pnl_config
            )
            if daily_status["hit"] and bool(daily_pnl_config.get("close_position_when_hit", True)):
                close_and_record(position, tick_time, ltp, daily_status["reason"])
                position = None
            elif not trade_window_open:
                close_and_record(position, tick_time, ltp, "BACKTEST_TIME_STOP")
                position = None
            else:
                point_exit_reason = should_exit(position, ltp, exit_config)
                if point_exit_reason:
                    close_and_record(position, tick_time, ltp, point_exit_reason)
                    position = None

        completed_long = long_store.update(ltp, tick_time=tick_time)
        completed_short = short_store.update(ltp, tick_time=tick_time)

        if completed_long is not None:
            previous = last_close(long_rows)
            append_candle(long_rows, completed_long)
            channel = mac_channel(long_rows)
            current_open = float(completed_long["open"])
            current = float(completed_long["close"])
            current_index = len(long_rows) - 1
            if mode == "replay" and tick_time >= start_dt:
                print(f"[{tick_time}] LONG TF CLOSE {current} MAC={channel}")
            if tick_time >= start_dt and channel and position is not None and position["side"] == "LONG":
                if current < channel["low"]:
                    close_and_record(position, tick_time, current, "MAC_BREAK_LOWER")
                    position = None
                else:
                    stop_loss = update_candle_stop(position, long_rows, current_index, candle_stop_points)
                    if mode == "replay" and stop_loss is not None:
                        print(f"[{tick_time}] LONG STOP {stop_loss}")
                    exit_reason = candle_stop_exit(position, current)
                    if exit_reason:
                        close_and_record(position, tick_time, current, exit_reason)
                        position = None
            if tick_time >= start_dt and trade_window_open and not (
                bool(daily_pnl_config.get("block_new_entries_after_hit", True)) and day_key in blocked_days
            ) and channel and position is None:
                signal = (
                    crossed_above(previous, current, channel["high"])
                    or jumped_above(previous, current_open, channel["high"])
                ) and "LONG" in enabled_sides
                if signal and not entry_price_confirms_signal("LONG", ltp, channel) and entry_price_confirmation_blocks(entry_config):
                    signal = False
                if signal:
                    quantity = get_quantity(config, ltp=ltp)
                    margin = calculate_entry_margin(dhan, config, instrument, "LONG", quantity, ltp)
                    position = {
                        "side": "LONG",
                        "entry_time": tick_time,
                        "entry_price": ltp,
                        "quantity": quantity,
                        "entry_candle_index": current_index,
                        **margin,
                    }
                    update_candle_stop(position, long_rows, current_index, candle_stop_points)
                    if mode == "replay":
                        print(
                            f"[{tick_time}] ENTRY LONG price={ltp} qty={quantity} "
                            f"margin_qty={margin['margin_quantity']} margin_required={margin['margin_required']} "
                            f"stop={position.get('stop_loss')}"
                        )

        if completed_short is not None:
            previous = last_close(short_rows)
            append_candle(short_rows, completed_short)
            channel = mac_channel(short_rows)
            current_open = float(completed_short["open"])
            current = float(completed_short["close"])
            current_index = len(short_rows) - 1
            if mode == "replay" and tick_time >= start_dt:
                print(f"[{tick_time}] SHORT TF CLOSE {current} MAC={channel}")
            if tick_time >= start_dt and channel and position is not None and position["side"] == "SHORT":
                if current > channel["high"]:
                    close_and_record(position, tick_time, current, "MAC_BREAK_UPPER")
                    position = None
                else:
                    stop_loss = update_candle_stop(position, short_rows, current_index, candle_stop_points)
                    if mode == "replay" and stop_loss is not None:
                        print(f"[{tick_time}] SHORT STOP {stop_loss}")
                    exit_reason = candle_stop_exit(position, current)
                    if exit_reason:
                        close_and_record(position, tick_time, current, exit_reason)
                        position = None
            if tick_time >= start_dt and trade_window_open and not (
                bool(daily_pnl_config.get("block_new_entries_after_hit", True)) and day_key in blocked_days
            ) and channel and position is None:
                signal = (
                    crossed_below(previous, current, channel["low"])
                    or jumped_below(previous, current_open, channel["low"])
                ) and "SHORT" in enabled_sides
                if signal and not entry_price_confirms_signal("SHORT", ltp, channel) and entry_price_confirmation_blocks(entry_config):
                    signal = False
                if signal:
                    quantity = get_quantity(config, ltp=ltp)
                    margin = calculate_entry_margin(dhan, config, instrument, "SHORT", quantity, ltp)
                    position = {
                        "side": "SHORT",
                        "entry_time": tick_time,
                        "entry_price": ltp,
                        "quantity": quantity,
                        "entry_candle_index": current_index,
                        **margin,
                    }
                    update_candle_stop(position, short_rows, current_index, candle_stop_points)
                    if mode == "replay":
                        print(
                            f"[{tick_time}] ENTRY SHORT price={ltp} qty={quantity} "
                            f"margin_qty={margin['margin_quantity']} margin_required={margin['margin_required']} "
                            f"stop={position.get('stop_loss')}"
                        )

        if mode == "replay" and tick_time >= start_dt:
            time.sleep(replay_delay)

    if position is not None:
        final_price = float(one_min_candles[-1]["close"])
        close_and_record(position, end_dt, final_price, "END_OF_BACKTEST")

    return {
        "instrument": instrument,
        "start": start_dt,
        "end": end_dt,
        "long_timeframe": long_timeframe,
        "short_timeframe": short_timeframe,
        "mac_length": indicators.MA_PERIOD,
        "candle_stop_points": candle_stop_points,
        "trade_start_time": trade_start_time,
        "trade_stop_time": trade_stop_time,
        "daily_pnl": daily_pnl_config,
        "backtest": backtest_config,
        "daily_stats": build_daily_stats(trades),
        "trades": trades,
    }


def print_trade(label, trade):
    print(
        f"{label} {trade.side} entry={trade.entry_price} exit={trade.exit_price} "
        f"points={trade.points:.2f} pnl={trade.pnl:.2f} reason={trade.reason}"
    )


def save_pnl_graph(result):
    """Save a cumulative realised-P&L graph when enabled in strategy_config.json."""
    graph_config = result.get("backtest", {})
    if not bool(graph_config.get("save_pnl_graph", False)):
        return

    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("P&L graph was requested but matplotlib is not installed.")
        return

    trades = result["trades"]
    cumulative = 0.0
    x_values = []
    y_values = []
    for trade in trades:
        cumulative += trade.pnl
        x_values.append(trade.exit_time)
        y_values.append(cumulative)

    output_path = Path(graph_config.get("pnl_graph_path", "backtest_pnl.png"))
    if not output_path.is_absolute():
        output_path = STRATEGY_DIR / output_path
    plt.figure(figsize=(11, 5))
    plt.plot(x_values, y_values, marker="o", label="Cumulative realised P&L")
    plt.axhline(0, color="black", linewidth=0.8)
    plt.title("Crude Oil Backtest Cumulative P&L")
    plt.xlabel("Trade exit time")
    plt.ylabel("P&L")
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(output_path)
    plt.close()
    print(f"P&L graph saved to: {output_path}")


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
    print(f"MAC length: {result['mac_length']}")
    print(f"Candle stop points: {result['candle_stop_points']}")
    if result["trade_start_time"] is not None and result["trade_stop_time"] is not None:
        print(f"Trade window: {result['trade_start_time'].strftime('%H:%M')} -> {result['trade_stop_time'].strftime('%H:%M')}")
    print(f"Trades: {len(trades)}")
    print(f"Winners: {len(winners)}")
    print(f"Losers: {len(losers)}")
    print(f"Total PnL: {total_pnl:.2f}")
    if margins:
        print(f"Max Dhan margin required: {max(margins):.2f}")
    if result["daily_stats"]:
        print("\nDaily results:")
        for day, stats in sorted(result["daily_stats"].items()):
            print(
                f"{day} pnl={stats['pnl']:.2f} trades={stats['trades']} "
                f"winners={stats['winners']} losers={stats['losers']} breakeven={stats['breakeven']}"
            )
    if trades:
        print("\nTrades:")
        for idx, trade in enumerate(trades, 1):
            print(
                f"{idx}. {trade.side} entry_time={trade.entry_time} entry={trade.entry_price} "
                f"exit_time={trade.exit_time} exit={trade.exit_price} "
                f"points={trade.points:.2f} pnl={trade.pnl:.2f} "
                f"margin_qty={trade.margin_quantity} margin_required={trade.margin_required} "
                f"reason={trade.reason}"
            )
    save_pnl_graph(result)


def main():
    config = load_config()
    start_dt, start_is_date_only = parse_datetime("Start date/time: ")
    end_dt, end_is_date_only = parse_datetime("End date/time: ")

    # A same-day date-only request uses the configured backtest session instead
    # of accidentally producing an empty midnight-to-midnight range.
    if start_is_date_only and end_is_date_only and start_dt.date() == end_dt.date():
        backtest_config = config.get("backtest", {})
        start_time = parse_trade_time(backtest_config.get("trade_start_time"), "trade_start_time")
        stop_time = parse_trade_time(backtest_config.get("trade_stop_time"), "trade_stop_time")
        if start_time is None or stop_time is None:
            raise ValueError(
                "For a same-day date-only backtest, set backtest.trade_start_time "
                "and backtest.trade_stop_time in strategy_config.json."
            )
        start_dt = datetime.combine(start_dt.date(), start_time)
        end_dt = datetime.combine(end_dt.date(), stop_time)
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)
        print(f"Using configured same-day backtest period: {start_dt} -> {end_dt}")

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
