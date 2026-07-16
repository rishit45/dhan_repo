import time
from datetime import datetime

from buy import place_buy_order
from candles import CandleStore
from config_loader import get_instrument, get_quantity, live_orders_enabled, load_config
from debug import print_data
from exit_manager import daily_pnl_status, position_pnl, should_exit
import indicators
from live_trading import (
    check_margin_before_order,
    order_accepted,
    preflight_live_trading,
    print_order_preview,
    validate_order_config,
)
from market_data import create_dhan, fetch_quote, fetch_historical_intraday_candles
from sell import place_sell_order





def limit_price_for_entry(side, entry_config, quote):
    if side == "LONG":
        source = entry_config.get("long_limit_price_source", "best_bid")
    else:
        source = entry_config.get("short_limit_price_source", "best_ask")
    return quote.get(source) or quote["ltp"]


def entry_check_price(side, entry_config, quote):
    order_type = entry_config.get("order_type", "LIMIT").upper()
    if order_type == "MARKET":
        return quote["ltp"]
    return limit_price_for_entry(side, entry_config, quote)


def entry_price_confirms_signal(side, price, channel):
    if price is None or channel is None:
        return False
    if side == "LONG":
        return float(price) > float(channel["high"])
    return float(price) < float(channel["low"])


def entry_price_confirmation_blocks(entry_config):
    mode = str(entry_config.get("price_confirmation", "warn")).lower()
    return mode in {"block", "strict", "true", "yes", "1"}


def entry_check_action(position, side_enabled, raw_signal, price_confirms, entry_config):
    if position is not None:
        return "skip_position_open"
    if not side_enabled:
        return "skip_side_disabled"
    if not raw_signal:
        return "skip_no_cross_or_jump"
    if price_confirms is False and entry_price_confirmation_blocks(entry_config):
        return "skip_price_not_confirmed"
    if price_confirms is False:
        return "enter_with_price_warning"
    return "enter"


def place_entry(dhan, instrument, side, quantity, entry_config, quote, live_orders, config, edis_config=None):
    order_type = entry_config.get("order_type", "LIMIT").upper()
    price = 0 if order_type == "MARKET" else limit_price_for_entry(side, entry_config, quote)
    transaction_type = "BUY" if side == "LONG" else "SELL"
    margin_price = quote["ltp"] if order_type == "MARKET" else price

    validate_order_config(instrument, quantity, order_type, margin_price)
    print_order_preview(f"ENTRY_{side}", instrument, transaction_type, quantity, order_type, price)
    check_margin_before_order(dhan, config, instrument, transaction_type, quantity, margin_price)

    if side == "LONG":
        return place_buy_order(dhan, instrument, quantity, order_type, price, live_orders)
    return place_sell_order(dhan, instrument, quantity, order_type, price, live_orders, edis_config)


def place_exit(dhan, instrument, position, quote, live_orders, edis_config=None):
    side = position["side"]
    quantity = position["quantity"]
    price = quote["ltp"]
    transaction_type = "SELL" if side == "LONG" else "BUY"

    validate_order_config(instrument, quantity, "LIMIT", price)
    print_order_preview(f"EXIT_{side}", instrument, transaction_type, quantity, "LIMIT", price)

    if side == "LONG":
        return place_sell_order(dhan, instrument, quantity, "LIMIT", price, live_orders, edis_config)
    return place_buy_order(dhan, instrument, quantity, "LIMIT", price, live_orders)


def mac_channel(candles):
    high_sma = indicators.compute_sma_high(candles)
    low_sma = indicators.compute_sma_low(candles)
    if high_sma is None or low_sma is None:
        return None
    return {"high": high_sma, "low": low_sma}


def candle_close(candles):
    if not candles:
        return None
    try:
        return float(candles[-1]["close"])
    except Exception:
        return None


def crossed_above(previous_close, current_close, level):
    if previous_close is None or current_close is None or level is None:
        return False
    return float(previous_close) <= float(level) and float(current_close) > float(level)


