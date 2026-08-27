"""Live/backtest parity: buy_no() must re-check BOUNDARY_NO_MIN_NET_EDGE.

find_boundary_no() (signals.py:352) selects on an ABSOLUTE net edge, and the
synthetic fill path re-checks it at the next bar's open
(kalshi_btc_backtest.py:1239). Portfolio.buy_no() re-checked only the
OVERPRICING RATIO, so live could fill a trade the backtest would reject.

The two gates are not redundant. With no_cost = 1 - bid:

    net_edge = (1 - true_p) - (1 - bid) = bid - true_p
    ratio    = bid / true_p
    =>  net_edge = true_p * (ratio - 1)

so clearing net_edge >= 0.05 needs true_p >= 0.05 / (ratio - 1).

At the old BOUNDARY_NO_OVERPRICING_MIN = 1.15 that was true_p >= 0.333, far
above the OTM contracts this signal trades — the ratio gate was nearly vacuous,
and a sweep confirmed it: 1.00 through 1.15 admitted the identical 71
candidates. Raised to 1.60 on 2026-08-22, the bar is true_p >= 0.083, so the
two gates now bind over genuinely different regions. These tests pin the
threshold they exercise explicitly rather than reading the live value, so a
future tuning pass cannot silently make them vacuous again.
"""
import sys
from contextlib import contextmanager
sys.path.insert(0, ".")
from kalshi_btc_bot import config as C


@contextmanager
def _paper_portfolio():
    from kalshi_btc_bot import portfolio as portfolio_module

    old_config = C.PAPER_TRADING
    # portfolio.py now reads _C.PAPER_TRADING at call time, so setting the
    # config module is enough. This used to patch portfolio's own frozen
    # copy as well — a workaround for the very bug that was just fixed.
    C.PAPER_TRADING = True
    try:
        yield portfolio_module
    finally:
        C.PAPER_TRADING = old_config


class _Quote:
    """Portfolio stub exercising only the revalidation arithmetic."""
    def __init__(self, bid, ask):
        self.bid, self.ask, self.rejects = bid, ask, []


def _gates(bid, true_p, is_boundary=True):
    """Return (ratio_ok, net_edge_ok) exactly as the live path computes them."""
    min_ratio = (C.BOUNDARY_NO_OVERPRICING_MIN if is_boundary
                 else C.NO_OVERPRICING_MIN)
    ratio_ok = true_p > 0 and bid / true_p >= min_ratio
    net_edge = (1.0 - true_p) - (1.0 - bid)
    return ratio_ok, net_edge >= C.BOUNDARY_NO_MIN_NET_EDGE, net_edge


def test_ratio_gate_does_not_imply_net_edge_gate():
    """The documented failure: ratio clears, absolute edge does not.

    Derived from the shipped bar rather than hardcoded, so raising the ratio
    gate cannot silently turn this into a no-op.
    """
    true_p = C.BOUNDARY_NO_MIN_NET_EDGE / (C.BOUNDARY_NO_OVERPRICING_MIN - 1.0) * 0.5
    bid = true_p * C.BOUNDARY_NO_OVERPRICING_MIN   # exactly at the ratio bar
    ratio_ok, edge_ok, net_edge = _gates(bid, true_p)
    assert ratio_ok, "ratio gate should pass exactly at the bar"
    assert not edge_ok, "net-edge gate should REJECT"
    assert net_edge < C.BOUNDARY_NO_MIN_NET_EDGE


def test_deep_otm_is_the_dangerous_region():
    """As true_p falls, the ratio gate admits ever-smaller absolute edge."""
    # net_edge = true_p * (ratio - 1), so the dangerous region is
    # true_p < MIN_NET_EDGE / (ratio - 1). Follow the bar, don't hardcode it.
    bar = C.BOUNDARY_NO_MIN_NET_EDGE / (C.BOUNDARY_NO_OVERPRICING_MIN - 1.0)
    for true_p in (bar * 0.25, bar * 0.5, bar * 0.9):
        bid = true_p * C.BOUNDARY_NO_OVERPRICING_MIN   # exactly at the ratio bar
        ratio_ok, edge_ok, net_edge = _gates(bid, true_p)
        assert ratio_ok
        assert not edge_ok, f"true_p={true_p} unexpectedly cleared the edge bar"
        assert net_edge < C.BOUNDARY_NO_MIN_NET_EDGE


