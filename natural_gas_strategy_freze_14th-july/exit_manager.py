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

    if mode in {"flexible", "independent"}:
        target_type = str(exit_config.get("target_type", "points")).lower()
        stop_loss_type = str(exit_config.get("stop_loss_type", "points")).lower()
        if target_type not in {"points", "pnl"}:
            raise ValueError("exit.target_type must be 'points' or 'pnl'")
        if stop_loss_type not in {"points", "pnl"}:
            raise ValueError("exit.stop_loss_type must be 'points' or 'pnl'")

        target = float(exit_config.get(f"target_{target_type}", 0))
        stop = float(exit_config.get(f"stop_loss_{stop_loss_type}", 0))
        if target and (points >= target if target_type == "points" else pnl >= target):
            return "TARGET_POINTS" if target_type == "points" else "TARGET_PNL"
        if stop and (points <= -stop if stop_loss_type == "points" else pnl <= -stop):
            return "STOP_POINTS" if stop_loss_type == "points" else "STOP_PNL"
        return None

    if mode in {"target_points_stop_loss_pnl", "points_target_pnl_stop", "mixed"}:
        target_points = float(exit_config.get("target_points", 0))
        stop_loss_pnl = float(exit_config.get("stop_loss_pnl", 0))
        if target_points and points >= target_points:
            return "TARGET_POINTS"
        if stop_loss_pnl and pnl <= -stop_loss_pnl:
            return "STOP_PNL"
        return None

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