def crossed_below(previous_close, current_close, level):
    if previous_close is None or current_close is None or level is None:
        return False
    return float(previous_close) >= float(level) and float(current_close) < float(level)


def jumped_above(previous_close, current_open, level):
    if previous_close is None or current_open is None or level is None:
        return False
    return float(previous_close) < float(level) and float(current_open) > float(level)


def jumped_below(previous_close, current_open, level):
    if previous_close is None or current_open is None or level is None:
        return False
    return float(previous_close) > float(level) and float(current_open) < float(level)


def append_completed_candle(candles, completed_candle):
    return indicators.append_candle(
        candles,
        completed_candle.get("open"),
        completed_candle.get("high"),
        completed_candle.get("low"),
        completed_candle.get("close"),
    )


def seed_sma_candles(closed_candles):
    seeded = []
    for candle in list(closed_candles):
        if candle.get("close") is None:
            continue
        seeded.append({
            "open": float(candle.get("open")),
            "high": float(candle.get("high")),
            "low": float(candle.get("low")),
            "close": float(candle.get("close")),
        })
    return seeded


def candle_stop_loss(side, candles, entry_candle_index, current_candle_index, buffer_points):
    if entry_candle_index is None or current_candle_index is None:
        return None
    if not candles:
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
    buffer_points = float(buffer_points)
    if side == "LONG":
        return float(reference_candle["low"]) - buffer_points
    return float(reference_candle["high"]) + buffer_points


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
    if stop_loss is None:
        return None
    position["stop_loss"] = stop_loss
    return stop_loss


def candle_stop_exit(position, close_price):
    if position is None or close_price is None:
        return None
    stop_loss = position.get("stop_loss")
    if stop_loss is None:
        return None
    if position["side"] == "LONG" and float(close_price) <= float(stop_loss):
        return "CANDLE_STOP_LOWER"
    if position["side"] == "SHORT" and float(close_price) >= float(stop_loss):
        return "CANDLE_STOP_UPPER"
    return None


