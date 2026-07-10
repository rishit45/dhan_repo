def place_buy_order(dhan, instrument, quantity, order_type, price, live_orders):
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

    if not live_orders:
        print(f"[BUY DRY RUN] {order}")
        return {"dry_run": True, "order": order}

    return dhan.place_order(**order)
