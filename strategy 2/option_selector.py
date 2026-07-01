from debug import print_data


def get_current_week_expiry(dhan, underlying):
    response = dhan.expiry_list(
        under_security_id=int(underlying["security_id"]),
        under_exchange_segment=underlying["exchange_segment"],
    )
    print_data("EXPIRY_LIST_RESPONSE", response)
    if isinstance(response, dict) and response.get("status") == "failure":
        raise RuntimeError(f"Dhan expiry-list request failed: {response}")

    data = response.get("data", response) if isinstance(response, dict) else response
    if isinstance(data, dict):
        data = data.get("expiryDates") or data.get("expiry") or data.get("data") or []
    expiries = list(data)
    if not expiries:
        raise RuntimeError(f"No expiries returned by Dhan: {response}")
    return expiries[0]


def fetch_option_chain(dhan, underlying, expiry):
    response = dhan.option_chain(
        under_security_id=int(underlying["security_id"]),
        under_exchange_segment=underlying["exchange_segment"],
        expiry=expiry,
    )
    print_data("OPTION_CHAIN_RESPONSE", response)
    if isinstance(response, dict) and response.get("status") == "failure":
        raise RuntimeError(f"Dhan option-chain request failed: {response}")
    return extract_chain(response)


def extract_chain(response):
    data = response
    while isinstance(data, dict):
        if isinstance(data.get("oc"), dict):
            return data["oc"]
        if isinstance(data.get("optionChain"), dict):
            return data["optionChain"]
        nested = data.get("data")
        if isinstance(nested, dict) and nested is not data:
            data = nested
            continue
        numeric = {}
        for key, value in data.items():
            try:
                float(key)
            except (TypeError, ValueError):
                continue
            numeric[key] = value
        if numeric:
            return numeric
        break
    raise ValueError(f"Unsupported option-chain response: {response}")


def select_options_from_oi(chain, underlying_ltp, selection_config, instrument_defaults):
    premium_min = float(selection_config.get("premium_min", 20))
    premium_max = float(selection_config.get("premium_max", 35))
    rank_limit = int(selection_config.get("candidate_rank_limit", 20))

    ce_rows = collect_side_rows(chain, "ce", underlying_ltp, prefer_otm=True)
    pe_rows = collect_side_rows(chain, "pe", underlying_ltp, prefer_otm=True)
    print_data("CE_OI_ROWS", ce_rows)
    print_data("PE_OI_ROWS", pe_rows)

    ce = choose_by_oi_and_premium(ce_rows, premium_min, premium_max, rank_limit)
    pe = choose_by_oi_and_premium(pe_rows, premium_min, premium_max, rank_limit)

    return {
        "CE": build_instrument(ce, "CE", instrument_defaults) if ce else None,
        "PE": build_instrument(pe, "PE", instrument_defaults) if pe else None,
        "max_oi_ce_strike": ce_rows[0]["strike"] if ce_rows else None,
        "max_oi_pe_strike": pe_rows[0]["strike"] if pe_rows else None,
    }


def collect_side_rows(chain, side, underlying_ltp, prefer_otm=True):
    rows = []
    for strike, row in chain.items():
        try:
            strike_value = float(strike)
        except (TypeError, ValueError):
            continue

        side_data = None
        if isinstance(row, dict):
            side_data = row.get(side) or row.get(side.upper())
        if not isinstance(side_data, dict):
            continue

        if prefer_otm:
            if side == "ce" and strike_value <= underlying_ltp:
                continue
            if side == "pe" and strike_value >= underlying_ltp:
                continue

        rows.append(
            {
                "strike": strike_value,
                "oi": number(side_data, ("oi", "open_interest", "OI")),
                "ltp": number(side_data, ("last_price", "lastPrice", "ltp", "LTP")),
                "security_id": str(side_data.get("security_id") or side_data.get("securityId") or ""),
                "raw": side_data,
            }
        )

    return sorted(rows, key=lambda item: item["oi"], reverse=True)


def choose_by_oi_and_premium(rows, premium_min, premium_max, rank_limit):
    ranked = rows[:rank_limit]
    for row in ranked:
        if premium_min <= row["ltp"] <= premium_max and row["security_id"]:
            return row
    for row in ranked:
        if row["security_id"]:
            return row
    return None


def build_instrument(row, side, defaults):
    return {
        "name": f"NIFTY_{int(row['strike'])}_{side}",
        "tradingsymbol": f"NIFTY {int(row['strike'])} {side}",
        "security_id": row["security_id"],
        "exchange_segment": defaults.get("exchange_segment", "NSE_FNO"),
        "lot_size": int(defaults.get("lot_size", 50)),
        "product_type": defaults.get("product_type", "INTRADAY"),
        "validity": defaults.get("validity", "DAY"),
        "strike": row["strike"],
        "option_type": side,
        "selected_oi": row["oi"],
        "selected_ltp": row["ltp"],
    }


def number(data, keys):
    for key in keys:
        value = data.get(key)
        if value is not None:
            return float(value)
    return 0.0
