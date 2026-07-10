import argparse
import copy
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path


STRATEGY_DIR = Path(__file__).resolve().parent
if str(STRATEGY_DIR) not in sys.path:
    sys.path.insert(0, str(STRATEGY_DIR))

from backtest import (  # noqa: E402
    append_candle,
    candle_stop_exit,
    close_position,
    crossed_above,
    crossed_below,
    entry_price_confirmation_blocks,
    entry_price_confirms_signal,
    fetch_1min_history,
    jumped_above,
    jumped_below,
    last_close,
    mac_channel,
    update_candle_stop,
    validate_timeframe,
)
from candles import CandleStore  # noqa: E402
from config_loader import get_instrument, get_quantity, load_config  # noqa: E402
from exit_manager import should_exit  # noqa: E402
from market_data import create_dhan  # noqa: E402
import indicators  # noqa: E402


DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%d-%m-%Y %H:%M",
    "%d-%m-%Y %H:%M:%S",
)


def parse_datetime(value):
    value = str(value).strip()
    for fmt in DATETIME_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise ValueError("Use date format like 2026-07-09 15:36")


def ask_datetime(prompt):
    while True:
        try:
            return parse_datetime(input(prompt).strip())
        except ValueError as exc:
            print(exc)


def floor_time(dt, timeframe_minutes):
    day_start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    minutes_since_start = dt.hour * 60 + dt.minute
    candle_start = minutes_since_start - (minutes_since_start % int(timeframe_minutes))
    return day_start + timedelta(minutes=candle_start)


def clean(value):
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean(item) for item in value]
    return value


def print_json(title, data):
    print(f"\n===== {title} =====")
    print(json.dumps(clean(data), indent=2, sort_keys=True))


def history_summary(one_min_candles):
    if not one_min_candles:
        return {
            "count": 0,
            "first": None,
            "last": None,
            "counts_by_date": {},
            "last_20_times": [],
        }

    counts_by_date = {}
    for candle in one_min_candles:
        day = candle["time"].date().isoformat()
        counts_by_date[day] = counts_by_date.get(day, 0) + 1

    return {
        "count": len(one_min_candles),
        "first": one_min_candles[0]["time"],
        "last": one_min_candles[-1]["time"],
        "counts_by_date": counts_by_date,
        "last_20_times": [candle["time"] for candle in one_min_candles[-20:]],
    }


def action_reason(
    side,
    position,
    side_enabled,
    raw_signal,
    price_confirms,
    entry_config,
    before_start,
):
    if before_start:
        return "BEFORE_SIMULATION_START"
    if position is not None:
        return "POSITION_ALREADY_OPEN"
    if not side_enabled:
        return f"{side}_DISABLED_IN_CONFIG"
    if not raw_signal:
        return "NO_CROSS_OR_JUMP"
    if price_confirms is False and entry_price_confirmation_blocks(entry_config):
        return "PRICE_CONFIRMATION_BLOCKED"
    return f"WOULD_ENTER_{side}"


def entry_diagnostic(
    side,
    timeframe,
    completed_candle,
    previous_close,
    channel,
    position,
    enabled_sides,
    entry_config,
    evaluation_time,
    evaluation_ltp,
    start_dt,
):
    current_open = float(completed_candle["open"])
    current_close = float(completed_candle["close"])

    if channel is None:
        return {
            "side": side,
            "timeframe_minutes": timeframe,
            "candle_start": completed_candle["time"],
            "evaluated_at": evaluation_time,
            "candle": completed_candle,
            "previous_close": previous_close,
            "mac": None,
            "reason": "MAC_NOT_READY",
        }

    if side == "LONG":
        close_cross = crossed_above(previous_close, current_close, channel["high"])
        open_jump = jumped_above(previous_close, current_open, channel["high"])
        level_name = "mac_high"
        level = channel["high"]
    else:
        close_cross = crossed_below(previous_close, current_close, channel["low"])
        open_jump = jumped_below(previous_close, current_open, channel["low"])
        level_name = "mac_low"
        level = channel["low"]

    raw_signal = close_cross or open_jump
    side_enabled = side in enabled_sides
    price_confirms = entry_price_confirms_signal(side, evaluation_ltp, channel)
    reason = action_reason(
        side,
        position,
        side_enabled,
        raw_signal,
        price_confirms,
        entry_config,
        evaluation_time < start_dt,
    )

    return {
        "side": side,
        "timeframe_minutes": timeframe,
        "candle_start": completed_candle["time"],
        "evaluated_at": evaluation_time,
        "important_note": "candle_start is the candle open time; evaluated_at is when the next quote/tick closed that candle",
        "candle": completed_candle,
        "previous_close": previous_close,
        "mac_high": channel["high"],
        "mac_low": channel["low"],
        level_name: level,
        "close_cross": close_cross,
        "open_jump": open_jump,
        "raw_signal": raw_signal,
        "side_enabled": side_enabled,
        "position_side_at_entry_check": position.get("side") if position else None,
        "evaluation_ltp_used_for_order_check": evaluation_ltp,
        "price_confirms_mac": price_confirms,
        "price_confirmation_mode": entry_config.get("price_confirmation", "warn"),
        "price_confirmation_blocks": entry_price_confirmation_blocks(entry_config),
        "reason": reason,
    }


