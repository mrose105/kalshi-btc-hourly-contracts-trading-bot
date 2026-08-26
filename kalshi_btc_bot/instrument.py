"""Instrument profiles — what the bot is trading, and the data facts that follow.

The strategies, portfolio, risk guards and exits are instrument-agnostic. What
is NOT agnostic is the data layer: which Kalshi series to scan, where spot comes
from, and what vol values are physically plausible. Every live-vs-backtest
divergence bug this bot has shipped (RANGE_WIDTH 250-vs-100, the momentum
window, the session breaker) was an instrument fact encoded as a constant in
code that assumed BTC. This module makes those facts explicit and selectable so
a second instrument cannot silently inherit BTC's calibration.

Select with the KALSHI_INSTRUMENT env var:

    KALSHI_INSTRUMENT=BTC   (default — byte-identical to pre-instrument behavior)
    KALSHI_INSTRUMENT=SPX

Adding an instrument means measuring its facts, not guessing them. Each
profile below carries its own derivation inline — what was measured, over what
window, and what is still a guess. Read those comments before trusting a
number; the SPX distance constants in particular are NOT measured, they are
BTC values scaled by a sigma ratio, and say so.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Instrument:
    name: str

    # Kalshi series to scan, in priority order. Multiple series are unioned into
    # one ladder; contracts.py derives geometry from each market's own
    # floor/cap strikes, so mixing a RANGE series with an ABOVE/BELOW series
    # needs no special handling downstream.
    series: tuple[str, ...]

    # Plausible bounds on HOURLY vol, in the units DistModel receives after
    # `vol * sqrt(BARS_PER_HOUR)`. These clamp data glitches; they must NOT clamp
    # normal pricing. Measured against the live tick EWMA estimator, not bar vol
    # — the two differ by microstructure bias and are not interchangeable.
    vol_h_floor: float
    vol_h_cap: float

    # Hourly-vol boundaries for the LOW/NORMAL/HIGH regime classification in
    # regime.py. Not cosmetic: DistModel.true_prob scales vol by 1.15 in HIGH
    # and 0.92 in LOW, so a misclassified instrument carries a standing bias on
    # every contract it prices.
    vol_regime_low_h: float
    vol_regime_high_h: float

    # True when the underlying only trades during the US equity session, which
    # means overnight/weekend gaps in the bar history and no continuous
    # operation. BTC is 24/7 and leaves this False.
    market_hours: bool

    # ---------------------------------------------------------------------
    # Price-DISTANCE constants, in the instrument's own units.
    # ---------------------------------------------------------------------
    # Every one of these was a bare dollar literal in config.py, calibrated to
    # BTC's ~$300 hourly sigma. Ported to SPX unchanged they would be nonsense:
    # SPX's hourly sigma is ~26 points, so "skip entries within $40 of a
    # boundary" would become "skip within 40 POINTS" — 1.6 sigma, wide enough
    # to reject essentially every candidate. That is the RANGE_WIDTH 250-vs-100
    # failure again, which is what this module exists to prevent.
    #
    # SPX values are the BTC values expressed in SIGMA and re-priced. Sigma is
    # the right basis because these gates all answer "how likely is one move to
    # bust this position", which is a sigma question, not a price question.
    # Measured 2026-08-22 on 60d of 5-minute bars:
    #     BTC  spot 77,097   hourly sigma 0.388%  =  $299.21
    #     SPX  spot  7,674   hourly sigma 0.334%  =   25.65 points
    #     scale = 25.65 / 299.21 = 0.0857
    #
    # PROVISIONAL. Derived by scaling, not measured on SPX order-book data —
    # the bot has never run on SPX and there are no KXINXU/KXINX recordings.
    # Re-derive from real fills before trusting any SPX result, exactly as the
    # vol cone above is still flagged provisional.
    min_range_boundary_buffer: float
    strike_cluster_dist: float
    max_otm_b: float
    max_otm_t: float
    no_dist_min: float
    no_dist_max: float
    boundary_no_otm_min: float
    boundary_no_otm_max: float
    time_exit_near_dist: float
    boundary_risk_dist: float

    # How to render a strike. BTC strikes are dollars; index strikes are points.
    def fmt_strike(self, v: float) -> str:
        return f"${v:,.0f}" if self.name == "BTC" else f"{v:,.0f}"


BTC = Instrument(
    name="BTC",
    series=("KXBTC",),
    # Unchanged from the original model.py module constants — see the vol-cone
    # note there for the 2026-07-06 tightening from 0.080 to 0.030.
    vol_h_floor=0.003,
    vol_h_cap=0.030,
    # Unchanged from config.VOL_REGIME_{LOW,HIGH}_H, which these replaced.
    vol_regime_low_h=0.005,
    vol_regime_high_h=0.015,
    market_hours=False,
    # Unchanged from the config.py literals these replaced — BTC behaviour is
    # byte-identical, which test_instrument_distances.py asserts.
    min_range_boundary_buffer=40,
    strike_cluster_dist=150,
    max_otm_b=150,
    max_otm_t=100,
    no_dist_min=-300,
    no_dist_max=100,
    boundary_no_otm_min=-250,
    boundary_no_otm_max=-10,
    time_exit_near_dist=15,
    boundary_risk_dist=15,
)

SPX = Instrument(
    name="SPX",
    # KXINXU — hourly "S&P 500 above/below", `greater_or_equal` digitals on a
    #          5-point strike ladder. This is the liquid one: 162,748 volume
    #          across 200 open markets on 2026-08-17, 1-2c near-money spreads.
    #          Supplies ABOVE (and NO-side BELOW exposure) candidates.
    # KXINX  — daily "S&P 500 range", settles 4pm ET. Supplies the only true
    #          RANGE contracts, so find_best/find_snipe's compression thesis has
    #          an instrument. Thin (3,357 volume, 11 of 60 strikes trade), which
    #          MIN_VOLUME filters naturally, and MIN_HOURS/MAX_HOURS confines it
    #          to the afternoon since everything expires at 16:00.
    series=("KXINXU", "KXINX"),
    # PROVISIONAL — pending live tick-EWMA calibration. Derived offline from
    # yfinance bars, not from the bot's own feed, so it is an estimate of the
    # right order rather than a matched measurement.
    # Derived from 3,005 contiguous 5-minute ^GSPC bars over 2026-06-22..08-17:
    # rolling 1h realized vol ran p1=0.000571 (5.3% ann), p50=0.001792 (16.8%),
    # p99=0.006079 (56.9%), max=0.008076 (75.6%). Floor sits below the observed
    # p1; cap leaves ~2x headroom over the worst hour for a crisis regime.
    #
    # Do NOT reuse BTC's cone here: 85% of those SPX observations fall BELOW
    # BTC's 0.003 floor, which is 1.67x SPX's median vol. Inheriting it would
    # clamp vol upward on nearly every true_prob call and fatten every tail.
    vol_h_floor=0.0004,
    vol_h_cap=0.015,
    # Split at the measured p25 and p95 of SPX's own rolling 1h realized vol,
    # giving a ~25/70/5 LOW/NORMAL/HIGH split. Inheriting BTC's 0.005/0.015
    # would classify 99% of SPX observations as LOW — every percentile through
    # p95 sits below BTC's LOW boundary — pinning the regime to a constant and
    # applying true_prob's 0.92 LOW haircut permanently.
    vol_regime_low_h=0.00125,
    vol_regime_high_h=0.004,
    market_hours=True,
    # BTC value x 0.0857 (the sigma ratio above), rounded to a half point where
    # that is finer than the quote tick. Sub-5-point values are meaningful:
    # otm_distance measures spot-to-strike and spot is continuous, so the
    # 5-point ladder limits which STRIKES exist, not what distance is
    # measurable.
    min_range_boundary_buffer=3.5,    # BTC 40
    strike_cluster_dist=13.0,         # BTC 150
    max_otm_b=13.0,                   # BTC 150
    max_otm_t=8.5,                    # BTC 100
    no_dist_min=-26.0,                # BTC -300
    no_dist_max=8.5,                  # BTC 100
    boundary_no_otm_min=-21.0,        # BTC -250
    boundary_no_otm_max=-0.9,         # BTC -10
    time_exit_near_dist=1.5,          # BTC 15
    boundary_risk_dist=1.5,           # BTC 15
)

_PROFILES = {"BTC": BTC, "SPX": SPX}


def active() -> Instrument:
    """The instrument this process is trading. Defaults to BTC so an existing
    deployment that sets no env var behaves exactly as before."""
    name = os.environ.get("KALSHI_INSTRUMENT", "BTC").strip().upper()
    try:
        return _PROFILES[name]
    except KeyError:
        raise SystemExit(
            f"KALSHI_INSTRUMENT={name!r} is not a known instrument. "
            f"Choose one of: {', '.join(sorted(_PROFILES))}"
        )


ACTIVE = active()


def make_feed():
    """Construct the price feed for the active instrument.

    Imported lazily so `import instrument` stays free of the feed module's
    requests/yfinance dependencies, and so selecting BTC never imports the SPX
    feed (or vice versa).
    """
    if ACTIVE.name == "SPX":
        from .spx_feed import SPXFeed
        return SPXFeed()
    from .feed import BTCFeed
    return BTCFeed()
