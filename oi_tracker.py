"""
NIFTY Multi-Expiry Option Chain OI Concentration Tracker
==========================================================

Tracks Open Interest (OI) across ALL available Nifty expiries (weekly + monthly)
for both CE and PE, and identifies:

  1. MAX OI STRIKE   — the strike with the highest current OI (classic "wall" /
                       max-pain-style concentration — where writers are heavily
                       positioned right now)
  2. MAX OI BUILDUP  — the strike where OI is increasing the FASTEST cycle-to-cycle
                       (fresh writing happening right now, in real time)

IMPORTANT — Dhan API rate limit:
    Dhan's Option Chain endpoint allows only 1 request every 3 seconds,
    GLOBALLY across your account (not per-expiry). This is a hard server-side
    limit, not something this script can bypass.

    With N expiries tracked, one full cycle through all of them takes
    at least N x 3 seconds. For Nifty (typically 6-8 live expiries: weekly +
    monthly across current/next month), expect a refresh cycle of roughly
    20-25 seconds per expiry, not true sub-second "tick by tick".

    This script is built to be HONEST about that: each expiry is refreshed
    as fast as the rate limit allows, sequentially, in a continuous loop.
"""

import os
import time
from datetime import datetime
from collections import defaultdict, deque

from Dhan_Tradehull import Tradehull


# ── CREDENTIALS ───────────────────────────────────────────────────────────────

CLIENT_ID    = "1111875444"
ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiJ9.eyJpc3MiOiJkaGFuIiwicGFydG5lcklkIjoiIiwiZXhwIjoxNzgyMDE4MTY4LCJpYXQiOjE3ODE5MzE3NjgsInRva2VuQ29uc3VtZXJUeXBlIjoiU0VMRiIsIndlYmhvb2tVcmwiOiIiLCJkaGFuQ2xpZW50SWQiOiIxMTExODc1NDQ0In0.ZMo0agYZ2pMlSeg1ZvGY1o4ZjRz2rhYhzSoGzgbpWXv9IGgKHDwVKhZPqgxfm8s5YP32l2Vuf2ku1zK86YMjSw"

if not CLIENT_ID or not ACCESS_TOKEN:
    raise EnvironmentError(
        "Missing credentials. Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN as "
        "environment variables before running."
    )

tsl = Tradehull(CLIENT_ID, ACCESS_TOKEN)


# ── CONFIG ────────────────────────────────────────────────────────────────────

UNDERLYING       = "NIFTY"
EXCHANGE         = "INDEX"     # NIFTY index option chain (Dhan resolves to NSE internally)
NUM_STRIKES      = 15          # strikes on each side of ATM to pull (15 = 31 strikes total)
RATE_LIMIT_SECS  = 3.0         # Dhan's hard limit: 1 request / 3 sec, do not go faster
HISTORY_LEN      = 5           # how many past snapshots to keep per expiry, for buildup tracking


# ── STATE ─────────────────────────────────────────────────────────────────────
# oi_history[expiry_date] = deque of {strike: {"CE": oi, "PE": oi}} snapshots, newest last

oi_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=HISTORY_LEN))


# ── HELPERS ───────────────────────────────────────────────────────────────────

def fetch_expiries():
    """Fetch every live expiry for NIFTY (mix of weekly + monthly), oldest first."""
    expiries = tsl.get_expiry_list(Underlying=UNDERLYING, exchange=EXCHANGE)
    if not expiries:
        raise RuntimeError("No expiries returned — check credentials / market hours.")
    return expiries


def classify_expiry(expiry_date_str, all_expiries):
    """
    Label an expiry as WEEKLY or MONTHLY.
    Heuristic: the LAST expiry date that falls within a given calendar month
    is the monthly expiry; every other expiry in that month is weekly.
    """
    target = datetime.strptime(expiry_date_str, "%Y-%m-%d")
    same_month = [
        e for e in all_expiries
        if datetime.strptime(e, "%Y-%m-%d").month == target.month
        and datetime.strptime(e, "%Y-%m-%d").year == target.year
    ]
    monthly_expiry = max(same_month)  # last expiry in that month = monthly contract
    return "MONTHLY" if expiry_date_str == monthly_expiry else "WEEKLY"


def fetch_chain_snapshot(expiry_index):
    """
    Pull one option chain snapshot for the given expiry index.
    Returns: (expiry_date_str, atm_strike, dataframe) or (None, None, None) on failure.
    """
    try:
        atm_strike, df = tsl.get_option_chain(
            Underlying  = UNDERLYING,
            exchange    = EXCHANGE,
            expiry      = expiry_index,
            num_strikes = NUM_STRIKES,
        )
        return atm_strike, df
    except Exception as e:
        print(f"  ⚠️  Fetch failed for expiry index {expiry_index}: {e}")
        return None, None


