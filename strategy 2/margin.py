from debug import print_data


def estimate_margin_per_lot(dhan, instrument, lot_size, price):
    if not hasattr(dhan, "margin_calculator"):
        return None

    response = dhan.margin_calculator(
        security_id=instrument["security_id"],
        exchange_segment=instrument["exchange_segment"],
        transaction_type="SELL",
        quantity=int(lot_size),
        product_type=instrument.get("product_type", "INTRADAY"),
        price=float(price),
    )
    print_data("MARGIN_RESPONSE", response)
    if isinstance(response, dict) and response.get("status") == "failure":
        print(f"[MARGIN] failed: {response}")
        return None

    data = response.get("data", response) if isinstance(response, dict) else response
    if isinstance(data, dict):
        for key in ("total_margin", "totalMargin", "margin", "span", "requiredMargin"):
            value = data.get(key)
            if value is not None:
                return float(value)
    return None
