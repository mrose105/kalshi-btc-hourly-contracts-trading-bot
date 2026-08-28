"""Delayed entry (config.DELAYED_ENTRY_DIP) — gate semantics and OFF-parity.

The first test is the important one. Every parameter added to this bot ships
defaulting to current behaviour, and the default must be provably a no-op —
otherwise a flag that was never switched on silently changes live trading.
"""
import sys
sys.path.insert(0, ".")
from kalshi_btc_bot import config as C
from kalshi_btc_bot.pending import PendingEntries


def _sig(cost, ticker="KXBTC-TEST-B64650", name="BOUNDARY_NO"):
    return {"ticker": ticker, "signal": name, "no_cost": cost,
            "true_prob": 0.20, "hours": 0.3}


_SENTINEL = object()


class _Dip:
    """Set the delayed-entry band for a block and restore it.

    cap defaults to None (no cap) so the pre-band tests below keep testing the
    FLOOR semantics they were written for.

    WATCHLIST_ENTRY_DIP is pinned to 0.0 for the block. `_pending` now has two
    consumers, and arming is enabled by EITHER flag, so a test of delayed entry
    has to say what the other one is doing or it measures the combination. That
    is not hypothetical: this fixture left the live 0.05 in place, so the
    dip=0.0 passthrough test started arming as soon as the two were decoupled.
    The combination is covered in test_watchlist_entry.py, which owns it.
    """
    def __init__(self, dip, wait=None, cap=None):
        self.dip, self.wait, self.cap = dip, wait, cap

    def __enter__(self):
        self._d, self._w = C.DELAYED_ENTRY_DIP, C.DELAYED_ENTRY_MAX_WAIT_MINS
        self._c, self._wl = C.DELAYED_ENTRY_DIP_MAX, C.WATCHLIST_ENTRY_DIP
        C.DELAYED_ENTRY_DIP = self.dip
        C.DELAYED_ENTRY_DIP_MAX = self.cap
        C.WATCHLIST_ENTRY_DIP = 0.0
        if self.wait is not None:
            C.DELAYED_ENTRY_MAX_WAIT_MINS = self.wait
        return self

    def __exit__(self, *a):
        C.DELAYED_ENTRY_DIP = self._d
        C.DELAYED_ENTRY_MAX_WAIT_MINS = self._w
        C.DELAYED_ENTRY_DIP_MAX = self._c
        C.WATCHLIST_ENTRY_DIP = self._wl


def test_zero_is_off_and_passes_every_signal_through_untouched():
    """THE PARITY TEST. 0.0 must reproduce pre-feature behaviour exactly."""
    with _Dip(0.0):
        p = PendingEntries()
        for cost in (0.74, 0.60, 0.90, 0.30):
            s = _sig(cost)
            out, status = p.gate(s)
            assert out is s, "0.0 must return the SAME object, unbuffered"
            assert status == "off"
        assert len(p) == 0, "nothing may be queued while the feature is off"
        assert p.describe(_sig(0.74), "off") is None


def test_unvalidated_delay_may_not_run_with_real_money():
    """DELAYED_ENTRY_DIP is a measurement run, not a validated parameter.

    The settlement study (config.DELAYED_ENTRY_DIP) found the edge FLAT in
    entry price: -7.0% at the signal, -9.6% waiting for -10%. It is switched on
    to test whether live signal-revalidation separates noise dips from
    spot-moved dips — a question the recording cannot answer. Until that holds
    on data recorded after switch-on, it does not touch the real account.
    """
    if C.DELAYED_ENTRY_DIP > 0:
        assert C.PAPER_TRADING is True, (
            f"DELAYED_ENTRY_DIP={C.DELAYED_ENTRY_DIP} is unvalidated and "
            f"PAPER_TRADING is False — validate on paper first, or set the "
            f"dip back to 0.0")


