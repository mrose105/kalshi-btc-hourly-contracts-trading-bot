"""
kalshi_btc_bot/vol_surface.py — Implied vol term structure for Kalshi RANGE contracts.

Kalshi contracts are all near-ATM in log-moneyness (< 0.2% OTM for BTC at 100K),
so a traditional moneyness smile adds little. The informative dimension is EXPIRY:
Kalshi uses the same lagged vol estimate across all expiry windows, but the lag
effect is stronger for short-dated contracts (vol decay is faster near expiry).

Method:
    For each expiry window in the ladder, find the ATM RANGE contract and solve
    for Kalshi's implied hourly vol via Brent's method on the RANGE pricing function.

    vol_edge(expiry) = kalshi_iv(expiry) - our_ewma_vol_h

    Positive → Kalshi overestimates vol → RANGE is underpriced → buy YES.
    Larger positive edge → stronger structural mispricing at that expiry.

Usage in backtest:
    vs = KalshiVolTerm()
    if vs.fit(ladder, spot, our_vol_h):
        print(vs.summary())
        edge = vs.vol_edge(contract_hours, our_vol_h)
"""
import math
import numpy as np
from scipy.optimize import brentq
from scipy.stats import norm


# ─────────────────────────────────────────────
# CORE: RANGE PRICING + IMPLIED VOL SOLVER
# ─────────────────────────────────────────────

def _range_price(vol_h: float, spot: float, lo: float, hi: float, hours: float) -> float:
    """
    Price a RANGE binary contract at a given hourly vol.
    No drift (risk-neutral, appropriate for short durations up to 4h).
    """
    if hours <= 0 or spot <= 0 or vol_h <= 0:
        return 0.0
    vol_t = vol_h * math.sqrt(hours)
    # Itô convexity correction — matches model.py's true_prob measure so
    # implied vols solved here are consistent with the pricer.
    mu    = math.log(spot) - 0.5 * vol_t * vol_t
    try:
        z_lo = (math.log(max(1.0, lo)) - mu) / vol_t
        z_hi = (math.log(max(1.0, hi)) - mu) / vol_t
        return float(max(0.0, min(1.0, norm.cdf(z_hi) - norm.cdf(z_lo))))
    except Exception:
        return 0.0


def implied_vol_range(ask: float, spot: float, lo: float, hi: float, hours: float,
                       vol_lo: float = 0.0005, vol_hi: float = 0.20) -> float | None:
    """
    Solve for hourly vol such that RANGE binary price = ask.

    Uses Brent's method. The RANGE price is monotone DECREASING in vol:
    higher vol → wider distribution → lower probability of staying in range.

    Returns None if ask is outside [0.02, 0.92] or no root found in interval.
    """
    if ask <= 0.02 or ask >= 0.92 or hours <= 0 or spot <= 0:
        return None
    try:
        f_lo = _range_price(vol_lo, spot, lo, hi, hours) - ask
        f_hi = _range_price(vol_hi, spot, lo, hi, hours) - ask
        if f_lo * f_hi > 0:
            return None
        sigma = brentq(
            lambda v: _range_price(v, spot, lo, hi, hours) - ask,
            vol_lo, vol_hi, xtol=1e-7, maxiter=50,
        )
        return float(sigma)
    except Exception:
        return None


def binary_range_vega(vol_h: float, spot: float, lo: float, hi: float,
                       hours: float, dv: float = 5e-5) -> float:
    """
    Numerical vega of RANGE contract: ∂P/∂vol_h via central finite difference.

    For ATM symmetric range this is negative (higher vol → lower prob):
        vega = [P(v+dv) - P(v-dv)] / (2·dv) < 0

    The dollar edge from vol discrepancy:
        dollar_edge ≈ |vega| × (kalshi_iv - our_vol_h)   [positive = we have edge]
    """
    p_hi = _range_price(vol_h + dv, spot, lo, hi, hours)
    p_lo = _range_price(vol_h - dv, spot, lo, hi, hours)
    return (p_hi - p_lo) / (2.0 * dv)


# ─────────────────────────────────────────────
# VOL TERM STRUCTURE
# ─────────────────────────────────────────────

