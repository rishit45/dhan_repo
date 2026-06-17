# Place Order via DhanHQ API
# Places a single order — configure symbol, qty, price below
# Set ACCESS_TOKEN and CLIENT_ID in Env Variables tab

import json
import requests
from datetime import datetime

ACCESS_TOKEN = "{{ACCESS_TOKEN}}"
CLIENT_ID = "{{CLIENT_ID}}"
BASE_URL = "https://api.dhan.co/v2"

instruments = {
    "NIFTY_JUN_FUT": {
        "SECURITY_ID": "62329",
        "exchangeSegment": "NSE_FNO"
    }
    ,"NIFTY_50": {
        "SECURITY_ID": "62329",
        "exchangeSegment": "NSE_FNO"
    },
    "NATURALGAS_JUN_FUT": {
        "SECURITY_ID": "504265",
        "exchangeSegment": "MCX_COMM"
    },
    "CRUDEOIL_JUN_FUT": {
        "SECURITY_ID": "499095",
        "exchangeSegment": "MCX_COMM"
    }
}


def place_order(instrument_name,TXN_TYPE,PRODUCT_TYPE,ORDER_TYPE,QUANTITY,PRICE):

    headers = {
        "access-token": ACCESS_TOKEN,
        "Content-Type": "application/json"
    }

    instrument = instruments[instrument_name]

    order = {
        "dhanClientId": CLIENT_ID,
        "transactionType": TXN_TYPE,
        "exchangeSegment": instrument["exchangeSegment"],
        "productType": PRODUCT_TYPE,
        "orderType": ORDER_TYPE,
        "validity": "DAY",
        "securityId": instrument["SECURITY_ID"],
        "quantity": QUANTITY,
        "price": PRICE,
        "disclosedQuantity": 0,
        "afterMarketOrder": True
    }
    print(f"Placing {TXN_TYPE} {ORDER_TYPE} order for {QUANTITY} ")
    res = requests.post(f"{BASE_URL}/orders", headers=headers, json=order)

    print(f"Status: {res.status_code}")
    data = res.json()
    print(f"Response: {json.dumps(data, indent=2)}")

    if res.status_code == 200 and data.get("orderId"):
        print(f"Order placed successfully. Order ID: {data['orderId']}")
    else:
        print(f"Order failed: {data.get('remarks', 'Unknown error')}")
##example
place_order("NIFTY_JUN_FUT","BUY","INTRADAY","MARKET",1,0)