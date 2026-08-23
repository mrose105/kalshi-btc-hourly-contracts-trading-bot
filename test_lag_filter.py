"""config.LAG_FILTER_MAX_ADVERSE — don't buy a quote Kalshi hasn't repriced.

Kalshi's contract prices trail the underlying: measured peak correlation at 20s
over 1.2M observations, absorbing only 30-40% of a Coinbase move. Buying right
after spot has moved TOWARD the band means paying a stale quote that is about
to fall — which is what the systematic entry drawdown was (100% of entries,
median MAE -13.6%).

The direction that hurts depends on which side is being faded, and getting that
backwards would make the filter reject exactly the good entries. That is what
most of these tests are about.
"""
import sys
sys.path.insert(0, ".")
from kalshi_btc_bot import config as C
from kalshi_btc_bot.signals import SignalEngine


class _Flat:
    """true_prob fixed, so only the gates under test can change the outcome."""
    @staticmethod
    def posterior_prob(*a, **k):
        return {"prior_prob": 0.16, "market_prob": 0.30,
                "true_prob": 0.16, "market_weight": 0.0}


class _Lag:
    def __init__(self, adv):
        self.adv = adv

    def __enter__(self):
        self._old = C.LAG_FILTER_MAX_ADVERSE
        C.LAG_FILTER_MAX_ADVERSE = self.adv
        return self

    def __exit__(self, *a):
        C.LAG_FILTER_MAX_ADVERSE = self._old


SPOT = 73000.0


def _ladder():
    """Two bands BELOW spot — what z<0 fades. Both clear every other gate."""
    def row(strike, bid, ask):
        return {"ticker": f"KXBTC-26AUG2016-B{strike}", "ask": ask, "bid": bid,
                "strike": float(strike), "low": float(strike) - 50,
                "high": float(strike) + 50, "hours": 0.20,
                "otm_dist": float(strike + 50 - SPOT),
                "type": "RANGE", "itm": False, "vol": 900}
    return [row(72750, 0.30, 0.33), row(72850, 0.28, 0.31)]


def _reg(z, dspot):
    return {"regime": "RANGING", "direction": "NEUTRAL", "zscore": z,
            "mom": 0.0, "vol": 0.0001, "dspot_lag": dspot}


def _find(z, dspot):
    return SignalEngine(_Flat()).find_boundary_no(
        SPOT, 0.001, _reg(z, dspot), _ladder(), {}, 500.0, 500.0)


def test_off_admits_everything():
    """PARITY: 0 must reproduce pre-filter behaviour."""
    with _Lag(0):
        for dspot in (-500.0, -25.0, 0.0, +25.0, +500.0):
            assert _find(-1.6, dspot) is not None, dspot


def test_rejects_an_adverse_move_into_a_band_below_spot():
    """z<0 fades bands BELOW spot, so a FALLING spot walks into them."""
    with _Lag(25.0):
        assert _find(-1.6, -100.0) is None, "spot fell $100 toward the band"
        assert _find(-1.6, -26.0) is None, "just over the bar"


def test_allows_a_move_away_from_a_band_below_spot():
    with _Lag(25.0):
        assert _find(-1.6, +100.0) is not None, "spot rose AWAY from the band"
        assert _find(-1.6, -24.0) is not None, "just under the bar"
        assert _find(-1.6, 0.0) is not None


def test_the_direction_flips_with_the_side_being_faded():
    """THE BUG THIS GUARDS. z>0 fades bands ABOVE spot, so a RISING spot is the
    adverse one. A filter that ignored side would reject the good entries."""
    lad = [{"ticker": "KXBTC-26AUG2016-B73250", "ask": 0.33, "bid": 0.30,
            "strike": 73250.0, "low": 73200.0, "high": 73300.0, "hours": 0.20,
            "otm_dist": -200.0, "type": "RANGE", "itm": False, "vol": 900}]
    eng = SignalEngine(_Flat())
    with _Lag(25.0):
        # band ABOVE spot, spot RISING into it -> reject
        assert eng.find_boundary_no(SPOT, 0.001, _reg(+1.6, +100.0), lad,
                                    {}, 500.0, 500.0) is None
        # band ABOVE spot, spot FALLING away -> allow
        assert eng.find_boundary_no(SPOT, 0.001, _reg(+1.6, -100.0), lad,
                                    {}, 500.0, 500.0) is not None


def test_missing_dspot_lag_does_not_block_trading():
    """A regime dict without the field (older recording, replay) must pass."""
    with _Lag(25.0):
        reg = _reg(-1.6, 0.0)
        del reg["dspot_lag"]
        assert SignalEngine(_Flat()).find_boundary_no(
            SPOT, 0.001, reg, _ladder(), {}, 500.0, 500.0) is not None


def test_tighter_is_never_looser():
    """Monotonicity: raising the bar cannot admit something a lower bar blocked."""
    for dspot in (-10.0, -30.0, -60.0, -200.0):
        admitted = []
        for adv in (100.0, 50.0, 25.0, 10.0):
            with _Lag(adv):
                admitted.append(_find(-1.6, dspot) is not None)
        # once blocked at a looser bar, must stay blocked at every tighter one
        for i in range(1, len(admitted)):
            assert not (admitted[i] and not admitted[i - 1]), (dspot, admitted)


def test_regime_engine_publishes_the_field():
    src = open("kalshi_btc_bot/regime.py").read()
    assert '"dspot_lag"' in src
    assert "_C.LAG_FILTER_SECS" in src, "lookback must follow config"


def test_config_is_read_module_qualified():
    src = open("kalshi_btc_bot/signals.py").read()
    assert "_C.LAG_FILTER_MAX_ADVERSE" in src
    assert "from .config import LAG_FILTER_MAX_ADVERSE" not in src


def test_unvalidated_filter_may_not_run_with_real_money():
    if C.LAG_FILTER_MAX_ADVERSE and C.LAG_FILTER_MAX_ADVERSE > 0:
        assert C.PAPER_TRADING is True, (
            f"LAG_FILTER_MAX_ADVERSE={C.LAG_FILTER_MAX_ADVERSE} is unvalidated "
            f"(n=27) — validate on paper first, or set it to 0")


def test_shipped_values():
    assert C.LAG_FILTER_SECS == 20, "must match the measured 20s peak"
    assert C.LAG_FILTER_MAX_ADVERSE == 25.0


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
