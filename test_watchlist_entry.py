"""config.WATCHLIST_ENTRY_DIP — arm strict, fill on the model's valuation.

The point of this feature is that the ENTRY GATES GOING STALE IS THE DISCOUNT.
As spot drifts toward the band, true_prob rises, the overpricing ratio
collapses, and find_boundary_no stops returning the contract — precisely while
it gets cheap. So the fill condition deliberately does NOT re-check those
gates; it checks only the dip and the model's own valuation.

Off (0.0) must be a no-op, as with every parameter this repo ships.
"""
import sys
sys.path.insert(0, ".")
from kalshi_btc_bot import config as C
from kalshi_btc_bot.pending import PendingEntries


class _Watch:
    def __init__(self, dip, ne=0.05):
        self.dip, self.ne = dip, ne

    def __enter__(self):
        self._d = C.WATCHLIST_ENTRY_DIP
        self._n = C.WATCHLIST_ENTRY_NET_EDGE
        self._o = C.DELAYED_ENTRY_DIP
        C.WATCHLIST_ENTRY_DIP = self.dip
        C.WATCHLIST_ENTRY_NET_EDGE = self.ne
        C.DELAYED_ENTRY_DIP = 0.10          # arming path must be on to queue
        return self

    def __exit__(self, *a):
        C.WATCHLIST_ENTRY_DIP = self._d
        C.WATCHLIST_ENTRY_NET_EDGE = self._n
        C.DELAYED_ENTRY_DIP = self._o


class _Dist:
    """Stub pricer — returns whatever true_prob the test wants."""
    def __init__(self, tp):
        self.tp = tp

    def true_prob(self, row, spot, vol, hours, regime):
        return self.tp


def _row(bid, ticker="KXBTC-T-B64650"):
    return {"ticker": ticker, "bid": bid, "ask": bid + 0.02, "hours": 0.2,
            "type": "RANGE", "low": 64600.0, "high": 64700.0}


def _armed(ref_cost, ticker="KXBTC-T-B64650"):
    p = PendingEntries()
    out, st = p.gate({"ticker": ticker, "signal": "BOUNDARY_NO",
                      "no_cost": ref_cost, "true_prob": 0.10})
    assert st == "queued" and out is None
    return p


REG = {"regime": "RANGING", "mom": 0.0, "zscore": 0.0}


def test_off_is_a_no_op():
    """PARITY: 0.0 must never fill, whatever the ladder shows."""
    with _Watch(0.0):
        p = _armed(0.80)
        fills = p.watchlist_fills({"KXBTC-T-B64650": _row(0.40)},
                                  _Dist(0.05), 64650.0, 0.0001, REG)
        assert fills == []
        assert len(p) == 1, "the ticker must stay armed"


def test_fires_on_dip_plus_model_value():
    """ref 0.80, dip 10% -> need cost <= 0.72; model says NO worth 0.95."""
    with _Watch(0.10, ne=0.05):
        p = _armed(0.80)
        # bid 0.30 -> cost 0.70, a 12.5% dip; true_p 0.05 -> net edge 0.25
        fills = p.watchlist_fills({"KXBTC-T-B64650": _row(0.30)},
                                  _Dist(0.05), 64650.0, 0.0001, REG)
        assert len(fills) == 1
        tk, row = fills[0]
        assert abs(row["no_cost"] - 0.70) < 1e-9
        assert row["true_prob"] == 0.05
        assert row["signal"] == "BOUNDARY_NO"
        assert abs(row["watchlist_ref"] - 0.80) < 1e-9
        assert len(p) == 0, "a filled ticker must be dequeued"


def test_does_not_fire_without_enough_dip():
    with _Watch(0.10, ne=0.05):
        p = _armed(0.80)
        # cost 0.75 is only a 6.25% dip
        assert p.watchlist_fills({"KXBTC-T-B64650": _row(0.25)},
                                 _Dist(0.05), 64650.0, 0.0001, REG) == []
        assert len(p) == 1


def test_does_not_fire_when_the_model_no_longer_likes_it():
    """THE FILTER. Deep discount, but true_prob has risen — skip it.

    This is what separates a discount from a contract that has simply become a
    worse bet, and it is why net_edge >= 0.05 beat >= 0.00 in measurement.
    """
    with _Watch(0.10, ne=0.05):
        p = _armed(0.80)
        # cost 0.60 (25% dip) but true_p 0.45 -> net edge = 0.55 - 0.60 < 0
        assert p.watchlist_fills({"KXBTC-T-B64650": _row(0.40)},
                                 _Dist(0.45), 64650.0, 0.0001, REG) == []
        assert len(p) == 1, "must stay armed, not be discarded"


def test_net_edge_bar_is_enforced_exactly():
    with _Watch(0.10, ne=0.05):
        # cost 0.70, true_p 0.25 -> net edge = 0.75 - 0.70 = 0.05, exactly at bar
        p = _armed(0.80)
        assert len(p.watchlist_fills({"KXBTC-T-B64650": _row(0.30)},
                                     _Dist(0.25), 64650.0, 0.0001, REG)) == 1
        # true_p 0.26 -> net edge 0.04, just under
        p = _armed(0.80)
        assert p.watchlist_fills({"KXBTC-T-B64650": _row(0.30)},
                                 _Dist(0.26), 64650.0, 0.0001, REG) == []


def test_does_not_require_the_entry_gates_to_re_pass():
    """The whole point: the ladder row carries no z-score, ratio or OTM data,
    and the fill still happens. Stale gates ARE the discount."""
    with _Watch(0.10, ne=0.05):
        p = _armed(0.80)
        bare = {"ticker": "KXBTC-T-B64650", "bid": 0.30, "hours": 0.2}
        fills = p.watchlist_fills({"KXBTC-T-B64650": bare},
                                  _Dist(0.05), 64650.0, 0.0001, REG)
        assert len(fills) == 1


def test_missing_or_broken_ladder_row_is_skipped_not_crashed():
    with _Watch(0.10, ne=0.05):
        for rows in ({}, {"KXBTC-T-B64650": {"ticker": "x", "bid": 0}},
                     {"KXBTC-T-B64650": {"ticker": "x"}}):
            p = _armed(0.80)
            assert p.watchlist_fills(rows, _Dist(0.05), 64650.0, 0.0001, REG) == []
            assert len(p) == 1


def test_a_throwing_pricer_does_not_take_down_the_scan():
    class _Boom:
        def true_prob(self, *a, **k): raise RuntimeError("model blew up")
    with _Watch(0.10, ne=0.05):
        p = _armed(0.80)
        assert p.watchlist_fills({"KXBTC-T-B64650": _row(0.30)},
                                 _Boom(), 64650.0, 0.0001, REG) == []


def test_shipped_default_is_off():
    assert C.WATCHLIST_ENTRY_DIP == 0.0, (
        "unvalidated: n=39, P(ROC>0)=69%, 4 of 9 days negative")


def test_it_reads_config_at_call_time():
    src = open("kalshi_btc_bot/pending.py").read()
    assert '_C, "WATCHLIST_ENTRY_DIP"' in src
    assert "from .config import WATCHLIST_ENTRY_DIP" not in src


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
