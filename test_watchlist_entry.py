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
    """Stub pricer — returns whatever probability the test wants.

    Exposes posterior_prob, because watchlist_fills prices with the POSTERIOR
    to match the arming gate. A stub offering only true_prob would let a
    regression back to the prior pass unnoticed.
    """
    def __init__(self, tp):
        self.tp = tp

    def posterior_prob(self, row, spot, vol, hours, regime, bid=None, ask=None):
        return {"true_prob": self.tp, "prior_prob": self.tp,
                "market_prob": None, "market_weight": 0.0}


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


def test_unvalidated_watchlist_may_not_run_with_real_money():
    """n=14 across 6 days, P(ROC>0)=79%. That is fourteen trades.

    Switched on 2026-08-23 as a PAPER measurement, by explicit request. Same
    guard as DELAYED_ENTRY_DIP: it does not touch the real account until it
    holds on data recorded after switch-on.
    """
    if C.WATCHLIST_ENTRY_DIP > 0:
        assert C.PAPER_TRADING is True, (
            f"WATCHLIST_ENTRY_DIP={C.WATCHLIST_ENTRY_DIP} is unvalidated and "
            f"PAPER_TRADING is False — validate on paper first, or set it to 0.0")


def test_off_by_zero_is_still_supported():
    """The escape hatch must keep working even though the flag ships on."""
    with _Watch(0.0):
        p = _armed(0.80)
        assert p.watchlist_fills({"KXBTC-T-B64650": _row(0.40)},
                                 _Dist(0.05), 64650.0, 0.0001, REG) == []


def test_it_prices_with_the_posterior_not_the_prior():
    """Consistency with the arming gate. A prior-priced fill would put two
    different probability estimates inside one strategy."""
    src = open("kalshi_btc_bot/pending.py").read()
    body = src.split("def watchlist_fills")[1].split("\n    def ")[0]
    assert "posterior_prob(" in body
    assert "dist.true_prob(" not in body


def test_it_reads_config_at_call_time():
    src = open("kalshi_btc_bot/pending.py").read()
    assert '_C, "WATCHLIST_ENTRY_DIP"' in src
    assert "from .config import WATCHLIST_ENTRY_DIP" not in src


# ---------------------------------------------------------------------------
# THE INERT-FEATURE BUG, found live 2026-08-24.
#
# _pending is written in exactly one place, and gate() used to return early on
# DELAYED_ENTRY_DIP <= 0. Shipped config was DELAYED_ENTRY_DIP = 0.0 with
# WATCHLIST_ENTRY_DIP = 0.05, so nothing ever armed and watchlist_fills()
# returned [] for the life of the process. Every test above passed the whole
# time, because each one sets DELAYED_ENTRY_DIP = 0.10 in its own fixture.
#
# The tests knew the arming path had to be on. The config did not.
# ---------------------------------------------------------------------------


class _WatchOnly:
    """Watchlist on, delayed entry OFF — exactly the shipped configuration."""
    def __init__(self, dip=0.05, ne=0.05):
        self.dip, self.ne = dip, ne

    def __enter__(self):
        self._d = C.WATCHLIST_ENTRY_DIP
        self._n = C.WATCHLIST_ENTRY_NET_EDGE
        self._o = C.DELAYED_ENTRY_DIP
        C.WATCHLIST_ENTRY_DIP = self.dip
        C.WATCHLIST_ENTRY_NET_EDGE = self.ne
        C.DELAYED_ENTRY_DIP = 0.0
        return self

    def __exit__(self, *a):
        C.WATCHLIST_ENTRY_DIP = self._d
        C.WATCHLIST_ENTRY_NET_EDGE = self._n
        C.DELAYED_ENTRY_DIP = self._o


def test_watchlist_arms_without_delayed_entry():
    """THE BUG. WATCHLIST_ENTRY_DIP alone must arm the ticker."""
    with _WatchOnly():
        p = PendingEntries()
        out, st = p.gate({"ticker": "KXBTC-T-B64650", "signal": "BOUNDARY_NO",
                          "no_cost": 0.87, "true_prob": 0.10})
        assert st == "queued", f"status={st!r} — nothing armed, watchlist inert"
        assert out is None, "must withhold the buy; the arming price is the ref"
        assert len(p) == 1