def maybe_open_position(config, side, diagnostic, evaluation_time, evaluation_ltp, candle_index):
    if not diagnostic["reason"].startswith("WOULD_ENTER_"):
        return None
    quantity = get_quantity(config, ltp=evaluation_ltp)
    return {
        "side": side,
        "entry_time": evaluation_time,
        "entry_price": float(evaluation_ltp),
        "quantity": quantity,
        "entry_candle_index": candle_index,
    }


def run_diagnosis(config, one_min_candles, start_dt, target_dt, long_timeframe, short_timeframe, side_filter):
    config = copy.deepcopy(config)
    config["live_orders"] = False
    indicators.MA_PERIOD = int(config.get("moving_average_channel", {}).get("length", 20))

    entry_config = config.get("entry", {})
    enabled_sides = set(entry_config.get("enabled_sides", ["LONG", "SHORT"]))
    exit_config = config.get("exit", {})
    candle_stop_points = float(exit_config.get("candle_stop_points", 3))

    long_store = CandleStore(timeframe_minutes=long_timeframe, history_len=1000)
    short_store = CandleStore(timeframe_minutes=short_timeframe, history_len=1000)
    long_rows = []
    short_rows = []
    position = None
    records = []
    trades = []
    nearby = {"LONG": [], "SHORT": []}

    wanted = {
        "LONG": floor_time(target_dt, long_timeframe),
        "SHORT": floor_time(target_dt, short_timeframe),
    }
    wanted_sides = {"LONG", "SHORT"} if side_filter == "BOTH" else {side_filter}

    for tick in one_min_candles:
        tick_time = tick["time"]
        ltp = float(tick["close"])

        if tick_time >= start_dt and position is not None:
            point_exit_reason = should_exit(position, ltp, exit_config)
            if point_exit_reason:
                trades.append(close_position(position, tick_time, ltp, point_exit_reason))
                position = None

        completed_long = long_store.update(ltp, tick_time=tick_time)
        completed_short = short_store.update(ltp, tick_time=tick_time)

        if completed_long is not None:
            previous = last_close(long_rows)
            append_candle(long_rows, completed_long)
            channel = mac_channel(long_rows)
            current = float(completed_long["close"])
            current_index = len(long_rows) - 1
            position_before_exit = copy.deepcopy(position)
            exit_reason = None

            if tick_time >= start_dt and channel and position is not None and position["side"] == "LONG":
                if current < channel["low"]:
                    exit_reason = "MAC_BREAK_LOWER"
                else:
                    update_candle_stop(position, long_rows, current_index, candle_stop_points)
                    exit_reason = candle_stop_exit(position, current)
                if exit_reason:
                    trades.append(close_position(position, tick_time, current, exit_reason))
                    position = None

            diagnostic = entry_diagnostic(
                "LONG",
                long_timeframe,
                completed_long,
                previous,
                channel,
                position,
                enabled_sides,
                entry_config,
                tick_time,
                ltp,
                start_dt,
            )
            diagnostic["position_before_exit_check"] = position_before_exit
            diagnostic["exit_reason_before_entry_check"] = exit_reason
            diagnostic["candle_index"] = current_index
            if abs((completed_long["time"] - wanted["LONG"]).total_seconds()) <= long_timeframe * 60 * 2:
                nearby["LONG"].append({
                    "candle_start": completed_long["time"],
                    "close": current,
                    "mac": channel,
                    "reason": diagnostic["reason"],
                })
            if "LONG" in wanted_sides and completed_long["time"] == wanted["LONG"]:
                records.append(diagnostic)

            if tick_time >= start_dt and position is None:
                opened = maybe_open_position(config, "LONG", diagnostic, tick_time, ltp, current_index)
                if opened is not None:
                    position = opened
                    update_candle_stop(position, long_rows, current_index, candle_stop_points)

        if completed_short is not None:
            previous = last_close(short_rows)
            append_candle(short_rows, completed_short)
            channel = mac_channel(short_rows)
            current = float(completed_short["close"])
            current_index = len(short_rows) - 1
            position_before_exit = copy.deepcopy(position)
            exit_reason = None

            if tick_time >= start_dt and channel and position is not None and position["side"] == "SHORT":
                if current > channel["high"]:
                    exit_reason = "MAC_BREAK_UPPER"
                else:
                    update_candle_stop(position, short_rows, current_index, candle_stop_points)
                    exit_reason = candle_stop_exit(position, current)
                if exit_reason:
                    trades.append(close_position(position, tick_time, current, exit_reason))
                    position = None

            diagnostic = entry_diagnostic(
                "SHORT",
                short_timeframe,
                completed_short,
                previous,
                channel,
                position,
                enabled_sides,
                entry_config,
                tick_time,
                ltp,
                start_dt,
            )
            diagnostic["position_before_exit_check"] = position_before_exit
            diagnostic["exit_reason_before_entry_check"] = exit_reason
            diagnostic["candle_index"] = current_index
            if abs((completed_short["time"] - wanted["SHORT"]).total_seconds()) <= short_timeframe * 60 * 2:
                nearby["SHORT"].append({
                    "candle_start": completed_short["time"],
                    "close": current,
                    "mac": channel,
                    "reason": diagnostic["reason"],
                })
            if "SHORT" in wanted_sides and completed_short["time"] == wanted["SHORT"]:
                records.append(diagnostic)

            if tick_time >= start_dt and position is None:
                opened = maybe_open_position(config, "SHORT", diagnostic, tick_time, ltp, current_index)
                if opened is not None:
                    position = opened
                    update_candle_stop(position, short_rows, current_index, candle_stop_points)

    return {
        "target_input": target_dt,
        "simulation_start": start_dt,
        "loaded_one_min_history": history_summary(one_min_candles),
        "wanted_candle_start": {side: wanted[side] for side in wanted_sides},
        "long_timeframe": long_timeframe,
        "short_timeframe": short_timeframe,
        "mac_length": indicators.MA_PERIOD,
        "entry_price_confirmation": entry_config.get("price_confirmation", "warn"),
        "records": records,
        "nearby": {side: nearby[side] for side in wanted_sides},
        "simulated_trades_before_or_during_window": [
            {
                "side": trade.side,
                "entry_time": trade.entry_time,
                "entry_price": trade.entry_price,
                "exit_time": trade.exit_time,
                "exit_price": trade.exit_price,
                "reason": trade.reason,
                "points": trade.points,
                "pnl": trade.pnl,
            }
            for trade in trades
        ],
    }


