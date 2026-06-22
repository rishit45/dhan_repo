import time

from candles import get_instrument
from config_loader import get_all_instrument_configs, load_config
from oi_tracker import OITracker
from signal_generator import (
    POLL_INTERVAL_SECS,
    dhan,
    run_strategy_cycle,
)


def print_ohlc(all_instruments):
    for instrument_key in all_instruments:
        instrument = get_instrument(instrument_key)
        candle = instrument.consume_completed()
        if candle is None:
            continue
        print(
            "[1M CLOSED] {key} time={time} O={open} H={high} L={low} C={close}".format(
                key=instrument_key,
                time=candle["time"].strftime("%H:%M"),
                open=candle["open"],
                high=candle["high"],
                low=candle["low"],
                close=candle["close"],
            )
        )


if __name__ == "__main__":
    raw_config = load_config()
    all_instruments = get_all_instrument_configs(raw_config)
    strategy_config = raw_config.get("strategy", {})
    poll_interval = strategy_config.get("poll_interval_secs", POLL_INTERVAL_SECS)
    fire_once = strategy_config.get("fire_once_per_instrument", True)
    place_live_orders = strategy_config.get("place_live_orders", False)

    oi_config = raw_config.get("oi_tracker", {})
    oi_tracker = OITracker(dhan, oi_config)
    oi_rate_limit = float(oi_config.get("rate_limit_secs", 3))
    next_oi_refresh = 0

    fired = set()
    previous_ltps = {}

    print("Main cloud loop started")

    while True:
        now = time.time()

        run_strategy_cycle(
            all_instruments,
            fired,
            previous_ltps,
            fire_once=fire_once,
            place_live_orders=place_live_orders,
        )
        print_ohlc(all_instruments)

        if oi_config.get("enabled", False) and now >= next_oi_refresh:
            try:
                oi_result = oi_tracker.refresh()
                oi_tracker.print_result(oi_result)
            except Exception as exc:
                print(f"[OI] failed: {exc}")
            next_oi_refresh = now + oi_rate_limit

        time.sleep(poll_interval)
