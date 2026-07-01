def average(values, length):
    if len(values) < length:
        return None
    return sum(values[-length:]) / length


def moving_average_channel(opens, closes, length=55):
    length = int(length)
    avg_open = average(opens, length)
    avg_close = average(closes, length)
    if avg_open is None or avg_close is None:
        return None

    return {
        "avg_open": avg_open,
        "avg_close": avg_close,
        "high": max(avg_open, avg_close),
        "low": min(avg_open, avg_close)
    }


def short_entry_signal(current_close, previous_close, channel):
    if channel is None or previous_close is None:
        return None
    if previous_close >= channel["low"] and current_close < channel["low"]:
        return "SHORT"
    return None