def run():
    config = load_config()
    instrument = get_instrument(config)
    dhan = create_dhan(config)
    live_orders = live_orders_enabled(config)
    candle_config = config.get("candles", {})
    mac_config = config.get("moving_average_channel", {})
    entry_config = config.get("entry", {})
    exit_config = config.get("exit", {})
    daily_pnl_config = config.get("daily_pnl", {})
    edis_config = config.get("edis", {})
    enabled_sides = set(entry_config.get("enabled_sides", ["LONG", "SHORT"]))
    mac_length = int(mac_config.get("length", 20))
    candle_stop_points = float(exit_config.get("candle_stop_points", 3))

    # Short and long candle stores are fully driven by strategy_config.json.
    short_timeframe = int(candle_config.get("short_timeframe_minutes", 3))
    long_timeframe = int(candle_config.get("timeframe_minutes", 5))

    short_candles = CandleStore(
        timeframe_minutes=short_timeframe,
        history_len=candle_config.get("history_len", 200),
    )

    long_candles = CandleStore(
        timeframe_minutes=long_timeframe,
        history_len=candle_config.get("history_len", 200),
    )

    # Seed historical candles directly from Dhan intraday data.
    try:
        long_history = fetch_historical_intraday_candles(
            dhan,
            instrument,
            interval=long_timeframe,
            periods=int(mac_length),
            history_days=7,
        )
        short_history = fetch_historical_intraday_candles(
            dhan,
            instrument,
            interval=short_timeframe,
            periods=int(mac_length),
            history_days=7,
        )
        if long_history:
            long_candles.load_history(long_history)
            print(f"Seeded {len(long_history)} historical {long_timeframe}-minute candles from Dhan")
        if short_history:
            short_candles.load_history(short_history)
            print(f"Seeded {len(short_history)} historical {short_timeframe}-minute candles from Dhan")
    except Exception as exc:
        print(f"Could not seed Dhan intraday candle history: {exc}")

    # wire MA period to indicators module
    indicators.MA_PERIOD = mac_length

    # initialize SMA candle containers from the already-closed historical candles
    sma_candles_5min = seed_sma_candles(long_candles.closed_candles)
    sma_candles_3min = seed_sma_candles(short_candles.closed_candles)
    mac5min = mac_channel(sma_candles_5min)
    mac3min = mac_channel(sma_candles_3min)

    print(f"Seeded {len(sma_candles_5min)} historical {long_timeframe}-min candles and {len(sma_candles_3min)} historical {short_timeframe}-min candles for MA.")
    print_data(f"INITIAL_MAC{long_timeframe}MIN", mac5min)
    print_data(f"INITIAL_MAC{short_timeframe}MIN", mac3min)

    position = None
    trade_executed_since_last_long = False
    last_trade_summary = None
    realized_pnl_today = 0.0
    pnl_day = None
    daily_limit_reached = False

    start_time = datetime.now()
    print(f"Crude oil strategy started at {start_time.isoformat()}")
    print("Dhan does not provide Moving Average Channel directly; calculating MAC locally.")
    print_data("CRUDE_OIL_STRATEGY_CONFIG", config)
    print_data("CRUDE_OIL_STRATEGY_INSTRUMENT", instrument)
    startup_quote = fetch_quote(dhan, instrument)
    print_data("STARTUP_QUOTE", startup_quote)
    preflight_live_trading(dhan, config, instrument, startup_quote)

    # plotting removed — print MAC values instead
    while True:
        try:
            quote = fetch_quote(dhan, instrument)
            ltp = quote["ltp"]

            current_day = datetime.now().date()
            if current_day != pnl_day:
                pnl_day = current_day
                realized_pnl_today = 0.0
                daily_limit_reached = False

            daily_status = daily_pnl_status(realized_pnl_today, position, ltp, daily_pnl_config)
            if daily_status["hit"]:
                daily_limit_reached = bool(daily_pnl_config.get("block_new_entries_after_hit", True))
                if position is not None and bool(daily_pnl_config.get("close_position_when_hit", True)):
                    daily_reason = daily_status["reason"]
                    print(f"[DAILY PNL EXIT] {daily_reason} total_pnl={daily_status['total_pnl']:.2f}")
                    response = place_exit(dhan, instrument, position, quote, live_orders, edis_config)
                    if order_accepted(response, live_orders):
                        realized_pnl_today += position_pnl(position, ltp)
                        last_trade_summary = (
                            f"EXIT {position['side']} qty={position['quantity']} price={ltp} reason={daily_reason}"
                        )
                        trade_executed_since_last_long = True
                        position = None
                    else:
                        print(f"[DAILY EXIT ORDER NOT ACCEPTED] Keeping position open. response={response}")

            point_exit_reason = should_exit(position, ltp, exit_config)
            if point_exit_reason:
                print(f"[POINT EXIT] {point_exit_reason} position={position} ltp={ltp}")
                response = place_exit(dhan, instrument, position, quote, live_orders, edis_config)
                if order_accepted(response, live_orders):
                    try:
                        last_trade_summary = (
                            f"EXIT {position['side']} qty={position['quantity']} "
                            f"price={ltp} reason={point_exit_reason}"
                        )
                        realized_pnl_today += position_pnl(position, ltp)
                        daily_limit_reached = (
                            bool(daily_pnl_config.get("block_new_entries_after_hit", True))
                            and daily_pnl_status(realized_pnl_today, None, None, daily_pnl_config)["hit"]
                        )
                        trade_executed_since_last_long = True
                    except Exception:
                        last_trade_summary = f"EXIT position price={ltp} reason={point_exit_reason}"
                        trade_executed_since_last_long = True
                    position = None
                else:
                    print(f"[POINT EXIT ORDER NOT ACCEPTED] Keeping position open. response={response}")

            # update both candle stores with the latest ltp
            completed_short = short_candles.update(ltp)
            completed_long = long_candles.update(ltp)
            # compute and print MAC and candles only when a long candle closes
            if completed_long is not None:
                previous_long_close = candle_close(sma_candles_5min)
                sma_candles_5min = append_completed_candle(sma_candles_5min, completed_long)
                mac5min = mac_channel(sma_candles_5min)
                if mac5min is None:
                    print(f"MAC{long_timeframe}MIN waiting for {indicators.MA_PERIOD} candles, current count={len(sma_candles_5min)}")

                # get last closed short candle if available
                last_short = None
                try:
                    last_short = list(short_candles.closed_candles)[-1]
                except Exception:
                    last_short = None

                print(f"\n===== {long_timeframe}MIN UPDATE =====")
                print(f"Strategy start: {start_time.isoformat()}")
                print(f"LONG_CANDLE_CLOSED: {completed_long}")
                if last_short is not None:
                    print(f"LAST_SHORT_CANDLE: {last_short}")
                print_data(f"MAC{long_timeframe}MIN", mac5min)

                # 5-min MAC entry/exit logic for long trades only.
                if mac5min is not None:
                    long_channel = mac5min
                    long_open = completed_long.get("open")
                    long_close = completed_long.get("close")
                    long_candle_index = len(sma_candles_5min) - 1
                    if position is not None and position["side"] == "LONG":
                        if long_close < long_channel["low"]:
                            exit_reason = "MAC_BREAK_LOWER"
                        else:
                            exit_reason = None
                            stop_loss = update_candle_stop(position, sma_candles_5min, long_candle_index, candle_stop_points)
                            if stop_loss is not None:
                                print_data("LONG_CANDLE_STOP", {"stop_loss": stop_loss, "candle_index": long_candle_index})
                            exit_reason = candle_stop_exit(position, long_close)

                        if exit_reason:
                            response = place_exit(dhan, instrument, position, quote, live_orders, edis_config)
                            if order_accepted(response, live_orders):
                                last_trade_summary = f"EXIT {position['side']} qty={position['quantity']} price={ltp} reason={exit_reason}"
                                realized_pnl_today += position_pnl(position, ltp)
                                daily_limit_reached = (
                                    bool(daily_pnl_config.get("block_new_entries_after_hit", True))
                                    and daily_pnl_status(realized_pnl_today, None, None, daily_pnl_config)["hit"]
                                )
                                trade_executed_since_last_long = True
                                position = None
                            else:
                                print(f"[EXIT ORDER NOT ACCEPTED] Keeping position open. response={response}")

                    long_cross = crossed_above(previous_long_close, long_close, long_channel["high"])
                    long_jump = jumped_above(previous_long_close, long_open, long_channel["high"])
                    long_raw_signal = long_cross or long_jump
                    long_side_enabled = "LONG" in enabled_sides
                    long_projected_price = entry_check_price("LONG", entry_config, quote)
                    long_price_confirms = entry_price_confirms_signal("LONG", long_projected_price, long_channel)
                    print_data(
                        "LONG_ENTRY_CHECK",
                        {
                            "timeframe_minutes": long_timeframe,
                            "candle_start": completed_long.get("time"),
                            "previous_close": previous_long_close,
                            "candle_open": long_open,
                            "candle_close": long_close,
                            "mac_high": long_channel["high"],
                            "mac_low": long_channel["low"],
                            "close_cross_above_mac_high": long_cross,
                            "open_jump_above_mac_high": long_jump,
                            "raw_signal": long_raw_signal,
                            "side_enabled": long_side_enabled,
                            "position_side": position.get("side") if position else None,
                            "projected_entry_price": long_projected_price,
                            "price_confirms_mac": long_price_confirms,
                            "price_confirmation_blocks": entry_price_confirmation_blocks(entry_config),
                            "action": entry_check_action(
                                position,
                                long_side_enabled,
                                long_raw_signal,
                                long_price_confirms,
                                entry_config,
                            ),
                        },
                    )

                    if position is None and not daily_limit_reached:
                        if long_raw_signal and long_side_enabled:
                            signal = "LONG"
                        else:
                            signal = None

                        if signal:
                            projected_price = entry_check_price(signal, entry_config, quote)
                            if not entry_price_confirms_signal(signal, projected_price, long_channel):
                                print_data(
                                    "ENTRY_PRICE_NOT_CONFIRMED",
                                    {
                                        "side": signal,
                                        "projected_price": projected_price,
                                        "mac": long_channel,
                                        "candle_open": long_open,
                                        "candle_close": long_close,
                                        "reason": "entry price no longer above MAC high",
                                        "action": "skip" if entry_price_confirmation_blocks(entry_config) else "warn_only",
                                    },
                                )
                                if entry_price_confirmation_blocks(entry_config):
                                    signal = None

                        if signal:
                            quantity = get_quantity(config, ltp=ltp)
                            response = place_entry(
                                dhan,
                                instrument,
                                signal,
                                quantity,
                                entry_config,
                                quote,
                                live_orders,
                                config,
                                edis_config,
                            )
                            if order_accepted(response, live_orders):
                                position = {
                                    "side": signal,
                                    "entry_price": ltp,
                                    "quantity": quantity,
                                    "entry_candle_index": long_candle_index,
                                }
                                stop_loss = update_candle_stop(position, sma_candles_5min, long_candle_index, candle_stop_points)
                                print_data("LONG_ENTRY_STOP", {"stop_loss": stop_loss, "candle_index": long_candle_index})
                                trade_executed_since_last_long = True
                                last_trade_summary = f"ENTRY {signal} qty={quantity} price={ltp}"
                            else:
                                print(f"[ENTRY ORDER NOT ACCEPTED] No position opened. response={response}")

            # When a short-timeframe candle completes, evaluate entry using its MAC.
            if completed_short is not None:
                short_open = completed_short.get("open")
                short_close = completed_short.get("close")
                previous_short_close = candle_close(sma_candles_3min)
                sma_candles_3min = append_completed_candle(sma_candles_3min, completed_short)
                mac3min = mac_channel(sma_candles_3min)
                if mac3min is None:
                    print(f"MAC{short_timeframe}MIN waiting for {indicators.MA_PERIOD} candles, current count={len(sma_candles_3min)}")

                print(f"\n===== {short_timeframe}MIN UPDATE =====")
                print(f"SHORT_CANDLE_CLOSED: {completed_short}")
                print_data(f"MAC{short_timeframe}MIN", mac3min)

                # ensure we have enough data for MAC and no open position
                if mac3min is not None:
                    # Short-timeframe MAC entry/exit logic for short trades only.
                    short_candle_index = len(sma_candles_3min) - 1
                    if position is not None and position["side"] == "SHORT":
                        if short_close > mac3min["high"]:
                            exit_reason = "MAC_BREAK_UPPER"
                        else:
                            exit_reason = None
                            stop_loss = update_candle_stop(position, sma_candles_3min, short_candle_index, candle_stop_points)
                            if stop_loss is not None:
                                print_data("SHORT_CANDLE_STOP", {"stop_loss": stop_loss, "candle_index": short_candle_index})
                            exit_reason = candle_stop_exit(position, short_close)

                        if exit_reason:
                            response = place_exit(dhan, instrument, position, quote, live_orders, edis_config)
                            if order_accepted(response, live_orders):
                                last_trade_summary = f"EXIT {position['side']} qty={position['quantity']} price={ltp} reason={exit_reason}"
                                realized_pnl_today += position_pnl(position, ltp)
                                daily_limit_reached = (
                                    bool(daily_pnl_config.get("block_new_entries_after_hit", True))
                                    and daily_pnl_status(realized_pnl_today, None, None, daily_pnl_config)["hit"]
                                )
                                trade_executed_since_last_long = True
                                position = None
                            else:
                                print(f"[EXIT ORDER NOT ACCEPTED] Keeping position open. response={response}")

                    short_cross = crossed_below(previous_short_close, short_close, mac3min["low"])
                    short_jump = jumped_below(previous_short_close, short_open, mac3min["low"])
                    short_raw_signal = short_cross or short_jump
                    short_side_enabled = "SHORT" in enabled_sides
                    short_projected_price = entry_check_price("SHORT", entry_config, quote)
                    short_price_confirms = entry_price_confirms_signal("SHORT", short_projected_price, mac3min)
                    print_data(
                        "SHORT_ENTRY_CHECK",
                        {
                            "timeframe_minutes": short_timeframe,
                            "candle_start": completed_short.get("time"),
                            "previous_close": previous_short_close,
                            "candle_open": short_open,
                            "candle_close": short_close,
                            "mac_high": mac3min["high"],
                            "mac_low": mac3min["low"],
                            "close_cross_below_mac_low": short_cross,
                            "open_jump_below_mac_low": short_jump,
                            "raw_signal": short_raw_signal,
                            "side_enabled": short_side_enabled,
                            "position_side": position.get("side") if position else None,
                            "projected_entry_price": short_projected_price,
                            "price_confirms_mac": short_price_confirms,
                            "price_confirmation_blocks": entry_price_confirmation_blocks(entry_config),
                            "action": entry_check_action(
                                position,
                                short_side_enabled,
                                short_raw_signal,
                                short_price_confirms,
                                entry_config,
                            ),
                        },
                    )

                    if position is None and not daily_limit_reached:
                        if short_raw_signal and short_side_enabled:
                            signal = "SHORT"
                        else:
                            signal = None

                        if signal:
                            projected_price = entry_check_price(signal, entry_config, quote)
                            if not entry_price_confirms_signal(signal, projected_price, mac3min):
                                print_data(
                                    "ENTRY_PRICE_NOT_CONFIRMED",
                                    {
                                        "side": signal,
                                        "projected_price": projected_price,
                                        "mac": mac3min,
                                        "candle_open": short_open,
                                        "candle_close": short_close,
                                        "reason": "entry price no longer below MAC low",
                                        "action": "skip" if entry_price_confirmation_blocks(entry_config) else "warn_only",
                                    },
                                )
                                if entry_price_confirmation_blocks(entry_config):
                                    signal = None

                        if signal:
                            quantity = get_quantity(config, ltp=ltp)
                            response = place_entry(
                                dhan,
                                instrument,
                                signal,
                                quantity,
                                entry_config,
                                quote,
                                live_orders,
                                config,
                                edis_config,
                            )
                            if order_accepted(response, live_orders):
                                position = {
                                    "side": signal,
                                    "entry_price": ltp,
                                    "quantity": quantity,
                                    "entry_candle_index": short_candle_index,
                                }
                                stop_loss = update_candle_stop(position, sma_candles_3min, short_candle_index, candle_stop_points)
                                print_data("SHORT_ENTRY_STOP", {"stop_loss": stop_loss, "candle_index": short_candle_index})
                                trade_executed_since_last_long = True
                                last_trade_summary = f"ENTRY {signal} qty={quantity} price={ltp}"
                            else:
                                print(f"[ENTRY ORDER NOT ACCEPTED] No position opened. response={response}")

            if trade_executed_since_last_long and last_trade_summary:
                print("\n******************************")
                print("*** CRUDE OIL STRATEGY EXECUTED ***")
                print(last_trade_summary)
                print("******************************\n")
                trade_executed_since_last_long = False
                last_trade_summary = None

        except Exception as exc:
            print(f"[STRATEGY ERROR] {exc}")

        time.sleep(float(config.get("poll_interval_secs", 5)))


if __name__ == "__main__":
    run()
