# Crude Oil Strategy

Live MCX crude oil strategy copied from `strategy_` with crude-specific settings.

## Rules

- Instrument: `CRUDEOIL JUL FUT`, security id `520702`, lot size `100`.
- Candles are built from Dhan quote/ticker LTP, same as `strategy_`.
- Long timeframe: 5 minutes.
- Short timeframe: 4 minutes.
- MAC length: 55.
- Long entry: previous 5-minute close <= MAC high and current 5-minute close > MAC high, or previous close < MAC high and new candle open > MAC high.
- Short entry: previous 4-minute close >= MAC low and current 4-minute close < MAC low, or previous close > MAC low and new candle open < MAC low.
- Entry price confirmation logs a warning when the projected order price has moved back inside MAC. Set `entry.price_confirmation` to `block` if you want those trades skipped.
- Long MAC exit: 5-minute close < MAC low.
- Short MAC exit: 4-minute close > MAC high.
- Candle stop: 3 points beyond the reference candle.

For the candle stop, entry candle is candle 1. Candles 1, 2, and 3 use candle 1 as the reference. From candle 4 onward, candle `n` uses candle `n-2`.

Long stop = reference candle low - 3. The stop exit is checked using the closed long candle close, not every live tick.

Short stop = reference candle high + 3. The stop exit is checked using the closed short candle close, not every live tick.

## Run

```powershell
cd crude_oil_strategy
py main.py
```

`live_orders` is enabled in `strategy_config.json`, so startup confirmation is required before live orders can be placed.

## Backtest

```powershell
cd crude_oil_strategy
py backtest.py
```

Date/time format examples:

```text
2026-07-01 09:00
2026-07-01 23:30
```

The backtester fetches 1-minute Dhan historical candles, treats each 1-minute close as the LTP tick, and builds the 5-minute long candles plus 4-minute short candles locally.

Modes:

- `instant`: calculates the result immediately.
- `replay`: walks through historical candles and prints candles/trades as it goes.
