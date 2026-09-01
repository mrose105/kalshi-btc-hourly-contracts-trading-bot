"""Every gate the live strategy depends on must be able to fail a test.

Found 2026-08-27 by mutation scan (tools/mutate_config.py): perturb a config
constant, re-run the suite, see whether anything notices. Eleven of twenty-nine
live gates could be changed to anything at all and all 167 tests still passed.

The worst was BOUNDARY_NO_YES_ASK_MAX, shipped ~an hour earlier in that same
session — rationale documented, three broken fixtures repaired, committed — and
with no test on the gate itself. Change 0.30 to 0.64 and nothing complained.

This is the same failure as the inert gates and the frozen imports, one level
up: a parameter that cannot affect anything, and nothing noticed. The gates
here are the ones the mutation scan proved were unguarded.
"""
import sys
sys.path.insert(0, ".")

from kalshi_btc_bot import config as C
from kalshi_btc_bot.signals import SignalEngine


class _Flat:
    """Fixed probability, so only the gate under test can change the outcome."""
    def __init__(self, tp=0.05):
        self.tp = tp

    def posterior_prob(self, *a, **k):
        return {"prior_prob": self.tp, "market_prob": 0.30,
                "true_prob": self.tp, "market_weight": 0.0}


SPOT = 73000.0


def _reg(z=-1.60):
    return {"regime": "RANGING", "direction": "NEUTRAL", "zscore": z,
            "mom": 0.0, "vol": 0.0001, "dspot_lag": 0.0}


def _row(ask=None, bid=None, hours=0.20, strike=72750, vol=900):
    """A candidate that clears every gate EXCEPT the one a test varies.

    Derived from the shipped values, never hardcoded — three separate fixture
    sets went vacuous today by pinning literals that a threshold move stepped
    over.
    """
    if ask is None:
        ask = round(min(0.30, C.BOUNDARY_NO_YES_ASK_MAX) - 0.01, 4)
    if bid is None:
        bid = round(ask - 0.02, 4)
    return {"ticker": f"KXBTC-26AUG2016-B{strike}", "ask": ask, "bid": bid,
            "strike": float(strike), "low": float(strike) - 50,
            "high": float(strike) + 50, "hours": hours,
            "otm_dist": float(strike + 50 - SPOT), "type": "RANGE",
            "itm": False, "vol": vol}


def _find(rows, z=-1.60):
    return SignalEngine(_Flat()).find_boundary_no(
        SPOT, 0.001, _reg(z), rows, {}, 500.0, 500.0)


def test_the_fixture_actually_produces_a_signal():
    """Guard the guard. If this fails every test below is vacuous."""
    assert _find([_row()]) is not None, (
        "baseline candidate no longer clears the gates — every test in this "
        "file is now testing nothing")


# --- BOUNDARY_NO_YES_ASK_MAX -------------------------------------------------
# Shipped 2026-08-27 at 0.30, from 0.65 which never bound. The cheap-NO end
# (high yes_bid = band near spot) ran 43% wins against a 66% break-even.

def test_ask_ceiling_rejects_above_the_bar():
    over = round(C.BOUNDARY_NO_YES_ASK_MAX + 0.05, 4)
    assert _find([_row(ask=over, bid=round(over - 0.02, 4))]) is None, (
        f"an ask of {over} cleared a ceiling of {C.BOUNDARY_NO_YES_ASK_MAX}")


def test_ask_ceiling_admits_at_the_bar():
    at = C.BOUNDARY_NO_YES_ASK_MAX
    assert _find([_row(ask=at, bid=round(at - 0.02, 4))]) is not None, (
        "a candidate exactly at the ceiling must still trade")


def test_ask_floor_rejects_below_the_bar():
    under = round(C.BOUNDARY_NO_YES_ASK_MIN - 0.02, 4)
    assert _find([_row(ask=under, bid=round(under - 0.01, 4))]) is None


def test_the_ceiling_is_below_a_coin_flip():
    """no_cost = 1 - yes_bid, so the ceiling sets how close to 50/50 we go.

    At ask 0.65 the NO could cost ~$0.35 — a contract needing only 35% to break
    even, on a band sitting near spot. Measured -36% ROC.
    """
    assert C.BOUNDARY_NO_YES_ASK_MAX <= 0.40, (
        f"ask ceiling {C.BOUNDARY_NO_YES_ASK_MAX} admits NO entries below "
        f"${1 - C.BOUNDARY_NO_YES_ASK_MAX:.2f}, which measured -36% ROC")


# --- BOUNDARY_NO_HOURS_MAX ---------------------------------------------------
# The single biggest cut in the funnel: -84% of rows.

def test_window_rejects_a_contract_with_too_long_to_run():
    late = C.BOUNDARY_NO_HOURS_MAX + 0.10
    assert _find([_row(hours=late)]) is None, (
        f"{late:.2f}h cleared a {C.BOUNDARY_NO_HOURS_MAX}h window")


def test_window_rejects_a_contract_too_close_to_expiry():
    early = max(0.0, C.BOUNDARY_NO_HOURS_MIN - 0.02)
    assert _find([_row(hours=early)]) is None


