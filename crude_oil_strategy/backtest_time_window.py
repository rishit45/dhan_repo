import argparse
import copy
import json
import sys
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path


STRATEGY_DIR = Path(__file__).resolve().parent
if str(STRATEGY_DIR) not in sys.path:
    sys.path.insert(0, str(STRATEGY_DIR))

from backtest import (  # noqa: E402
    append_candle,
    calculate_entry_margin,
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
    print_trade,
    update_candle_stop,
    validate_timeframe,
)
from candles import CandleStore  # noqa: E402
from config_loader import get_instrument, get_quantity  # noqa: E402
from exit_manager import should_exit  # noqa: E402
from market_data import create_dhan  # noqa: E402
import indicators  # noqa: E402


DATE_FORMATS = ("%Y-%m-%d", "%Y %m %d", "%d-%m-%Y")
TIME_FORMATS = ("%H:%M", "%H:%M:%S", "%H")


def load_config():
    with open(STRATEGY_DIR / "strategy_config.json", "r", encoding="utf-8") as config_file:
        return json.load(config_file)


def parse_date(value):
    value = str(value).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    raise ValueError("Use date format YYYY-MM-DD, for example 2026-06-08")


def parse_time(value):
    value = str(value).strip()
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            pass
    raise ValueError("Use time format HH:MM, for example 17:00")


def ask_date(prompt):
    while True:
        try:
            return parse_date(input(prompt).strip())
        except ValueError as exc:
            print(exc)


def ask_time(prompt, default):
    value = input(f"{prompt} [{default}]: ").strip()
    if not value:
        return parse_time(default)
    return parse_time(value)


def ask_int(prompt, default):
    value = input(f"{prompt} [{default}]: ").strip()
    if not value:
        return int(default)
    return int(value)


def is_in_session(dt, session_start, session_end):
    current = dt.time()
    if session_start <= session_end:
        return session_start <= current <= session_end
    return current >= session_start or current <= session_end


def session_label(dt, session_start, session_end):
    if session_start <= session_end or dt.time() >= session_start:
        return dt.date()
    return (dt - timedelta(days=1)).date()


def close_existing_position(trades, position, exit_time, exit_price, reason, replay=False):
    trade = close_position(position, exit_time, exit_price, reason)
    trades.append(trade)
    if replay:
        print_trade("EXIT", trade)
    return None


