from collections import defaultdict, deque
from datetime import datetime


DEFAULT_CONFIG = {
    "enabled": False,
    "underlying": "NIFTY",
    "underlying_security_id": "13",
    "underlying_segment": "IDX_I",
    "expiry_index": 0,
    "rate_limit_secs": 3,
    "history_len": 5,
}


class OITracker:
    def __init__(self, dhan, config=None):
        self.dhan = dhan
        self.config = DEFAULT_CONFIG.copy()
        self.config.update(config or {})
        self.history = defaultdict(lambda: deque(maxlen=int(self.config["history_len"])))
        self.expiries = []
        self.last_result = None

    def refresh(self):
        if not self.config.get("enabled", False):
            return None

        expiry = self._selected_expiry()
        response = self._get_option_chain(expiry)
        chain = self._extract_chain(response)
        result = self._analyze(expiry, chain)
        self.last_result = result
        return result

    def print_result(self, result=None):
        result = result or self.last_result
        if result is None:
            print("[OI] no data yet")
            return

        print(
            "[OI] {underlying} expiry={expiry} "
            "CE max {ce_strike} ({ce_oi}) | PE max {pe_strike} ({pe_oi}) | bias={bias}".format(
                underlying=self.config["underlying"],
                expiry=result["expiry"],
                ce_strike=result["max_oi_ce_strike"],
                ce_oi=result["max_oi_ce_value"],
                pe_strike=result["max_oi_pe_strike"],
                pe_oi=result["max_oi_pe_value"],
                bias=result["bias"],
            )
        )

        if result["snapshots_collected"] >= 2:
            print(
                "[OI] buildup CE {ce_strike} (+{ce_value}) | PE {pe_strike} (+{pe_value})".format(
                    ce_strike=result["buildup_ce_strike"],
                    ce_value=result["buildup_ce_value"],
                    pe_strike=result["buildup_pe_strike"],
                    pe_value=result["buildup_pe_value"],
                )
            )

    def _selected_expiry(self):
        if not self.expiries:
            self.expiries = self._get_expiry_list()

        expiry_index = int(self.config.get("expiry_index", 0))
        if not self.expiries:
            raise RuntimeError("No expiries returned by Dhan.")
        if expiry_index >= len(self.expiries):
            raise IndexError(f"expiry_index {expiry_index} out of range: {self.expiries}")

        return self.expiries[expiry_index]

    def _get_expiry_list(self):
        security_id = str(self.config["underlying_security_id"])

        if hasattr(self.dhan, "get_expiry_list"):
            response = self.dhan.get_expiry_list(
                underlying_security_id=security_id,
                underlying_type=self._sdk_underlying_type(),
            )
        else:
            response = self.dhan.expiry_list(
                under_security_id=int(security_id),
                under_exchange_segment=self._api_underlying_segment(),
            )

        data = response.get("data", response) if isinstance(response, dict) else response
        if isinstance(data, dict):
            data = data.get("expiryDates") or data.get("expiry") or data.get("data") or []

        return list(data)

    def _get_option_chain(self, expiry):
        security_id = str(self.config["underlying_security_id"])

        if hasattr(self.dhan, "get_option_chain"):
            return self.dhan.get_option_chain(
                underlying_security_id=security_id,
                underlying_type=self._sdk_underlying_type(),
                expiry_date=expiry,
            )

        return self.dhan.option_chain(
            under_security_id=int(security_id),
            under_exchange_segment=self._api_underlying_segment(),
            expiry=expiry,
        )

    def _extract_chain(self, response):
        data = response

        while isinstance(data, dict):
            if data.get("status") == "failure":
                raise ValueError(f"Dhan option-chain request failed: {data}")

            if isinstance(data.get("oc"), dict):
                return data["oc"]

            if isinstance(data.get("optionChain"), dict):
                return data["optionChain"]

            nested_data = data.get("data")
            if isinstance(nested_data, dict) and nested_data is not data:
                data = nested_data
                continue

            numeric_strikes = {}
            for strike, row in data.items():
                try:
                    float(strike)
                except (TypeError, ValueError):
                    continue
                numeric_strikes[strike] = row

            if numeric_strikes:
                return numeric_strikes

            break

        raise ValueError(f"Unsupported option-chain response: {response}")

    def _analyze(self, expiry, chain):
        snapshot = {}

        for strike, row in chain.items():
            try:
                strike_value = float(strike)
            except (TypeError, ValueError):
                continue

            ce = row.get("ce", row.get("CE", {})) if isinstance(row, dict) else {}
            pe = row.get("pe", row.get("PE", {})) if isinstance(row, dict) else {}

            snapshot[strike_value] = {
                "CE_OI": self._number(ce.get("oi", ce.get("open_interest", ce.get("OI", 0)))),
                "PE_OI": self._number(pe.get("oi", pe.get("open_interest", pe.get("OI", 0)))),
            }

        if not snapshot:
            sample_keys = list(chain)[:5] if isinstance(chain, dict) else []
            raise ValueError(f"Option chain had no numeric strikes to analyze. Sample keys: {sample_keys}")

        history = self.history[expiry]
        history.append(snapshot)

        max_oi_ce_strike = max(snapshot, key=lambda strike: snapshot[strike]["CE_OI"])
        max_oi_pe_strike = max(snapshot, key=lambda strike: snapshot[strike]["PE_OI"])
        max_oi_ce_value = snapshot[max_oi_ce_strike]["CE_OI"]
        max_oi_pe_value = snapshot[max_oi_pe_strike]["PE_OI"]

        buildup_ce_strike = None
        buildup_pe_strike = None
        buildup_ce_value = 0
        buildup_pe_value = 0

        if len(history) >= 2:
            oldest = history[0]
            newest = history[-1]
            for strike, latest in newest.items():
                if strike not in oldest:
                    continue
                ce_delta = latest["CE_OI"] - oldest[strike]["CE_OI"]
                pe_delta = latest["PE_OI"] - oldest[strike]["PE_OI"]
                if ce_delta > buildup_ce_value:
                    buildup_ce_value = ce_delta
                    buildup_ce_strike = strike
                if pe_delta > buildup_pe_value:
                    buildup_pe_value = pe_delta
                    buildup_pe_strike = strike

        bias = None
        if max_oi_ce_value and max_oi_pe_value:
            bias = "CALL_HEAVY" if max_oi_ce_value > max_oi_pe_value else "PUT_HEAVY"

        return {
            "time": datetime.now(),
            "expiry": expiry,
            "max_oi_ce_strike": max_oi_ce_strike,
            "max_oi_ce_value": max_oi_ce_value,
            "max_oi_pe_strike": max_oi_pe_strike,
            "max_oi_pe_value": max_oi_pe_value,
            "buildup_ce_strike": buildup_ce_strike,
            "buildup_ce_value": buildup_ce_value,
            "buildup_pe_strike": buildup_pe_strike,
            "buildup_pe_value": buildup_pe_value,
            "bias": bias,
            "snapshots_collected": len(history),
            "raw_snapshot": snapshot,
        }

    def _number(self, value):
        if value is None:
            return 0
        return int(float(value))

    def _sdk_underlying_type(self):
        segment = str(self.config["underlying_segment"]).upper()
        if segment in {"IDX_I", "INDEX"}:
            return "INDEX"
        return segment

    def _api_underlying_segment(self):
        segment = str(self.config["underlying_segment"]).upper()
        if segment == "INDEX":
            return "IDX_I"
        return segment