def test_watchlist_only_actually_fills():
    """End to end on the shipped config: arm, dip, fill."""
    with _WatchOnly(dip=0.05, ne=0.05):
        p = PendingEntries()
        p.gate({"ticker": "KXBTC-T-B64650", "signal": "BOUNDARY_NO",
                "no_cost": 0.87, "true_prob": 0.10})
        # cost 0.70 is a 19.5% dip off 0.87; true_p 0.05 -> net edge 0.25
        fills = p.watchlist_fills({"KXBTC-T-B64650": _row(0.30)},
                                  _Dist(0.05), 64650.0, 0.0001, REG)
        assert len(fills) == 1, "armed and deeply discounted, but no fill"
        assert abs(fills[0][1]["watchlist_ref"] - 0.87) < 1e-9


def test_gate_never_fills_in_watchlist_only_mode():
    """gate() has no dip of its own here — every fill comes from the ladder."""
    with _WatchOnly():
        p = PendingEntries()
        sig = {"ticker": "KXBTC-T-B64650", "signal": "BOUNDARY_NO",
               "no_cost": 0.87, "true_prob": 0.10}
        p.gate(sig)
        for cost in (0.80, 0.70, 0.50, 0.20):
            out, st = p.gate({**sig, "no_cost": cost})
            assert out is None and st == "waiting", (cost, st)
        assert len(p) == 1, "must stay armed for watchlist_fills()"


def test_both_off_still_passes_straight_through():
    """PARITY: the genuine off case must be unchanged."""
    with _WatchOnly(dip=0.0):
        p = PendingEntries()
        sig = {"ticker": "KXBTC-T-B64650", "signal": "BOUNDARY_NO",
               "no_cost": 0.87, "true_prob": 0.10}
        out, st = p.gate(sig)
        assert st == "off" and out is sig
        assert len(p) == 0


def test_delayed_entry_alone_is_unaffected():
    """The other consumer must keep its own fill behaviour."""
    old_w, old_d = C.WATCHLIST_ENTRY_DIP, C.DELAYED_ENTRY_DIP
    C.WATCHLIST_ENTRY_DIP, C.DELAYED_ENTRY_DIP = 0.0, 0.10
    try:
        p = PendingEntries()
        sig = {"ticker": "KXBTC-T-B64650", "signal": "BOUNDARY_NO",
               "no_cost": 1.00, "true_prob": 0.10}
        assert p.gate(sig)[1] == "queued"
        out, st = p.gate({**sig, "no_cost": 0.89})   # 11% dip, inside the cap
        assert st == "triggered" and out is not None
    finally:
        C.WATCHLIST_ENTRY_DIP, C.DELAYED_ENTRY_DIP = old_w, old_d


def test_no_switched_on_feature_may_be_unreachable():
    """THE CLASS, not the instance.

    Any flag that is on in the shipped config must be able to affect behaviour.
    Asserted structurally: every consumer of `_pending` must be represented in
    `_arming_on()`, so turning one on cannot be silently gated by another.
    """
    src = open("kalshi_btc_bot/pending.py").read()
    # assert, don't index — a missing _arming_on must report as a failure, not
    # crash the runner with IndexError and skip every test after it.
    assert "def _arming_on" in src, "no _arming_on: arming is gated on one flag"
    arming = src.split("def _arming_on")[1].split("def gate")[0]
    assert "DELAYED_ENTRY_DIP" in arming
    assert "WATCHLIST_ENTRY_DIP" in arming, (
        "watchlist_fills() consumes _pending but its flag does not enable "
        "arming — switching it on would do nothing")

    # And the live config must not contain an unreachable switched-on feature.
    if getattr(C, "WATCHLIST_ENTRY_DIP", 0.0) > 0:
        p = PendingEntries()
        _, st = p.gate({"ticker": "KXBTC-T-B1", "signal": "BOUNDARY_NO",
                        "no_cost": 0.87, "true_prob": 0.10})
        assert st == "queued", (
            f"WATCHLIST_ENTRY_DIP={C.WATCHLIST_ENTRY_DIP} is on but gate() "
            f"returned {st!r} under the LIVE config — the feature is inert")


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
