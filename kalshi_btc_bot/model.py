import math

from scipy.stats import norm

from .contracts import is_in_money

BARS_PER_HOUR = 900   # 4-second polling

# Vol cone: calibrated to BTC historical realized vol range
# Hourly vol floor/cap expressed in hourly-vol units
_VOL_H_FLOOR = 0.003   # ~30% annualized — never let model assume no movement
_VOL_H_CAP   = 0.080   # ~120%+ annualized — extreme regime ceiling

# ─────────────────────────────────────────────
# DISTRIBUTION MODEL
# ─────────────────────────────────────────────
class DistModel:
    """
    Binary option pricing model for Kalshi RANGE/ABOVE/BELOW contracts.

    Pricing formula: lognormal GBM with regime-conditional drift.
    Vol input:       EWMA per-bar vol from BTCFeed.ewma_volatility().
    CDF:             scipy.stats.norm (numerically stable, replaces hand-rolled erf).
    Vol regime:      HIGH → +15% vol adjustment; LOW → -8%; NORMAL → flat.
    """

    def true_prob(self, contract: dict, spot: float,
                  vol: float, hours: float, regime: dict) -> float:
        if hours <= 0:
            return 1.0 if is_in_money(contract, spot) else 0.0
        if spot <= 0:
            return 0.0

        # Annualize per-bar EWMA vol → hourly vol
        vol_h = vol * math.sqrt(BARS_PER_HOUR)

        # Vol regime scaling: high vol = fatter tails, low vol = narrower
        vol_regime = regime.get("vol_regime", "NORMAL")
        if vol_regime == "HIGH":
            vol_h *= 1.15
        elif vol_regime == "LOW":
            vol_h *= 0.92

        vol_h = max(_VOL_H_FLOOR, min(_VOL_H_CAP, vol_h))
        vol_t = vol_h * math.sqrt(hours)

        # Regime-conditional drift (log-space)
        r = regime["regime"]
        drift = 0.0
        if r == "TRENDING":
            drift = regime["mom"] * 0.3
        elif r == "REVERTING":
            drift = -regime["zscore"] * vol_t * 0.15
        elif r == "BREAKOUT":
            drift = regime["mom"] * 0.5

        mu = math.log(spot) + drift
        t  = contract["type"]

        try:
            if t == "ABOVE":
                z = (math.log(contract["low"]) - mu) / vol_t
                return float(max(0.0, min(1.0, norm.sf(z))))
            elif t == "BELOW":
                z = (math.log(contract["high"]) - mu) / vol_t
                return float(max(0.0, min(1.0, norm.cdf(z))))
            elif t == "RANGE":
                z_lo = (math.log(max(1, contract["low"]))  - mu) / vol_t
                z_hi = (math.log(max(1, contract["high"])) - mu) / vol_t
                return float(max(0.0, min(1.0, norm.cdf(z_hi) - norm.cdf(z_lo))))
        except Exception:
            return 0.0
        return 0.0
