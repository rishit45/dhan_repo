import time
from datetime import datetime

from target_sl_calculator import calculate_target_and_sl


def place_exit_order(dhan, signal):
    """
    Places the opposite-side market order to close a position.
    """
    exit_transaction_type = "SELL" if signal["transaction_type"] == "BUY" else "BUY"

    return dhan.place_order(
        security_id=signal["security_id"],
        exchange_segment=signal["exchange_segment"],
        transaction_type=exit_transaction_type,
        quantity=signal["quantity"],
        order_type="MARKET",
        product_type=signal["product_type"],
        price=0,
        validity="DAY",
    )


def is_target_hit(ltp, transaction_type, target_price):
    """
    Args:
        ltp                (float) - current live traded price
        transaction_type    (str)  - "BUY" or "SELL", the side the position was opened with
        target_price        (float)- the computed target price

    Returns:
        bool - True if target is hit
    """
    if transaction_type == "BUY":
        return ltp >= target_price   # long position, profit as price rises to/above target
    else:
        return ltp <= target_price   # short position, profit as price falls to/below target


def is_sl_hit(ltp, transaction_type, sl_price):
    """
    Args:
        ltp                (float) - current live traded price
        transaction_type    (str)  - "BUY" or "SELL", the side the position was opened with
        sl_price             (float)- the computed stop loss price

    Returns:
        bool - True if SL is hit
    """
    if transaction_type == "BUY":
        return ltp <= sl_price   # long position, loss as price falls to/below SL
    else:
        return ltp >= sl_price   # short position, loss as price rises to/above SL


def is_square_off_time(square_off_time):
    """
    Args:
        square_off_time (time) - forced-exit time from config

    Returns:
        bool - True if current time >= square_off_time
    """
    now = datetime.now().time()   # current time of day
    return now >= square_off_time


def get_ltp(dhan, signal):
    """
    Fetches live LTP for the instrument in a signal, using dhanhq's get_ltp().

    Args:
        signal (dict) - the entry signal, must include exchange_segment + security_id

    Returns:
        ltp (float) - current live traded price
    """
    exchange_segment = signal["exchange_segment"]
    security_id      = signal["security_id"]

    instruments = [(exchange_segment, security_id)]
    response    = dhan.get_ltp(instruments)   # {"data": {exchange_segment: {security_id: {"last_price": ...}}}}

    ltp = response["data"][exchange_segment][security_id]["last_price"]  # current live price, float
    return ltp


def monitor_position(dhan, signal, instrument_config, poll_interval=2):
    """
    Polls live price for ONE open position until target, SL, or square-off
    time is hit, then places the exit order automatically.

    Args:
        signal              (dict) - the entry signal that was already executed
        instrument_config    (dict) - validated config for this instrument (for square_off_time)
        poll_interval         (float) - seconds between live price checks

    Returns:
        exit_reason (str) - "TARGET", "SL", or "SQUARE_OFF"
    """
    entry_price      = signal["reference_price"]    # price recorded at signal time
    transaction_type = signal["transaction_type"]    # "BUY" or "SELL"

    target_price, sl_price = calculate_target_and_sl(entry_price, transaction_type, instrument_config)

    print(f"[MONITOR] {signal['tradingsymbol']} | entry={entry_price} "
          f"target={target_price} sl={sl_price}")

    square_off_time = instrument_config["square_off_time"]   # forced-exit time from config

    while True:
        if is_square_off_time(square_off_time):
            place_exit_order(dhan, signal)
            return "SQUARE_OFF"

        ltp = get_ltp(dhan, signal)   # current live traded price

        if is_target_hit(ltp, transaction_type, target_price):
            place_exit_order(dhan, signal)
            return "TARGET"

        if is_sl_hit(ltp, transaction_type, sl_price):
            place_exit_order(dhan, signal)
            return "SL"

        time.sleep(poll_interval)
