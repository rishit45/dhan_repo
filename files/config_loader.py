import json
from datetime import datetime


CONFIG_PATH = "config.json"

VALID_ACTIONS = {"BUY", "SELL"}
VALID_ORDER_TYPES = {"MARKET", "LIMIT", "STOP_LOSS", "STOP_LOSS_MARKET"}
VALID_PRODUCT_TYPES = {"CNC", "INTRADAY", "MARGIN", "MTF"}
VALID_VALIDITIES = {"DAY", "IOC"}
VALID_MODES = {"points", "absolute"}
VALID_TRIGGER_OPERATORS = {
    "GREATER_THAN",
    "GREATER_THAN_EQUAL",
    "LESS_THAN",
    "LESS_THAN_EQUAL",
    "EQUAL",
    "CROSSING_UP",
    "CROSSING_DOWN",
}


def load_config(path=CONFIG_PATH):
    with open(path, "r") as f:
        return json.load(f)


def list_instruments(raw_config):
    return list(raw_config["instruments"].keys())


def _merged_value(instrument_settings, order_template, defaults, key, fallback=None):
    return instrument_settings.get(key, order_template.get(key, defaults.get(key, fallback)))


def get_instrument_config(raw_config, instrument_key):
    if instrument_key not in raw_config["instruments"]:
        raise KeyError(
            f"'{instrument_key}' not found in config.json. "
            f"Available: {list_instruments(raw_config)}"
        )

    instrument_settings = raw_config["instruments"][instrument_key]
    defaults = raw_config.get("defaults", {})
    order_template = raw_config.get("order_template", {})

    action = _merged_value(instrument_settings, order_template, defaults, "action", "BUY").upper()
    order_type = _merged_value(
        instrument_settings, order_template, defaults, "order_type", "LIMIT"
    ).upper()
    product_type = _merged_value(
        instrument_settings, order_template, defaults, "product_type", "INTRADAY"
    ).upper()
    validity = _merged_value(instrument_settings, order_template, defaults, "validity", "DAY").upper()
    target_mode = instrument_settings.get("target_mode", defaults.get("target_mode", "points"))
    sl_mode = instrument_settings.get("sl_mode", defaults.get("sl_mode", "points"))

    if action not in VALID_ACTIONS:
        raise ValueError(f"Invalid action '{action}' for {instrument_key}")
    if order_type not in VALID_ORDER_TYPES:
        raise ValueError(f"Invalid order_type '{order_type}' for {instrument_key}")
    if product_type not in VALID_PRODUCT_TYPES:
        raise ValueError(f"Invalid product_type '{product_type}' for {instrument_key}")
    if validity not in VALID_VALIDITIES:
        raise ValueError(f"Invalid validity '{validity}' for {instrument_key}")
    if target_mode not in VALID_MODES:
        raise ValueError(f"Invalid target_mode '{target_mode}' for {instrument_key}")
    if sl_mode not in VALID_MODES:
        raise ValueError(f"Invalid sl_mode '{sl_mode}' for {instrument_key}")

    trigger_operator = instrument_settings.get("trigger_operator")
    if trigger_operator is None:
        raise ValueError(f"Missing trigger_operator for {instrument_key}")

    trigger_operator = trigger_operator.upper()
    if trigger_operator not in VALID_TRIGGER_OPERATORS:
        raise ValueError(f"Invalid trigger_operator '{trigger_operator}' for {instrument_key}")

    if "trigger_price" not in instrument_settings:
        raise ValueError(f"Missing trigger_price for {instrument_key}")

    entry_time = datetime.strptime(instrument_settings.get("entry_time", "09:20"), "%H:%M").time()
    square_off_time = datetime.strptime(
        instrument_settings.get("square_off_time", "15:15"), "%H:%M"
    ).time()

    quantity = _merged_value(instrument_settings, order_template, defaults, "quantity", None)
    if quantity is None:
        quantity = instrument_settings.get("qty", 1)

    return {
        "tradingsymbol": instrument_settings["tradingsymbol"],
        "security_id": str(instrument_settings["security_id"]),
        "exchange_segment": instrument_settings["exchange_segment"].upper(),
        "action": action,
        "quantity": int(quantity),
        "order_type": order_type,
        "product_type": product_type,
        "validity": validity,
        "price": float(_merged_value(instrument_settings, order_template, defaults, "price", 0)),
        "trigger_operator": trigger_operator,
        "trigger_price": float(instrument_settings["trigger_price"]),
        "target_mode": target_mode,
        "target_value": float(instrument_settings["target_value"]),
        "sl_mode": sl_mode,
        "sl_value": float(instrument_settings["sl_value"]),
        "entry_time": entry_time,
        "square_off_time": square_off_time,
    }


def get_all_instrument_configs(raw_config):
    return {
        instrument_key: get_instrument_config(raw_config, instrument_key)
        for instrument_key in list_instruments(raw_config)
    }


if __name__ == "__main__":
    raw_config = load_config()
    for key in list_instruments(raw_config):
        print(key, "->", get_instrument_config(raw_config, key))