class KalshiVolTerm:
    """
    Vol term structure extracted from Kalshi's live/synthetic RANGE ladder.

    For each expiry window, solves for Kalshi's implied hourly vol at the ATM contract.
    Compares to our EWMA vol to find the expiry with the largest vol-lag edge.

    Analogy to gs-quant's vol surface:
        Our ATM-per-expiry structure ≡ a gs-quant flat-smile vol surface sliced at
        each expiry. Since all contracts are < 0.2% OTM, moneyness doesn't add info;
        the term structure is the meaningful dimension.
    """

    def __init__(self):
        self.term: dict[float, float] = {}    # hours → kalshi implied vol_h
        self.vega: dict[float, float] = {}    # hours → binary vega at IV
        self.best_expiry: float | None = None
        self.best_edge_vol: float = 0.0       # kalshi_iv - our_vol_h (positive = edge)
        self.our_vol_h: float = 0.0
        self._fitted = False

    @property
    def fitted(self) -> bool:
        return self._fitted

    def fit(self, ladder: list, spot: float, our_vol_h: float) -> bool:
        """
        Fit vol term structure from a RANGE ladder.

        For each expiry, picks the closest-to-ATM RANGE contract (smallest |otm_dist|)
        and solves for its implied vol.

        Args:
            ladder:     list of contract dicts (from build_ladder or live feed)
            spot:       current BTC spot price
            our_vol_h:  our EWMA vol in hourly units (same units as vol_h in model.py)

        Returns:
            True if >= 2 expiry points solved successfully.
        """
        self.our_vol_h = our_vol_h
        self.term = {}
        self.vega = {}

        # Group RANGE contracts by expiry
        by_expiry: dict[float, list] = {}
        for c in ladder:
            if c["type"] != "RANGE":
                continue
            h = round(c["hours"], 4)
            by_expiry.setdefault(h, []).append(c)

        for hours, contracts in by_expiry.items():
            # Prefer ITM contracts (most informative price), then closest to ATM
            atm_c = min(contracts, key=lambda c: abs(c["otm_dist"]))
            ask   = atm_c.get("ask", 0)
            lo, hi = atm_c["low"], atm_c["high"]

            iv = implied_vol_range(ask, spot, lo, hi, hours)
            if iv is not None:
                self.term[hours] = iv
                # Compute vega at implied vol for dollar-edge weighting
                vg = binary_range_vega(iv, spot, lo, hi, hours)
                self.vega[hours] = vg

        if len(self.term) < 2:
            self.best_expiry   = None
            self.best_edge_vol = 0.0
            self._fitted       = False
            return False

        # Best expiry: largest (kalshi_iv - our_vol_h) → most overpriced vol → cheapest RANGE
        best_h = max(self.term, key=lambda h: self.term[h] - our_vol_h)
        self.best_expiry   = best_h
        self.best_edge_vol = self.term[best_h] - our_vol_h
        self._fitted       = True
        return True

    def vol_edge(self, hours: float, our_vol_h: float | None = None) -> float:
        """
        Vol-space edge at a given expiry: kalshi_iv(hours) - our_vol_h.

        Positive → Kalshi overestimates vol → RANGE underpriced → we have edge.
        Returns 0.0 if no solved IV for that expiry.
        """
        iv = self.term.get(round(hours, 4))
        if iv is None:
            return 0.0
        ov = our_vol_h if our_vol_h is not None else self.our_vol_h
        return iv - ov

    def dollar_edge(self, contract: dict, spot: float, our_vol_h: float | None = None) -> float:
        """
        Vega-weighted vol edge: how much the vol discrepancy moves the binary price.

        dollar_edge = |vega| × vol_edge   [positive = contract is cheap]

        Since RANGE vega is negative, and vol_edge is positive when Kalshi overestimates:
            dollar_edge = (-vega) × vol_edge > 0 when we have edge
        """
        hours = round(contract.get("hours", 0), 4)
        ve    = self.vol_edge(hours, our_vol_h)
        vg    = self.vega.get(hours, 0.0)
        return -vg * ve   # vg < 0, ve > 0 when edge exists → result > 0

    def preferred_expiry(self, candidate_hours: list[float], threshold: float = 0.0) -> float | None:
        """
        From a list of candidate expiry windows, return the one with the largest
        vol edge above threshold. Used by SignalEngine to prefer the best-priced expiry.
        """
        if not self._fitted:
            return None
        best_h, best_e = None, threshold
        for h in candidate_hours:
            e = self.vol_edge(round(h, 4))
            if e > best_e:
                best_e = e
                best_h = h
        return best_h

    def atm_spread(self) -> float:
        """
        Spread between longest and shortest expiry implied vols.
        Positive → Kalshi's lag is larger at long expiries (normal contango in lag).
        """
        if len(self.term) < 2:
            return 0.0
        h_min = min(self.term)
        h_max = max(self.term)
        return self.term[h_max] - self.term[h_min]

    def summary(self) -> str:
        """One-line diagnostic summary."""
        if not self._fitted:
            return "vol_term: not fitted"
        pts = "  ".join(
            f"{h:.3f}h:{v:.5f}({v - self.our_vol_h:+.5f})"
            for h, v in sorted(self.term.items())
        )
        return (f"vol_term best={self.best_expiry}h "
                f"edge_vol={self.best_edge_vol:+.5f} "
                f"atm_spread={self.atm_spread():+.5f} | {pts}")
