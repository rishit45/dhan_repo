def position_pnl(position, price):
    """Return the marked-to-market P&L for a strategy position."""
    if position is None or price is None:
        return 0.0

    entry_price = float(position["entry_price"])
    quantity = int(position["quantity"])
    price = float(price)
    points = price - entry_price if position["side"] == "LONG" else entry_price - price
    return points * quantity


def daily_pnl_status(realized_pnl, position, price, daily_config):
    """Evaluate the configured daily P&L guard without placing an order."""
    config = daily_config or {}
    if not bool(config.get("enabled", False)):
        return {"enabled": False, "hit": False, "reason": None, "total_pnl": float(realized_pnl)}

    unrealized_pnl = position_pnl(position, price) if bool(config.get("include_unrealized", True)) else 0.0
    total_pnl = float(realized_pnl) + unrealized_pnl
    target_pnl = float(config.get("target_pnl", 0) or 0)
    stop_loss_pnl = abs(float(config.get("stop_loss_pnl", 0) or 0))

    if target_pnl > 0 and total_pnl >= target_pnl:
        return {"enabled": True, "hit": True, "reason": "DAILY_TARGET", "total_pnl": total_pnl}
    if stop_loss_pnl > 0 and total_pnl <= -stop_loss_pnl:
        return {"enabled": True, "hit": True, "reason": "DAILY_STOP_LOSS", "total_pnl": total_pnl}
    return {"enabled": True, "hit": False, "reason": None, "total_pnl": total_pnl}


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

        target = float(exit_config.get(f"target_{target_type}", 0) or 0)
        stop = abs(float(exit_config.get(f"stop_loss_{stop_loss_type}", 0) or 0))
        target_value = pnl if target_type == "pnl" else points
        stop_value = pnl if stop_loss_type == "pnl" else points
        if target > 0 and target_value >= target:
            return f"TARGET_{target_type.upper()}"
        if stop > 0 and stop_value <= -stop:
            return f"STOP_{stop_loss_type.upper()}"
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