def test_genuinely_good_trade_still_passes_both():
    """The fix must not reject trades the signal legitimately selected."""
    bid, true_p = 0.20, 0.10           # net_edge 0.10, ratio 2.0x
    ratio_ok, edge_ok, _ = _gates(bid, true_p)
    assert ratio_ok and edge_ok


def test_v2_order_payload_maps_outcome_direction_and_price():
    """The V2 endpoint is a YES-leg book even when callers trade NO."""
    from kalshi_btc_bot import portfolio as portfolio_module

    p = portfolio_module.Portfolio.__new__(portfolio_module.Portfolio)
    cases = (
        ("buy", "yes", 0.38, "bid", "0.3800"),
        ("sell", "yes", 0.38, "ask", "0.3800"),
        ("buy", "no", 0.38, "ask", "0.6200"),
        ("sell", "no", 0.38, "bid", "0.6200"),
    )
    for action, outcome, price, book_side, yes_price in cases:
        payload = p.order_payload("BTC-TEST", action, outcome, 3, price)
        assert payload["side"] == book_side, (action, outcome, payload)
        assert payload["price"] == yes_price, (action, outcome, payload)
    assert portfolio_module._ORDER_CREATE_ENDPOINT == "/portfolio/events/orders"


def test_v2_fill_count_and_price_parse():
    """Fill parsing is order correctness and survives the fee-policy removal."""
    from kalshi_btc_bot.portfolio import Portfolio

    response = {"fill_count": "3.00", "average_fill_price": "0.3800"}
    filled, price = Portfolio._parse_fill(response, 0.38)
    assert filled == 3 and price == 0.38


def test_v2_no_fill_price_is_converted_from_yes_leg():
    from kalshi_btc_bot.portfolio import Portfolio

    response = {"fill_count": "3.00", "average_fill_price": "0.6200",
                "average_fee_paid": "0.0167"}
    filled, no_price = Portfolio._parse_fill(response, 0.38, "no")
    assert filled == 3
    assert abs(no_price - 0.38) < 1e-12


def test_live_buy_no_enforces_net_edge():
    """END-TO-END: the real buy_no() must reject the decayed quote.

    Fails before the fix (buy_no only re-checked the ratio), passes after.

    The ask is chosen to CLEAR the spread gate on purpose. A first draft used
    yes_ask=0.16, which rejects at 28% > MAX_SPREAD_PCT before the net-edge
    logic is ever reached — the test passed while proving nothing. 0.115/0.14 is
    a 0.025 spread at 17.9%, inside both spread limits, so the only thing left
    that can reject it is the gate under test.
    """
    with _paper_portfolio() as portfolio_module:
        p = portfolio_module.Portfolio(client=None)
        p.sync()
        # Must clear the spread gate AND the ratio gate, so the only thing left
        # that can reject is the net-edge gate under test.
        _tp = (C.BOUNDARY_NO_MIN_NET_EDGE
               / (C.BOUNDARY_NO_OVERPRICING_MIN - 1.0)) * 0.5
        _bid = round(_tp * C.BOUNDARY_NO_OVERPRICING_MIN, 4)
        _ask = round(_bid * 1.15, 4)
        p._fresh_quote = lambda tk, attempts=3: (_bid, _ask)
        assert _ask - _bid <= C.MAX_SPREAD and (_ask - _bid) / _ask <= C.MAX_SPREAD_PCT, \
            "fixture must clear the spread gate or the test proves nothing"
        assert _bid / _tp >= C.BOUNDARY_NO_OVERPRICING_MIN, \
            "fixture must clear the ratio gate or the test proves nothing"
        reasons = []
        p._log_reject = lambda tk, why: reasons.append(why)
        contract = {"ticker": "BTC-TEST-B60000", "signal": "BOUNDARY_NO",
                    "hours": 0.5, "type": "RANGE", "low": 59950, "high": 60050}
        ok = p.buy_no(contract, true_prob=_tp)
        assert ok is False, "buy_no filled a trade under the net-edge bar"
        joined = " | ".join(reasons).lower()
        assert "net edge" in joined, (
            f"rejected, but not by the net-edge gate — reasons were: {reasons}")