def test_off_by_signal_type_even_when_enabled():
    """SNIPE/YES are not in DELAYED_ENTRY_SIGNALS and must pass through."""
    with _Dip(0.10):
        p = PendingEntries()
        s = _sig(0.30, name="SNIPE")
        out, status = p.gate(s)
        assert out is s and status == "off"


def test_first_sighting_is_queued_not_bought():
    with _Dip(0.10):
        p = PendingEntries()
        out, status = p.gate(_sig(0.74))
        assert out is None and status == "queued"
        assert len(p) == 1


def test_fills_only_at_or_below_the_dip_target():
    with _Dip(0.10):
        p = PendingEntries()
        p.gate(_sig(0.80))                       # ref 0.80, target 0.72
        for cost in (0.79, 0.75, 0.7201):
            out, status = p.gate(_sig(cost))
            assert out is None, f"{cost} is not a 10% dip from 0.80"
            assert status == "waiting"
        s = _sig(0.72)
        out, status = p.gate(s)
        assert out is s and status == "triggered"
        assert len(p) == 0, "a filled ticker must be dequeued"


def test_reference_never_re_anchors_downward():
    """'Wait for -10%' from a moving floor always eventually triggers.

    Anchoring to each new low is the same defect as the rejected scale-in
    sweep: a threshold measured from a drifting reference is not a threshold.
    """
    with _Dip(0.10):
        p = PendingEntries()
        p.gate(_sig(0.80))                       # ref 0.80
        for cost in (0.76, 0.74, 0.73):          # drifts down, never -10%
            out, _ = p.gate(_sig(cost))
            assert out is None
        assert p.pending()[0][1] == 0.80, "ref moved off the first sighting"
        out, status = p.gate(_sig(0.7250))       # still above 0.72
        assert out is None and status == "waiting"


def test_reference_does_not_re_anchor_upward_either():
    """A rally then a pullback to the start is not a dip."""
    with _Dip(0.10):
        p = PendingEntries()
        p.gate(_sig(0.70))                       # ref 0.70, target 0.63
        p.gate(_sig(0.85))
        out, status = p.gate(_sig(0.70))
        assert out is None and status == "waiting"


def test_expiry_drops_the_pending_entry():
    with _Dip(0.10, wait=20.0):
        p = PendingEntries()
        p.gate(_sig(0.80), now=1000.0)
        assert p.expire(now=1000.0 + 19 * 60) == []
        assert p.expire(now=1000.0 + 21 * 60) == ["KXBTC-TEST-B64650"]
        assert len(p) == 0


def test_expired_ticker_requeues_at_the_new_price_not_the_old_ref():
    """After a timeout the old reference is gone — no stale anchor."""
    with _Dip(0.10, wait=20.0):
        p = PendingEntries()
        p.gate(_sig(0.80), now=0.0)
        p.expire(now=21 * 60)
        out, status = p.gate(_sig(0.60), now=22 * 60)
        assert out is None and status == "queued"
        assert p.pending()[0][1] == 0.60


def test_tickers_are_tracked_independently():
    with _Dip(0.10):
        p = PendingEntries()
        p.gate(_sig(0.80, ticker="A"))
        p.gate(_sig(0.50, ticker="B"))
        assert len(p) == 2
        out, _ = p.gate(_sig(0.72, ticker="A"))
        assert out is not None
        out, status = p.gate(_sig(0.48, ticker="B"))
        assert out is None and status == "waiting"   # B needs 0.45


def test_discard_forgets_a_ticker():
    with _Dip(0.10):
        p = PendingEntries()
        p.gate(_sig(0.80))
        p.discard("KXBTC-TEST-B64650")
        assert len(p) == 0
        out, status = p.gate(_sig(0.79))
        assert status == "queued" and out is None


def test_describe_reports_the_target_not_the_current_price():
    with _Dip(0.10):
        p = PendingEntries()
        s = _sig(0.80)
        _, status = p.gate(s)
        msg = p.describe(s, status)
        assert "0.800" in msg and "0.720" in msg, msg
        s2 = _sig(0.76)
        _, status = p.gate(s2)
        assert "0.720" in p.describe(s2, status)