def build_parser():
    parser = argparse.ArgumentParser(
        description="Print a crude oil strategy candle and the exact reason a trade did or did not trigger."
    )
    parser.add_argument("--candle", help="Candle start time, for example: 2026-07-09 15:36")
    parser.add_argument("--side", choices=["LONG", "SHORT", "BOTH"], default="BOTH")
    parser.add_argument("--start", help="Simulation start time. Default: same date 09:00")
    parser.add_argument("--long-timeframe", type=int)
    parser.add_argument("--short-timeframe", type=int)
    parser.add_argument("--warmup-days", type=int, default=10)
    parser.add_argument(
        "--history-end",
        help="Optional Dhan history end time. Default: target candle + 1 day, useful for MCX evening data.",
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    config = load_config()
    candle_config = config.get("candles", {})

    target_dt = parse_datetime(args.candle) if args.candle else ask_datetime("Candle start time: ")
    start_dt = (
        parse_datetime(args.start)
        if args.start
        else target_dt.replace(hour=9, minute=0, second=0, microsecond=0)
    )
    if start_dt > target_dt:
        raise ValueError("Simulation start must be before the candle time")

    long_timeframe = validate_timeframe(
        "Long",
        args.long_timeframe or candle_config.get("timeframe_minutes", 5),
    )
    short_timeframe = validate_timeframe(
        "Short",
        args.short_timeframe or candle_config.get("short_timeframe_minutes", 4),
    )
    side_filter = args.side.upper()

    dhan = create_dhan(config)
    instrument = get_instrument(config)
    end_dt = parse_datetime(args.history_end) if args.history_end else target_dt + timedelta(days=1)

    print(f"Instrument: {instrument.get('tradingsymbol', instrument['security_id'])}")
    print(f"Fetching history from {start_dt.date()} to {end_dt.date()} with {args.warmup_days} warmup days")
    one_min_candles = fetch_1min_history(
        dhan,
        instrument,
        start_dt,
        end_dt,
        warmup_days=args.warmup_days,
    )
    print(f"Loaded {len(one_min_candles)} one-minute candles")

    result = run_diagnosis(
        config,
        one_min_candles,
        start_dt,
        target_dt,
        long_timeframe,
        short_timeframe,
        side_filter,
    )
    print_json("DIAGNOSIS", result)

    if not result["records"]:
        print("\nNo completed candle matched that start time. Check the nearby list above.")
    else:
        for record in result["records"]:
            print(
                f"\nRESULT: {record['side']} {record['timeframe_minutes']}min "
                f"candle {record['candle_start']} -> {record['reason']}"
            )


if __name__ == "__main__":
    main()
