import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from pathlib import Path

from dhanhq import DhanContext, dhanhq

IST = timezone(timedelta(hours=5, minutes=30))
TOKEN_ENDPOINT = "https://auth.dhan.co/app/generateAccessToken"


def load_dotenv(path=None):
    """Load simple KEY=value values from the strategy's .env file.

    Existing system environment variables win, so deployment secrets can be
    supplied by the operating system instead of a local .env file.
    """
    path = Path(path) if path else Path(__file__).resolve().parent / ".env"
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _to_ist_datetime(epoch_seconds):
    try:
        ts = float(epoch_seconds)
    except Exception:
        return None
    return datetime.fromtimestamp(ts, IST).replace(tzinfo=None)


def _floor_time(dt, timeframe_minutes):
    day_start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    minutes_since_start = dt.hour * 60 + dt.minute
    candle_start = minutes_since_start - (minutes_since_start % timeframe_minutes)
    return day_start + timedelta(minutes=candle_start)


def _combine_1min_candles(raw_candles, timeframe_minutes):
    if timeframe_minutes <= 1:
        return raw_candles

    aggregated = []
    current_bucket = None
    for candle in raw_candles:
        bucket_time = _floor_time(candle["time"], timeframe_minutes)
        if current_bucket is None or bucket_time != current_bucket["time"]:
            if current_bucket is not None:
                aggregated.append(current_bucket)
            current_bucket = {
                "time": bucket_time,
                "open": candle["open"],
                "high": candle["high"],
                "low": candle["low"],
                "close": candle["close"],
            }
            continue
        current_bucket["high"] = max(current_bucket["high"], candle["high"])
        current_bucket["low"] = min(current_bucket["low"], candle["low"])
        current_bucket["close"] = candle["close"]
    if current_bucket is not None:
        aggregated.append(current_bucket)
    return aggregated


def _closed_candles_only(candles, timeframe_minutes, now=None):
    now = now or datetime.now(IST).replace(tzinfo=None)
    current_bucket = _floor_time(now, timeframe_minutes)
    return [candle for candle in candles if candle["time"] < current_bucket]


def _parse_intraday_data(data):
    if not isinstance(data, dict):
        return []
    timestamps = data.get("timestamp") or []
    opens = data.get("open") or []
    highs = data.get("high") or []
    lows = data.get("low") or []
    closes = data.get("close") or []
    if not (isinstance(timestamps, list) and isinstance(closes, list)):
        return []
    candles = []
    length = min(len(timestamps), len(opens), len(highs), len(lows), len(closes))
    for idx in range(length):
        time = _to_ist_datetime(timestamps[idx])
        if time is None:
            continue
        try:
            candles.append({
                "time": time,
                "open": float(opens[idx]),
                "high": float(highs[idx]),
                "low": float(lows[idx]),
                "close": float(closes[idx]),
            })
        except Exception:
            continue
    candles.sort(key=lambda c: c["time"])
    return candles


def _history_instrument_type_candidates(instrument):
    configured_type = instrument.get("instrument_type")
    if configured_type:
        return [configured_type]
    exchange = instrument.get("exchange_segment", "").upper()
    if exchange.startswith("MCX"):
        return ["FUTCOM", "FUTSTK", "FUTIDX", "FUT"]
    if exchange.endswith("_FNO") or exchange.endswith("_FUT"):
        return ["FUTIDX", "FUTSTK", "FUT"]
    return ["OPTIDX", "OPTSTK", "FUTIDX", "FUTSTK", "FUT"]