def run_time_window_backtest(
    config,
    one_min_candles,
    start_dt,
    end_dt,
    session_start,
    session_end,
    long_timeframe,
    short_timeframe,
    replay=False,
    dhan=None,
):
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
    candle_stop_points = float(exit_config.get("candle_stop_points", 3))

    long_store = CandleStore(timeframe_minutes=long_timeframe, history_len=1000)
    short_store = CandleStore(timeframe_minutes=short_timeframe, history_len=1000)
    long_rows = []
    short_rows = []
    trades = []
    skipped_signals = []
    position = None
    last_tick_time = None
    last_ltp = None

    for tick in one_min_candles:
        tick_time = tick["time"]
        if tick_time < start_dt or tick_time > end_dt:
            continue
        ltp = float(tick["close"])
        active_now = is_in_session(tick_time, session_start, session_end)

        if position is not None and last_tick_time is not None:
            day_changed = session_label(tick_time, session_start, session_end) != position.get("session_label")
            left_session = is_in_session(last_tick_time, session_start, session_end) and not active_now
            if day_changed or left_session:
                position = close_existing_position(
                    trades,
                    position,
                    last_tick_time,
                    last_ltp,
                    "SESSION_END",
                    replay,
                )

        if active_now and position is not None:
            point_exit_reason = should_exit(position, ltp, exit_config)
            if point_exit_reason:
                position = close_existing_position(trades, position, tick_time, ltp, point_exit_reason, replay)

        completed_long = long_store.update(ltp, tick_time=tick_time)
        completed_short = short_store.update(ltp, tick_time=tick_time)

        if completed_long is not None:
            previous = last_close(long_rows)
            append_candle(long_rows, completed_long)
            channel = mac_channel(long_rows)
            current_open = float(completed_long["open"])
            current = float(completed_long["close"])
            current_index = len(long_rows) - 1

            if active_now and channel and position is not None and position["side"] == "LONG":
                if current < channel["low"]:
                    position = close_existing_position(trades, position, tick_time, current, "MAC_BREAK_LOWER", replay)
                else:
                    stop_loss = update_candle_stop(position, long_rows, current_index, candle_stop_points)
                    exit_reason = candle_stop_exit(position, current)
                    if replay and stop_loss is not None:
                        print(f"[{tick_time}] LONG STOP {stop_loss}")
                    if exit_reason:
                        position = close_existing_position(trades, position, tick_time, current, exit_reason, replay)

            if channel:
                raw_signal = crossed_above(previous, current, channel["high"]) or jumped_above(
                    previous,
                    current_open,
                    channel["high"],
                )
                if raw_signal and not active_now:
                    skipped_signals.append((tick_time, "LONG", "OUTSIDE_TIME_WINDOW", current))
                if active_now and position is None:
                    signal = raw_signal and "LONG" in enabled_sides
                    if signal and not entry_price_confirms_signal("LONG", ltp, channel) and entry_price_confirmation_blocks(entry_config):
                        signal = False
                        skipped_signals.append((tick_time, "LONG", "PRICE_CONFIRMATION_BLOCKED", ltp))
                    if signal:
                        quantity = get_quantity(config, ltp=ltp)
                        margin = calculate_entry_margin(dhan, config, instrument, "LONG", quantity, ltp)
                        position = {
                            "side": "LONG",
                            "entry_time": tick_time,
                            "entry_price": ltp,
                            "quantity": quantity,
                            "entry_candle_index": current_index,
                            "session_label": session_label(tick_time, session_start, session_end),
                            **margin,
                        }
                        update_candle_stop(position, long_rows, current_index, candle_stop_points)
                        if replay:
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

            if active_now and channel and position is not None and position["side"] == "SHORT":
                if current > channel["high"]:
                    position = close_existing_position(trades, position, tick_time, current, "MAC_BREAK_UPPER", replay)
                else:
                    stop_loss = update_candle_stop(position, short_rows, current_index, candle_stop_points)
                    exit_reason = candle_stop_exit(position, current)
                    if replay and stop_loss is not None:
                        print(f"[{tick_time}] SHORT STOP {stop_loss}")
                    if exit_reason:
                        position = close_existing_position(trades, position, tick_time, current, exit_reason, replay)

            if channel:
                raw_signal = crossed_below(previous, current, channel["low"]) or jumped_below(
                    previous,
                    current_open,
                    channel["low"],
                )
                if raw_signal and not active_now:
                    skipped_signals.append((tick_time, "SHORT", "OUTSIDE_TIME_WINDOW", current))
                if active_now and position is None:
                    signal = raw_signal and "SHORT" in enabled_sides
                    if signal and not entry_price_confirms_signal("SHORT", ltp, channel) and entry_price_confirmation_blocks(entry_config):
                        signal = False
                        skipped_signals.append((tick_time, "SHORT", "PRICE_CONFIRMATION_BLOCKED", ltp))
                    if signal:
                        quantity = get_quantity(config, ltp=ltp)
                        margin = calculate_entry_margin(dhan, config, instrument, "SHORT", quantity, ltp)
                        position = {
                            "side": "SHORT",
                            "entry_time": tick_time,
                            "entry_price": ltp,
                            "quantity": quantity,
                            "entry_candle_index": current_index,
                            "session_label": session_label(tick_time, session_start, session_end),
                            **margin,
                        }
                        update_candle_stop(position, short_rows, current_index, candle_stop_points)
                        if replay:
                            print(
                                f"[{tick_time}] ENTRY SHORT price={ltp} qty={quantity} "
                                f"margin_qty={margin['margin_quantity']} margin_required={margin['margin_required']} "
                                f"stop={position.get('stop_loss')}"
                            )

        last_tick_time = tick_time
        last_ltp = ltp

    if position is not None and last_tick_time is not None:
        position = close_existing_position(trades, position, last_tick_time, last_ltp, "END_OF_BACKTEST", replay)

    return {
        "instrument": instrument,
        "start": start_dt,
        "end": end_dt,
        "session_start": session_start,
        "session_end": session_end,
        "long_timeframe": long_timeframe,
        "short_timeframe": short_timeframe,
        "mac_length": indicators.MA_PERIOD,
        "candle_stop_points": candle_stop_points,
        "target_points": float(exit_config.get("target_points", 0)),
        "stop_loss_points": float(exit_config.get("stop_loss_points", 0)),
        "trades": trades,
        "skipped_signals": skipped_signals,
    }


