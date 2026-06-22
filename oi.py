import os
import time
from datetime import datetime
from collections import defaultdict, deque

from Dhan_Tradehull import Tradehull


UNDERLYING      = "NIFTY"   
EXCHANGE        = "INDEX"   
NUM_STRIKES     = 15       
RATE_LIMIT_SECS = 3.0       


def get_credentials():
    client_id    = os.environ.get("DHAN_CLIENT_ID")     
    access_token = os.environ.get("DHAN_ACCESS_TOKEN")  

    if not client_id or not access_token:
        raise EnvironmentError(
            "Missing credentials. Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN "
            "as environment variables before running."
        )
    return client_id, access_token


def fetch_expiries(tsl):
    expiries = tsl.get_expiry_list(Underlying=UNDERLYING, exchange=EXCHANGE)
    if not expiries:
        raise RuntimeError("No expiries returned - check credentials / market hours.")
    return expiries


def classify_expiry(expiry_date_str, all_expiries):
    target = datetime.strptime(expiry_date_str, "%Y-%m-%d")  

    same_month = [                                            
        e for e in all_expiries
        if datetime.strptime(e, "%Y-%m-%d").month == target.month
        and datetime.strptime(e, "%Y-%m-%d").year == target.year
    ]

    monthly_expiry = max(same_month) 
    return "MONTHLY" if expiry_date_str == monthly_expiry else "WEEKLY"


def fetch_chain_snapshot(tsl, expiry_index):
    try:
        atm_strike, df = tsl.get_option_chain(  
            Underlying  = UNDERLYING,
            exchange    = EXCHANGE,
            expiry      = expiry_index,
            num_strikes = NUM_STRIKES,
        )
        return atm_strike, df
    except Exception as e:
        print(f"  [WARN] Fetch failed for expiry index {expiry_index}: {e}")
        return None, None


def analyze_snapshot(oi_history, expiry_date, df):
    """
    Returns a flat dict of individually-named values for this expiry's
    current cycle - each key is a standalone variable you can pull out
    and use anywhere in a larger project.
    """
    snapshot = {}  

    for _, row in df.iterrows():
        strike = row["Strike Price"]               # strike price for this row, float
        snapshot[strike] = {
            "CE_OI":  row.get("CE OI") or 0,         # open interest, call side, this strike
            "PE_OI":  row.get("PE OI") or 0,         # open interest, put side, this strike
            "CE_CHG": row.get("CE Chg in OI") or 0,  # Dhan-reported change in CE OI vs prev day
            "PE_CHG": row.get("PE Chg in OI") or 0,  # Dhan-reported change in PE OI vs prev day
        }

    history = oi_history[expiry_date]  
    history.append(snapshot)       
    max_oi_ce_strike = max(snapshot, key=lambda s: snapshot[s]["CE_OI"])  
    max_oi_ce_value   = snapshot[max_oi_ce_strike]["CE_OI"]                

    max_oi_pe_strike = max(snapshot, key=lambda s: snapshot[s]["PE_OI"]) 
    max_oi_pe_value   = snapshot[max_oi_pe_strike]["PE_OI"]                

    buildup_ce_strike = None   
    buildup_ce_value  = 0    
    buildup_pe_strike = None  
    buildup_pe_value  = 0     
    snapshots_collected = len(history) 
    if snapshots_collected >= 2:
        oldest = history[0]   
        newest = history[-1] 
        for strike in newest:
            if strike not in oldest:
                continue

            ce_delta = newest[strike]["CE_OI"] - oldest[strike]["CE_OI"] 
            pe_delta = newest[strike]["PE_OI"] - oldest[strike]["PE_OI"]  

            if ce_delta > buildup_ce_value:
                buildup_ce_value  = ce_delta
                buildup_ce_strike = strike

            if pe_delta > buildup_pe_value:
                buildup_pe_value  = pe_delta
                buildup_pe_strike = strike

    bias = None 
    if max_oi_ce_value and max_oi_pe_value:
        bias = "CALL_HEAVY" if max_oi_ce_value > max_oi_pe_value else "PUT_HEAVY"

    return {
        "expiry_date":          expiry_date,           # "YYYY-MM-DD" this result belongs to
        "max_oi_ce_strike":     max_oi_ce_strike,       # strike with highest current CE OI
        "max_oi_ce_value":      max_oi_ce_value,        # OI value at that CE strike
        "max_oi_pe_strike":     max_oi_pe_strike,       # strike with highest current PE OI
        "max_oi_pe_value":      max_oi_pe_value,        # OI value at that PE strike
        "buildup_ce_strike":    buildup_ce_strike,      # strike with fastest CE OI growth (or None)
        "buildup_ce_value":     buildup_ce_value,        # size of that CE OI growth
        "buildup_pe_strike":    buildup_pe_strike,      # strike with fastest PE OI growth (or None)
        "buildup_pe_value":     buildup_pe_value,        # size of that PE OI growth
        "bias":                 bias,                   # "CALL_HEAVY" / "PUT_HEAVY" / None
        "snapshots_collected":  snapshots_collected,     # cycles of history collected so far
        "raw_snapshot":         snapshot,                # full {strike: {...}} dict, every strike, this cycle
    }


