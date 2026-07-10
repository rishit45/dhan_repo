def should_exit(position, ltp, exit_config):
    if position is None or ltp is None:
        return None

    mode = str(exit_config.get("mode", "points")).lower()
    side = position["side"]
    entry_price = float(position["entry_price"])
    quantity = int(position["quantity"])
    ltp = float(ltp)

    if side == "LONG":
        points = ltp - entry_price
    else:
        points = entry_price - ltp

    pnl = points * quantity

    if mode == "pnl":
        target = float(exit_config.get("target_pnl", 0))
        stop = float(exit_config.get("stop_loss_pnl", 0))
        if target and pnl >= target:
            return "TARGET_PNL"
        if stop and pnl <= -stop:
            return "STOP_PNL"
        return None

    target = float(exit_config.get("target_points", 0))
    stop = float(exit_config.get("stop_loss_points", 0))
    if target and points >= target:
        return "TARGET_POINTS"
    if stop and points <= -stop:
        return "STOP_POINTS"
    return None