def fetch_historical_intraday_candles(dhan, instrument, interval, periods=20, history_days=7):
    security_id = _security_id_for_quote(instrument["security_id"])
    exchange_segment = instrument["exchange_segment"]
    now = datetime.now(IST).replace(tzinfo=None, second=0, microsecond=0)
    from_dt = (now - timedelta(days=history_days)).replace(hour=0, minute=0)
    # Use exact timestamps so MCX evening-session candles are included.
    from_date = from_dt.strftime("%Y-%m-%d %H:%M:%S")
    to_date = now.strftime("%Y-%m-%d %H:%M:%S")
    candidate_types = _history_instrument_type_candidates(instrument)
    timeframe_minutes = int(interval)
    api_interval = timeframe_minutes if timeframe_minutes in {1, 5, 15, 25, 60} else 1
    last_error = None
    for instrument_type in candidate_types:
        print(f"Trying Dhan intraday minute data for {exchange_segment} {instrument_type} interval {api_interval} from {from_date} to {to_date}")
        response = dhan.intraday_minute_data(
            security_id,
            exchange_segment,
            instrument_type,
            from_date,
            to_date,
            interval=api_interval,
        )
        if not isinstance(response, dict) or response.get("status") != "success":
            last_error = response.get("remarks") if isinstance(response, dict) else response
            print(f"Dhan intraday attempt failed for {instrument_type}: {last_error}")
            continue
        candles = _parse_intraday_data(response.get("data"))
        if not candles:
            last_error = "Dhan returned no intraday candles"
            print(f"Dhan intraday attempt returned no candles for {instrument_type}")
            continue
        if api_interval != timeframe_minutes:
            candles = _combine_1min_candles(candles, timeframe_minutes)
        candles = _closed_candles_only(candles, timeframe_minutes)
        if len(candles) < periods:
            last_error = f"Dhan returned only {len(candles)} closed {timeframe_minutes}-minute candles"
            print(f"Dhan intraday attempt returned insufficient candles for {instrument_type}: {last_error}")
            continue
        print(f"Dhan intraday history loaded {len(candles)} closed candles for {instrument_type} timeframe {timeframe_minutes}")
        return candles[-periods:]
    raise RuntimeError(f"Unable to fetch Dhan intraday candles: {last_error}")


def _parse_historical_daily_data(data):
    if not isinstance(data, dict):
        return []
    closes = data.get("close") or []
    if not isinstance(closes, list):
        return []
    parsed = []
    for value in closes:
        try:
            parsed.append(float(value))
        except Exception:
            continue
    return parsed


def fetch_historical_closes(dhan, instrument, periods=20, history_days=30):
    security_id = _security_id_for_quote(instrument["security_id"])
    exchange_segment = instrument["exchange_segment"]
    from_date = (datetime.now() - timedelta(days=history_days)).date().isoformat()
    to_date = datetime.now().date().isoformat()
    candidate_types = _history_instrument_type_candidates(instrument)
    last_error = None
    for instrument_type in candidate_types:
        print(f"Trying Dhan historical daily data for {exchange_segment} {instrument_type} from {from_date} to {to_date}")
        response = dhan.historical_daily_data(
            security_id,
            exchange_segment,
            instrument_type,
            from_date,
            to_date,
            expiry_code=0,
        )
        if not isinstance(response, dict) or response.get("status") != "success":
            last_error = response.get("remarks") if isinstance(response, dict) else response
            print(f"Dhan daily history attempt failed for {instrument_type}: {last_error}")
            continue
        closes = _parse_historical_daily_data(response.get("data"))
        if not closes:
            last_error = "Dhan returned no daily closes"
            print(f"Dhan daily history attempt returned no closes for {instrument_type}")
            continue
        print(f"Dhan daily history loaded {len(closes)} closes for {instrument_type}")
        return closes[-periods:]
    raise RuntimeError(f"Unable to fetch Dhan historical closes: {last_error}")