def print_report(expiry_label, atm_strike, result):
    print(f"\n{'='*60}")
    print(f"  NIFTY  |  {expiry_label} expiry  |  {result['expiry_date']}  |  ATM: {atm_strike}")
    print(f"{'='*60}")

    print(f"  MAX OI (writer concentration right now)")
    print(f"     CALL (CE) wall  -> Strike {result['max_oi_ce_strike']:>8}   OI: {result['max_oi_ce_value']:,}")
    print(f"     PUT  (PE) wall  -> Strike {result['max_oi_pe_strike']:>8}   OI: {result['max_oi_pe_value']:,}")

    if result["snapshots_collected"] < 2:
        print(f"  OI BUILDUP - warming up ({result['snapshots_collected']}/2 snapshots collected)")
    else:
        print(f"  OI BUILDUP (fastest fresh writing - last {result['snapshots_collected']} cycles)")

        if result["buildup_ce_strike"] is not None:
            print(f"     CALL (CE) fresh writing -> Strike {result['buildup_ce_strike']:>8}   +{result['buildup_ce_value']:,} OI")
        else:
            print(f"     CALL (CE) fresh writing -> no net increase detected yet")

        if result["buildup_pe_strike"] is not None:
            print(f"     PUT  (PE) fresh writing -> Strike {result['buildup_pe_strike']:>8}   +{result['buildup_pe_value']:,} OI")
        else:
            print(f"     PUT  (PE) fresh writing -> no net increase detected yet")

    if result["bias"] == "CALL_HEAVY":
        print(f"  READING: Heavier CALL writing -> resistance-style cap near {result['max_oi_ce_strike']}")
    elif result["bias"] == "PUT_HEAVY":
        print(f"  READING: Heavier PUT writing -> support-style floor near {result['max_oi_pe_strike']}")


def is_market_open():
    now = datetime.now()                          # current local datetime
    if now.weekday() >= 5:                        # 5 = Saturday, 6 = Sunday
        return False

    market_open  = now.replace(hour=9,  minute=15, second=0, microsecond=0)  # 09:15 cutoff
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)  # 15:30 cutoff
    return market_open <= now <= market_close


def get_oi_snapshot(tsl=None, oi_history=None, num_strikes=NUM_STRIKES):

    if tsl is None:
        client_id, access_token = get_credentials()
        tsl = Tradehull(client_id, access_token)

    if oi_history is None:
        oi_history = defaultdict(deque)  # oi_history[expiry_date] -> unbounded deque, keeps all snapshots

    all_expiries = fetch_expiries(tsl)  # list of every live expiry date string for NIFTY

    results = []  # one entry per expiry for this cycle

    for idx, expiry_date in enumerate(all_expiries):
        atm_strike, df = fetch_chain_snapshot(tsl, idx)  # atm_strike: float, df: DataFrame or None

        if df is None or df.empty:
            continue

        expiry_label = classify_expiry(expiry_date, all_expiries)  # "WEEKLY" or "MONTHLY"
        result = analyze_snapshot(oi_history, expiry_date, df)     # dict of named output variables
        result["expiry_label"] = expiry_label                      # add label into the same dict
        result["atm_strike"]   = atm_strike                        # add ATM strike into the same dict

        results.append(result)

        time.sleep(RATE_LIMIT_SECS)  # respect Dhan's 1 request / RATE_LIMIT_SECS rate limit

    return results, oi_history


def run_oi_tracker(num_strikes=NUM_STRIKES, rate_limit_secs=RATE_LIMIT_SECS):

    global NUM_STRIKES, RATE_LIMIT_SECS
    NUM_STRIKES     = num_strikes
    RATE_LIMIT_SECS = rate_limit_secs

    client_id, access_token = get_credentials()  # (client_id, access_token) tuple from env vars
    tsl = Tradehull(client_id, access_token)     # authenticated Tradehull client used for all API calls

    oi_history = defaultdict(deque)  # oi_history[expiry_date] -> unbounded deque, keeps all snapshots

    print("Starting NIFTY multi-expiry OI tracker")
    print(f"   Rate limit: 1 request / {RATE_LIMIT_SECS}s (Dhan server-side hard limit)\n")

    if not is_market_open():
        print("   [NOTICE] Market is currently closed. OI values returned now")
        print("            will be stale or near-zero and are not reliable.\n")

    cycle_count = 0  # how many full passes over all expiries have completed

    while True:
        if not is_market_open():
            print("\n[STOP] Market closed. Stopping tracker.")
            break

        cycle_count += 1
        now = datetime.now()  # timestamp for this cycle's header

        print(f"\n\n{'#'*60}")
        print(f"  CYCLE {cycle_count}  |  {now.strftime('%H:%M:%S')}")
        print(f"{'#'*60}")

        results, oi_history = get_oi_snapshot(tsl=tsl, oi_history=oi_history, num_strikes=NUM_STRIKES)

        for result in results:
            print_report(result["expiry_label"], result["atm_strike"], result)

def run_oi_historical():
    client_id, access_token = get_credentials()
    tsl = Tradehull(client_id, access_token)



if __name__ == "__main__":
    try:
        run_oi_tracker()
    except KeyboardInterrupt:
        print("\n\nStopped by user.")