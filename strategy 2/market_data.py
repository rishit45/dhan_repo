from dhanhq import DhanContext, dhanhq


def create_dhan(config):
    dhan_config = config.get("dhan", {})
    client_id = str(dhan_config.get("client_id", "")).strip()
    access_token = str(dhan_config.get("access_token", "")).strip()

    if not client_id or not access_token:
        print("[DHAN CONFIG] Add client_id and access_token in strategy_config.json")

    return dhanhq(DhanContext(client_id, access_token))


def fetch_quote(dhan, instrument):
    response = dhan.quote_data(
        securities={
            instrument["exchange_segment"]: [instrument["security_id"]]
        }
    )
    if isinstance(response, dict) and response.get("status") == "failure":
        print(f"[QUOTE WARNING] quote_data failed, falling back to ticker_data: {response}")
        return fetch_ticker_quote(dhan, instrument, response)

    return extract_quote(response, instrument)


def fetch_ticker_quote(dhan, instrument, quote_failure=None):
    response = dhan.ticker_data(
        securities={
            instrument["exchange_segment"]: [instrument["security_id"]]
        }
    )
    quote = extract_ticker(response, instrument)
    quote["quote_data_failure"] = quote_failure
    return quote


def extract_ticker(response, instrument):
    if isinstance(response, dict) and response.get("status") == "failure":
        raise RuntimeError(f"Dhan ticker failed: {response}")

    data = response.get("data", response) if isinstance(response, dict) else response
    while isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]

    segment_data = data.get(instrument["exchange_segment"], {}) if isinstance(data, dict) else {}
    quote = segment_data.get(str(instrument["security_id"]))
    if not isinstance(quote, dict):
        quote = segment_data.get(instrument["security_id"])
    if not isinstance(quote, dict):
        raise RuntimeError(f"Could not read ticker from Dhan response: {response}")

    ltp = first_number(quote, ("last_price", "lastPrice", "ltp", "LTP"))
    if ltp is None:
        raise RuntimeError(f"Could not read LTP from Dhan ticker response: {response}")

    return {
        "raw": quote,
        "ltp": ltp,
        "best_bid": None,
        "best_ask": None,
        "source": "ticker_data",
    }


def fetch_ltp(dhan, exchange_segment, security_id):
    response = dhan.ticker_data(securities={exchange_segment: [security_id]})
    data = response.get("data", response) if isinstance(response, dict) else response
    while isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    quote = data.get(exchange_segment, {}).get(str(security_id)) if isinstance(data, dict) else None
    if not isinstance(quote, dict):
        quote = data.get(exchange_segment, {}).get(security_id) if isinstance(data, dict) else None
    if isinstance(quote, dict):
        return first_number(quote, ("last_price", "lastPrice", "ltp", "LTP"))
    raise RuntimeError(f"Could not read LTP from Dhan response: {response}")


def extract_quote(response, instrument):
    if isinstance(response, dict) and response.get("status") == "failure":
        raise RuntimeError(f"Dhan quote failed: {response}")

    data = response.get("data", response) if isinstance(response, dict) else response
    while isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]

    segment_data = data.get(instrument["exchange_segment"], {}) if isinstance(data, dict) else {}
    quote = segment_data.get(str(instrument["security_id"]))
    if not isinstance(quote, dict):
        quote = segment_data.get(instrument["security_id"])
    if not isinstance(quote, dict):
        raise RuntimeError(f"Could not read quote from Dhan response: {response}")

    ltp = first_number(
        quote,
        ("last_price", "lastPrice", "ltp", "LTP", "last_traded_price", "lastTradedPrice")
    )
    best_bid, best_ask = extract_best_bid_ask(quote)

    return {
        "raw": quote,
        "ltp": ltp,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "source": "quote_data",
    }


def first_number(data, keys):
    for key in keys:
        if key in data and data[key] is not None:
            return float(data[key])
    return None


def extract_best_bid_ask(quote):
    depth = quote.get("depth") or quote.get("market_depth") or quote.get("marketDepth") or quote
    buy_depth = depth.get("buy") or depth.get("buyDepth") or depth.get("bids") or depth.get("bid") or []
    sell_depth = depth.get("sell") or depth.get("sellDepth") or depth.get("asks") or depth.get("ask") or []
    return depth_price(buy_depth), depth_price(sell_depth)


def depth_price(rows):
    if not rows:
        return None
    row = rows[0]
    if isinstance(row, dict):
        return first_number(row, ("price", "bid_price", "ask_price", "bidPrice", "askPrice"))
    if isinstance(row, (list, tuple)) and row:
        return float(row[0])
    return None