def generate_access_token(dhan_config, auth_config):
    """Generate a Dhan token from PIN and TOTP environment variables.

    The secrets deliberately stay outside the strategy JSON and are never
    logged or written back to disk.
    """
    client_id_env = str(auth_config.get("client_id_env", "DHAN_CLIENT_ID")).strip()
    client_id = os.environ.get(client_id_env, "").strip()
    pin_env = str(auth_config.get("pin_env", "DHAN_PIN")).strip()
    totp_env = str(auth_config.get("totp_env", "DHAN_TOTP")).strip()
    pin = os.environ.get(pin_env, "").strip()
    totp = os.environ.get(totp_env, "").strip()
    if not client_id:
        raise RuntimeError(
            f"Set {client_id_env} (Dhan client ID) before using dhan_auth.access_token_source=generate"
        )
    if not pin or not totp:
        raise RuntimeError(
            f"Set {pin_env} (Dhan PIN) and {totp_env} (current authenticator TOTP) before using dhan_auth.access_token_source=generate"
        )

    url = f"{TOKEN_ENDPOINT}?{urlencode({'dhanClientId': client_id, 'pin': pin, 'totp': totp})}"
    request = Request(url, method="POST")
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Dhan access-token generation failed: {exc}") from exc

    token = payload.get("accessToken") if isinstance(payload, dict) else None
    if not token:
        raise RuntimeError("Dhan did not return an access token; verify your PIN and current TOTP")
    print("[DHAN AUTH] Generated a new access token for this run.")
    return str(token)


def create_dhan(config):
    dhan_config = config.get("dhan", {})
    auth_config = config.get("dhan_auth", {})
    client_id = str(dhan_config.get("client_id", "")).strip()
    token_source = str(auth_config.get("access_token_source", "config")).strip().lower()
    if token_source == "generate":
        load_dotenv()
        client_id_env = str(auth_config.get("client_id_env", "DHAN_CLIENT_ID")).strip()
        client_id = os.environ.get(client_id_env, "").strip()
        access_token = generate_access_token(dhan_config, auth_config)
    elif token_source == "config":
        access_token = str(dhan_config.get("access_token", "")).strip()
    else:
        raise ValueError("dhan_auth.access_token_source must be 'config' or 'generate'")

    if not client_id or not access_token:
        print("[DHAN CONFIG] Add client_id and access_token, or configure dhan_auth.access_token_source=generate")

    return dhanhq(DhanContext(client_id, access_token))


def fetch_quote(dhan, instrument):
    response = dhan.quote_data(
        securities={
            instrument["exchange_segment"]: [_security_id_for_quote(instrument["security_id"])]
        }
    )
    if isinstance(response, dict) and response.get("status") == "failure":
        print(f"[QUOTE WARNING] quote_data failed for {instrument}: {response}")
        return fetch_ticker_quote(dhan, instrument, response)

    return extract_quote(response, instrument)


def fetch_ticker_quote(dhan, instrument, quote_failure=None):
    response = dhan.ticker_data(
        securities={
            instrument["exchange_segment"]: [_security_id_for_quote(instrument["security_id"])]
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

    ltp = first_number(
        quote,
        ("last_price", "lastPrice", "ltp", "LTP", "last_traded_price", "lastTradedPrice")
    )
    if ltp is None:
        raise RuntimeError(f"Could not read LTP from Dhan ticker response: {response}")

    return {
        "raw": quote,
        "ltp": ltp,
        "best_bid": None,
        "best_ask": None,
        "source": "ticker_data",
    }


def _security_id_for_quote(security_id):
    s = str(security_id)
    return int(s) if s.isdigit() else s


def extract_quote(response, instrument):
    if isinstance(response, dict) and response.get("status") == "failure":
        raise RuntimeError(f"Dhan quote failed: {response}")

    data = response.get("data", response) if isinstance(response, dict) else response
    while isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]

    segment_data = data.get(instrument["exchange_segment"], {}) if isinstance(data, dict) else {}
    quote = segment_data.get(str(instrument["security_id"]))
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
    buy_depth = (
        depth.get("buy")
        or depth.get("buyDepth")
        or depth.get("bids")
        or depth.get("bid")
        or []
    )
    sell_depth = (
        depth.get("sell")
        or depth.get("sellDepth")
        or depth.get("asks")
        or depth.get("ask")
        or []
    )
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