# ---------------------------------------------------------------------------
# THE RATIO GATE vs THE WATCHLIST, found live 2026-08-25.
#
# ratio = bid / true_p, and a dip IS yes_bid falling — so every cent of
# discount the watchlist waits for lowers the ratio. The gate is
# anti-correlated with the strategy it guards and can only ever refuse what
# the watchlist finds. The first fill the watchlist ever produced:
#
#   ⏳→📉 WATCHLIST C-26AUG2509-B79150 $0.740 (ref $0.870, -14.9%) true=21%
#   🚫 skipped — overpricing 1.19x fell under 1.60x (bid $0.240)
#
# Selection criteria lock at arming; valuation stays live. These tests pin
# both halves of that split.
# ---------------------------------------------------------------------------


def _watchlist_contract(**kw):
    c = {"ticker": "BTC-TEST-B60000", "signal": "BOUNDARY_NO", "hours": 0.2,
         "type": "RANGE", "low": 59950, "high": 60050, "watchlist_ref": 0.870}
    c.update(kw)
    return c


def test_watchlist_fill_is_exempt_from_the_ratio_recheck():
    """THE BUG. A discounted fill must not be refused for being discounted."""
    with _paper_portfolio() as portfolio_module:
        p = portfolio_module.Portfolio(client=None)
        p.sync()
        # DERIVED from the shipped bar, never hardcoded. An earlier draft used
        # 0.20/0.28 (ratio 1.40), which sat under the 1.60 bar of the day and
        # sailed over it when the bar moved to 1.25 — the fixture stopped
        # testing anything without failing. Same trap this file's docstring
        # warns about.
        #
        # Need ratio < bar AND net_edge >= the bar, and net_edge =
        # true_p * (ratio - 1), so true_p must be large enough to clear the
        # edge test at a deliberately sub-bar ratio.
        _ratio = C.BOUNDARY_NO_OVERPRICING_MIN * 0.9        # under the bar
        _tp = round(C.BOUNDARY_NO_MIN_NET_EDGE / (_ratio - 1.0) * 1.15, 4)
        _bid = round(_tp * _ratio, 4)
        _ask = round(_bid + 0.02, 4)
        assert _bid / _tp < C.BOUNDARY_NO_OVERPRICING_MIN, "fixture must fail the ratio bar"
        assert _bid - _tp >= C.BOUNDARY_NO_MIN_NET_EDGE, "fixture must clear net edge"
        assert (_ask - _bid) <= C.MAX_SPREAD, "fixture must clear the spread gate"
        p._fresh_quote = lambda tk, attempts=3: (_bid, _ask)
        reasons = []
        p._log_reject = lambda tk, why: reasons.append(why)
        ok = p.buy_no(_watchlist_contract(), true_prob=_tp)
        joined = " | ".join(reasons).lower()
        assert "overpricing" not in joined, (
            f"watchlist fill rejected by the ratio gate: {reasons}")
        assert ok is not False or "overpricing" not in joined


def test_a_normal_signal_is_still_held_to_the_ratio_gate():
    """PARITY. The exemption must be scoped to watchlist fills only."""
    with _paper_portfolio() as portfolio_module:
        p = portfolio_module.Portfolio(client=None)
        p.sync()
        _ratio = C.BOUNDARY_NO_OVERPRICING_MIN * 0.9        # under the bar
        _tp = round(C.BOUNDARY_NO_MIN_NET_EDGE / (_ratio - 1.0) * 1.15, 4)
        _bid = round(_tp * _ratio, 4)
        _ask = round(_bid + 0.02, 4)
        p._fresh_quote = lambda tk, attempts=3: (_bid, _ask)
        p._orderbook = lambda tk: {"yes": [[int(_bid * 100), 500]]}
        reasons = []
        p._log_reject = lambda tk, why: reasons.append(why)
        c = _watchlist_contract()
        del c["watchlist_ref"]            # an ordinary BOUNDARY_NO signal
        ok = p.buy_no(c, true_prob=_tp)
        assert ok is False, "a non-watchlist signal must still face the ratio gate"
        assert "overpricing" in " | ".join(reasons).lower(), reasons


