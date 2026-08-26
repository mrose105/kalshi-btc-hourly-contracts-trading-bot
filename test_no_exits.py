"""The NO exit ladder: edge_gone must need a real gain, and the stop is 30%.

Both changes come from one measurement (2026-08-25, 41 settlement-resolved
armings over 33 expiries, fees both sides):

    edge_gone fired on 27 of 41, worth +$7.64 where holding was +$19.21
    -> the tier cost $11.57, only ~$1 of it fee, the rest forfeited convergence

    whole ladder:  edge_gone ON  + 40% stop  ->  -3.8% ROC, PF 0.59
                   edge_gone OFF + 30% stop  ->  +3.7% ROC, PF 1.44

The stop is the half that was already working (+$6.19 vs holding). Only
edge_gone leaked. These tests pin that asymmetry so a future "simplify the
exits" pass cannot quietly delete the wrong one.
"""
import re
import sys
sys.path.insert(0, ".")

from kalshi_btc_bot import config as C

SRC = open("kalshi_btc_bot/positions.py").read()


def _edge_gone_block():
    i = SRC.index("reason=\"edge_gone")
    return SRC[max(0, i - 1400):i]


def test_edge_gone_is_gated_on_a_minimum_gain():
    """THE BUG. `no_pnl_pct > 0` is near-vacuous on a position decaying to $1."""
    blk = _edge_gone_block()
    assert "NO_EDGE_GONE_MIN_GAIN" in blk, (
        "edge_gone still fires on any positive pnl — it cost $11.57 over 27 "
        "exits by selling winners before they converged")
    assert "no_pnl_pct > 0 and not fresh" not in SRC, "old ungated form still present"


def test_the_gain_bar_is_read_at_call_time():
    """Frozen imports are this repo's recurring bug class."""
    blk = _edge_gone_block()
    assert '_C, "NO_EDGE_GONE_MIN_GAIN"' in blk or "_C.NO_EDGE_GONE_MIN_GAIN" in blk
    assert "from .config import NO_EDGE_GONE_MIN_GAIN" not in SRC


def test_zero_restores_the_old_behaviour():
    """PARITY. The escape hatch must be a true no-op."""
    blk = _edge_gone_block()
    m = re.search(r'getattr\(_C,\s*"NO_EDGE_GONE_MIN_GAIN",\s*([\d.]+)\)', blk)
    assert m, "must default to a literal when the setting is absent"
    assert float(m.group(1)) == 0.0, (
        "the fallback must be 0.0 so an older config behaves exactly as before")


def test_shipped_values():
    assert C.NO_EDGE_GONE_MIN_GAIN == 0.15
    assert C.NO_STOP == 0.30, "the 40% stop realised 1.7x the premium per loss"


def test_the_stop_is_not_looser_than_the_premium_it_protects():
    """The economic constraint, independent of any sweep.

    Entry ~$0.81 wins ~$0.19. A stop at X of cost realises 0.81X. Above ~0.35
    a single stop-out erases more than 1.5 winners, and the measured ROC decays
    monotonically from 30% upward (+3.7 -> -0.7 -> -2.2 -> -4.6 -> -5.4).
    """
    entry, premium = 0.81, 0.19
    loss = entry * C.NO_STOP
    assert loss / premium < 1.5, (
        f"stop {C.NO_STOP:.0%} realises ${loss:.2f}, {loss/premium:.1f}x the "
        f"${premium:.2f} premium — one loss erases that many winners")


def test_the_stop_still_exists():
    """The stop was the half that WORKED (+$6.19 vs holding). Never delete it."""
    assert C.NO_STOP is not None and C.NO_STOP > 0
    assert "no_pnl_pct <= -_C.NO_STOP" in SRC, (
        "the stop must read config module-qualified — a bare NO_STOP is a\n         frozen import and a sweep against it silently no-ops")


def test_catastrophe_floor_still_bypasses_everything():
    assert "no_pnl_pct <= -_C.MIN_HOLD_CATASTROPHE" in SRC
    assert C.MIN_HOLD_CATASTROPHE > C.NO_STOP, (
        "the catastrophe floor must sit BELOW the ordinary stop or it can "
        "never fire")


def test_profit_tiers_are_untouched():
    """Only edge_gone was implicated. The others were not measured as leaks."""
    assert C.NO_PROFIT_CAPTURE == 0.80
    assert C.NO_TIME_PROFIT == 0.40
    assert "misprice_captured" in SRC and "misprice_time" in SRC


def test_unvalidated_exit_change_may_not_run_with_real_money():
    """n=41, 95% CI [-5.9%, +12.4%] includes zero, ~24 configs swept that day."""
    if C.NO_STOP != 0.40 or C.NO_EDGE_GONE_MIN_GAIN > 0:
        assert C.PAPER_TRADING is True, (
            "the 2026-08-26 exit changes are measured on 41 armings with a CI "
            "spanning zero — validate on paper first, or revert to "
            "NO_STOP=0.40 / NO_EDGE_GONE_MIN_GAIN=0.0")


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
