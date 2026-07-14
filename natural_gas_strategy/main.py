import time
from datetime import datetime

from buy import place_buy_order
from candle_diagnostics import build_candle_diagnostic
from candles import CandleStore
from config_loader import get_instrument, get_quantity, live_orders_enabled, load_config
from debug import print_data
from exit_manager import should_exit
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


def live_candle_price(side, quote):
    """Use the executable quote side for a directional live candle.

    A long signal is built from best ask (the buy price), so its candle low is
    the minimum buy price seen in that interval.  A short signal is built from
    best bid (the sell price), so its candle high is the maximum sell price
    seen in that interval.  LTP is used only if quote depth is unavailable.
    """
    source = "best_ask" if side == "LONG" else "best_bid"
    return float(quote.get(source) or quote["ltp"])


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


def _round_to_tradable_quantity(quantity, instrument):
    lot_size = int(instrument.get("lot_size", 1))
    quantity = int(quantity)
    if lot_size <= 1:
        return max(quantity, 1)
    rounded = (quantity // lot_size) * lot_size
    return max(rounded, lot_size)


def quantity_with_trend_filter(config, instrument, side, ltp, trend_channel, trend_config):
    base_quantity = get_quantity(config, ltp=ltp)
    if not trend_config.get("enabled", True) or trend_channel is None:
        print_data(
            "LONG_SHORT_QUANTITY_CHECKER",
            {
                "side": side,
                "ltp": ltp,
                "base_quantity": base_quantity,
                "final_quantity": base_quantity,
                "reason": "disabled_or_mac_not_ready",
                "trend_mac": trend_channel,
            },
        )
        return base_quantity

    ratio = float(trend_config.get("half_quantity_ratio", 0.5))
    opposite_mode = str(trend_config.get("opposite_quantity_mode", "half")).lower()
    reason = "normal_between_or_same_direction"
    raw_quantity = base_quantity
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
    elif float(trend_channel["low"]) < float(ltp) < float(trend_channel["high"]):
        raw_quantity = 0
        reason = "one_hour_ltp_between_mac_zero"

    final_quantity = 0 if raw_quantity <= 0 else _round_to_tradable_quantity(raw_quantity, instrument)
    print_data(
        "LONG_SHORT_QUANTITY_CHECKER",
        {
            "side": side,
            "ltp": ltp,
            "trend_mac_high": trend_channel["high"],
            "trend_mac_low": trend_channel["low"],
            "base_quantity": base_quantity,
            "raw_quantity": raw_quantity,
            "final_quantity": final_quantity,
            "reason": reason,
            "lot_size": instrument.get("lot_size"),
        },
    )
    return final_quantity


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


def exit_price_for_position(position, quote):
    """Return the executable top-of-book price for closing a position."""
    source = "best_bid" if position["side"] == "LONG" else "best_ask"
    return float(quote.get(source) or quote["ltp"])


def place_exit(dhan, instrument, position, quote, live_orders, edis_config=None):
    side = position["side"]
    quantity = position["quantity"]
    price = exit_price_for_position(position, quote)
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


def daily_stop_loss_limit(daily_target_config):
    """Return the positive daily loss amount that stops the strategy."""
    if not daily_target_config.get("enabled", False):
        return 0.0
    return abs(float(daily_target_config.get("stop_loss_pnl", 0) or 0))


def daily_target_status(realized_pnl, position, ltp, daily_target_config):
    target = daily_target_limit(daily_target_config)
    stop_loss = daily_stop_loss_limit(daily_target_config)
    unrealized_pnl = 0.0
    if daily_target_config.get("include_unrealized", True):
        unrealized_pnl = position_pnl(position, ltp)
    total_pnl = float(realized_pnl) + unrealized_pnl
    target_hit = target > 0 and total_pnl >= target
    stop_loss_hit = stop_loss > 0 and total_pnl <= -stop_loss
    return {
        "enabled": bool(daily_target_config.get("enabled", False)),
        "target_pnl": target,
        "stop_loss_pnl": stop_loss,
        "realized_pnl": float(realized_pnl),
        "unrealized_pnl": unrealized_pnl,
        "total_pnl": total_pnl,
        "target_hit": target_hit,
        "stop_loss_hit": stop_loss_hit,
        "hit": target_hit or stop_loss_hit,
        "reason": "DAILY_TARGET" if target_hit else "DAILY_STOP_LOSS" if stop_loss_hit else None,
    }


def run():
    config = load_config()
    instrument = get_instrument(config)
    dhan = create_dhan(config)
    live_orders = live_orders_enabled(config)
    candle_config = config.get("candles", {})
    mac_config = config.get("moving_average_channel", {})
    entry_config = config.get("entry", {})
    exit_config = config.get("exit", {})
    edis_config = config.get("edis", {})
    trend_config = config.get("long_short_quantity_checker", {})
    daily_target_config = config.get("daily_target", {})
    enabled_sides = set(entry_config.get("enabled_sides", ["LONG", "SHORT"]))
    mac_length = int(mac_config.get("length", 20))

    # Short and long candle stores are fully driven by natural_gas_strategyconfig.json.
    short_timeframe = int(candle_config.get("short_timeframe_minutes", 3))
    long_timeframe = int(candle_config.get("timeframe_minutes", 5))
    trend_timeframe = int(trend_config.get("timeframe_minutes", 60))

    short_candles = CandleStore(
        timeframe_minutes=short_timeframe,
        history_len=candle_config.get("history_len", 200),
    )

    long_candles = CandleStore(
        timeframe_minutes=long_timeframe,
        history_len=candle_config.get("history_len", 200),
    )

    trend_candles = CandleStore(
        timeframe_minutes=trend_timeframe,
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
        trend_history = fetch_historical_intraday_candles(
            dhan,
            instrument,
            interval=trend_timeframe,
            periods=int(mac_length),
            history_days=10,
        )
        if long_history:
            long_candles.load_history(long_history)
            print(f"Seeded {len(long_history)} historical {long_timeframe}-minute candles from Dhan")
        if short_history:
            short_candles.load_history(short_history)
            print(f"Seeded {len(short_history)} historical {short_timeframe}-minute candles from Dhan")
        if trend_history:
            trend_candles.load_history(trend_history)
            print(f"Seeded {len(trend_history)} historical {trend_timeframe}-minute trend candles from Dhan")
    except Exception as exc:
        print(f"Could not seed Dhan intraday candle history: {exc}")

    # wire MA period to indicators module
    indicators.MA_PERIOD = mac_length

    # initialize SMA candle containers from the already-closed historical candles
    sma_candles_5min = seed_sma_candles(long_candles.closed_candles)
    sma_candles_3min = seed_sma_candles(short_candles.closed_candles)
    sma_candles_60min = seed_sma_candles(trend_candles.closed_candles)
    mac5min = mac_channel(sma_candles_5min)
    mac3min = mac_channel(sma_candles_3min)
    mac60min = mac_channel(sma_candles_60min)

    print(f"Seeded {len(sma_candles_5min)} historical {long_timeframe}-min candles and {len(sma_candles_3min)} historical {short_timeframe}-min candles for MA.")
    print_data(f"INITIAL_MAC{long_timeframe}MIN", mac5min)
    print_data(f"INITIAL_MAC{short_timeframe}MIN", mac3min)
    print_data(f"INITIAL_MAC{trend_timeframe}MIN_TREND", mac60min)

    position = None
    trade_executed_since_last_long = False
    last_trade_summary = None

    start_time = datetime.now()
    current_trade_day = start_time.date()
    realized_pnl_today = 0.0
    daily_target_reached = False
    print(f"Strategy 1 started at {start_time.isoformat()}")
    print("Dhan does not provide Moving Average Channel directly; calculating MAC locally.")
    print_data("natural_gas_strategy1_CONFIG", config)
    print_data("natural_gas_strategy1_INSTRUMENT", instrument)
    startup_quote = fetch_quote(dhan, instrument)
    print_data("STARTUP_QUOTE", startup_quote)
    preflight_live_trading(dhan, config, instrument, startup_quote)

    # plotting removed — print MAC values instead
    while True:
        try:
            quote = fetch_quote(dhan, instrument)
            ltp = quote["ltp"]
            now = datetime.now()
            if now.date() != current_trade_day:
                current_trade_day = now.date()
                realized_pnl_today = 0.0
                daily_target_reached = False
                print_data("DAILY_PNL_LIMIT_RESET", {"trade_day": current_trade_day.isoformat()})

            daily_mark_price = exit_price_for_position(position, quote) if position else ltp
            daily_status = daily_target_status(
                realized_pnl_today, position, daily_mark_price, daily_target_config
            )
            if daily_status["hit"] and not daily_target_reached:
                print_data("DAILY_PNL_LIMIT_HIT", daily_status)
                daily_target_reached = True

            if (
                daily_target_reached
                and position is not None
                and daily_target_config.get("close_position_when_hit", True)
            ):
                exit_price = exit_price_for_position(position, quote)
                response = place_exit(dhan, instrument, position, quote, live_orders, edis_config)
                if order_accepted(response, live_orders):
                    exit_pnl = position_pnl(position, exit_price)
                    realized_pnl_today += exit_pnl
                    last_trade_summary = (
                        f"EXIT {position['side']} qty={position['quantity']} price={exit_price} "
                        f"reason={daily_status['reason']} pnl={exit_pnl:.2f}"
                    )
                    trade_executed_since_last_long = True
                    position = None
                    print_data(
                        "DAILY_PNL_LIMIT_STATUS",
                        daily_target_status(realized_pnl_today, position, exit_price, daily_target_config),
                    )
                else:
                    print(f"[EXIT ORDER NOT ACCEPTED] Daily P&L limit hit but position kept open. response={response}")

            # Directional trading candles use executable quote sides, not LTP:
            # LONG = buy/ask stream; SHORT = sell/bid stream.
            completed_short = short_candles.update(
                live_candle_price("SHORT", quote),
                tick_time=now,
                best_bid=quote.get("best_bid"),
                best_ask=quote.get("best_ask"),
            )
            completed_long = long_candles.update(
                live_candle_price("LONG", quote),
                tick_time=now,
                best_bid=quote.get("best_bid"),
                best_ask=quote.get("best_ask"),
            )
            completed_trend = trend_candles.update(
                ltp, tick_time=now, best_bid=quote.get("best_bid"), best_ask=quote.get("best_ask")
            )
            if completed_trend is not None:
                sma_candles_60min = append_completed_candle(sma_candles_60min, completed_trend)
                mac60min = mac_channel(sma_candles_60min)
                print(f"\n===== {trend_timeframe}MIN TREND UPDATE =====")
                print(f"TREND_CANDLE_CLOSED: {completed_trend}")
                print_data(f"MAC{trend_timeframe}MIN_TREND", mac60min)
                print_data(
                    "CANDLE_DIAGNOSTIC",
                    build_candle_diagnostic(trend_timeframe, completed_trend, mac60min),
                )
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
                print_data(
                    "CANDLE_DIAGNOSTIC",
                    build_candle_diagnostic(long_timeframe, completed_long, mac5min),
                )

                # 5-min MAC entry/exit logic for long trades only.
                if mac5min is not None:
                    long_channel = mac5min
                    long_open = completed_long.get("open")
                    long_close = completed_long.get("close")
                    if position is not None and position["side"] == "LONG":
                        if long_close < long_channel["low"]:
                            exit_reason = "MAC_BREAK_LOWER"
                        else:
                            exit_reason = should_exit(
                                position, exit_price_for_position(position, quote), exit_config
                            )

                        if exit_reason:
                            exit_price = exit_price_for_position(position, quote)
                            response = place_exit(dhan, instrument, position, quote, live_orders, edis_config)
                            if order_accepted(response, live_orders):
                                exit_pnl = position_pnl(position, exit_price)
                                realized_pnl_today += exit_pnl
                                status = daily_target_status(realized_pnl_today, None, exit_price, daily_target_config)
                                daily_target_reached = daily_target_reached or status["hit"]
                                last_trade_summary = f"EXIT {position['side']} qty={position['quantity']} price={exit_price} reason={exit_reason} pnl={exit_pnl:.2f}"
                                trade_executed_since_last_long = True
                                position = None
                                print_data("DAILY_TARGET_STATUS", status)
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

                    if position is None:
                        if daily_target_reached and daily_target_config.get("block_new_entries_after_hit", True):
                            if long_raw_signal and long_side_enabled:
                                print_data(
                                    "ENTRY_SKIPPED_DAILY_TARGET",
                                    daily_target_status(realized_pnl_today, position, ltp, daily_target_config),
                                )
                            signal = None
                        elif long_raw_signal and long_side_enabled:
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
                            quantity = quantity_with_trend_filter(
                                config,
                                instrument,
                                signal,
                                ltp,
                                mac60min,
                                trend_config,
                            )
                            if quantity <= 0:
                                print_data(
                                    "ENTRY_SKIPPED_ZERO_QUANTITY",
                                    {"side": signal, "ltp": ltp, "trend_mac": mac60min},
                                )
                                signal = None

                        if signal:
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
                                }
                                trade_executed_since_last_long = True
                                last_trade_summary = f"ENTRY {signal} qty={quantity} price={ltp}"
                            else:
                                print(f"[ENTRY ORDER NOT ACCEPTED] No position opened. response={response}")

                # if any trade executed in the last long interval, print a prominent banner
                if trade_executed_since_last_long and last_trade_summary:
                    print("\n******************************")
                    print(f"*** STRATEGY EXECUTED ({long_timeframe}min) ***")
                    print(last_trade_summary)
                    print("******************************\n")
                    trade_executed_since_last_long = False
                    last_trade_summary = None

            exit_reason = should_exit(position, exit_price_for_position(position, quote), exit_config)
            if exit_reason:
                print(f"[EXIT] {exit_reason} position={position}")
                exit_price = exit_price_for_position(position, quote)
                response = place_exit(dhan, instrument, position, quote, live_orders, edis_config)
                if order_accepted(response, live_orders):
                    try:
                        exit_pnl = position_pnl(position, exit_price)
                        realized_pnl_today += exit_pnl
                        status = daily_target_status(realized_pnl_today, None, exit_price, daily_target_config)
                        daily_target_reached = daily_target_reached or status["hit"]
                        last_trade_summary = f"EXIT {position['side']} qty={position['quantity']} price={exit_price} reason={exit_reason} pnl={exit_pnl:.2f}"
                        trade_executed_since_last_long = True
                        print_data("DAILY_TARGET_STATUS", status)
                    except Exception:
                        last_trade_summary = f"EXIT position price={quote['ltp']} reason={exit_reason}"
                        trade_executed_since_last_long = True
                    position = None
                else:
                    print(f"[EXIT ORDER NOT ACCEPTED] Keeping position open. response={response}")
            # When a 3-minute candle completes, evaluate entry using the 3-minute MAC.
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
                print_data(
                    "CANDLE_DIAGNOSTIC",
                    build_candle_diagnostic(short_timeframe, completed_short, mac3min),
                )

                # ensure we have enough data for MAC and no open position
                if mac3min is not None:
                    # 3-min MAC entry/exit logic for short trades only.
                    if position is not None and position["side"] == "SHORT":
                        if short_close > mac3min["high"]:
                            exit_reason = "MAC_BREAK_UPPER"
                        else:
                            exit_reason = should_exit(
                                position, exit_price_for_position(position, quote), exit_config
                            )

                        if exit_reason:
                            exit_price = exit_price_for_position(position, quote)
                            response = place_exit(dhan, instrument, position, quote, live_orders, edis_config)
                            if order_accepted(response, live_orders):
                                exit_pnl = position_pnl(position, exit_price)
                                realized_pnl_today += exit_pnl
                                status = daily_target_status(realized_pnl_today, None, exit_price, daily_target_config)
                                daily_target_reached = daily_target_reached or status["hit"]
                                last_trade_summary = f"EXIT {position['side']} qty={position['quantity']} price={exit_price} reason={exit_reason} pnl={exit_pnl:.2f}"
                                trade_executed_since_last_long = True
                                position = None
                                print_data("DAILY_TARGET_STATUS", status)
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

                    if position is None:
                        if daily_target_reached and daily_target_config.get("block_new_entries_after_hit", True):
                            if short_raw_signal and short_side_enabled:
                                print_data(
                                    "ENTRY_SKIPPED_DAILY_TARGET",
                                    daily_target_status(realized_pnl_today, position, ltp, daily_target_config),
                                )
                            signal = None
                        elif short_raw_signal and short_side_enabled:
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
                            quantity = quantity_with_trend_filter(
                                config,
                                instrument,
                                signal,
                                ltp,
                                mac60min,
                                trend_config,
                            )
                            if quantity <= 0:
                                print_data(
                                    "ENTRY_SKIPPED_ZERO_QUANTITY",
                                    {"side": signal, "ltp": ltp, "trend_mac": mac60min},
                                )
                                signal = None

                        if signal:
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
                                }
                                trade_executed_since_last_long = True
                                last_trade_summary = f"ENTRY {signal} qty={quantity} price={ltp}"
                            else:
                                print(f"[ENTRY ORDER NOT ACCEPTED] No position opened. response={response}")

        except Exception as exc:
            print(f"[STRATEGY ERROR] {exc}")

        time.sleep(float(config.get("poll_interval_secs", 5)))


if __name__ == "__main__":
    run()
