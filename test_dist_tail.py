"""config.DIST_TAIL_DF — Student-t tail shape in DistModel.true_prob.

The parity test is the important one: DIST_TAIL_DF = None must reproduce the
Gaussian bit-for-bit, so the flag can always be switched back to prove a
behaviour change came from somewhere else.
"""
import math
import sys
sys.path.insert(0, ".")
from scipy.stats import norm
from kalshi_btc_bot import config as C
from kalshi_btc_bot.model import DistModel


class _DF:
    """Set DIST_TAIL_DF for a block and restore it."""
    def __init__(self, df):
        self.df = df

    def __enter__(self):
        self._old = C.DIST_TAIL_DF
        C.DIST_TAIL_DF = self.df
        return self

    def __exit__(self, *a):
        C.DIST_TAIL_DF = self._old


M = DistModel()
RANGE = {"type": "RANGE", "low": 64600.0, "high": 64700.0}
FAR   = {"type": "RANGE", "low": 68000.0, "high": 68100.0}
REG   = {"regime": "RANGING", "direction": "NEUTRAL", "mom": 0.0, "zscore": 0.0}
SPOT, VOL, HRS = 64650.0, 0.00012, 0.5


def test_none_reproduces_the_gaussian_exactly():
    """PARITY. None must be byte-identical to hand-rolled norm.cdf."""
    with _DF(None):
        got = M.true_prob(RANGE, SPOT, VOL, HRS, REG)
        scale, df = M._tail_scale(0.004)
        assert df is None and scale == 0.004, (scale, df)
        assert M._cdf(0.3, None) == float(norm.cdf(0.3))
        assert M._sf(0.3, None) == float(norm.sf(0.3))
    assert 0.0 <= got <= 1.0


def test_df_at_or_below_two_falls_back_to_gaussian():
    """t-variance is df/(df-2) — undefined at df<=2. Must not divide by zero."""
    for bad in (2.0, 1.5, 0.0, -3.0):
        with _DF(bad):
            scale, df = M._tail_scale(0.004)
            assert df is None, f"df={bad} must fall back, got {df}"
            assert scale == 0.004
            assert 0.0 <= M.true_prob(RANGE, SPOT, VOL, HRS, REG) <= 1.0


def test_variance_is_preserved_by_the_scale_correction():
    """Switching to t must fatten the tails WITHOUT widening the forecast.

    Without dividing by sqrt(df/(df-2)) the distribution would also get wider,
    and fattening could not be told apart from a vol increase.
    """
    vt = 0.01
    with _DF(3.0):
        scale, df = M._tail_scale(vt)
        assert df == 3.0
        implied_var = (scale ** 2) * (df / (df - 2.0))
        assert abs(implied_var - vt ** 2) < 1e-18, (implied_var, vt ** 2)


def test_high_df_converges_back_to_the_gaussian():
    """Student-t -> normal as df -> infinity. A sanity check on the wiring."""
    with _DF(None):
        gauss = M.true_prob(RANGE, SPOT, VOL, HRS, REG)
    with _DF(500.0):
        heavy = M.true_prob(RANGE, SPOT, VOL, HRS, REG)
    assert abs(gauss - heavy) < 0.01, (gauss, heavy)


def test_fat_tails_raise_far_otm_probability():
    """The measured defect: P(YES) understated 3.9% vs 14.5% actual far-OTM."""
    with _DF(None):
        gauss = M.true_prob(FAR, SPOT, VOL, HRS, REG)
    with _DF(3.0):
        student = M.true_prob(FAR, SPOT, VOL, HRS, REG)
    assert student > gauss, (
        f"t must assign MORE probability to a distant band: "
        f"normal {gauss:.6f} vs t {student:.6f}")


def test_fat_tails_also_raise_at_the_money_probability():
    """The other half: P(YES) understated 66.6% vs 90.3% actual near-money.

    This is the whole reason a vol change could not fix the model — the peak
    and the tail were BOTH under-predicted, and sigma trades one for the other.
    """
    with _DF(None):
        gauss = M.true_prob(RANGE, SPOT, VOL, HRS, REG)
    with _DF(3.0):
        student = M.true_prob(RANGE, SPOT, VOL, HRS, REG)
    assert student > gauss, (
        f"t must also assign MORE probability to the band spot sits in: "
        f"normal {gauss:.6f} vs t {student:.6f}")


def test_probabilities_stay_in_range_across_the_ladder():
    with _DF(3.0):
        for strike in range(60000, 70000, 250):
            c = {"type": "RANGE", "low": float(strike), "high": float(strike + 100)}
            for h in (0.08, 0.5, 2.0, 4.0):
                p = M.true_prob(c, SPOT, VOL, h, REG)
                assert 0.0 <= p <= 1.0, (strike, h, p)


def test_above_and_below_contracts_are_wired_too():
    for c in ({"type": "ABOVE", "low": 65000.0, "high": 65000.0},
              {"type": "BELOW", "low": 64000.0, "high": 64000.0}):
        with _DF(None):
            g = M.true_prob(c, SPOT, VOL, HRS, REG)
        with _DF(3.0):
            s = M.true_prob(c, SPOT, VOL, HRS, REG)
        assert 0.0 <= g <= 1.0 and 0.0 <= s <= 1.0
        assert abs(s - g) > 1e-9, f"{c['type']} ignored DIST_TAIL_DF"


def test_shipped_value_is_the_validated_one():
    """3.0, chosen for stability over TUNE's grid-edge argmin of 2.5.

    Every df in [2.5, 20] beat the Gaussian on BOTH windows, so the choice
    within the range is not critical; 2.5 sat on the grid boundary next to the
    df->2 variance singularity.
    """
    assert C.DIST_TAIL_DF == 3.0, C.DIST_TAIL_DF


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