def test_watchlist_fill_still_faces_the_net_edge_gate():
    """Valuation is NOT locked. Freezing it at arming is how you buy a knife."""
    with _paper_portfolio() as portfolio_module:
        p = portfolio_module.Portfolio(client=None)
        p.sync()
        # Deeply discounted, but spot walked into the band and true_prob rose:
        # net edge is now negative. Must still refuse.
        _tp, _bid, _ask = 0.30, 0.31, 0.33
        assert _bid - _tp < C.BOUNDARY_NO_MIN_NET_EDGE
        p._fresh_quote = lambda tk, attempts=3: (_bid, _ask)
        reasons = []
        p._log_reject = lambda tk, why: reasons.append(why)
        ok = p.buy_no(_watchlist_contract(), true_prob=_tp)
        assert ok is False, "a watchlist fill with no edge left was accepted"
        assert "net edge" in " | ".join(reasons).lower(), reasons


def test_zero_true_prob_is_still_rejected_on_the_watchlist_path():
    """The exemption must not open a divide-by-zero hole."""
    with _paper_portfolio() as portfolio_module:
        p = portfolio_module.Portfolio(client=None)
        p.sync()
        p._fresh_quote = lambda tk, attempts=3: (0.28, 0.30)
        reasons = []
        p._log_reject = lambda tk, why: reasons.append(why)
        ok = p.buy_no(_watchlist_contract(), true_prob=0.0)
        assert ok is False, "true_prob=0 must never buy"


# ---------------------------------------------------------------------------
# THE SPREAD GATE, found live 2026-08-25 killing a -25.3% watchlist fill with
#   "spread $0.050 (15%) over MAX_SPREAD $0.05/25%"
# Two separate defects in one line: float precision, and measuring the spread
# against the wrong leg.
# ---------------------------------------------------------------------------


def test_an_exact_five_cent_spread_is_not_a_float_coin_flip():
    """0.33-0.28 = 0.050000000000000044; 0.38-0.33 = 0.04999999999999999.

    Same five cents, opposite verdicts against a 0.05 bar. 41% of the 95
    possible 5c spreads landed on the wrong side.
    """
    wrong = [(i / 100.0, (i + 5) / 100.0) for i in range(1, 96)
             if ((i + 5) / 100.0 - i / 100.0) > 0.05]
    assert wrong, "fixture assumption broken — no float error to guard against"
    for b, a in wrong:
        assert round(a - b, 9) <= 0.05, f"{b}/{a} still mis-compares"


def test_no_path_measures_spread_against_the_no_cost():
    """THE BUG. A NO buyer risks 1-yes_bid, not yes_ask.

    yes 0.15/0.20 is a 25% spread on the YES ask but 6% of the 0.85 NO cost.
    The old gate rejected hardest exactly where trading is cheapest.
    """
    with _paper_portfolio() as portfolio_module:
        p = portfolio_module.Portfolio(client=None)
        p.sync()
        _bid, _ask = 0.15, 0.20           # 5c: 25% of yes_ask, 5.9% of NO cost
        _tp = 0.05                        # ratio 3.0, net edge 0.10 — both fine
        p._fresh_quote = lambda tk, attempts=3: (_bid, _ask)
        reasons = []
        p._log_reject = lambda tk, why: reasons.append(why)
        p.buy_no({"ticker": "BTC-TEST-B60000", "signal": "BOUNDARY_NO",
                  "hours": 0.2, "type": "RANGE", "low": 59950, "high": 60050},
                 true_prob=_tp)
        assert "spread" not in " | ".join(reasons).lower(), (
            f"6% of capital at risk rejected as a 25% spread: {reasons}")


def test_a_genuinely_expensive_spread_on_the_no_leg_still_rejects():
    """PARITY. The fix must not disable the gate — only aim it correctly."""
    with _paper_portfolio() as portfolio_module:
        p = portfolio_module.Portfolio(client=None)
        p.sync()
        # NO cost 0.30, spread 0.12 -> 40% of capital at risk. Genuinely bad.
        _bid, _ask = 0.70, 0.82
        p._fresh_quote = lambda tk, attempts=3: (_bid, _ask)
        reasons = []
        p._log_reject = lambda tk, why: reasons.append(why)
        ok = p.buy_no({"ticker": "BTC-TEST-B60000", "signal": "BOUNDARY_NO",
                       "hours": 0.2, "type": "RANGE", "low": 59950,
                       "high": 60050}, true_prob=0.05)
        assert ok is False
        assert "spread" in " | ".join(reasons).lower(), reasons


