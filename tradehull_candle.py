from Dhan_Tradehull import Tradehull
from collections import deque
from datetime import datetime
import time


CLIENT_ID = ""
ACCESS_TOKEN = ""

tsl = Tradehull(
    CLIENT_ID,
    ACCESS_TOKEN
)


WATCHLIST = [
   "NATURALGAS JUN FUT",
   "CRUDE JUN FUT",
   "NIFTY 50",
   "NIFTY JUN FUT"
]


instrument_data = {}


# CANDLE FUNCTION

def process_tick(symbol, price):

    global instrument_data

    if symbol not in instrument_data:

        instrument_data[symbol] = {
            "current_minute": None,
            "current_candle": None,
            "candles": deque(maxlen=20),  # FIX 1: initialize candles deque (was missing, caused KeyError)
        }

    stock = instrument_data[symbol]

    minute = datetime.now().replace(
        second=0,
        microsecond=0
    )

    # First Tick
    if stock["current_minute"] is None:

        stock["current_minute"] = minute

        stock["current_candle"] = {
            "time": minute,
            "open": price,
            "high": price,
            "low": price,
            "close": price
        }

        return

    # Same Minute
    if minute == stock["current_minute"]:

        candle = stock["current_candle"]

        candle["high"] = max(
            candle["high"],
            price
        )

        candle["low"] = min(
            candle["low"],
            price
        )

        candle["close"] = price

    # New Minute
    else:

        completed = stock["current_candle"]

        stock["candles"].append(
            completed
        )

        print("\n===================")
        print(symbol)
        print("===================")

        print(
            f"TIME  : {completed['time']}"
        )

        print(
            f"OPEN  : {completed['open']}"
        )

        print(
            f"HIGH  : {completed['high']}"
        )

        print(
            f"LOW   : {completed['low']}"
        )

        print(
            f"CLOSE : {completed['close']}"
        )

       

        stock["current_minute"] = minute

        stock["current_candle"] = {
            "time": minute,
            "open": price,
            "high": price,
            "low": price,
            "close": price
        }


# LIVE LOOP

while True:

    # FIX 3: get_ltp_data requires
    prices = tsl.get_ltp_data(
        names=WATCHLIST
    )

    for symbol in WATCHLIST:

        if symbol not in prices:
            continue

        ltp = prices[symbol]

        process_tick(
            symbol,
            float(ltp)
        )

    time.sleep(1)

    # FIX 4: capture `now` AFTER sleep so the market-close check uses the current time
    now = datetime.now()


    if now.hour >= 15 and now.minute >= 30:
        print("Market Closed")
        break

# orderid = tsl.order_placement(tradingsymbol='TCS', exchange='NFO', quantity=75, price=0, trigger_price=0,order_type='LIMIT', transaction_type='BUY', trade_type='MIS')