def print_summary(result):
    trades = result["trades"]
    total_pnl = sum(trade.pnl for trade in trades)
    winners = [trade for trade in trades if trade.pnl > 0]
    losers = [trade for trade in trades if trade.pnl < 0]
    margins = [trade.margin_required for trade in trades if trade.margin_required is not None]

    print("\n===== CRUDE TIME WINDOW BACKTEST =====")
    print(f"Instrument: {result['instrument'].get('tradingsymbol', result['instrument']['security_id'])}")
    print(f"Date range: {result['start']} -> {result['end']}")
    print(f"Entries allowed daily: {result['session_start']} -> {result['session_end']}")
    print(f"Long timeframe: {result['long_timeframe']} min")
    print(f"Short timeframe: {result['short_timeframe']} min")
    print(f"MAC length: {result['mac_length']}")
    print(f"Candle stop points: {result['candle_stop_points']}")
    print(f"Target points: {result['target_points']}")
    print(f"Stop loss points: {result['stop_loss_points']}")
    print(f"Trades: {len(trades)}")
    print(f"Winners: {len(winners)}")
    print(f"Losers: {len(losers)}")
    print(f"Total PnL: {total_pnl:.2f}")
    if margins:
        print(f"Max Dhan margin required: {max(margins):.2f}")
    print(f"Signals skipped outside time window: {len(result['skipped_signals'])}")

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


def build_parser():
    parser = argparse.ArgumentParser(
        description="Backtest crude oil only during a daily time window, for example entries after 17:00."
    )
    parser.add_argument("--from-date", dest="from_date", help="Start date, for example 2026-06-08")
    parser.add_argument("--to-date", dest="to_date", help="End date, for example 2026-07-08")
    parser.add_argument("--entry-start", default=None, help="Daily entry start time, default 17:00")
    parser.add_argument("--entry-end", default=None, help="Daily entry end time, default 23:59")
    parser.add_argument("--long-timeframe", type=int)
    parser.add_argument("--short-timeframe", type=int)
    parser.add_argument("--replay", action="store_true", help="Print entries/exits as the backtest walks forward")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    config = load_config()
    candle_config = config.get("candles", {})

    from_date = parse_date(args.from_date) if args.from_date else ask_date("From date: ")
    to_date = parse_date(args.to_date) if args.to_date else ask_date("To date: ")
    if to_date < from_date:
        raise ValueError("To date must be same as or after from date")

    session_start = parse_time(args.entry_start) if args.entry_start else ask_time("Entry start time", "17:00")
    session_end = parse_time(args.entry_end) if args.entry_end else ask_time("Entry end time", "23:59")
    long_timeframe = validate_timeframe(
        "Long",
        args.long_timeframe or ask_int("Long timeframe minutes", candle_config.get("timeframe_minutes", 5)),
    )
    short_timeframe = validate_timeframe(
        "Short",
        args.short_timeframe or ask_int("Short timeframe minutes", candle_config.get("short_timeframe_minutes", 4)),
    )

    start_dt = datetime.combine(from_date, clock_time(0, 0))
    end_dt = datetime.combine(to_date, clock_time(23, 59, 59))

    dhan = create_dhan(config)
    instrument = get_instrument(config)
    candles = fetch_1min_history(dhan, instrument, start_dt, end_dt)
    if not candles:
        raise RuntimeError("No historical candles found")

    print(f"Loaded {len(candles)} one-minute candles")
    print(f"Entry window: {session_start} to {session_end}")
    result = run_time_window_backtest(
        config,
        candles,
        start_dt,
        end_dt,
        session_start,
        session_end,
        long_timeframe,
        short_timeframe,
        replay=args.replay,
        dhan=dhan,
    )
    print_summary(result)


if __name__ == "__main__":
    main()