def test_band_fires_inside_the_window():
    """[5%, 10%]: a dip landing in the band buys."""
    with _Dip(0.05, cap=0.10):
        p = PendingEntries()
        p.gate(_sig(1.00))                        # ref 1.00, band 0.90-0.95
        assert p.gate(_sig(0.96))[1] == "waiting"
        s = _sig(0.93)
        out, status = p.gate(s)
        assert out is s and status == "triggered"


def test_band_abandons_a_dip_that_blows_through_the_cap():
    """The whole point of the cap: -20% is spot moving, not noise.

    Settlement-resolved: dips through the [5%,10%] band win 50% vs a 67% base.
    """
    with _Dip(0.05, cap=0.10):
        p = PendingEntries()
        p.gate(_sig(1.00))                        # band 0.90-0.95
        out, status = p.gate(_sig(0.80))          # -20%, straight through
        assert out is None and status == "abandoned"
        assert len(p) == 0, "an abandoned ticker must be dequeued"


def test_abandoned_ticker_does_not_fill_on_a_bounce_back():
    """By the time it bounces the information has already arrived."""
    with _Dip(0.05, cap=0.10):
        p = PendingEntries()
        p.gate(_sig(1.00))
        assert p.gate(_sig(0.80))[1] == "abandoned"
        # Bounces back into the old band — must re-queue at the NEW price,
        # not fire against the stale 1.00 reference.
        out, status = p.gate(_sig(0.92))
        assert out is None and status == "queued"
        assert p.pending()[0][1] == 0.92


def test_band_boundaries_are_inclusive_at_both_ends():
    with _Dip(0.05, cap=0.10):
        p = PendingEntries()
        p.gate(_sig(1.00))
        assert p.gate(_sig(0.95))[1] == "triggered"      # exactly -5%
        p2 = PendingEntries()
        p2.gate(_sig(1.00))
        assert p2.gate(_sig(0.90))[1] == "triggered"     # exactly -10%


def test_no_cap_restores_pure_floor_behaviour():
    """DELAYED_ENTRY_DIP_MAX = None must never abandon."""
    with _Dip(0.05, cap=None):
        p = PendingEntries()
        p.gate(_sig(1.00))
        out, status = p.gate(_sig(0.40))          # -60%
        assert out is not None and status == "triggered"


def test_describe_reports_the_band_not_a_single_target():
    with _Dip(0.05, cap=0.10):
        p = PendingEntries()
        s = _sig(1.00)
        _, st = p.gate(s)
        msg = p.describe(s, st)
        assert "0.900" in msg and "0.950" in msg, msg


_SPOT = 73000.0    # z < 0 fades OTM contracts BELOW spot, so spot >= high


def _boundary_ladder():
    """Two qualifying BOUNDARY_NO contracts, one clearly better-ranked.

    Every gate matters here: strikes must sit below _SPOT (directional gate for
    z<0), otm_dist inside [-250, -10], hours inside [0.08, 0.50], yes_ask
    inside [0.10, 0.65]. A fixture that silently fails one of them returns []
    and the test passes while proving nothing.
    """
    def row(strike, bid, ask):
        return {"ticker": f"KXBTC-26AUG2016-B{strike}", "ask": ask, "bid": bid,
                "strike": float(strike), "low": float(strike) - 50,
                "high": float(strike) + 50, "hours": 0.25,
                "otm_dist": float(strike + 50 - _SPOT),
                "type": "RANGE", "itm": False, "vol": 900}
    # Both rows must clear BOUNDARY_NO_OVERPRICING_MIN against _FlatDist's
    # true_prob of 0.16, or find_boundary_no returns fewer candidates than the
    # test asserts: 0.30/0.16 = 1.88x, 0.28/0.16 = 1.75x.
    # Bids/asks DERIVED from the shipped gates, never hardcoded. These were
    # 0.30/0.33 and 0.28/0.31 — comfortably inside the 0.65 ask ceiling of the
    # day, and instantly outside the 0.30 ceiling — so the fixture stopped
    # producing candidates and every test here "failed" on a threshold move
    # rather than on behaviour. Same trap as the ratio fixtures.
    _ask = min(0.30, C.BOUNDARY_NO_YES_ASK_MAX)
    _b1  = round(_ask - 0.02, 4)          # top candidate
    _b2  = round(_ask - 0.04, 4)          # second, must rank below it
    return [row(72750, _b1, _ask), row(72850, _b2, round(_ask - 0.02, 4))]


