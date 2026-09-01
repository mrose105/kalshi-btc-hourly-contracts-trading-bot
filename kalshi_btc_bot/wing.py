"""Pair each BOUNDARY_NO fill with a YES leg one strike toward spot.

DISABLED 2026-08-28. WING_ENABLED defaults False and should stay there until
this is re-measured. Every figure below was produced by resolving contracts
from the last `universe` observation, which is ~T-5min rather than settlement
(see test_expiring_window.py). Re-run against the quotes stream before
believing any of it: the companion ATM study went from 93% win / +99.8% ROC to
40% / -26.7% under the same correction, and the "wing toward spot" leg has not
been re-measured at all.

The code is kept because the structure is worth re-testing once the recorder
has captured real final-minute data. It is not kept because the numbers hold.


MEASURED 2026-08-27 on 99 settlement-resolved signals across 71 expiries. The
NO leg and a YES leg on the adjacent band, attributed separately:

    NO leg alone                85% win   +$43.72   $12.29   +3.6%   P>0 80%
    YES wing TOWARD spot        52% win  +$136.55    $6.35  +21.7%   P>0 100%
    YES wing AWAY from spot      3% win   -$61.14    $1.07  -57.6%   P>0 0%
    NO + toward wing            52% win  +$180.27   $18.63   +9.8%   P>0 97%

Only the TOWARD-spot leg is bought. The away leg won three times in ninety-nine
and gave back 58% of what was spent on it — it is the blackjack-insurance side
of the ladder, and buying it is taking the opposite side of the premium this
strategy exists to sell.

WHY THE NEAR BAND. The calibration curve over 16,796 band-observations at these
same moments:

    distance from spot   implied   realized   YES edge   NO edge
    $0-100                 0.326      0.416     +0.078     -0.102
    $100-200               0.127      0.077     -0.064     +0.038
    $200-300               0.038      0.010     -0.040     +0.016
    $300+                  0.018      0.000     -0.031     +0.005

At a z-extreme in a mean-reverting regime the market UNDERPRICES the band spot
is already in — 32.6% implied against 41.6% realized. The NO strategy harvests
the +3.8c overpricing at $100-200; this leg harvests the +7.8c underpricing one
band closer, on the other side of the book. Same conditioning, bigger gap,
opposite direction.

Note the far tails are overpriced too and are NOT harvestable: past $300 the
BID is 0.000, so buying NO there costs 1 - 0 = $1.00 to win $1.00. The market
will sell you the lottery ticket and will not pay you to take the other side.

NOT AN ESTABLISHED EDGE. n=99, one conditioning, resolved at settlement while
the live bot exits early. The near-band trade has never run with entry rules of
its own — it has only ever been measured as a passenger on BOUNDARY_NO.
"""
from . import config as _C


def toward_spot(ladder: list, no_contract: dict, spot: float,
                regime: dict | None = None) -> dict | None:
    """The band spot is CURRENTLY INSIDE, or None.

    Selected by where spot actually is — `low <= spot < high` — not by counting
    strikes from the NO leg.

    WHY THIS CHANGED, 2026-09-01. The rule used to step exactly one strike from
    the NO band toward spot. BOUNDARY_NO_DIST gates the NO leg to roughly
    $100-200 OTM, so one strike lands ~$0-100 out — which is the occupied band
    only when spot happens to sit in it, and its NEIGHBOUR otherwise. Which one
    you got was geometry, not intent, and the two do not measure the same:

        OCCUPIED band (spot inside)     n=250  138 expiries  mean +0.0036
                                               95% CI [-0.0641, +0.0718]
        ADJACENT band (1 strike away)   n=703  188 expiries  mean -0.0331
                                               95% CI [-0.0646, +0.0004]

    Net of per-observation fees, settlement resolved from the quotes stream
    (wing_calibration.py). 3.7c apart and on opposite sides of zero, and the
    ADJACENT band was the common case — 703 observations against 250, because
    the neighbour is nearly always on the ladder while the occupied band often
    is not.

    Returning None when the occupied band is absent is the point of the change,
    not a limitation: the old fallback to a neighbour is precisely the negative
    population. The wing is optional and its absence must never block the NO
    leg (app.py wraps this call), so declining is always safe.

    THIS IS NOT AN EDGE CLAIM. +0.0036 with an interval spanning zero is the
    least-bad cell, not a proven one, and the $0-100 aggregate that contains it
    is significantly NEGATIVE at [-0.0526, -0.0006]. The justification is
    "stop systematically buying the worse population", not "capture the better
    one". See docs/CONFIG_RATIONALE.md#wing_enabled.
    """
    if not getattr(_C, "WING_ENABLED", False):
        return None
    try:
        if spot is None:
            return None

        # TRENDING: buy the LANDING ZONE, not the band spot is leaving.
        #
        # Fading is the reverting/ranging trade. In a trend the band spot sits
        # in is the one it is about to vacate, so buying it is betting against
        # the regime the engine just identified. One strike FORWARD in the
        # direction of travel is where spot is heading — the landing zone.
        #
        # DORMANT AS WRITTEN, and deliberately so. find_boundary_no() returns
        # early unless the regime is RANGING or REVERTING (signals.py), and the
        # wing only exists as a companion to a BOUNDARY_NO fill, so this branch
        # cannot currently be reached. Reaching it means letting the wing fire
        # independently of the NO leg, which is a strategy change with no
        # measurement behind it and needs its own decision. The code is here so
        # that decision is a one-line gate rather than a rewrite.
        _r = (regime or {}).get("regime", "")
        _d = (regime or {}).get("direction", "")
        if _r == "TRENDING" and _d in ("UP", "DN"):
            occupied = next((c for c in ladder
                             if c.get("low") is not None and c.get("high") is not None
                             and float(c["low"]) <= float(spot) < float(c["high"])), None)
            if occupied is None:
                return None
            rows = sorted((c for c in ladder if c.get("low") is not None),
                          key=lambda c: float(c["low"]))
            idx = next((i for i, c in enumerate(rows)
                        if c.get("ticker") == occupied.get("ticker")), None)
            if idx is None:
                return None
            j = idx + (1 if _d == "UP" else -1)
            if not (0 <= j < len(rows)):
                return None
            landing = rows[j]
            if landing.get("ticker") == no_contract.get("ticker"):
                return None
            ask = landing.get("ask") or 0
            if ask <= 0 or ask > _C.MAX_ASK:
                return None
            return landing

        for c in ladder:
            lo, hi = c.get("low"), c.get("high")
            if lo is None or hi is None:
                continue
            if not (float(lo) <= float(spot) < float(hi)):
                continue
            # Never buy the NO leg's own band back, and never pay more than the
            # YES entry ceiling.
            if c.get("ticker") == no_contract.get("ticker"):
                return None
            ask = c.get("ask") or 0
            if ask <= 0 or ask > _C.MAX_ASK:
                return None
            return c
        return None
    except Exception:
        return None


def size_for(no_count: int) -> int:
    """Contracts on the wing, as a multiple of the NO leg.

    1.0x is what was measured. The wing costs about half the NO leg per
    contract ($6.35 against $12.29 at 15), so 1.0x is roughly a 1:2 capital
    split rather than 1:1.
    """
    ratio = getattr(_C, "WING_SIZE_RATIO", 1.0)
    return max(1, int(round(no_count * ratio)))
