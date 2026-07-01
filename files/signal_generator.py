import time
from datetime import datetime

from dhanhq import DhanContext, dhanhq

from candles import configure_timeframe, get_instrument, get_timeframe_label, process_tick
from config_loader import get_all_instrument_configs, load_config


def create_dhan_client():
    raw_config = load_config()
    dhan_config = raw_config.get("dhan", {})
    client_id = str(dhan_config.get("client_id", "")).strip()
    access_token = str(dhan_config.get("access_token", "")).strip()

    if not client_id or not access_token:
        print("[DHAN CONFIG] client_id/access_token missing in config.json -> dhan section")

    dhan_context = DhanContext(client_id, access_token)
    return dhanhq(dhan_context)


dhan = create_dhan_client()

POLL_INTERVAL_SECS = 5

EXCHANGE_SEGMENTS = {
    "NSE": dhanhq.NSE,
    "NSE_EQ": dhanhq.NSE,
    "BSE": dhanhq.BSE,
    "BSE_EQ": dhanhq.BSE,
    "NSE_FNO": dhanhq.NSE_FNO,
    "BSE_FNO": dhanhq.BSE_FNO,
    "MCX": dhanhq.MCX,
    "MCX_COMM": dhanhq.MCX,
    "CUR": dhanhq.CUR,
    "NSE_CURRENCY": dhanhq.CUR,
    "INDEX": dhanhq.INDEX,
    "IDX_I": dhanhq.INDEX,
}

TRANSACTION_TYPES = {
    "BUY": dhanhq.BUY,
    "SELL": dhanhq.SELL,
}

ORDER_TYPES = {
    "LIMIT": dhanhq.LIMIT,
    "MARKET": dhanhq.MARKET,
    "STOP_LOSS": dhanhq.SL,
    "SL": dhanhq.SL,
    "STOP_LOSS_MARKET": dhanhq.SLM,
    "SLM": dhanhq.SLM,
}

PRODUCT_TYPES = {
    "CNC": dhanhq.CNC,
    "INTRADAY": dhanhq.INTRA,
    "INTRA": dhanhq.INTRA,
    "MARGIN": dhanhq.MARGIN,
    "MTF": dhanhq.MTF,
}


def resolve_constant(mapping, value):
    return mapping.get(value, value)


def _security_id_for_quote(security_id):
    security_id = str(security_id)
    return int(security_id) if security_id.isdigit() else security_id


def _dhan_failure_message(response):
    remarks = response.get("remarks") if isinstance(response, dict) else None
    if isinstance(remarks, dict):
        useful = {key: value for key, value in remarks.items() if value not in ("", None)}
        if useful:
            return useful
    elif remarks not in ("", None):
        return remarks

    return response


def _extract_ltp(response, exchange_segment, security_id):
    if isinstance(response, dict) and response.get("status") == "failure":
        raise RuntimeError(f"Dhan quote failed: {_dhan_failure_message(response)}")

    data = response.get("data", response) if isinstance(response, dict) else response
    security_id = str(security_id)

    if data in ("", None):
        raise RuntimeError(f"Dhan quote returned empty data: {response}")

    if isinstance(data, dict):
        while isinstance(data.get("data"), dict):
            data = data["data"]

        segment_data = data.get(exchange_segment)
        if isinstance(segment_data, dict):
            quote = segment_data.get(security_id) or segment_data.get(int(security_id))
            if isinstance(quote, dict):
                for key in ("last_price", "lastPrice", "ltp", "LTP", "last_traded_price", "lastTradedPrice"):
                    if key in quote:
                        return float(quote[key])

        for key in ("last_price", "lastPrice", "ltp", "LTP", "last_traded_price", "lastTradedPrice"):
            if key in data:
                return float(data[key])

    raise RuntimeError(f"Could not read LTP from Dhan response: {response}")


def _build_securities_payload(all_instruments):
    securities = {}
    for instrument_config in all_instruments.values():
        exchange_segment = instrument_config["exchange_segment"]
        security_id = _security_id_for_quote(instrument_config["security_id"])
        securities.setdefault(exchange_segment, []).append(security_id)
    return securities


def get_ltp(instrument_config, quote_response=None):
    exchange_segment = instrument_config["exchange_segment"]
    security_id = instrument_config["security_id"]

    if quote_response is not None:
        return _extract_ltp(quote_response, exchange_segment, security_id)

    if hasattr(dhan, "get_ltp"):
        instruments = [(exchange_segment, security_id)]
        response = dhan.get_ltp(instruments)
    elif hasattr(dhan, "ticker_data"):
        response = dhan.ticker_data(
            securities={exchange_segment: [_security_id_for_quote(security_id)]}
        )
    else:
        raise AttributeError("This dhanhq version has neither get_ltp() nor ticker_data().")

    return _extract_ltp(response, exchange_segment, security_id)


def get_ltp_batch(all_instruments):
    if not hasattr(dhan, "ticker_data"):
        return None

    securities = _build_securities_payload(all_instruments)
    if not securities:
        return None

    return dhan.ticker_data(securities=securities)


