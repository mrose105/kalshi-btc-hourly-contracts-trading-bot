"""Live/backtest parity: buy_no() must re-check BOUNDARY_NO_MIN_NET_EDGE.

find_boundary_no() (signals.py:352) selects on an ABSOLUTE net edge, and the
synthetic fill path re-checks it at the next bar's open
(kalshi_btc_backtest.py:1239). Portfolio.buy_no() re-checked only the
OVERPRICING RATIO, so live could fill a trade the backtest would reject.

The two gates are not redundant. With no_cost = 1 - bid:

    net_edge = (1 - true_p) - (1 - bid) = bid - true_p
    ratio    = bid / true_p
    =>  net_edge = true_p * (ratio - 1)

so at BOUNDARY_NO_OVERPRICING_MIN = 1.15, clearing net_edge >= 0.05 needs
true_p >= 0.333 — while BOUNDARY_NO deliberately targets OTM contracts far
below that. The ratio gate is therefore nearly vacuous for exactly the
contracts this signal trades.
"""
import sys
from contextlib import contextmanager
sys.path.insert(0, ".")
from kalshi_btc_bot import config as C


@contextmanager
def _paper_portfolio():
    from kalshi_btc_bot import portfolio as portfolio_module

    old_config = C.PAPER_TRADING
    old_module = portfolio_module.PAPER_TRADING
    C.PAPER_TRADING = True
    portfolio_module.PAPER_TRADING = True
    try:
        yield portfolio_module
    finally:
        C.PAPER_TRADING = old_config
        portfolio_module.PAPER_TRADING = old_module


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
    """The documented failure: ratio passes, absolute edge is a third of the bar."""
    bid, true_p = 0.115, 0.10          # scan saw bid 0.15; it decayed to 0.115
    ratio_ok, edge_ok, net_edge = _gates(bid, true_p)
    assert ratio_ok, "ratio gate should pass at 1.15x"
    assert not edge_ok, "net-edge gate should REJECT"
    assert abs(net_edge - 0.015) < 1e-9, net_edge


def test_deep_otm_is_the_dangerous_region():
    """As true_p falls, the ratio gate admits ever-smaller absolute edge."""
    for true_p in (0.02, 0.05, 0.10, 0.20):
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
        p._fresh_quote = lambda tk, attempts=3: (0.115, 0.14)
        assert 0.14 - 0.115 <= C.MAX_SPREAD and (0.14 - 0.115) / 0.14 <= C.MAX_SPREAD_PCT, \
            "fixture must clear the spread gate or the test proves nothing"
        reasons = []
        p._log_reject = lambda tk, why: reasons.append(why)
        contract = {"ticker": "BTC-TEST-B60000", "signal": "BOUNDARY_NO",
                    "hours": 0.5, "type": "RANGE", "low": 59950, "high": 60050}
        ok = p.buy_no(contract, true_prob=0.10)
        assert ok is False, "buy_no filled a trade with net_edge 0.015 < 0.05"
        joined = " | ".join(reasons).lower()
        assert "net edge" in joined, (
            f"rejected, but not by the net-edge gate — reasons were: {reasons}")


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
            assert p.buy_no(contract, true_prob=0.40) is True
        finally:
            portfolio_module.recorder.record_order = original

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == (
        "buy", "BTC-TEST-B64650", "no", 0.62, 0.64,
        {"yes": yes_levels}, 0.38, wanted, 3, 0.38,
    )
    assert kwargs == {"reason": "BOUNDARY_NO", "true_prob": 0.40}


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
    assert abs(p.real_cash - 4.20) < 1e-9, p.real_cash
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
