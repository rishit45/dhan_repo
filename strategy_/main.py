import time
import os
import sys
from datetime import datetime, timedelta

from buy import place_buy_order
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
    enabled_sides = set(entry_config.get("enabled_sides", ["LONG", "SHORT"]))
    mac_length = int(mac_config.get("length", 20))

    # Short and long candle stores. Long timeframe is 5 min, short timeframe is 3 min.
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
            interval=5,
            periods=int(mac_length),
            history_days=7,
        )
        short_history = fetch_historical_intraday_candles(
            dhan,
            instrument,
            interval=3,
            periods=int(mac_length),
            history_days=7,
        )
        if long_history:
            long_candles.load_history(long_history)
            print(f"Seeded {len(long_history)} historical 5-minute candles from Dhan")
        if short_history:
            short_candles.load_history(short_history)
            print(f"Seeded {len(short_history)} historical 3-minute candles from Dhan")
    except Exception as exc:
        print(f"Could not seed Dhan intraday candle history: {exc}")

    # wire MA period to indicators module
    indicators.MA_PERIOD = mac_length

    # initialize SMA candle containers from the already-closed historical candles
    sma_candles_5min = seed_sma_candles(long_candles.closed_candles)
    sma_candles_3min = seed_sma_candles(short_candles.closed_candles)
    mac5min = mac_channel(sma_candles_5min)
    mac3min = mac_channel(sma_candles_3min)

    print(f"Seeded {len(sma_candles_5min)} historical 5-min candles and {len(sma_candles_3min)} historical 3-min candles for MA.")
    print_data("INITIAL_MAC5MIN", mac5min)
    print_data("INITIAL_MAC3MIN", mac3min)

    position = None
    trade_executed_since_last_long = False
    last_trade_summary = None

    start_time = datetime.now()
    print(f"Strategy 1 started at {start_time.isoformat()}")
    print("Dhan does not provide Moving Average Channel directly; calculating MAC locally.")
    print_data("STRATEGY_1_CONFIG", config)
    print_data("STRATEGY_1_INSTRUMENT", instrument)
    startup_quote = fetch_quote(dhan, instrument)
    print_data("STARTUP_QUOTE", startup_quote)
    preflight_live_trading(dhan, config, instrument, startup_quote)

    # plotting removed — print MAC values instead
    while True:
        try:
            quote = fetch_quote(dhan, instrument)
            ltp = quote["ltp"]
            # update both candle stores with the latest ltp
            completed_short = short_candles.update(ltp)
            completed_long = long_candles.update(ltp)
            # compute and print MAC and candles only when a long candle closes
            if completed_long is not None:
                sma_candles_5min = append_completed_candle(sma_candles_5min, completed_long)
                mac5min = mac_channel(sma_candles_5min)
                if mac5min is None:
                    print(f"MAC5MIN waiting for {indicators.MA_PERIOD} candles, current count={len(sma_candles_5min)}")

                # get last closed short candle if available
                last_short = None
                try:
                    last_short = list(short_candles.closed_candles)[-1]
                except Exception:
                    last_short = None

                print("\n===== 5MIN UPDATE =====")
                print(f"Strategy start: {start_time.isoformat()}")
                print(f"LONG_CANDLE_CLOSED: {completed_long}")
                if last_short is not None:
                    print(f"LAST_SHORT_CANDLE: {last_short}")
                print_data("MAC5MIN", mac5min)

                # 5-min MAC entry/exit logic for long trades only.
                if mac5min is not None:
                    long_channel = mac5min
                    long_close = completed_long.get("close")
                    if position is not None and position["side"] == "LONG":
                        if long_close < long_channel["low"]:
                            exit_reason = "MAC_BREAK_LOWER"
                        else:
                            exit_reason = should_exit(position, ltp, exit_config)

                        if exit_reason:
                            response = place_exit(dhan, instrument, position, quote, live_orders, edis_config)
                            if order_accepted(response, live_orders):
                                last_trade_summary = f"EXIT {position['side']} qty={position['quantity']} price={ltp} reason={exit_reason}"
                                trade_executed_since_last_long = True
                                position = None
                            else:
                                print(f"[EXIT ORDER NOT ACCEPTED] Keeping position open. response={response}")

                    if position is None:
                        if long_close > long_channel["high"] and "LONG" in enabled_sides:
                            signal = "LONG"
                        else:
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
                                }
                                trade_executed_since_last_long = True
                                last_trade_summary = f"ENTRY {signal} qty={quantity} price={ltp}"
                            else:
                                print(f"[ENTRY ORDER NOT ACCEPTED] No position opened. response={response}")

                # if any trade executed in the last long interval, print a prominent banner
                if trade_executed_since_last_long and last_trade_summary:
                    print("\n******************************")
                    print("*** STRATEGY EXECUTED (5min) ***")
                    print(last_trade_summary)
                    print("******************************\n")
                    trade_executed_since_last_long = False
                    last_trade_summary = None

            exit_reason = should_exit(position, ltp, exit_config)
            if exit_reason:
                print(f"[EXIT] {exit_reason} position={position}")
                response = place_exit(dhan, instrument, position, quote, live_orders, edis_config)
                if order_accepted(response, live_orders):
                    try:
                        last_trade_summary = f"EXIT {position['side']} qty={position['quantity']} price={quote['ltp']} reason={exit_reason}"
                        trade_executed_since_last_long = True
                    except Exception:
                        last_trade_summary = f"EXIT position price={quote['ltp']} reason={exit_reason}"
                        trade_executed_since_last_long = True
                    position = None
                else:
                    print(f"[EXIT ORDER NOT ACCEPTED] Keeping position open. response={response}")
            # When a 3-minute candle completes, evaluate entry using the 3-minute MAC.
            if completed_short is not None:
                short_close = completed_short.get("close")
                sma_candles_3min = append_completed_candle(sma_candles_3min, completed_short)
                mac3min = mac_channel(sma_candles_3min)
                if mac3min is None:
                    print(f"MAC3MIN waiting for {indicators.MA_PERIOD} candles, current count={len(sma_candles_3min)}")

                print("\n===== 3MIN UPDATE =====")
                print(f"SHORT_CANDLE_CLOSED: {completed_short}")
                print_data("MAC3MIN", mac3min)

                # ensure we have enough data for MAC and no open position
                if mac3min is not None:
                    # 3-min MAC entry/exit logic for short trades only.
                    if position is not None and position["side"] == "SHORT":
                        if short_close > mac3min["high"]:
                            exit_reason = "MAC_BREAK_UPPER"
                        else:
                            exit_reason = should_exit(position, ltp, exit_config)

                        if exit_reason:
                            response = place_exit(dhan, instrument, position, quote, live_orders, edis_config)
                            if order_accepted(response, live_orders):
                                last_trade_summary = f"EXIT {position['side']} qty={position['quantity']} price={ltp} reason={exit_reason}"
                                trade_executed_since_last_long = True
                                position = None
                            else:
                                print(f"[EXIT ORDER NOT ACCEPTED] Keeping position open. response={response}")

                    if position is None:
                        if short_close < mac3min["low"] and "SHORT" in enabled_sides:
                            signal = "SHORT"
                        else:
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
