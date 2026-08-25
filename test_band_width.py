"""Strike clustering must adapt to the grid Kalshi actually listed.

Kalshi does not always list the hourly on a 100-wide grid. Over ~700k recorded
market-observations 97% of bands are 100 wide and 3% are 250, and it varies by
WINDOW rather than by time to expiry: 26AUG2514/15/16 were each 186 markets at
100 wide while 26AUG2517 was 78 markets at 250 wide for its entire life, from
four hours out to expiry.

STRIKE_CLUSTER_DIST = 150 is 1.5 bands on the 100 grid, so it blocks the
adjacent strike — the whole point of the control. On a 250 grid adjacent
strikes sit 250 apart, so a fixed 150 can NEVER fire and the control silently
vanishes. That is exactly the failure it was added for (2026-07-07: four RANGE
positions on adjacent strikes 62550-62850, one breakout busted all four).

Scoped deliberately: only the "which band in the ladder" gate scales. The
sigma-based distance gates stay in dollars, because a $250 move is a $250 move
whatever grid prices it.
"""
import sys
sys.path.insert(0, ".")

from kalshi_btc_bot import config as C
from kalshi_btc_bot.signals import _cluster_dist, _clustered


def _c(low, high, strike):
    return {"ticker": f"KXBTC-26AUG2517-B{int(strike)}", "low": float(low),
            "high": float(high), "strike": float(strike)}


def _open(strike):
    tk = f"KXBTC-26AUG2517-B{int(strike)}"
    return {tk: {"contract": {"strike": float(strike)}}}


def test_hundred_grid_is_unchanged():
    """PARITY. The calibrated grid must reproduce the configured constant."""
    assert _cluster_dist(_c(78750, 78850, 78800)) == C.STRIKE_CLUSTER_DIST


def test_two_fifty_grid_widens_the_gate():
    d = _cluster_dist(_c(78750, 79000, 78875))
    assert d == 375.0, d
    assert d > 250, "must exceed the strike spacing or it can never fire"


def test_the_bug_adjacent_strikes_on_a_250_grid_are_now_blocked():
    """THE BUG. 250-apart strikes sailed through a 150 limit."""
    # real geometry from the live 17:00 window on 2026-08-25
    new = _c(79000, 79250, 79125)
    assert _clustered(new["ticker"], new["strike"], _open(78875), new), (
        "adjacent 250-wide strikes not treated as clustered — one BTC move "
        "busts both and every MAX_POSITIONS slot fills with correlated bets")


def test_same_case_without_the_fix_would_have_passed():
    """Documents the old behaviour so the regression is unmistakable."""
    assert abs(79125 - 78875) == 250
    assert 250 >= C.STRIKE_CLUSTER_DIST, (
        "adjacent 250-grid strikes are further apart than the fixed constant, "
        "which is why the control could never fire")


def test_a_genuinely_distant_strike_is_still_allowed():
    """The fix must not block everything on a wide grid."""
    far = _c(79750, 80000, 79875)      # 1000 from the open position
    assert not _clustered(far["ticker"], far["strike"], _open(78875), far)


def test_missing_band_geometry_falls_back_to_the_constant():
    """Callers without lo/hi must behave exactly as before."""
    assert _cluster_dist(None) == C.STRIKE_CLUSTER_DIST
    assert _cluster_dist({}) == C.STRIKE_CLUSTER_DIST
    assert _cluster_dist({"low": None, "high": None}) == C.STRIKE_CLUSTER_DIST
    assert _cluster_dist({"low": 100.0, "high": 100.0}) == C.STRIKE_CLUSTER_DIST


def test_clustering_still_scoped_to_one_expiry_window():
    """A same-strike position in a DIFFERENT expiry must not block entry."""
    new = _c(79000, 79250, 79125)
    other = {"KXBTC-26AUG2520-B79125": {"contract": {"strike": 79125.0}}}
    assert not _clustered(new["ticker"], new["strike"], other, new)


def test_sigma_based_gates_were_deliberately_left_in_dollars():
    """Scope guard. Widening these would change the risk profile, not relabel it."""
    import ast
    import inspect
    from kalshi_btc_bot import signals as S
    # Inspect the CODE, not the prose. The docstring names these constants on
    # purpose, to record that they were left alone — matching on raw text made
    # the explanation itself trip the test.
    fn = ast.parse(inspect.getsource(S._cluster_dist)).body[0]
    fn.body = [n for n in fn.body if not (isinstance(n, ast.Expr)
                                          and isinstance(n.value, ast.Constant)
                                          and isinstance(n.value.value, str))]
    used = {n.id for n in ast.walk(ast.Module(body=fn.body, type_ignores=[]))
            if isinstance(n, ast.Name)}
    for name in ("BOUNDARY_NO_OTM_MIN", "BOUNDARY_NO_OTM_MAX",
                 "BOUNDARY_RISK_DIST", "TIME_EXIT_NEAR_DIST"):
        assert name not in used, (
            f"{name} is being scaled by band width — that is a risk-profile "
            f"change and was explicitly out of scope")
    assert "STRIKE_CLUSTER_DIST" in used, "the one gate in scope must be used"


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
