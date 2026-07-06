from debug import print_data


DERIVATIVE_SEGMENTS = {
    "NSE_FNO",
    "BSE_FNO",
    "MCX_COMM",
    "NSE_CURRENCY",
    "BSE_CURRENCY",
}

EQUITY_SEGMENTS = {"NSE_EQ", "BSE_EQ"}

ALLOWED_PRODUCTS = {
    "NSE_EQ": {"CNC", "INTRADAY", "MARGIN", "MTF"},
    "BSE_EQ": {"CNC", "INTRADAY", "MARGIN", "MTF"},
    "NSE_FNO": {"INTRADAY", "MARGIN"},
    "BSE_FNO": {"INTRADAY", "MARGIN"},
    "MCX_COMM": {"INTRADAY", "MARGIN"},
    "NSE_CURRENCY": {"INTRADAY", "MARGIN"},
    "BSE_CURRENCY": {"INTRADAY", "MARGIN"},
}


def normalize_order_response(response):
    if not isinstance(response, dict):
        return {"accepted": False, "reason": f"Unexpected response type: {type(response).__name__}"}
    if response.get("dry_run") is True:
        return {"accepted": True, "order_id": "DRY_RUN"}
    if response.get("status") == "success":
        data = response.get("data")
        order_id = None
        if isinstance(data, dict):
            order_id = (
                data.get("orderId")
                or data.get("order_id")
                or data.get("orderNo")
                or data.get("order_no")
            )
        return {"accepted": True, "order_id": order_id}
    return {"accepted": False, "reason": response.get("remarks", response)}


def order_accepted(response, live_orders):
    result = normalize_order_response(response)
    if live_orders:
        return result["accepted"]
    return result["accepted"] and isinstance(response, dict) and response.get("dry_run") is True


def validate_order_config(instrument, quantity, order_type, price):
    segment = str(instrument.get("exchange_segment", "")).upper()
    product_type = str(instrument.get("product_type", "INTRADAY")).upper()
    lot_size = int(instrument.get("lot_size", 1))
    quantity = int(quantity)
    order_type = str(order_type).upper()

    allowed = ALLOWED_PRODUCTS.get(segment)
    if allowed is not None and product_type not in allowed:
        raise ValueError(f"{segment} product_type must be one of {sorted(allowed)}, got {product_type}")
    if segment in DERIVATIVE_SEGMENTS and product_type in {"CNC", "MTF"}:
        raise ValueError(f"{product_type} is not allowed for derivative/commodity segment {segment}")
    if lot_size <= 0:
        raise ValueError("instrument.lot_size must be greater than zero")
    if quantity <= 0:
        raise ValueError("quantity must be greater than zero")
    if quantity % lot_size != 0:
        raise ValueError(f"quantity {quantity} must be a multiple of lot_size {lot_size}")
    if order_type not in {"LIMIT", "MARKET", "STOP_LOSS", "STOP_LOSS_MARKET"}:
        raise ValueError(f"Unsupported order_type {order_type}")
    if order_type != "MARKET" and float(price) <= 0:
        raise ValueError("LIMIT/SL orders need a positive price")


def validate_edis_config(instrument, edis_config):
    edis_config = edis_config or {}
    if not edis_config.get("enabled", False):
        return
    segment = str(instrument.get("exchange_segment", "")).upper()
    product_type = str(instrument.get("product_type", "INTRADAY")).upper()
    if segment not in EQUITY_SEGMENTS or product_type != "CNC":
        raise ValueError("eDIS should be enabled only for CNC equity delivery sells. Keep it disabled for MCX/F&O intraday.")


def print_order_preview(label, instrument, transaction_type, quantity, order_type, price):
    order = {
        "label": label,
        "tradingsymbol": instrument.get("tradingsymbol", instrument.get("security_id")),
        "security_id": instrument.get("security_id"),
        "exchange_segment": instrument.get("exchange_segment"),
        "transaction_type": transaction_type,
        "quantity": int(quantity),
        "order_type": order_type,
        "product_type": instrument.get("product_type", "INTRADAY"),
        "price": float(price),
        "notional": float(price) * int(quantity) if float(price) > 0 else None,
    }
    print_data("LIVE_ORDER_PREVIEW", order)


