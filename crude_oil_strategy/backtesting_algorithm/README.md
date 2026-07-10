# Strategy Backtester

Backtests the same MAC/cross strategy used by `strategy_`.

Run from this folder:

```powershell
py backtest.py
```

The script asks for:

- start datetime
- end datetime
- long candle timeframe
- short candle timeframe
- replay mode or instant mode

Modes:

- `replay`: prints candles/trades as if the market is running live.
- `instant`: calculates the full result immediately.

Notes:

- No live orders are placed.
- Historical 1-minute Dhan candles are used as LTP ticks by feeding each candle close into the same candle builder used by `strategy_`.
- Long entry requires previous long close <= MAC high and current long close > MAC high.
- Short entry requires previous short close >= MAC low and current short close < MAC low.
