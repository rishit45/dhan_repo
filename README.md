Project README — Configuration & variables

Overview
- This repository contains several trading strategies and helper modules. Key strategy code lives under `strategy_/` and `strategy 2/`.
- This README lists configuration variables, where to change them, how to seed historical candles, and how to reference values in code.

Where to edit configuration
- Primary strategy config: `strategy_/strategy_config.json` (used by `strategy_/main.py`).
- Generic/local configs: `files/config.json` and `files/config_loader.py` helpers.

Environment variables
- `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN` — required for Dhan API access when using Dhan clients. Set these in your shell or CI environment.

Top-level config keys (example: `strategy_/strategy_config.json`)
- `dhan`: object with Dhan API credentials and client settings.
  - `client_id`, `access_token` (often provided via env vars instead)
- `live_orders`: boolean. If `false`, orders are not sent to broker.
- `poll_interval_secs`: float. Main loop sleep between quote polls (default 5).

Instrument / trading
- `instrument` (object): the instrument the strategy trades:
  - `name`: logical name used in code
  - `tradingsymbol`: human symbol name
  - `security_id`: string or number used by Dhan API (ensure it's a string in config)
  - `exchange_segment`: e.g., `MCX_COMM`, `NSE`, etc.
  - `lot_size`: integer
  - `product_type`: e.g., `INTRADAY`
  - `validity`: e.g., `DAY`

Position sizing
- `quantity`: object
  - `mode`: `fixed` or other modes supported by your code
  - `value`: quantity or lot count when `mode` is `fixed`
  - `capital`: (optional) capital used for other sizing modes

Entry rules (`entry`)
- `enabled_sides`: array of strings, e.g., `["LONG", "SHORT"]`
- `order_type`: `LIMIT` or `MARKET`
- `long_limit_price_source`: when placing LONG LIMIT orders, source in quote dict (e.g., `best_bid`)
- `short_limit_price_source`: for SHORT LIMIT orders

Candles (`candles`)
- `timeframe_minutes`: integer for long candle timeframe (default 5)
- `short_timeframe_minutes`: optional short candle timeframe (default 1 in code)
- `history_len`: how many historical candles to retain
- `history_csv`: optional path to a CSV file to seed `CandleStore` at startup
  - CSV accepted fields: `time` (ISO or common formats), `open`, `high`, `low`, `close`.
  - Time formats accepted: ISO8601 or any of `"%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M"`
  - Example CSV header:
    time,open,high,low,close
    2026-06-30 09:15:00,123.4,125.0,122.9,124.5

Moving Average Channel (`moving_average_channel`)
- `moving_average_channel.length`: integer MAC length (default 20 in `strategy_/` and 55 in `strategy 2/`).
- Implementation:
  - Function signature: `moving_average_channel(opens, closes, length=20)`
  - Returns a dict with keys: `avg_open`, `avg_close`, `high`, `low`.
  - `high` == max(avg_open, avg_close)
  - `low` == min(avg_open, avg_close)
- Where used: `strategy_/main.py`, `strategy 2/main.py` (compute channel from long candle opens/closes)

Candle seeding and MAC initialization
- `strategy_/main.py` will load a CSV if `candles.history_csv` is present in `strategy_config.json`:

    history_path = candle_config.get("history_csv")
    if history_path:
        long_candles.load_history_from_csv(history_path)

- `CandleStore.load_history_from_csv(path, time_format=None)` reads rows and calls `load_history()`.
- `CandleStore` methods:
  - `update(price, tick_time=None)`: feed LTP to aggregator; returns a completed candle when a candle closes.
  - `closes()`: list of closed candle close values.
  - `opens()`: list of closed candle open values.
  - `load_history(candles)` and `load_history_from_csv(path, time_format=None)` to seed history.

Entry/exit logic (how to change behavior)
- Entry checks in `strategy_/main.py` use short-candle closes against the MAC computed from long candles.
  - Compute MAC via: `compute_long_channel(long_candles, mac_length)` -> channel dict
  - Channel keys: `channel['high']`, `channel['low']`.
  - Entry signal example (simplified):
    - if `short_close > channel['high']` => LONG entry
    - if `short_close < channel['low']` => SHORT entry
- Exits: `exit_manager.should_exit(position, ltp, exit_config)` supports two modes:
  - `mode: "points"` with `target_points`, `stop_loss_points`
  - `mode: "pnl"` with `target_pnl`, `stop_loss_pnl`
  - This function returns an exit reason string or `None`.

Order placement
- Entry/exit functions call `place_buy_order` / `place_sell_order` located in `strategy_/buy.py` and `strategy_/sell.py`.
- Orders use `instrument` dict fields: `security_id` and `exchange_segment`.

Useful code references
- `strategy_/main.py`: main loop, candle updates, MAC printing, entry/exit flow.
- `strategy_/candles.py`: `CandleStore` used by strategies.
- `strategy_/indicators.py`: `moving_average_channel(...)` and `entry_signal(...)` implementation.
- `files/config_loader.py`: helpers to build instrument dicts used across code.

Quick tips for making changes
- To change MAC length: edit `moving_average_channel.length` in `strategy_/strategy_config.json`.
- To seed MAC from historical data: create a CSV with `time,open,high,low,close` and set `candles.history_csv` to its path.
- To adjust polling: change `poll_interval_secs` in the strategy config.
- To enable live orders: set `live_orders` to `true` and ensure Dhan credentials are set.

If you want, I can:
- Generate a sample `history.csv` using recent LTPs.
- Add inline comments to `strategy_/strategy_config.json` explaining each field.

---
File locations referenced above:
- `strategy_/strategy_config.json` (main strategy config)
- `strategy_/main.py` (main loop)
- `strategy_/candles.py` (candle store)
- `strategy_/indicators.py` (MAC code)
- `files/config_loader.py` (instrument building)

