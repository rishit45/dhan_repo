from edis import prepare_edis_for_sell
from debug import print_data


def place_sell_order(
    dhan,
    instrument,
    quantity,
    order_type,
    price,
    live_orders=False,
    edis_config=None,
):
    order = {
        "security_id": instrument["security_id"],
        "exchange_segment": instrument["exchange_segment"],
        "transaction_type": "SELL",
        "quantity": int(quantity),
        "order_type": order_type,
        "product_type": instrument.get("product_type", "INTRADAY"),
        "price": float(price),
        "validity": instrument.get("validity", "DAY")
    }

    print_data("SELL_ORDER_STRUCTURE", order)
    prepare_edis_for_sell(dhan, instrument, quantity, edis_config, live_orders)

    if not live_orders:
        print(f"[SELL DRY RUN] {order}")
        return {"dry_run": True, "order": order}

    response = dhan.place_order(**order)
    print_data("SELL_ORDER_RESPONSE", response)
    return response
