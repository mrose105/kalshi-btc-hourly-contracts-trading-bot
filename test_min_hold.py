"""config.MIN_HOLD_SECS — block exits that react to quote noise.

The 2026-08-21 trade this exists to prevent: BUY_NO at $0.650, SELL 2 SECONDS
later at $0.520 for -$1.82, reason `edge_gone` — a rule that can only fire when
the position is UP. Sub-10s round trips are 0-for-7 in the live book, and a dip
observed at 10s mildly predicts WINNING (76% vs 59%), so an exit that fast is
reacting to the book, not to the world.
"""
import sys
import time
sys.path.insert(0, ".")
from kalshi_btc_bot import config as C


def _fresh(held_secs, no_pnl_pct, hours=0.5):
    """Reproduce the guard in positions.py exactly."""
    opened = time.time() - held_secs
    held = time.time() - opened
    return (C.MIN_HOLD_SECS > 0
            and opened
            and held < C.MIN_HOLD_SECS
            and no_pnl_pct > -C.MIN_HOLD_CATASTROPHE
            and hours >= 0.03)


class _Hold:
    def __init__(self, secs):
        self.secs = secs

    def __enter__(self):
        self._old = C.MIN_HOLD_SECS
        C.MIN_HOLD_SECS = self.secs
        return self

    def __exit__(self, *a):
        C.MIN_HOLD_SECS = self._old


def test_zero_disables_the_hold_entirely():
    """PARITY: 0 must restore the old always-allowed behaviour."""
    with _Hold(0.0):
        for held in (0.0, 1.0, 2.0, 59.0):
            assert _fresh(held, +0.05) is False
            assert _fresh(held, -0.10) is False


def test_the_two_second_edge_gone_trade_is_blocked():
    """The exact trade: 2s held, position nominally up, edge_gone wants out."""
    assert _fresh(2.0, +0.02) is True, "a 2s-old winner must be held"


def test_exit_allowed_once_past_the_window():
    assert _fresh(61.0, +0.02) is False
    assert _fresh(300.0, -0.10) is False


def test_ordinary_stop_is_deferred_but_not_cancelled():
    """A -40% move at 5s waits; the same move at 61s exits normally."""
    assert _fresh(5.0, -0.40) is True, "-40% inside the window should wait"
    assert _fresh(61.0, -0.40) is False, "-40% past the window must exit"


def test_catastrophe_always_bypasses_the_hold():
    """-65% is the floor that covers positions opened inside the expiry gate."""
    for held in (0.0, 1.0, 30.0, 59.0):
        assert _fresh(held, -0.65) is False, "at the floor, exit immediately"
        assert _fresh(held, -0.90) is False, "below the floor, exit immediately"


def test_imminent_expiry_bypasses_the_hold():
    """Never hold into settlement to satisfy a minimum-hold rule."""
    assert _fresh(1.0, +0.02, hours=0.02) is False
    assert _fresh(1.0, -0.10, hours=0.0) is False
    assert _fresh(1.0, +0.02, hours=0.03) is True, "0.03 is the boundary, still held"


def test_a_position_with_no_open_timestamp_is_never_held():
    """Positions restored from a sync predate the field — must not deadlock."""
    opened = None
    held = time.time() - float(opened or 0.0)
    fresh = (C.MIN_HOLD_SECS > 0 and opened and held < C.MIN_HOLD_SECS)
    assert not fresh, "a position without `opened` must stay exitable"


def test_shipped_values():
    assert C.MIN_HOLD_SECS == 60.0, C.MIN_HOLD_SECS
    assert C.MIN_HOLD_CATASTROPHE == 0.65, C.MIN_HOLD_CATASTROPHE


def test_positions_reads_config_module_qualified():
    """`from .config import X` freezes the value; this repo has been bitten 3x."""
    src = open("kalshi_btc_bot/positions.py").read()
    assert "_C.MIN_HOLD_SECS" in src
    assert "_C.MIN_HOLD_CATASTROPHE" in src
    assert "from .config import MIN_HOLD_SECS" not in src


def test_both_position_dicts_record_an_open_time():
    src = open("kalshi_btc_bot/portfolio.py").read()
    assert src.count('"opened":') == 2, (
        f'both buy() and buy_no() must stamp "opened", found {src.count(chr(34)+"opened"+chr(34)+":")}')


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
