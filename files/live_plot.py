import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from collections import deque
from statistics import mean
from datetime import timedelta


class LivePlot:
    def __init__(self, maxlen=300, mac_length=20, long_candle_minutes=5):
        plt.ion()
        self.maxlen = maxlen
        self.mac_length = int(mac_length)
        self.long_candle_minutes = int(long_candle_minutes)

        self.times_long = deque(maxlen=maxlen)
        self.opens_long = deque(maxlen=maxlen)
        self.highs_long = deque(maxlen=maxlen)
        self.lows_long = deque(maxlen=maxlen)
        # draw long candles and MAC
        self._draw_candles(self.ax_long, self.times_long, self.opens_long, self.highs_long, self.lows_long, self.closes_long)
        # compute MAC series and plot
        avg_open = []
        avg_close = []
        upper = []
        lower = []
        if len(self.closes_long) >= self.mac_length:
            for i in range(len(self.closes_long)):
                if i + 1 < self.mac_length:
                    avg_open.append(None)
                    avg_close.append(None)
                    upper.append(None)
                    lower.append(None)
                    continue
                window_opens = list(self.opens_long)[i + 1 - self.mac_length : i + 1]
                window_closes = list(self.closes_long)[i + 1 - self.mac_length : i + 1]
                ao = mean(window_opens)
                ac = mean(window_closes)
                avg_open.append(ao)
                avg_close.append(ac)
                upper.append(max(ao, ac))
                lower.append(min(ao, ac))

            self.ax_long.plot(self.times_long, avg_open, color="blue", label="avg_open")
            self.ax_long.plot(self.times_long, avg_close, color="orange", label="avg_close")
            self.ax_long.plot(self.times_long, upper, color="green", linestyle="--", label="upper")
            self.ax_long.plot(self.times_long, lower, color="red", linestyle="--", label="lower")
            self.ax_long.legend(loc="upper left")

        # set x-axis to full day range so times are continuous across the day
        all_times = list(self.times_long) if self.times_long else list(self.times_short)
        if all_times:
            first_time_num = all_times[0]
            # compute day start (midnight) for the first_time
            first_dt = mdates.num2date(first_time_num)
            day_start = first_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start + timedelta(days=1)
            day_start_num = mdates.date2num(day_start)
            day_end_num = mdates.date2num(day_end)
            self.ax_long.set_xlim(day_start_num, day_end_num)
            self.ax_short.set_xlim(day_start_num, day_end_num)

        # compute y-limits for long pane including MAC with padding
        long_values = []
        long_values.extend([v for v in list(self.opens_long) if v is not None])
        long_values.extend([v for v in list(self.highs_long) if v is not None])
        long_values.extend([v for v in list(self.lows_long) if v is not None])
        long_values.extend([v for v in list(self.closes_long) if v is not None])
        long_values.extend([v for v in avg_open if v is not None])
        long_values.extend([v for v in avg_close if v is not None])
        long_values.extend([v for v in upper if v is not None])
        long_values.extend([v for v in lower if v is not None])
        if long_values:
            min_v = min(long_values)
            max_v = max(long_values)
            span = max_v - min_v if max_v != min_v else max_v * 0.01
            pad = max(span * 0.12, 0.0001)
            self.ax_long.set_ylim(min_v - pad, max_v + pad)

        # draw short candles and markers
        self._draw_candles(self.ax_short, self.times_short, self.opens_short, self.highs_short, self.lows_short, self.closes_short)
        for t, price, side in self.markers:
            if side in ("LONG", "BUY"):
                color = "blue"
                label = "BUY"
            elif side in ("SHORT", "SELL"):
                color = "magenta"
                label = "SELL"
            else:
                color = "black"
                label = str(side)
            self.ax_short.scatter(t, price, color=color, s=60, zorder=10)
            self.ax_short.text(t, price, label, fontsize=8, rotation=45, verticalalignment="bottom")

        # compute y-limits for short pane including markers
        short_values = []
        short_values.extend([v for v in list(self.opens_short) if v is not None])
        short_values.extend([v for v in list(self.highs_short) if v is not None])
        short_values.extend([v for v in list(self.lows_short) if v is not None])
        short_values.extend([v for v in list(self.closes_short) if v is not None])
        short_values.extend([price for _, price, _ in self.markers])
        if short_values:
            min_v = min(short_values)
            max_v = max(short_values)
            span = max_v - min_v if max_v != min_v else max_v * 0.01
            pad = max(span * 0.12, 0.0001)
            self.ax_short.set_ylim(min_v - pad, max_v + pad)

        self.fig_long.canvas.draw()
        self.fig_long.canvas.flush_events()
        self.fig_short.canvas.draw()
        self.fig_short.canvas.flush_events()

        # draw short candles and markers
        self._draw_candles(self.ax_short, self.times_short, self.opens_short, self.highs_short, self.lows_short, self.closes_short)
        for t, price, side in self.markers:
            if side in ("LONG", "BUY"):
                color = "blue"
                label = "BUY"
            elif side in ("SHORT", "SELL"):
                color = "magenta"
                label = "SELL"
            else:
                color = "black"
                label = str(side)
            self.ax_short.scatter(t, price, color=color, s=60, zorder=10)
            self.ax_short.text(t, price, label, fontsize=8, rotation=45, verticalalignment="bottom")

        self.fig_long.canvas.draw()
        self.fig_long.canvas.flush_events()
        self.fig_short.canvas.draw()
        self.fig_short.canvas.flush_events()
