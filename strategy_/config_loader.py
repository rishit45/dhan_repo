import json

from debug import print_data


CONFIG_PATH = "strategy_config.json"


def load_config(path=CONFIG_PATH):
    with open(path, "r") as config_file:
        return json.load(config_file)


def get_instrument(config):
    instrument = config["instrument"].copy()
    instrument["security_id"] = str(instrument["security_id"])
    instrument["exchange_segment"] = instrument["exchange_segment"].upper()
    instrument["lot_size"] = int(instrument.get("lot_size", 1))
    return instrument


def get_quantity(config, ltp=None):
    settings = config.get("quantity", {})
    mode = str(settings.get("mode", "fixed")).lower()
    lot_size = int(config["instrument"].get("lot_size", 1))

    if mode == "fixed":
        quantity = int(settings.get("value", lot_size))
    elif mode == "capital":
        if ltp is None:
            raise ValueError("ltp is required when quantity.mode is capital")
        capital = float(settings.get("capital", 0))
        lots = int(capital // (float(ltp) * lot_size))
        quantity = max(lots, 1) * lot_size
    else:
        raise ValueError("quantity.mode must be fixed or capital")

    if quantity < lot_size:
        final_quantity = lot_size
    else:
        final_quantity = (quantity // lot_size) * lot_size

    print_data(
        "STRATEGY_1_QUANTITY",
        {"mode": mode, "ltp": ltp, "lot_size": lot_size, "quantity": final_quantity},
    )
    return final_quantity


def live_orders_enabled(config):
    return bool(config.get("live_orders", False))
