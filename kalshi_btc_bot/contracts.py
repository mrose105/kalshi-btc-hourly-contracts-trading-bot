from .instrument import ACTIVE as _INSTRUMENT

# ─────────────────────────────────────────────
# CONTRACT PARSER
# ─────────────────────────────────────────────
# Strike labels are the main way ladder/signal output is read by eye, so they
# follow the instrument: BTC strikes are dollars, index strikes are points.
_S = _INSTRUMENT.fmt_strike


def _from_strikes(market: dict) -> dict | None:
    """Build a contract from the exchange's own strike fields.

    Kalshi returns `floor_strike` / `cap_strike` on every market, which is the
    authoritative range. Inferring geometry from the ticker string instead was
    wrong on both axes:

      * the ticker's trailing number is the range MIDPOINT, not its floor
        (`B74625` is $74,500–$74,749.99, mid 74,625);
      * band width varies by expiry — 250 on the hourlies, 500 on the weekly —
        and was hardcoded to 100.

    Combined, the modelled band sat $50 off center and 2.5x too narrow, moving
    true_prob by up to 37 points against a MIN_EDGE of 1.5 and throwing gamma
    off by ~50%. Using the exchange's own numbers also removes the old
    spot-dependent ABOVE/BELOW guess, which could reclassify the same ticker as
    BTC moved.
    """
    floor_raw = market.get("floor_strike")
    cap_raw   = market.get("cap_strike")
    try:
        floor_v = float(floor_raw) if floor_raw is not None else None
        cap_v   = float(cap_raw)   if cap_raw   is not None else None
    except (TypeError, ValueError):
        return None

    if floor_v is not None and cap_v is not None:
        return {"type": "RANGE", "direction": "NEUTRAL",
                "strike": (floor_v + cap_v) / 2.0,
                "low": floor_v, "high": cap_v,
                "label": f"{_S(floor_v)}-{_S(cap_v)}"}
    if floor_v is not None:
        return {"type": "ABOVE", "direction": "UP",
                "strike": floor_v, "low": floor_v, "high": float("inf"),
                "label": f"≥{_S(floor_v)}"}
    if cap_v is not None:
        return {"type": "BELOW", "direction": "DN",
                "strike": cap_v, "low": 0, "high": cap_v,
                "label": f"≤{_S(cap_v)}"}
    return None


def parse_contract(ticker: str, spot: float, market: dict | None = None) -> dict:
    """Resolve a market to {type, low, high, strike, label}.

    Prefers the exchange's floor/cap strikes when the market payload is given
    (see _from_strikes). The ticker-string path below is a fallback for callers
    without a payload; it assumes a 100-wide band and reads the trailing number
    as the floor, so it is approximate and should not be relied on for pricing.
    """
    if market:
        parsed = _from_strikes(market)
        if parsed is not None:
            return parsed
    try:
        part = ticker.split("-")[-1]
        if part.startswith("T"):
            strike = float(part[1:].replace(".99", ""))
            if strike > spot * 0.98:
                return {"type": "ABOVE", "direction": "UP",
                        "strike": strike, "low": strike, "high": float("inf"),
                        "label": f"≥{_S(strike)}"}
            else:
                return {"type": "BELOW", "direction": "DN",
                        "strike": strike, "low": 0, "high": strike,
                        "label": f"≤{_S(strike)}"}
        elif part.startswith("B"):
            low  = float(part[1:])
            high = low + 100
            return {"type": "RANGE", "direction": "NEUTRAL",
                    "strike": low + 50, "low": low, "high": high,
                    "label": f"{_S(low)}-{_S(high)}"}
    except Exception:
        pass
    return {"type": "UNKNOWN", "direction": "NEUTRAL",
            "strike": 0, "low": 0, "high": 0, "label": "?"}

def is_in_money(contract: dict, spot: float) -> bool:
    t = contract["type"]
    if t == "ABOVE": return spot >= contract["low"]
    if t == "BELOW": return spot <= contract["high"]
    if t == "RANGE": return contract["low"] <= spot < contract["high"]
    return False

def otm_distance(contract: dict, spot: float) -> float:
    t = contract["type"]
    if t == "ABOVE": return spot - contract["low"]
    if t == "BELOW": return contract["high"] - spot
    if t == "RANGE":
        if spot < contract["low"]:   return spot - contract["low"]
        if spot >= contract["high"]: return contract["high"] - spot
        return min(spot - contract["low"], contract["high"] - spot)
    return 0.0