def test_window_admits_inside_it():
    mid = (C.BOUNDARY_NO_HOURS_MIN + C.BOUNDARY_NO_HOURS_MAX) / 2
    assert _find([_row(hours=mid)]) is not None


def test_the_window_fits_inside_one_hourly_contract():
    """A VALUE bound, not a mechanism check.

    The three tests above derive their fixtures from the constant, so they
    follow it anywhere and can never fail on a config change — which is the
    right shape for surviving a tuning pass, and the wrong shape for catching
    a value drifting somewhere absurd. A mutation scan sees straight through
    them. This is the assertion that actually pins the range.

    KXBTC contracts are hourly, so a window beyond 1.0h reaches back before
    the contract was listed and the gate silently stops binding at the top.
    """
    assert C.BOUNDARY_NO_HOURS_MIN < C.BOUNDARY_NO_HOURS_MAX <= 1.0, (
        f"window {C.BOUNDARY_NO_HOURS_MIN}-{C.BOUNDARY_NO_HOURS_MAX}h is not "
        f"inside the life of an hourly contract")


# --- NO_TRUE_PROB_MAX --------------------------------------------------------

def test_a_contract_the_model_thinks_is_likely_is_refused():
    """Fading a band the model gives real probability to is not premium selling."""
    hot = SignalEngine(_Flat(tp=min(0.99, C.NO_TRUE_PROB_MAX + 0.10)))
    assert hot.find_boundary_no(SPOT, 0.001, _reg(), [_row()], {},
                                500.0, 500.0) is None


def test_we_never_fade_something_the_model_calls_a_coin_flip():
    """The VALUE bound behind the test above.

    This is a tail seller. Buying NO on a band the model gives better than even
    odds to is not selling a tail, it is taking the wrong side of a coin flip
    and calling it premium. The measured cost of drifting that way is in
    CONFIG_RATIONALE#boundary_no_yes_ask_max: the near-money buckets ran 43%
    wins against a 66% break-even.
    """
    assert 0.0 < C.NO_TRUE_PROB_MAX <= 0.55, (
        f"NO_TRUE_PROB_MAX={C.NO_TRUE_PROB_MAX} lets the bot fade bands it "
        f"believes are more likely than not to be reached")


# --- NO_EDGE_GONE_RATIO ------------------------------------------------------
# The exit threshold. Its companion NO_EDGE_GONE_MIN_GAIN got tests when it
# shipped; this one never did.

def test_edge_gone_ratio_is_read_by_the_exit_path():
    src = open("kalshi_btc_bot/positions.py").read()
    assert "NO_EDGE_GONE_RATIO" in src
    # Module-qualified ONLY. `from .config import NO_EDGE_GONE_RATIO` freezes
    # the value at import, so a sweep setting config.NO_EDGE_GONE_RATIO silently
    # no-ops — test_frozen_config.py caught exactly that here on 2026-09-01,
    # after no_exit_replay.py's --edge-gone-ratio flag was found to do nothing.
    assert "overprice_r < _C.NO_EDGE_GONE_RATIO" in src, (
        "the edge_gone tier no longer compares against the configured ratio, "
        "or reads it through a frozen import")


def test_edge_gone_ratio_sits_above_parity():
    """Below 1.0 the market price is UNDER the model's — that is not 'edge
    gone', that is the trade getting better. Firing there sells winners."""
    assert C.NO_EDGE_GONE_RATIO > 1.0, (
        f"NO_EDGE_GONE_RATIO={C.NO_EDGE_GONE_RATIO} fires while the contract "
        f"is still mispriced in our favour")


def test_edge_gone_ratio_is_below_the_entry_bar():
    """Entry needs ratio >= OVERPRICING_MIN; the exit must sit below it or a
    position would qualify to exit the instant it was opened."""
    assert C.NO_EDGE_GONE_RATIO < C.BOUNDARY_NO_OVERPRICING_MIN, (
        f"exit ratio {C.NO_EDGE_GONE_RATIO} >= entry ratio "
        f"{C.BOUNDARY_NO_OVERPRICING_MIN} — every fill exits immediately")


# --- ladder pre-filters ------------------------------------------------------

def test_ladder_filters_are_read_from_config():
    src = open("kalshi_btc_bot/ladder.py").read()
    for name in ("_C.MAX_ASK", "_C.MIN_VOLUME", "_C.MAX_SPREAD_PCT"):
        assert name in src, f"ladder no longer honours {name}"


def test_max_ask_cannot_be_looser_than_the_no_ceiling():
    """The ladder is the pre-filter. If MAX_ASK is tighter than the NO ask
    ceiling, the NO gate is unreachable above it and silently inert."""
    assert C.MAX_ASK >= C.BOUNDARY_NO_YES_ASK_MAX, (
        f"MAX_ASK={C.MAX_ASK} cuts below BOUNDARY_NO_YES_ASK_MAX="
        f"{C.BOUNDARY_NO_YES_ASK_MAX}, making part of the NO gate dead")


def test_min_volume_is_positive():
    assert C.MIN_VOLUME > 0, "a zero volume floor admits untradeable contracts"


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