def is_trigger_hit(ltp, operator, trigger_price, previous_ltp=None):
    if operator == "GREATER_THAN":
        return ltp > trigger_price
    if operator == "GREATER_THAN_EQUAL":
        return ltp >= trigger_price
    if operator == "LESS_THAN":
        return ltp < trigger_price
    if operator == "LESS_THAN_EQUAL":
        return ltp <= trigger_price
    if operator == "EQUAL":
        return ltp == trigger_price
    if operator == "CROSSING_UP":
        return previous_ltp is not None and previous_ltp < trigger_price <= ltp
    if operator == "CROSSING_DOWN":
        return previous_ltp is not None and previous_ltp > trigger_price >= ltp
    return False


def build_order(instrument_config, ltp):
    order_type = instrument_config["order_type"]
    price = instrument_config["price"]

    if order_type == "MARKET":
        price = 0
    elif price == 0:
        price = ltp

    return {
        "security_id": instrument_config["security_id"],
        "exchange_segment": resolve_constant(EXCHANGE_SEGMENTS, instrument_config["exchange_segment"]),
        "action": resolve_constant(TRANSACTION_TYPES, instrument_config["action"]),
        "quantity": instrument_config["quantity"],
        "order_type": resolve_constant(ORDER_TYPES, order_type),
        "product_type": resolve_constant(PRODUCT_TYPES, instrument_config["product_type"]),
        "price": price,
        "validity": instrument_config["validity"],
    }


def execute_order(order):
    return dhan.place_order(
        security_id=order["security_id"],
        exchange_segment=order["exchange_segment"],
        transaction_type=order["action"],
        quantity=order["quantity"],
        order_type=order["order_type"],
        product_type=order["product_type"],
        price=order["price"],
        validity=order["validity"],
    )


def evaluate_signal(instrument_key, instrument_config, previous_ltp=None, quote_response=None):
    now = datetime.now().time()
    if now < instrument_config["entry_time"]:
        return None

    ltp = get_ltp(instrument_config, quote_response=quote_response)
    instrument = process_tick(instrument_key, ltp)

    # Your strategy can use the forming candle's open/high/low.
    # close is filled only when the timeframe candle completes.
    # Closed candle values can be accessed like instrument["open-09:20"].
    triggered = is_trigger_hit(
        ltp=ltp,
        operator=instrument_config["trigger_operator"],
        trigger_price=instrument_config["trigger_price"],
        previous_ltp=previous_ltp,
    )

    return {
        "instrument_key": instrument_key,
        "ltp": ltp,
        "instrument": instrument,
        "triggered": triggered,
    }


def run_strategy_cycle(all_instruments, fired, previous_ltps, fire_once=True, place_live_orders=False):
    cycle_signals = []
    quote_response = None

    try:
        quote_response = get_ltp_batch(all_instruments)
    except Exception as exc:
        print(f"[LTP BATCH FAILED] falling back to per-instrument calls: {exc}")

    for instrument_key, instrument_config in all_instruments.items():
        already_fired = fire_once and instrument_key in fired

        try:
            signal = evaluate_signal(
                instrument_key,
                instrument_config,
                previous_ltp=previous_ltps.get(instrument_key),
                quote_response=quote_response,
            )
        except Exception as exc:
            print(
                "[LTP SKIP] {key} security_id={security_id} exchange={exchange}: {error}".format(
                    key=instrument_key,
                    security_id=instrument_config["security_id"],
                    exchange=instrument_config["exchange_segment"],
                    error=exc,
                )
            )
            continue

        if signal is None:
            continue

        previous_ltps[instrument_key] = signal["ltp"]
        cycle_signals.append(signal)

        if signal["triggered"] and not already_fired:
            order = build_order(instrument_config, signal["ltp"])
            print(f"[TRIGGER] {instrument_key} ltp={signal['ltp']} order={order}")
            if place_live_orders:
                placed_order = execute_order(order)
                print(f"[ORDER RESPONSE] {instrument_key}: {placed_order}")
            else:
                print(f"[ORDER DRY RUN] {instrument_key}: set strategy.place_live_orders=true to place live orders")
            fired.add(instrument_key)

    return cycle_signals


def print_ohlc(all_instruments):
    for instrument_key in all_instruments:
        instrument = get_instrument(instrument_key)
        candle = instrument.consume_completed()
        if candle is None:
            continue
        print(
            "[{timeframe} CLOSED] {key} time={time} O={open} H={high} L={low} C={close}".format(
                timeframe=get_timeframe_label(),
                key=instrument_key,
                time=candle["time"].strftime("%H:%M"),
                open=candle["open"],
                high=candle["high"],
                low=candle["low"],
                close=candle["close"],
            )
        )


def run_signal_generator():
    raw_config = load_config()
    all_instruments = get_all_instrument_configs(raw_config)
    strategy_config = raw_config.get("strategy", {})
    poll_interval = strategy_config.get("poll_interval_secs", POLL_INTERVAL_SECS)
    fire_once = strategy_config.get("fire_once_per_instrument", True)
    place_live_orders = strategy_config.get("place_live_orders", False)
    configure_timeframe(strategy_config.get("timeframe", "1min"))

    fired = set()
    previous_ltps = {}

    print("Dhan Cloud strategy started")

    while True:
        run_strategy_cycle(
            all_instruments,
            fired,
            previous_ltps,
            fire_once=fire_once,
            place_live_orders=place_live_orders,
        )
        print_ohlc(all_instruments)
        time.sleep(poll_interval)


if __name__ == "__main__":
    run_signal_generator()
