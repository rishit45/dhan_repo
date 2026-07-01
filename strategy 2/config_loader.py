import json

from debug import print_data


CONFIG_PATH = "strategy_config.json"


def load_config(path=CONFIG_PATH):
    with open(path, "r") as config_file:
        return json.load(config_file)


def live_orders_enabled(config):
    return bool(config.get("live_orders", False))


def get_quantity(config, ltp=None, margin_required=None):
    settings = config.get("quantity", {})
    defaults = config.get("instrument_defaults", {})
    lot_size = int(defaults.get("lot_size", 1))
    mode = str(settings.get("mode", "lots")).lower()

    if mode == "lots":
        lots = int(settings.get("lots", 1))
    elif mode == "margin":
        margin_capital = float(settings.get("margin_capital", 0))
        if margin_required is None or margin_required <= 0:
            lots = int(settings.get("lots", 1))
        else:
            lots = int(margin_capital // margin_required)
            lots = max(lots, 1)
    elif mode == "premium_capital":
        if ltp is None:
            raise ValueError("ltp is required for premium_capital quantity mode")
        margin_capital = float(settings.get("margin_capital", 0))
        lots = int(margin_capital // (float(ltp) * lot_size))
        lots = max(lots, 1)
    else:
        raise ValueError("quantity.mode must be lots, margin, or premium_capital")

    quantity = lots * lot_size
    print_data(
        "STRATEGY_2_QUANTITY",
        {
            "mode": mode,
            "ltp": ltp,
            "margin_required": margin_required,
            "lot_size": lot_size,
            "lots": lots,
            "quantity": quantity,
        },
    )
    return quantity