def test_ladder_keeps_a_row_that_is_cheap_on_either_leg():
    """The ladder feeds both lanes, so one leg's economics must not evict a row."""
    src = open("kalshi_btc_bot/ladder.py").read()
    body = src.split("spread = ya - yb")[1][:1200]
    assert "min(_yes_pct, _no_pct)" in body, (
        "ladder still filters on the YES leg alone — with ENABLE_YES off that "
        "drops rows the only live lane would happily trade")
    assert "round(" in body, "ladder spread compare must be cent-precise"


def test_paper_buy_no_records_pre_fill_order_values():
    """Paper BUY_NO records attempted size, depth, and actual partial fill."""
    with _paper_portfolio() as portfolio_module:
        p = portfolio_module.Portfolio(client=None)
        p.sync()
        p._fresh_quote = lambda tk, attempts=3: (0.62, 0.64)
        yes_levels = [[62, "3"], [61, "10"]]
        p._orderbook = lambda tk: {"yes": yes_levels, "no": []}
        p._log_trade = lambda *args, **kwargs: None

        calls = []
        original = portfolio_module.recorder.record_order
        portfolio_module.recorder.record_order = lambda *args, **kwargs: calls.append(
            (args, kwargs)
        )
        try:
            contract = {
                "ticker": "BTC-TEST-B64650", "signal": "BOUNDARY_NO",
                "hours": 0.5, "type": "RANGE", "low": 64600,
                "high": 64700, "itm": False, "otm_dist": -50,
            }
            wanted = int(p.budget(C.NO_TRADE_PCT) / 0.38)
            assert wanted > 3, "fixture must want more than the 3 lots of depth"
            _tp = 0.34            # 0.62 / 0.34 = 1.82x, clears the ratio bar
            assert 0.62 / _tp >= C.BOUNDARY_NO_OVERPRICING_MIN
            assert (1 - _tp) - 0.38 >= C.BOUNDARY_NO_MIN_NET_EDGE
            assert p.buy_no(contract, true_prob=_tp) is True
        finally:
            portfolio_module.recorder.record_order = original

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == (
        "buy", "BTC-TEST-B64650", "no", 0.62, 0.64,
        {"yes": yes_levels}, 0.38, wanted, 3, 0.38,
    )
    assert kwargs == {"reason": "BOUNDARY_NO", "true_prob": 0.34}


def test_urgent_paper_exit_credits_depth_fill_not_unfilled_quote():
    """An urgent exit cannot credit every contract at a size-less top quote."""
    with _paper_portfolio() as portfolio_module:
        p = portfolio_module.Portfolio(client=None)
        p.real_cash = 0.0
        p.positions = {
            "BTC-TEST-B64650": {
                "count": 14, "entry": 0.68, "cost": 9.52,
                "peak": 0.68, "peak_bid": 0.68, "is_no": True,
                "contract": {"ticker": "BTC-TEST-B64650"},
            }
        }
        p._fresh_quote = lambda tk, attempts=1: (0.59, 0.60)
        p._orderbook = lambda tk: {"yes": [], "no": [[30, "14"]]}
        p._log_trade = lambda *args, **kwargs: None

        recorded = []
        original = portfolio_module.recorder.record_order
        portfolio_module.recorder.record_order = lambda *args, **kwargs: recorded.append(
            (args, kwargs)
        )
        try:
            assert p.sell("BTC-TEST-B64650", 0.39,
                          reason="misprice_failed") is True
        finally:
            portfolio_module.recorder.record_order = original

    # 14 contracts against a single 30c level = $4.20, NOT 14 x the 0.39 quote
    # ($5.46) and not 14 x the 0.59 fresh top ($8.26). The quote carries no size.
    # Less the exit taker fee — an early exit is a taker order and pays one
    # (settlement would not). Fee accounting was reinstated 2026-08-22.
    from kalshi_btc_bot.fees import taker_fee
    fee = taker_fee(14, 0.30) if C.CHARGE_FEES else 0.0
    assert fee > 0, "fee accounting should be on by default"
    assert abs(p.real_cash - (4.20 - fee)) < 1e-9, p.real_cash
    assert abs(p.realized_pnl - ((0.30 - 0.68) * 14)) < 1e-9
    assert recorded[0][0][9] == 0.30  # record_order fill_px


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
