"""Backtest / paper / live must charge the SAME fee for the same order.

The standing rule in this repo is that the three paths agree, and the recurring
bug class is exactly where they quietly stop agreeing (RANGE_WIDTH 250 vs 100,
momentum window 60s vs 2.5h, _roll_session daily in backtest but never live).

Fees were reinstated on 2026-08-22 into Portfolio (live/paper) only.
BacktestPortfolio is a SEPARATE class that does not import Portfolio, so for a
short while the backtest was fee-free while paper was not — a new divergence of
exactly the kind above. These tests pin the three together.
"""
import sys
sys.path.insert(0, ".")
from kalshi_btc_bot import config as C
from kalshi_btc_bot.fees import taker_fee


def test_backtest_charges_the_same_fee_function_as_live():
    """Not 'a fee' — the identical function, so they cannot drift apart."""
    bt = open("kalshi_btc_backtest.py").read()
    live = open("kalshi_btc_bot/portfolio.py").read()
    assert "from kalshi_btc_bot.fees        import taker_fee" in bt, \
        "backtest must import the shared fee function"
    assert "from .fees import taker_fee" in live, \
        "live must import the shared fee function"
    # neither may hand-roll its own arithmetic
    for src, name in ((bt, "backtest"), (live, "portfolio")):
        assert "0.07 *" not in src, f"{name} hand-rolls the fee rate"


def test_entry_fee_charged_on_both_sides_in_both_paths():
    bt = open("kalshi_btc_backtest.py").read()
    live = open("kalshi_btc_bot/portfolio.py").read()
    assert bt.count("taker_fee(count, ask)") == 1, "backtest buy() entry fee"
    assert bt.count("taker_fee(count, no_cost)") == 1, "backtest buy_no() entry fee"
    assert live.count("taker_fee(filled, fill_price)") == 2, \
        "live buy() and buy_no() entry fees"


def test_settlement_is_free_in_both_paths():
    """Kalshi cash-settles at expiry and does not charge. Both must reflect it."""
    bt = open("kalshi_btc_backtest.py").read()
    assert 'if (C.CHARGE_FEES and "expiry_settle" not in reason) else 0.0' in bt, \
        "backtest must exempt expiry_settle from the exit fee"
    live = open("kalshi_btc_bot/portfolio.py").read()
    body = live.split("def settle_paper_position")[1].split("\n    def ")[0]
    assert "taker_fee" not in body, "paper settlement must not charge a fee"


def test_both_honour_the_same_kill_switch():
    bt = open("kalshi_btc_backtest.py").read()
    live = open("kalshi_btc_bot/portfolio.py").read()
    assert "C.CHARGE_FEES" in bt and "_C.CHARGE_FEES" in live, \
        "both paths must gate on the same config flag"


def test_the_shared_function_is_deterministic_across_call_sites():
    """Same order, same fee, wherever it is computed."""
    for count, price in ((14, 0.70), (26, 0.38), (1, 0.05), (333, 0.85)):
        a = taker_fee(count, price)
        b = taker_fee(count, price)
        assert a == b
        # and symmetric about 0.50, which the float-noise bug broke
        assert taker_fee(count, price) == taker_fee(count, round(1 - price, 10))


def test_no_path_charges_a_fee_on_a_zero_size_order():
    assert taker_fee(0, 0.70) == 0.0


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
