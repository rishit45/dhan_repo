import random
from datetime import datetime, timedelta

import matplotlib.dates as mdates
import matplotlib.pyplot as plt


def average(values, length):
    if len(values) < length:
        return None
    return sum(values[-length:]) / length


def moving_average_channel_for_index(opens, closes, idx, length=20):
    # compute channel using the `length` values ending at idx (inclusive)
    if idx + 1 < length:
        return None
    window_opens = opens[idx + 1 - length : idx + 1]
    window_closes = closes[idx + 1 - length : idx + 1]
    avg_open = average(window_opens, length)
    avg_close = average(window_closes, length)
    if avg_open is None or avg_close is None:
        return None
    upper = max(avg_open, avg_close)
    lower = min(avg_open, avg_close)
    return avg_open, avg_close, upper, lower


def plot_candles_and_mac(times, opens, highs, lows, closes, length=20, title="Candles + MAC"):
    dates = mdates.date2num(times)

    fig, ax = plt.subplots(figsize=(12, 6))

    # draw high-low lines and bodies
    for x, o, h, l, c in zip(dates, opens, highs, lows, closes):
        color = "green" if c >= o else "red"
        ax.vlines(x, l, h, color="black", linewidth=0.7)
        rect_height = abs(c - o)
        # ensure visible body when open==close
        if rect_height == 0:
            rect_height = 0.0001
        ax.add_patch(plt.Rectangle((x - 0.2, min(o, c)), 0.4, rect_height, color=color))

    # compute MAC series (avg_open/avg_close/upper/lower) aligned to times
    avg_open_series = [None] * len(closes)
    avg_close_series = [None] * len(closes)
    upper_series = [None] * len(closes)
    lower_series = [None] * len(closes)
    for i in range(len(closes)):
        mac = moving_average_channel_for_index(opens, closes, i, length=length)
        if mac is not None:
            ao, ac, up, lo = mac
            avg_open_series[i] = ao
            avg_close_series[i] = ac
            upper_series[i] = up
            lower_series[i] = lo

    # plot MAC lines
    ax.plot_date(dates, avg_open_series, "-", label="avg_open", color="blue")
    ax.plot_date(dates, avg_close_series, "-", label="avg_close", color="orange")
    ax.plot_date(dates, upper_series, "--", label="upper", color="green")
    ax.plot_date(dates, lower_series, "--", label="lower", color="red")

    ax.xaxis_date()
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.set_title(title)
    ax.legend()
    plt.tight_layout()
    plt.show()


def demo():
    # generate synthetic candle data
    N = 80
    base = datetime.now().replace(second=0, microsecond=0) - timedelta(minutes=N * 5)
    times = [base + timedelta(minutes=5 * i) for i in range(N)]
    price = 100.0
    opens = []
    highs = []
    lows = []
    closes = []
    for _ in times:
        o = price + random.uniform(-0.5, 0.5)
        h = o + random.uniform(0, 1.0)
        l = o - random.uniform(0, 1.0)
        c = l + random.uniform(0, h - l)
        opens.append(round(o, 4))
        highs.append(round(h, 4))
        lows.append(round(l, 4))
        closes.append(round(c, 4))
        price = c

    plot_candles_and_mac(times, opens, highs, lows, closes, length=20, title="Synthetic Candles + MAC")


if __name__ == "__main__":
    demo()
