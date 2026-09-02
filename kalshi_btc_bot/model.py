import math

from scipy.stats import norm, t as _student

from . import config as _C
from .contracts import is_in_money
from .config import BARS_PER_HOUR
from .instrument import ACTIVE as _INSTRUMENT

# Vol cone: hourly vol floor/cap expressed in hourly-vol units, per instrument.
# These MUST come from the active instrument rather than being module constants.
# The BTC values below are 1.67x SPX's median hourly vol — 85% of measured SPX
# observations fall below the BTC floor — so an index instrument inheriting them
# would have vol clamped upward on nearly every true_prob call. See
# instrument.py for each instrument's measured range.
_VOL_H_FLOOR = _INSTRUMENT.vol_h_floor
_VOL_H_CAP   = _INSTRUMENT.vol_h_cap

# BTC reference values, retained for provenance:
#   floor 0.003 — ~30% annualized, never let the model assume no movement.
#   cap   0.030 — ~280% annualized, extreme regime ceiling. 2026-07-06: was
#     0.080 (~749% annualized using this file's own √8760 annualization
#     convention) — 6x too loose to ever actually clamp a data-glitch or
#     flash-crash vol_h spike before it corrupts true_prob/gamma. 0.030 sits
#     safely above HIGH-regime-scaled vol (0.015*1.15≈0.0172, ~161%
#     annualized) so normal high-vol pricing is unaffected, but genuinely
#     bounds runaway readings.

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

    @staticmethod
    def _clamp_prob(p: float) -> float:
        return float(max(0.0, min(1.0, p)))

    @staticmethod
    def _tail_scale(vol_t: float) -> tuple:
        """(scale, df) for the terminal log-price distribution.

        df=None keeps the Gaussian. Otherwise Student-t, with the scale divided
        by sqrt(df/(df-2)) so the distribution's VARIANCE still equals vol_t^2 —
        without that correction, switching to t would silently widen the
        forecast as well as fattening it, and the two effects could not be told
        apart. See config.DIST_TAIL_DF for the calibration evidence.
        """
        df = getattr(_C, "DIST_TAIL_DF", None)
        if df is None or df <= 2.0:
            return vol_t, None
        return vol_t / math.sqrt(df / (df - 2.0)), float(df)

    @staticmethod
    def _cdf(z: float, df: float | None) -> float:
        return float(norm.cdf(z)) if df is None else float(_student.cdf(z, df))

    @staticmethod
    def _sf(z: float, df: float | None) -> float:
        return float(norm.sf(z)) if df is None else float(_student.sf(z, df))

    @staticmethod
    def _logit(p: float) -> float:
        p = max(0.001, min(0.999, p))
        return math.log(p / (1.0 - p))

    @staticmethod
    def _inv_logit(x: float) -> float:
        if x >= 0:
            z = math.exp(-x)
            return 1.0 / (1.0 + z)
        z = math.exp(x)
        return z / (1.0 + z)

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

        # Regime-conditional drift (log-space). Coefficients live in config so
        # they are sweepable; DRIFT_REVERTING_COEF defaults to 0.0 — OFF.
        #
        # WHY IT IS OFF. The reverting term was `-zscore * vol_t * 0.15`, a
        # hardcoded mean-reversion prior that shifts the distribution AWAY from
        # the direction of the move. BOUNDARY_NO buys NO on OTM bands in exactly
        # that direction, so the term lowered true_prob on precisely the
        # contracts the strategy trades.
        #
        # Measured 2026-09-01 over 17,613 band-observations at
        # BOUNDARY_NO-qualifying moments, settlement resolved as-of close from
        # the quotes stream (model_error_decomp.py):
        #
        #     side            n     model   no-drift  realized   ratio
        #     continuation  6291   0.1196    0.1602    0.1725    1.44x under
        #     occupied      3828   0.2559    0.2809    0.2503    0.98x
        #     counter       7494   0.1910    0.1485    0.1536    0.80x over
        #
        # The term under-predicted continuation bands by 44% and over-predicted
        # counter bands by 20%. In aggregate those cancel — the whole population
        # reads 1.01x, which is why this survived review — but the strategy only
        # ever buys the continuation side, so it took the full 44% error.
        #
        # Understated true_prob inflates ask/true_prob, which IS the entry gate
        # (BOUNDARY_NO_OVERPRICING_MIN). So the signal was substantially
        # manufactured by this term. On the 176 live NO round trips the model
        # said those bands get hit 14.0% of the time; they were hit 27.3%.
        #
        # Disabling it fixes BOTH sides at once — continuation 1.44x -> 1.08x,
        # counter 0.80x -> 1.03x — holding vol, tail shape and floors identical.
        #
        # EXPECT FEWER ENTRIES. Continuation true_prob rises ~34%, cutting the
        # overpricing ratio ~25% against a 1.25 gate. That is the point: those
        # entries were priced off the error, not off the market.
        r = regime["regime"]
        drift = 0.0
        if r == "TRENDING":
            drift = regime["mom"] * getattr(_C, "DRIFT_TRENDING_COEF", 0.3)
        elif r == "REVERTING":
            drift = (-regime["zscore"] * vol_t
                     * getattr(_C, "DRIFT_REVERTING_COEF", 0.0))
        elif r == "BREAKOUT":
            drift = regime["mom"] * getattr(_C, "DRIFT_BREAKOUT_COEF", 0.5)

        # Real-measure GBM mean of log(S_T): E[log(S_T)] = log(S_0) + (μ − σ²/2)·T.
        # drift already carries the μ·T term; subtract the Itô convexity correction
        # so the forecast distribution mean is unbiased. Impact is negligible at BTC
        # vols and T ≤ 4h (~0.005% log-space shift) but principled to include.
        mu = math.log(spot) + drift - 0.5 * vol_t * vol_t
        t  = contract["type"]

        # Tail shape. The Gaussian understated P(YES) at BOTH the peak and the
        # tail simultaneously, which no sigma corrects — see config.DIST_TAIL_DF.
        scale, df = self._tail_scale(vol_t)

        try:
            if t == "ABOVE":
                z = (math.log(contract["low"]) - mu) / scale
                return float(max(0.0, min(1.0, self._sf(z, df))))
            elif t == "BELOW":
                z = (math.log(contract["high"]) - mu) / scale
                return float(max(0.0, min(1.0, self._cdf(z, df))))
            elif t == "RANGE":
                z_lo = (math.log(max(1, contract["low"]))  - mu) / scale
                z_hi = (math.log(max(1, contract["high"])) - mu) / scale
                return float(max(0.0, min(1.0,
                                          self._cdf(z_hi, df) - self._cdf(z_lo, df))))
        except Exception:
            return 0.0
        return 0.0

    def market_prob(self, bid: float | None = None,
                    ask: float | None = None,
                    mid: float | None = None) -> float | None:
        """Top-of-book implied YES probability from the current bid/ask."""
        if mid is not None:
            mid = float(mid)
            if 0.0 < mid < 1.0:
                return self._clamp_prob(mid)
        bid = float(bid or 0.0)
        ask = float(ask or 0.0)
        if bid > 0 and ask > 0 and bid < ask:
            return self._clamp_prob((bid + ask) / 2.0)
        if ask > 0:
            return self._clamp_prob(ask)
        if bid > 0:
            return self._clamp_prob(bid)
        return None

    def posterior_prob(self, contract: dict, spot: float, vol: float,
                       hours: float, regime: dict,
                       bid: float | None = None,
                       ask: float | None = None,
                       market_mid: float | None = None) -> dict:
        """Blend model prior with current market-implied probability.

        The GBM output is the prior. The live Kalshi quote is evidence. We blend
        in log-odds space so a repricing like 21c -> 9c materially moves the
        estimate instead of only making the apparent edge larger.
        """
        prior = self.true_prob(contract, spot, vol, hours, regime)
        market = self.market_prob(bid=bid, ask=ask, mid=market_mid)
        if market is None:
            return {
                "prior_prob": prior,
                "market_prob": None,
                "true_prob": prior,
                "market_weight": 0.0,
            }

        bid_f = float(bid or 0.0)
        ask_f = float(ask or 0.0)
        spread = max(0.0, ask_f - bid_f) if bid_f > 0 and ask_f > 0 else 0.10
        spread_weight = max(0.0, 1.0 - min(spread, 0.12) / 0.12)
        time_weight = max(0.0, min(1.0, (4.0 - max(0.0, hours)) / 4.0))
        # Markets incorporate order-flow information the GBM does not. Make the
        # evidence meaningful but leave room for explicit signal/regime edge.
        weight = min(
            _C.BAYES_MARKET_WEIGHT_MAX,
            _C.BAYES_MARKET_WEIGHT_BASE + 0.10 * time_weight + 0.10 * spread_weight,
        )

        posterior_logit = ((1.0 - weight) * self._logit(prior)
                           + weight * self._logit(market))
        posterior = self._clamp_prob(self._inv_logit(posterior_logit))
        if _C.BAYES_MAX_MOVE > 0:
            posterior = max(prior - _C.BAYES_MAX_MOVE,
                            min(prior + _C.BAYES_MAX_MOVE, posterior))
        return {
            "prior_prob": prior,
            "market_prob": market,
            "true_prob": posterior,
            "market_weight": weight,
        }

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
