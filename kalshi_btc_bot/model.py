import math

from scipy.stats import norm

from .contracts import is_in_money

BARS_PER_HOUR = 900   # 4-second polling

# Vol cone: calibrated to BTC historical realized vol range
# Hourly vol floor/cap expressed in hourly-vol units
_VOL_H_FLOOR = 0.003   # ~30% annualized — never let model assume no movement
_VOL_H_CAP   = 0.030   # ~280% annualized — extreme regime ceiling. 2026-07-06:
                        # was 0.080 (~749% annualized using this file's own
                        # √8760 annualization convention) — 6x too loose to ever
                        # actually clamp a data-glitch/flash-crash vol_h spike
                        # before it corrupts true_prob/gamma. 0.030 sits safely
                        # above HIGH-regime-scaled vol (0.015*1.15≈0.0172,
                        # ~161% annualized) so normal high-vol pricing is
                        # unaffected, but genuinely bounds runaway readings.

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

        # Real-measure GBM mean of log(S_T): E[log(S_T)] = log(S_0) + (μ − σ²/2)·T.
        # drift already carries the μ·T term; subtract the Itô convexity correction
        # so the forecast distribution mean is unbiased. Impact is negligible at BTC
        # vols and T ≤ 4h (~0.005% log-space shift) but principled to include.
        mu = math.log(spot) + drift - 0.5 * vol_t * vol_t
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

    def gamma(self, contract: dict, spot: float, vol: float,
              hours: float, regime: dict, bump_pct: float = 0.001) -> float:
        """
        Simulated gamma: d^2(true_prob)/d(spot)^2 via central finite difference,
        dollar-scaled (x spot^2) so magnitude is comparable across price levels.

        High gamma = true_prob is highly sensitive to a small spot move — the
        near-strike / near-expiry zone where a binary's edge can flip faster
        than a fixed P&L exit threshold reacts.
        """
        if spot <= 0 or hours <= 0:
            return 0.0
        h = spot * bump_pct
        p_up  = self.true_prob(contract, spot + h, vol, hours, regime)
        p_mid = self.true_prob(contract, spot,     vol, hours, regime)
        p_dn  = self.true_prob(contract, spot - h, vol, hours, regime)
        return (p_up - 2 * p_mid + p_dn) / (h * h) * spot * spot
