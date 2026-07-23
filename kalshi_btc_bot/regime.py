import math

from .config import (
    BARS_PER_HOUR, BREAKOUT_ACCEL, REVERT_ZSCORE, TREND_BARS, TREND_THRESHOLD,
    VOL_REGIME_LOW_H, VOL_REGIME_HIGH_H, VOL_RATIO_COMPRESSION,
)
from .feed import BTCFeed

# ─────────────────────────────────────────────
# REGIME ENGINE
# ─────────────────────────────────────────────
class RegimeEngine:
    def detect(self, feed: BTCFeed) -> dict:
        mom   = feed.momentum(60)
        accel = feed.acceleration()
        vol   = feed.ewma_volatility()   # EWMA replaces plain rolling stdev
        z     = feed.zscore(300)
        cn, cd= feed.consecutive()

        if abs(accel) > BREAKOUT_ACCEL and abs(mom) > TREND_THRESHOLD * 2:
            regime    = "BREAKOUT"
            direction = "UP" if accel > 0 else "DN"
            conf      = min(0.90, abs(accel) / BREAKOUT_ACCEL * 0.5)
            use_t     = True
        elif cn >= TREND_BARS and abs(mom) > TREND_THRESHOLD:
            regime    = "TRENDING"
            direction = cd
            conf      = min(0.85, cn / 6)
            use_t     = True
        elif abs(z) > REVERT_ZSCORE and abs(accel) < 0.001:
            regime    = "REVERTING"
            direction = "DN" if z > 0 else "UP"
            conf      = min(0.80, abs(z) / 3)
            use_t     = True
        else:
            regime    = "RANGING"
            direction = "NEUTRAL"
            conf      = 0.5
            use_t     = False

        # Hourly vol = per-bar EWMA vol × sqrt(bars/hour)
        vol_h = vol * math.sqrt(BARS_PER_HOUR)
        if vol_h > VOL_REGIME_HIGH_H:
            vol_regime = "HIGH"    # stressed / high realized vol
        elif vol_h < VOL_REGIME_LOW_H:
            vol_regime = "LOW"     # calm / compressed vol
        else:
            vol_regime = "NORMAL"

        # Vol compression: fast EWMA << slow EWMA means Kalshi's lagged model
        # still prices as if vol is elevated → RANGE contracts are cheap → structural edge
        vr       = feed.vol_ratio() if hasattr(feed, "vol_ratio") else 1.0
        vol_comp = vr < VOL_RATIO_COMPRESSION

        return {
            "regime":          regime,
            "direction":       direction,
            "conf":            round(conf, 3),
            "use_t":           use_t,
            "mom":             round(mom, 5),
            "accel":           round(accel, 5),
            "vol":             round(vol, 9),  # per-tick vol ~1e-4; 6dp lost up to 1%
            "vol_h":           round(vol_h, 5),
            "vol_regime":      vol_regime,
            "zscore":          round(z, 3),
            "consec":          f"{cn}{cd}",
            "vol_ratio":       round(vr, 3),
            "vol_compression": vol_comp,
        }
