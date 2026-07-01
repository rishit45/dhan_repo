import time

from buy import place_buy_order
from candles import CandleStore
from config_loader import get_quantity, live_orders_enabled, load_config
from debug import print_data
from exit_manager import should_exit
from indicators import moving_average_channel, short_entry_signal
from margin import estimate_margin_per_lot
from market_data import create_dhan, fetch_ltp, fetch_quote
from option_selector import get_current_week_expiry, fetch_option_chain, select_options_from_oi
from sell import place_sell_order


def short_entry_price(entry_config, quote):
    source = entry_config.get("short_limit_price_source", "best_ask")
    return quote.get(source) or quote["ltp"]


def place_short_entry(dhan, instrument, quantity, entry_config, quote, live_orders, edis_config):
    order_type = entry_config.get("order_type", "LIMIT").upper()
    price = 0 if order_type == "MARKET" else short_entry_price(entry_config, quote)
    return place_sell_order(dhan, instrument, quantity, order_type, price, live_orders, edis_config)


def place_short_exit(dhan, instrument, position, quote, live_orders):
    return place_buy_order(dhan, instrument, position["quantity"], "LIMIT", quote["ltp"], live_orders)


def refresh_selected_options(dhan, config):
    underlying = config["underlying"]
    selection_config = config.get("option_selection", {})
    defaults = config.get("instrument_defaults", {})
    expiry = get_current_week_expiry(dhan, underlying)
    underlying_ltp = fetch_ltp(dhan, underlying["exchange_segment"], underlying["security_id"])
    chain = fetch_option_chain(dhan, underlying, expiry)
    selected = select_options_from_oi(chain, underlying_ltp, selection_config, defaults)
    print(
        "[OI SELECT] expiry={expiry} underlying_ltp={ltp} maxCE={max_ce} maxPE={max_pe} CE={ce} PE={pe}".format(
            expiry=expiry,
            ltp=underlying_ltp,
            max_ce=selected["max_oi_ce_strike"],
            max_pe=selected["max_oi_pe_strike"],
            ce=selected["CE"],
            pe=selected["PE"],
        )
    )
    return {key: value for key, value in selected.items() if key in {"CE", "PE"} and value}


def run():
    config = load_config()
    dhan = create_dhan(config)
    live_orders = live_orders_enabled(config)
    candle_config = config.get("candles", {})
    mac_config = config.get("moving_average_channel", {})
    entry_config = config.get("entry", {})
    exit_config = config.get("exit", {})
    edis_config = config.get("edis", {})
    selection_config = config.get("option_selection", {})
    mac_length = int(mac_config.get("length", 55))
    refresh_secs = float(selection_config.get("refresh_secs", 300))

    selected_options = {}
    candles = {}
    positions = {}
    next_refresh = 0

    print("Strategy 2 started")
    print("Short-only NIFTY current-week option strategy using OI selection and 5MIN MAC55.")
    print_data("STRATEGY_2_CONFIG", config)

    while True:
        now = time.time()
        try:
            if now >= next_refresh or not selected_options:
                selected_options = refresh_selected_options(dhan, config)
                print_data("STRATEGY_2_SELECTED_OPTIONS", selected_options)
                for key in selected_options:
                    candles.setdefault(
                        key,
                        CandleStore(
                            timeframe_minutes=candle_config.get("timeframe_minutes", 5),
                            history_len=candle_config.get("history_len", 300),
                        ),
                    )
                next_refresh = now + refresh_secs

            for option_key, instrument in selected_options.items():
                quote = fetch_quote(dhan, instrument)
                ltp = quote["ltp"]
                completed = candles[option_key].update(ltp)
                print_data(
                    "STRATEGY_2_QUOTE",
                    {
                        "option_key": option_key,
                        "instrument": instrument,
                        "quote": quote,
                    },
                )
                print_data(
                    "STRATEGY_2_CURRENT_CANDLE",
                    {
                        "option_key": option_key,
                        "candle": candles[option_key].current_candle,
                    },
                )
                print_data("STRATEGY_2_POSITIONS", positions)
                print(
                    "[QUOTE] {key} {symbol} ltp={ltp} bid={bid} ask={ask}".format(
                        key=option_key,
                        symbol=instrument["tradingsymbol"],
                        ltp=ltp,
                        bid=quote["best_bid"],
                        ask=quote["best_ask"],
                    )
                )

                position = positions.get(option_key)
                exit_reason = should_exit(position, ltp, exit_config)
                print_data(
                    "STRATEGY_2_EXIT_CHECK",
                    {"option_key": option_key, "ltp": ltp, "exit_reason": exit_reason},
                )
                if exit_reason:
                    print(f"[EXIT] {option_key} {exit_reason} position={position}")
                    place_short_exit(dhan, instrument, position, quote, live_orders)
                    positions.pop(option_key, None)

                if completed is None:
                    continue

                opens = candles[option_key].opens()
                closes = candles[option_key].closes()
                past_opens = opens[:-1]
                past_closes = closes[:-1]
                previous_close = past_closes[-1] if past_closes else None
                channel = moving_average_channel(
                    past_opens,
                    past_closes,
                    length=mac_length,
                )
                print_data(
                    "STRATEGY_2_SIGNAL_DATA",
                    {
                        "option_key": option_key,
                        "completed": completed,
                        "past_opens": past_opens,
                        "past_closes": past_closes,
                        "previous_close": previous_close,
                        "current_close": completed["close"],
                        "mac": channel,
                    },
                )
                print(f"[5MIN CLOSED] {option_key} {completed} MAC={channel}")

                if option_key in positions:
                    continue

                signal = short_entry_signal(completed["close"], previous_close, channel)
                print_data("STRATEGY_2_ENTRY_SIGNAL", {"option_key": option_key, "signal": signal})
                if signal != "SHORT":
                    continue

                margin_required = estimate_margin_per_lot(
                    dhan,
                    instrument,
                    instrument.get("lot_size", 50),
                    ltp,
                )
                quantity = get_quantity(config, ltp=ltp, margin_required=margin_required)
                place_short_entry(dhan, instrument, quantity, entry_config, quote, live_orders, edis_config)
                positions[option_key] = {
                    "side": "SHORT",
                    "entry_price": ltp,
                    "quantity": quantity,
                }
                print(f"[ENTRY] {option_key} {positions[option_key]} MAC={channel}")

        except Exception as exc:
            print(f"[STRATEGY ERROR] {exc}")

        time.sleep(float(config.get("poll_interval_secs", 5)))


if __name__ == "__main__":
    run()
