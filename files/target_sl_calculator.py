def calculate_target_price(entry_price, transaction_type, target_mode, target_value):
    """
    Computes the final target (take-profit) price.

    Args:
        entry_price       (float) - price at which the position was entered
        transaction_type   (str)  - "BUY" or "SELL"
        target_mode        (str)  - "points" or "absolute"
        target_value       (float)- points to add/subtract, or the absolute price itself

    Returns:
        target_price (float) - the actual price at which to exit for profit
    """
    if target_mode == "absolute":
        target_price = target_value   # config already gives the final exit price directly
        return target_price

    # target_mode == "points"
    if transaction_type == "BUY":
        target_price = entry_price + target_value   # long position profits as price rises
    else:  # SELL
        target_price = entry_price - target_value    # short position profits as price falls

    return target_price


def calculate_sl_price(entry_price, transaction_type, sl_mode, sl_value):
    """
    Computes the final stop loss price.

    Args:
        entry_price       (float) - price at which the position was entered
        transaction_type   (str)  - "BUY" or "SELL"
        sl_mode             (str) - "points" or "absolute"
        sl_value            (float)- points to add/subtract, or the absolute price itself

    Returns:
        sl_price (float) - the actual price at which to exit for a loss cut
    """
    if sl_mode == "absolute":
        sl_price = sl_value   # config already gives the final exit price directly
        return sl_price

    # sl_mode == "points"
    if transaction_type == "BUY":
        sl_price = entry_price - sl_value   # long position is stopped out as price falls
    else:  # SELL
        sl_price = entry_price + sl_value    # short position is stopped out as price rises

    return sl_price


def calculate_target_and_sl(entry_price, transaction_type, instrument_config):
    """
    Convenience wrapper - takes one validated instrument config dict
    (as returned by config_loader.get_instrument_config) and returns
    both the target and SL price in one call.

    Args:
        entry_price        (float) - price at which the position was entered
        transaction_type    (str)  - "BUY" or "SELL"
        instrument_config   (dict) - validated config for this instrument

    Returns:
        target_price (float)
        sl_price     (float)
    """
    target_price = calculate_target_price(
        entry_price       = entry_price,
        transaction_type  = transaction_type,
        target_mode       = instrument_config["target_mode"],
        target_value      = instrument_config["target_value"],
    )

    sl_price = calculate_sl_price(
        entry_price       = entry_price,
        transaction_type  = transaction_type,
        sl_mode           = instrument_config["sl_mode"],
        sl_value          = instrument_config["sl_value"],
    )

    return target_price, sl_price
