
# ─────────────────────────────────────────────
# CONTRACT PARSER
# ─────────────────────────────────────────────
def parse_contract(ticker: str, spot: float) -> dict:
    try:
        part = ticker.split("-")[-1]
        if part.startswith("T"):
            strike = float(part[1:].replace(".99", ""))
            if strike > spot * 0.98:
                return {"type": "ABOVE", "direction": "UP",
                        "strike": strike, "low": strike, "high": float("inf"),
                        "label": f"≥${strike:,.0f}"}
            else:
                return {"type": "BELOW", "direction": "DN",
                        "strike": strike, "low": 0, "high": strike,
                        "label": f"≤${strike:,.0f}"}
        elif part.startswith("B"):
            low  = float(part[1:])
            high = low + 100
            return {"type": "RANGE", "direction": "NEUTRAL",
                    "strike": low + 50, "low": low, "high": high,
                    "label": f"${low:,.0f}-${high:,.0f}"}
    except:
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
