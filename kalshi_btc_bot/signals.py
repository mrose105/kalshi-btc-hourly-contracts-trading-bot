from . import config as _C
from .config import (
    MAX_HOURS, MAX_OTM_B, MAX_OTM_T, MIN_HOURS,
    NO_CASH_MIN_PCT, NO_DIST_MAX, NO_DIST_MIN, NO_HOURS_MAX, NO_HOURS_MIN,
    NO_OVERPRICING_MIN, NO_TRUE_PROB_MAX, NO_YES_ASK_MAX, NO_YES_ASK_MIN, TIME_EXIT_MINS,
)
# MIN_EDGE and MIN_EDGE_COMPRESSION intentionally NOT imported as local names —
# read via _C.MIN_EDGE so that run_backtest()'s C.MIN_EDGE = override takes effect.

# ─────────────────────────────────────────────
# SIGNAL ENGINE
# ─────────────────────────────────────────────
class SignalEngine:
    def __init__(self, dist):
        self.dist = dist

    def find_best(self, spot, vol, regime, ladder, existing,
                  vol_term=None) -> dict | None:
        """
        vol_term: optional KalshiVolTerm — when provided, adds a vega-weighted vol-edge
        boost to contracts at expiry windows where Kalshi's lag is largest. This ranks
        contracts by actual arbitrage edge (vol-space × vega) rather than prob-space alone.
        Live bot passes None (unchanged); backtest passes a fitted KalshiVolTerm.
        """
        vol_comp  = regime.get("vol_compression", False)
        use_t     = regime["use_t"]
        direction = regime["direction"]

        # During vol compression Kalshi's lagged model overestimates vol →
        # RANGE contracts are structurally cheap → lower edge bar + allow wider OTM
        # During normal/high vol: tighten OTM gate to avoid directional lottery tickets
        eff_min_edge = _C.MIN_EDGE_COMPRESSION if vol_comp else _C.MIN_EDGE
        otm_gate     = MAX_OTM_B if vol_comp else 50   # non-compression: only $50 OTM max

        best_edge = eff_min_edge
        best      = None

        for c in ladder:
            if c["ticker"] in existing:
                continue
            if c["hours"] < MIN_HOURS or c["hours"] > MAX_HOURS:
                continue
            ctype = c["type"]
            if use_t:
                if ctype == "ABOVE" and direction == "DN":
                    continue
                if ctype == "BELOW" and direction == "UP":
                    continue
                if ctype in ("ABOVE", "BELOW") and c["otm_dist"] < -MAX_OTM_T:
                    continue
                if ctype == "RANGE" and c["otm_dist"] < -MAX_OTM_B:
                    continue
            else:
                if ctype != "RANGE":
                    continue
                if c["otm_dist"] < -otm_gate:
                    continue
                # Without a confirmed directional regime, OTM RANGE entries are pure
                # lottery tickets — BTC must move TO the range for us to win.
                # Only take OTM entries under vol compression (structural mispricing)
                # OR with a confirmed trending/reverting signal aimed at the range.
                if (not vol_comp and c["otm_dist"] < -20
                        and (regime["regime"] == "RANGING" or regime["conf"] < 0.60)):
                    continue

            # Tighten OTM limits as expiry approaches
            mins_left     = c["hours"] * 60
            dynamic_otm   = otm_gate
            if mins_left < 20:   dynamic_otm   = min(otm_gate, 30)
            elif mins_left < 30: dynamic_otm   = min(otm_gate, 60)
            dynamic_otm_t = 30 if mins_left < 20 else 60 if mins_left < 30 else MAX_OTM_T
            if ctype == "RANGE" and c["otm_dist"] < -dynamic_otm:
                continue
            if ctype in ("ABOVE", "BELOW") and c["otm_dist"] < -dynamic_otm_t:
                continue

            # Skip if time exit would fire immediately after entry
            if mins_left < TIME_EXIT_MINS and not c["itm"]:
                continue

            true_p    = self.dist.true_prob(c, spot, vol, c["hours"], regime)
            raw_edge  = true_p - c["ask"]

            # Boost near-money RANGE contracts during compression:
            # Kalshi prices at historical-high vol, we see vol has dropped →
            # the true probability of staying in range is higher than Kalshi thinks
            comp_boost = 0.0
            if vol_comp and ctype == "RANGE" and c["otm_dist"] >= -30:
                comp_boost = 0.015  # structural underpricing bonus

            rank_edge = raw_edge * 1.15 if c["itm"] else raw_edge
            rank_edge += comp_boost

            # Vol-term surface expiry preference: nudge rank_edge for the expiry window
            # where Kalshi's implied vol lag is largest. A tiny 0.2¢ preference is enough
            # to break ties between similar contracts without lowering the effective edge
            # bar (which would double-count the vol compression already in raw_edge).
            if (vol_term is not None and vol_term.fitted and ctype == "RANGE"
                    and vol_term.best_expiry is not None
                    and abs(c["hours"] - vol_term.best_expiry) < 0.01
                    and vol_term.best_edge_vol > 0):
                rank_edge += 0.002  # 0.2¢ tiebreaker — does NOT expand trade universe

            if rank_edge > best_edge:
                best_edge  = rank_edge
                vol_edge_v = (vol_term.vol_edge(c["hours"]) if vol_term and vol_term.fitted
                              else 0.0)
                best = {**c, "true_prob": true_p, "edge": raw_edge,
                        "vol_compression": vol_comp,
                        "vol_term_edge": vol_edge_v}

        return best

    def find_no_scalp(self, spot, vol, regime, ladder, existing,
                      real_cash: float, start_total: float) -> dict | None:
        """Scan for overpriced YES contracts worth fading via NO."""
        r = regime["regime"]
        d = regime["direction"]
        # Don't fade into strong upside momentum
        if (r == "TRENDING" and d == "UP") or (r == "BREAKOUT" and d == "UP"):
            return None
        if start_total > 0 and real_cash < start_total * NO_CASH_MIN_PCT:
            return None

        best_ratio = NO_OVERPRICING_MIN
        best       = None

        for c in ladder:
            if c["ticker"] in existing:
                continue
            if c["hours"] < NO_HOURS_MIN or c["hours"] > NO_HOURS_MAX:
                continue
            yes_ask = c["ask"]
            if yes_ask < NO_YES_ASK_MIN or yes_ask > NO_YES_ASK_MAX:
                continue
            d_val = c["otm_dist"]
            if d_val < NO_DIST_MIN or d_val > NO_DIST_MAX:
                continue

            true_p = self.dist.true_prob(c, spot, vol, c["hours"], regime)
            if true_p <= 0 or true_p >= NO_TRUE_PROB_MAX:
                continue

            ratio    = yes_ask / true_p
            edge_pct = (yes_ask - true_p) / true_p * 100

            if ratio > best_ratio:
                best_ratio = ratio
                best = {
                    **c,
                    "signal":            "MISPRICE_NO",
                    "true_prob":         true_p,
                    "overpricing_ratio": ratio,
                    "edge_pct":          edge_pct,
                    "no_cost":           1.0 - yes_ask,
                }

        return best

