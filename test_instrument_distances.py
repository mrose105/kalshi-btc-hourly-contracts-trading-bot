"""Price-distance constants must come from the instrument, not from literals.

Every live-vs-backtest divergence this bot has shipped was an instrument fact
encoded as a constant in code that assumed BTC (RANGE_WIDTH 250-vs-100, the
60s-vs-2.5h momentum window, _roll_session). Ten distance constants were still
bare BTC dollar values in config.py. Ported to SPX unchanged they would be off
by ~12x: BTC's hourly sigma is ~$299, SPX's is ~26 points.

The first test is the one that matters — BTC must be byte-identical, so this
refactor cannot have changed live behaviour.
"""
import sys
sys.path.insert(0, ".")

_FIELDS = {
    "MIN_RANGE_BOUNDARY_BUFFER": "min_range_boundary_buffer",
    "STRIKE_CLUSTER_DIST":       "strike_cluster_dist",
    "MAX_OTM_B":                 "max_otm_b",
    "MAX_OTM_T":                 "max_otm_t",
    "NO_DIST_MIN":               "no_dist_min",
    "NO_DIST_MAX":               "no_dist_max",
    "BOUNDARY_NO_OTM_MIN":       "boundary_no_otm_min",
    "BOUNDARY_NO_OTM_MAX":       "boundary_no_otm_max",
    "TIME_EXIT_NEAR_DIST":       "time_exit_near_dist",
    "BOUNDARY_RISK_DIST":        "boundary_risk_dist",
}

# The literal values that were in config.py before the refactor.
_BTC_BEFORE = {
    "MIN_RANGE_BOUNDARY_BUFFER": 40,  "STRIKE_CLUSTER_DIST": 150,
    "MAX_OTM_B": 150,                 "MAX_OTM_T": 100,
    "NO_DIST_MIN": -300,              "NO_DIST_MAX": 100,
    "BOUNDARY_NO_OTM_MIN": -250,      "BOUNDARY_NO_OTM_MAX": -10,
    "TIME_EXIT_NEAR_DIST": 15,        "BOUNDARY_RISK_DIST": 15,
}


def test_btc_is_byte_identical_to_the_old_literals():
    """THE PARITY TEST. The refactor must not have moved a single BTC value."""
    from kalshi_btc_bot import config as C
    from kalshi_btc_bot.instrument import ACTIVE
    assert ACTIVE.name == "BTC", "default instrument must still be BTC"
    for const, want in _BTC_BEFORE.items():
        assert getattr(C, const) == want, f"{const}: {getattr(C, const)} != {want}"


def test_every_distance_constant_is_sourced_from_the_instrument():
    """No literal may survive in config.py — that is how BTC leaked before."""
    src = open("kalshi_btc_bot/config.py").read()
    for const, field in _FIELDS.items():
        assert f"{const}" in src
        line = [l for l in src.splitlines() if l.startswith(f"{const} ")
                or l.startswith(f"{const}=")]
        assert line, const
        assert f"_INST.{field}" in line[0], (
            f"{const} is still a literal: {line[0]!r}")


def test_spx_defines_all_of_them():
    from kalshi_btc_bot.instrument import BTC, SPX
    for field in _FIELDS.values():
        assert hasattr(SPX, field), f"SPX missing {field}"
        assert hasattr(BTC, field), f"BTC missing {field}"


def test_spx_values_are_not_btc_values():
    """The whole point. A ported literal is the bug this prevents."""
    from kalshi_btc_bot.instrument import BTC, SPX
    for field in _FIELDS.values():
        assert getattr(SPX, field) != getattr(BTC, field), (
            f"{field} is identical on both instruments — almost certainly a "
            f"BTC literal that was never re-derived")


def test_spx_distances_are_scaled_to_spx_sigma():
    """SPX ~= BTC * 0.0857, the ratio of their hourly price sigmas.

    Measured 2026-08-22 on 60d of 5-minute bars: BTC $299.21/hour vs SPX 25.65
    points/hour. Allow generous tolerance for the deliberate rounding.
    """
    from kalshi_btc_bot.instrument import BTC, SPX
    k = 25.65 / 299.21
    for field in _FIELDS.values():
        btc, spx = getattr(BTC, field), getattr(SPX, field)
        expected = btc * k
        # rounding to a half point is a large relative move on small values
        tol = max(0.55, abs(expected) * 0.15)
        assert abs(spx - expected) <= tol, (
            f"{field}: SPX {spx} is not ~{expected:.2f} "
            f"(BTC {btc} x {k:.4f})")


def test_signs_are_preserved():
    """A sign flip would invert a gate — OTM_MIN/MAX and NO_DIST_MIN are < 0."""
    from kalshi_btc_bot.instrument import BTC, SPX
    for field in _FIELDS.values():
        b, s = getattr(BTC, field), getattr(SPX, field)
        assert (b < 0) == (s < 0), f"{field}: sign differs ({b} vs {s})"


def test_otm_bounds_stay_ordered():
    """MIN must be further OTM than MAX, or the window is empty."""
    from kalshi_btc_bot.instrument import BTC, SPX
    for inst in (BTC, SPX):
        assert inst.boundary_no_otm_min < inst.boundary_no_otm_max, inst.name
        assert inst.no_dist_min < inst.no_dist_max, inst.name


def test_spx_profile_still_carries_its_own_vol_cone():
    """Guards against a future edit collapsing SPX back onto BTC's cone."""
    from kalshi_btc_bot.instrument import BTC, SPX
    assert SPX.vol_h_floor < BTC.vol_h_floor
    assert SPX.vol_regime_low_h < BTC.vol_regime_low_h
    assert SPX.market_hours is True and BTC.market_hours is False


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t(); print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1; print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests)-failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