class _FlatDist:
    @staticmethod
    def posterior_prob(*a, **k):
        return {"prior_prob": 0.16, "market_prob": 0.30,
                "true_prob": 0.16, "market_weight": 0.0}


def _engine():
    from kalshi_btc_bot.signals import SignalEngine
    return SignalEngine(_FlatDist())


_REGIME = {"regime": "RANGING", "direction": "NEUTRAL", "zscore": -1.60}


def test_all_matches_returns_every_candidate_best_first():
    got = _engine().find_boundary_no(
        _SPOT, 0.001, _REGIME, _boundary_ladder(), {}, 500.0, 500.0,
        all_matches=True)
    assert len(got) == 2, got
    assert got[0]["overpricing_ratio"] >= got[1]["overpricing_ratio"]


def test_all_matches_default_is_unchanged_single_best():
    e, lad = _engine(), _boundary_ladder()
    one = e.find_boundary_no(_SPOT, 0.001, _REGIME, lad, {}, 500.0, 500.0)
    many = e.find_boundary_no(_SPOT, 0.001, _REGIME, lad, {}, 500.0, 500.0,
                              all_matches=True)
    assert isinstance(one, dict) and one["ticker"] == many[0]["ticker"], (
        "all_matches must not change which contract ranks first")


def test_all_matches_does_not_relax_the_ratio_bar():
    """Widening what is REPORTED must not widen what QUALIFIES."""
    old = C.BOUNDARY_NO_OVERPRICING_MIN
    try:
        C.BOUNDARY_NO_OVERPRICING_MIN = 99.0
        got = _engine().find_boundary_no(
            _SPOT, 0.001, _REGIME, _boundary_ladder(), {}, 500.0, 500.0,
            all_matches=True)
        assert got == [], got
    finally:
        C.BOUNDARY_NO_OVERPRICING_MIN = old


def test_non_top_ranked_queued_ticker_still_fills():
    """REGRESSION for the 2026-08-20 miss.

    B72750 was queued at $0.770, dipped to $0.50 (-35%) for 191 consecutive
    ticks, and never filled: another contract outranked it, so the scan stopped
    returning it and the queue never saw the dip. Gating only the top-ranked
    candidate reproduces the bug; gating all of them fixes it.
    """
    with _Dip(0.10):
        p = PendingEntries()
        top, second = _sig(0.80, ticker="TOP"), _sig(0.77, ticker="SECOND")
        for s in (top, second):
            assert p.gate(s)[1] == "queued"

        # SECOND collapses to 0.50; TOP still outranks it every scan.
        dipped = _sig(0.50, ticker="SECOND")

        # Old behaviour: only the top-ranked candidate is gated.
        fired = [s for s, _ in (p.gate(top),)]
        assert fired == [None], "TOP has not dipped, so nothing should fire"
        assert len(p) == 2, "SECOND is still stranded in the queue"

        # New behaviour: every candidate is gated, best-first.
        fire = None
        for cand in (top, dipped):
            s, _ = p.gate(cand)
            if s and fire is None:
                fire = s
        assert fire is dipped, "the dipped non-top candidate must fill"
        assert len(p) == 1, "only SECOND should be dequeued"


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