def verify_live_account_access(dhan):
    checks = {}
    if hasattr(dhan, "get_fund_limits"):
        checks["fund_limits"] = dhan.get_fund_limits()
        print_data("LIVE_FUND_LIMITS", checks["fund_limits"])
        if isinstance(checks["fund_limits"], dict) and checks["fund_limits"].get("status") == "failure":
            raise RuntimeError(f"Dhan fund limits check failed: {checks['fund_limits']}")
    if hasattr(dhan, "get_positions"):
        checks["positions"] = dhan.get_positions()
        print_data("LIVE_EXISTING_POSITIONS", checks["positions"])
        if isinstance(checks["positions"], dict) and checks["positions"].get("status") == "failure":
            raise RuntimeError(f"Dhan positions check failed: {checks['positions']}")
    return checks


def _number_from(data, keys):
    if not isinstance(data, dict):
        return None
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except Exception:
            continue
    return None


def confirm_live_start(config, instrument, quote):
    if not config.get("live_orders", False):
        return
    safety = config.get("live_safety", {})
    if not safety.get("require_startup_confirmation", True):
        print("[LIVE WARNING] Startup confirmation disabled by config.")
        return

    text = str(safety.get("confirmation_text", "LIVE"))
    print("\nLIVE ORDER MODE IS ENABLED")
    print(f"Instrument: {instrument.get('tradingsymbol', instrument.get('security_id'))}")
    print(f"Segment/product: {instrument.get('exchange_segment')} / {instrument.get('product_type')}")
    print(f"Lot size: {instrument.get('lot_size')}  Current LTP: {quote.get('ltp')}")
    entered = input(f"Type {text} to allow this session to place live orders: ").strip()
    if entered != text:
        raise RuntimeError("Live trading confirmation failed; no live orders will be placed.")


def check_margin_before_order(dhan, config, instrument, transaction_type, quantity, price):
    safety = config.get("live_safety", {})
    if not config.get("live_orders", False) or not safety.get("check_margin", True):
        return None
    if not hasattr(dhan, "margin_calculator"):
        raise RuntimeError("Dhan SDK does not expose margin_calculator; cannot verify margin before live order.")

    margin_quantity = _margin_quantity(config, instrument, quantity)
    response = dhan.margin_calculator(
        security_id=instrument["security_id"],
        exchange_segment=instrument["exchange_segment"],
        transaction_type=transaction_type,
        quantity=int(margin_quantity),
        product_type=instrument.get("product_type", "INTRADAY"),
        price=float(price),
    )
    print_data(
        "LIVE_MARGIN_CHECK",
        {
            "order_quantity": int(quantity),
            "margin_quantity": int(margin_quantity),
            "response": response,
        },
    )
    if isinstance(response, dict) and response.get("status") == "failure":
        raise RuntimeError(f"Margin check failed: {response}")
    data = response.get("data", response) if isinstance(response, dict) else response
    insufficient = _number_from(data, ("insufficientBalance", "insufficient_balance"))
    total_margin = _number_from(data, ("totalMargin", "total_margin", "requiredMargin", "required_margin"))
    available = _number_from(data, ("availableBalance", "available_balance", "availabelBalance"))
    if insufficient is not None and insufficient > 0:
        raise RuntimeError(f"Insufficient margin for live order: shortage={insufficient}, response={response}")
    if total_margin is not None and available is not None and total_margin > available:
        raise RuntimeError(f"Insufficient margin for live order: required={total_margin}, available={available}")
    return response


def _margin_quantity(config, instrument, quantity):
    safety = config.get("live_safety", {})
    mode = str(safety.get("margin_quantity_mode", "auto")).lower()
    segment = str(instrument.get("exchange_segment", "")).upper()
    lot_size = int(instrument.get("lot_size", 1))
    quantity = int(quantity)

    if mode == "order_quantity":
        return quantity
    if mode == "lots" or (mode == "auto" and segment in DERIVATIVE_SEGMENTS):
        if lot_size <= 0 or quantity % lot_size != 0:
            raise ValueError(f"Cannot convert quantity {quantity} to lots using lot_size {lot_size}")
        return max(quantity // lot_size, 1)
    return quantity


def preflight_live_trading(dhan, config, instrument, quote):
    validate_edis_config(instrument, config.get("edis", {}))
    if not config.get("live_orders", False):
        print("[LIVE MODE] live_orders=false; strategy will dry-run orders.")
        return
    print("[LIVE MODE] live_orders=true; running live account preflight.")
    verify_live_account_access(dhan)
    confirm_live_start(config, instrument, quote)
