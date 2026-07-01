from debug import print_data


def place_buy_order(dhan, instrument, quantity, order_type, price, live_orders=False):
    order = {
        "security_id": instrument["security_id"],
        "exchange_segment": instrument["exchange_segment"],
        "transaction_type": "BUY",
        "quantity": int(quantity),
        "order_type": order_type,
        "product_type": instrument.get("product_type", "INTRADAY"),
        "price": float(price),
        "validity": instrument.get("validity", "DAY")
    }

    print_data("BUY_ORDER_STRUCTURE", order)

    if not live_orders:
        print(f"[BUY DRY RUN] {order}")
        return {"dry_run": True, "order": order}

    response = dhan.place_order(**order)
    print_data("BUY_ORDER_RESPONSE", response)
    return response
