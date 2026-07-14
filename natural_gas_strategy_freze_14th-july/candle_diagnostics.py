"""Run a candle-level MAC diagnostic for the natural-gas strategy.

Examples:
    py candle_diagnostics.py
    py candle_diagnostics.py --candle "2026-07-13 10:15" --timeframe 5
"""

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

from backtest import aggregate_candles, fetch_1min_history
from config_loader import get_instrument
import indicators
from market_data import create_dhan, fetch_quote


STRATEGY_DIR = Path(__file__).resolve().parent
DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%d-%m-%Y %H:%M",
    "%d-%m-%Y %H:%M:%S",
)


def build_candle_diagnostic(timeframe_minutes, candle, channel, current_quote=None):
    """Return the LTP candle, close quote, and MAC values for one candle.

    The bid/ask fields are the last top-of-book values observed while that
    candle was live.  They are therefore unavailable for Dhan historical
    candles, which contain OHLC but no historical order-book snapshots.
    """
    return {
        "timeframe_minutes": int(timeframe_minutes),
        "candle_start": candle.get("time"),
        "ltp_candle": {
            "open": candle.get("open"),
            "high": candle.get("high"),
            "low": candle.get("low"),
            "close": candle.get("close"),
        },
        "bid_ask_at_candle_close": {
            "bid": candle.get("close_bid"),
            "ask": candle.get("close_ask"),
            "source": (
                "last Dhan quote observed before the candle closed"
                if candle.get("close_bid") is not None or candle.get("close_ask") is not None
                else "not available from Dhan intraday historical OHLC data"
            ),
        },
        "mac_channel_at_candle_close": (
            None
            if channel is None
            else {"high": channel.get("high"), "low": channel.get("low")}
        ),
        "current_dhan_quote": (
            None
            if current_quote is None
            else {
                "observed_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
                "ltp": current_quote.get("ltp"),
                "bid": current_quote.get("best_bid"),
                "ask": current_quote.get("best_ask"),
                "source": current_quote.get("source"),
                "note": "This is the quote at diagnostic run time, not a historical quote for the selected candle.",
            }
        ),
    }


def parse_datetime(value):
    value = str(value).strip()
    for fmt in DATETIME_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError("Use --candle in YYYY-MM-DD HH:MM format")


def load_strategy_config():
    """Load the config beside this script, independent of the shell cwd."""
    with (STRATEGY_DIR / "strategy_config.json").open(encoding="utf-8") as config_file:
        return json.load(config_file)


def channel_for_candle(candles, target_time, mac_length):
    rows = []
    target_candle = None
    target_channel = None
    for candle in candles:
        rows.append({
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": float(candle["close"]),
        })
        if len(rows) < mac_length:
            channel = None
        else:
            channel = {
                "high": sum(row["high"] for row in rows[-mac_length:]) / mac_length,
                "low": sum(row["low"] for row in rows[-mac_length:]) / mac_length,
            }
        if candle["time"] == target_time:
            target_candle = candle
            target_channel = channel
            break
    return target_candle, target_channel


def build_parser():
    parser = argparse.ArgumentParser(
        description="Print an LTP candle, its MAC channel, and the current Dhan bid/ask."
    )
    parser.add_argument("--candle", help="Candle start time, e.g. '2026-07-13 10:15'. Defaults to latest closed candle.")
    parser.add_argument("--timeframe", type=int, help="Candle timeframe in minutes. Defaults to strategy long timeframe.")
    parser.add_argument("--warmup-days", type=int, default=10, help="History days used to calculate MAC (default: 10).")
    parser.add_argument("--list-candles", action="store_true", help="List available candle start times, then exit.")
    parser.add_argument("--list-limit", type=int, default=50, help="Number of candle times shown with --list-candles (default: 50).")
    return parser


def main():
    args = build_parser().parse_args()
    config = load_strategy_config()
    instrument = get_instrument(config)
    timeframe = int(args.timeframe or config.get("candles", {}).get("timeframe_minutes", 5))
    if timeframe < 1:
        raise ValueError("--timeframe must be at least 1 minute")

    mac_length = int(config.get("moving_average_channel", {}).get("length", 55))
    indicators.MA_PERIOD = mac_length
    requested_time = parse_datetime(args.candle) if args.candle else None
    history_start = requested_time or datetime.now()
    history_end = (requested_time + timedelta(days=1)) if requested_time else datetime.now()

    dhan = create_dhan(config)
    one_minute = fetch_1min_history(
        dhan,
        instrument,
        history_start,
        history_end,
        warmup_days=args.warmup_days,
    )
    candles = aggregate_candles(one_minute, timeframe)
    if not candles:
        raise RuntimeError("Dhan returned no candles for the requested period")

    if args.list_candles:
        limit = max(int(args.list_limit), 1)
        print(json.dumps({
            "timeframe_minutes": timeframe,
            "candle_starts": [candle["time"] for candle in candles[-limit:]],
        }, indent=2, default=str))
        return

    if requested_time:
        target_time = requested_time
    else:
        current_bucket = datetime.now().replace(second=0, microsecond=0)
        current_bucket -= timedelta(minutes=current_bucket.minute % timeframe)
        closed = [candle for candle in candles if candle["time"] < current_bucket]
        if not closed:
            raise RuntimeError("No completed candle is available yet")
        target_time = closed[-1]["time"]
    target, channel = channel_for_candle(candles, target_time, mac_length)
    if target is None:
        available = [candle["time"].isoformat(sep=" ") for candle in candles[-10:]]
        raise RuntimeError(f"No {timeframe}-minute candle starts at {target_time}. Recent starts: {available}")

    quote = fetch_quote(dhan, instrument)
    diagnostic = build_candle_diagnostic(timeframe, target, channel, quote)
    diagnostic["instrument"] = {
        "tradingsymbol": instrument.get("tradingsymbol"),
        "security_id": instrument["security_id"],
        "exchange_segment": instrument["exchange_segment"],
    }
    diagnostic["mac_length"] = mac_length
    print(json.dumps(diagnostic, indent=2, default=str))


if __name__ == "__main__":
    main()
