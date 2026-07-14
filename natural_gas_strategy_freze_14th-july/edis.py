def prepare_edis_for_sell(dhan, instrument, quantity, edis_config=None, live_orders=False):
    edis_config = edis_config or {}
    if not edis_config.get("enabled", False):
        return None

    if not live_orders:
        print(
            "[EDIS DRY RUN] would prepare eDIS for {symbol} qty={qty}".format(
                symbol=instrument.get("tradingsymbol", instrument["security_id"]),
                qty=quantity,
            )
        )
        return {"dry_run": True}

    if hasattr(dhan, "generate_tpin"):
        response = dhan.generate_tpin()
        print(f"[EDIS TPIN] {response}")
        return response

    raise AttributeError("This dhanhq version does not expose generate_tpin().")