def update_history_and_analyze(expiry_date, df):
    """
    Push the new OI snapshot into history, then compute:
      - max_oi_ce_strike, max_oi_pe_strike   (current concentration)
      - max_buildup_ce_strike, max_buildup_pe_strike  (fastest-growing OI)
    """
    snapshot = {}
    for _, row in df.iterrows():
        strike = row["Strike Price"]
        snapshot[strike] = {
            "CE_OI": row.get("CE OI") or 0,
            "PE_OI": row.get("PE OI") or 0,
            "CE_CHG": row.get("CE Chg in OI") or 0,
            "PE_CHG": row.get("PE Chg in OI") or 0,
        }

    history = oi_history[expiry_date]
    history.append(snapshot)

    # ── 1. MAX OI (current concentration — where writers are positioned NOW) ──
    max_ce_strike = max(snapshot, key=lambda s: snapshot[s]["CE_OI"])
    max_pe_strike = max(snapshot, key=lambda s: snapshot[s]["PE_OI"])

    # ── 2. MAX BUILDUP (fastest OI increase across recent snapshots) ──────────
    # Compare oldest snapshot in our short rolling window to the newest.
    buildup_ce_strike = buildup_pe_strike = None
    max_ce_buildup = max_pe_buildup = 0

    if len(history) >= 2:
        oldest = history[0]
        newest = history[-1]
        for strike in newest:
            if strike not in oldest:
                continue
            ce_delta = newest[strike]["CE_OI"] - oldest[strike]["CE_OI"]
            pe_delta = newest[strike]["PE_OI"] - oldest[strike]["PE_OI"]
            if ce_delta > max_ce_buildup:
                max_ce_buildup = ce_delta
                buildup_ce_strike = strike
            if pe_delta > max_pe_buildup:
                max_pe_buildup = pe_delta
                buildup_pe_strike = strike

    return {
        "max_ce_strike":     max_ce_strike,
        "max_ce_oi":         snapshot[max_ce_strike]["CE_OI"],
        "max_pe_strike":     max_pe_strike,
        "max_pe_oi":         snapshot[max_pe_strike]["PE_OI"],
        "buildup_ce_strike": buildup_ce_strike,
        "buildup_ce_amount": max_ce_buildup,
        "buildup_pe_strike": buildup_pe_strike,
        "buildup_pe_amount": max_pe_buildup,
        "snapshots_collected": len(history),
    }


def print_report(expiry_date, expiry_label, atm_strike, analysis):
    print(f"\n{'═'*60}")
    print(f"  NIFTY  |  {expiry_label} expiry  |  {expiry_date}  |  ATM: {atm_strike}")
    print(f"{'═'*60}")

    print(f"  📍 MAX OI (writer concentration RIGHT NOW)")
    print(f"     CALL (CE) wall  → Strike {analysis['max_ce_strike']:>8}   OI: {analysis['max_ce_oi']:,}")
    print(f"     PUT  (PE) wall  → Strike {analysis['max_pe_strike']:>8}   OI: {analysis['max_pe_oi']:,}")

    if analysis["snapshots_collected"] < 2:
        print(f"  📈 OI BUILDUP — warming up ({analysis['snapshots_collected']}/2 snapshots collected)")
    else:
        ce_s = analysis["buildup_ce_strike"]
        pe_s = analysis["buildup_pe_strike"]
        print(f"  📈 OI BUILDUP (fastest fresh writing — last {analysis['snapshots_collected']} cycles)")
        if ce_s is not None:
            print(f"     CALL (CE) fresh writing → Strike {ce_s:>8}   +{analysis['buildup_ce_amount']:,} OI")
        else:
            print(f"     CALL (CE) fresh writing → no net increase detected yet")
        if pe_s is not None:
            print(f"     PUT  (PE) fresh writing → Strike {pe_s:>8}   +{analysis['buildup_pe_amount']:,} OI")
        else:
            print(f"     PUT  (PE) fresh writing → no net increase detected yet")

    # Simple bias read
    if analysis["max_ce_oi"] and analysis["max_pe_oi"]:
        if analysis["max_ce_oi"] > analysis["max_pe_oi"]:
            print(f"  🧭 Reading: Heavier CALL writing → resistance-style cap near {analysis['max_ce_strike']}")
        else:
            print(f"  🧭 Reading: Heavier PUT writing → support-style floor near {analysis['max_pe_strike']}")


# ── MAIN LOOP ─────────────────────────────────────────────────────────────────

def main():
    print("🚀 Starting NIFTY multi-expiry OI tracker")
    print(f"   Rate limit: 1 request / {RATE_LIMIT_SECS}s (Dhan server-side hard limit)\n")

    all_expiries = fetch_expiries()
    print(f"   Found {len(all_expiries)} live expiries:")
    for i, e in enumerate(all_expiries):
        label = classify_expiry(e, all_expiries)
        print(f"     [{i}] {e}  ({label})")

    cycle_count = 0

    while True:
        now = datetime.now()
        if now.hour >= 15 and now.minute >= 30:
            print("\n🔔 Market closed (15:30). Stopping tracker.")
            break

        cycle_count += 1
        print(f"\n\n{'#'*60}")
        print(f"  CYCLE {cycle_count}  |  {now.strftime('%H:%M:%S')}")
        print(f"{'#'*60}")

        for idx, expiry_date in enumerate(all_expiries):
            cycle_start = time.time()

            atm_strike, df = fetch_chain_snapshot(idx)

            if df is None or df.empty:
                print(f"  ⚠️  No data for expiry index {idx} ({expiry_date}), skipping.")
            else:
                label = classify_expiry(expiry_date, all_expiries)
                analysis = update_history_and_analyze(expiry_date, df)
                print_report(expiry_date, label, atm_strike, analysis)

            # ── Respect Dhan's 1 request / 3 sec rate limit ──────────────────
            elapsed = time.time() - cycle_start
            sleep_for = max(0, RATE_LIMIT_SECS - elapsed)
            time.sleep(sleep_for)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n🛑 Stopped by user.")
