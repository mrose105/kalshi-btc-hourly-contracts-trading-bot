"""config.CONFIRM_EXIT_DEPTH — price profit exits at depth, not top-of-book.

The 2026-08-21 defect: `edge_gone` fires only when a position is UP, yet booked
-$1.82. The decision used `1 - yes_ask` (top of book, no size); sell() walked
the ladder for all 14 lots and cleared 13c lower.

Stops must NOT be gated by this check — a worse executable price makes a stop
more valid, not less.
"""
import sys
sys.path.insert(0, ".")
from kalshi_btc_bot import config as C
from kalshi_btc_bot.positions import PositionManager
from kalshi_btc_bot.portfolio import Portfolio


class _Book:
    """Portfolio stub exposing only what _confirm_profit touches."""
    def __init__(self, levels):
        self.levels = levels

    def executable_exit(self, ticker, count, is_no):
        return Portfolio._walk_book(self.levels, max(1, int(count)))


def _mgr(levels):
    m = PositionManager.__new__(PositionManager)
    m.portfolio = _Book(levels)
    return m


class _Flag:
    def __init__(self, on):
        self.on = on

    def __enter__(self):
        self._old = C.CONFIRM_EXIT_DEPTH
        C.CONFIRM_EXIT_DEPTH = self.on
        return self

    def __exit__(self, *a):
        C.CONFIRM_EXIT_DEPTH = self._old


# 14 lots wanted. Only 3 rest at 83c; the rest sit far lower — the exact
# shape that turned a "profit" into a loss.
THIN = [[83, "3"], [52, "40"]]
DEEP = [[83, "500"]]


def test_flag_off_reproduces_the_old_behaviour():
    """PARITY: with the check off, every profit exit is allowed as before."""
    with _Flag(False):
        m = _mgr(THIN)
        pos = {"count": 14, "is_no": True}
        ok, px = m._confirm_profit("T", pos, entry=0.650, nominal_px=0.83)
        assert ok is True
        assert px == 0.83, "must pass the nominal price straight through"


def test_the_bad_exit_is_blocked():
    """Top-of-book says 0.83 (a win on a 0.650 entry); depth says otherwise."""
    with _Flag(True):
        m = _mgr(THIN)
        pos = {"count": 14, "is_no": True}
        ok, px = m._confirm_profit("T", pos, entry=0.650, nominal_px=0.83)
        assert ok is False, f"a loss at size must not book as a profit (px={px})"
        assert px < 0.650, px


def test_a_genuine_profit_still_exits():
    """The fix must not block exits that are real at size."""
    with _Flag(True):
        m = _mgr(DEEP)
        pos = {"count": 14, "is_no": True}
        ok, px = m._confirm_profit("T", pos, entry=0.650, nominal_px=0.83)
        assert ok is True
        assert abs(px - 0.83) < 1e-9, px


def test_it_sells_at_the_executable_price_not_the_quote():
    """The logged sell price must be the one depth supports."""
    with _Flag(True):
        m = _mgr([[70, "500"]])
        pos = {"count": 14, "is_no": True}
        ok, px = m._confirm_profit("T", pos, entry=0.650, nominal_px=0.83)
        assert ok is True
        assert abs(px - 0.70) < 1e-9, f"should price at 0.70, not the 0.83 quote: {px}"


def test_empty_book_does_not_claim_a_profit():
    """No resting depth means the gain cannot be realised."""
    with _Flag(True):
        for levels in ([], [[0, "0"]]):
            m = _mgr(levels)
            ok, _ = m._confirm_profit("T", {"count": 14, "is_no": True},
                                      entry=0.650, nominal_px=0.83)
            assert ok is False


def test_zero_entry_is_not_a_divide_by_zero():
    with _Flag(True):
        m = _mgr(DEEP)
        ok, _ = m._confirm_profit("T", {"count": 14, "is_no": True},
                                  entry=0.0, nominal_px=0.83)
        assert ok is False


def test_larger_size_gets_a_worse_price():
    """The whole point: the price depends on how much you need to sell."""
    with _Flag(True):
        m = _mgr(THIN)
        _, small = m._confirm_profit("T", {"count": 3, "is_no": True},
                                     entry=0.10, nominal_px=0.83)
        _, big = m._confirm_profit("T", {"count": 14, "is_no": True},
                                   entry=0.10, nominal_px=0.83)
        assert small > big, (small, big)


def test_stops_are_not_gated_by_the_depth_check():
    """A stop must fire regardless — worse depth makes it MORE valid."""
    src = open("kalshi_btc_bot/positions.py").read()
    body = src.split("if no_pnl_pct <= -NO_STOP")[1].split("continue")[0]
    assert "_confirm_profit" not in body, (
        "the stop path must not call _confirm_profit — a worse executable "
        "price makes a stop more valid, not less")


def test_only_profit_exits_call_the_confirmation():
    src = open("kalshi_btc_bot/positions.py").read()
    assert src.count("self._confirm_profit(") == 3, (
        f"expected exactly 3 profit exits gated, found "
        f"{src.count('self._confirm_profit(')}")


def test_config_is_read_module_qualified():
    src = open("kalshi_btc_bot/positions.py").read()
    assert "_C.CONFIRM_EXIT_DEPTH" in src
    assert "from .config import CONFIRM_EXIT_DEPTH" not in src


def test_shipped_default_is_on():
    assert C.CONFIRM_EXIT_DEPTH is True


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
