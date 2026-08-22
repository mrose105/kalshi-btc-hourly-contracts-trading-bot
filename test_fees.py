"""Kalshi taker fees — arithmetic, and that they reach paper P&L.

Fee accounting was written 2026-08-18 and deleted on 08-19 because it had been
entangled with a mistaken $0.38 entry-price cap. The cap was wrong; the
arithmetic was not. While it was missing, every backtest, replay and paper P&L
overstated results by roughly 1.8-2.5% of deployed capital.
"""
import math
import sys
sys.path.insert(0, ".")
from kalshi_btc_bot import config as C
from kalshi_btc_bot.fees import taker_fee, fee_pct_of_notional


class _Fees:
    def __init__(self, on):
        self.on = on

    def __enter__(self):
        self._old = C.CHARGE_FEES
        C.CHARGE_FEES = self.on
        return self

    def __exit__(self, *a):
        C.CHARGE_FEES = self._old


def test_formula_matches_kalshi_general_taker_fee():
    """ceil(rate * multiplier * count * price * (1-price)), to the cent."""
    for count, price in ((14, 0.70), (1, 0.05), (333, 0.85), (7, 0.50)):
        expect = math.ceil(0.07 * 1.0 * count * price * (1 - price) * 100) / 100
        assert taker_fee(count, price, rate=0.07, multiplier=1.0) == expect


def test_fee_peaks_at_fifty_cents():
    """price*(1-price) is maximised at 0.50 — the fee is a dome, not linear."""
    mid = taker_fee(100, 0.50)
    assert mid > taker_fee(100, 0.20)
    assert mid > taker_fee(100, 0.80)
    assert taker_fee(100, 0.30) == taker_fee(100, 0.70), "must be symmetric"


def test_always_rounds_up_never_down():
    """A fee that rounds down would understate cost. Sub-cent must become 1c."""
    raw = 0.07 * 1 * 0.02 * 0.98          # $0.001372 -> must become $0.01
    assert 0 < raw < 0.01, raw
    assert taker_fee(1, 0.02) == 0.01
    # and a value just over a cent must not round back down to it
    assert taker_fee(1, 0.20) == 0.02     # raw $0.0112


def test_degenerate_inputs_are_free_not_negative():
    for count, price in ((0, 0.70), (-5, 0.70), (14, 0.0), (14, 1.0),
                         (14, -0.1), (14, 1.5)):
        assert taker_fee(count, price) == 0.0, (count, price)
    assert fee_pct_of_notional(0, 0.70) == 0.0


def test_small_orders_are_disproportionately_expensive():
    """The ceil dominates at 1 lot — measured 2.70% median vs 1.83% at 14."""
    assert fee_pct_of_notional(1, 0.70) > fee_pct_of_notional(14, 0.70)


def test_measured_drag_on_a_realistic_no_entry():
    """14 lots at 70c: the live book's typical size and price."""
    pct = fee_pct_of_notional(14, 0.70)
    assert 0.015 < pct < 0.030, f"expected ~2%, got {pct:.2%}"


def test_paper_entry_charges_the_fee():
    from kalshi_btc_bot import portfolio as pm
    old_paper, old_cfg = pm.PAPER_TRADING, C.PAPER_TRADING
    pm.PAPER_TRADING = C.PAPER_TRADING = True
    try:
        with _Fees(True):
            p = pm.Portfolio(client=None)
            p.sync()
            p._fresh_quote = lambda tk, attempts=3: (0.62, 0.64)
            p._orderbook = lambda tk: {"yes": [[62, "500"]], "no": []}
            p._log_trade = lambda *a, **k: None
            cash0 = p.real_cash
            contract = {"ticker": "BTC-TEST-B64650", "signal": "BOUNDARY_NO",
                        "hours": 0.5, "type": "RANGE", "low": 64600,
                        "high": 64700, "itm": False, "otm_dist": -50}
            # 0.62 / 0.34 = 1.82x clears BOUNDARY_NO_OVERPRICING_MIN
            assert p.buy_no(contract, true_prob=0.34) is True
            pos = p.positions["BTC-TEST-B64650"]
            spent = cash0 - p.real_cash
            expected_fee = taker_fee(pos["count"], pos["entry"])
            assert expected_fee > 0
            assert abs(spent - (pos["cost"] + expected_fee)) < 1e-9, (
                f"spent {spent}, cost {pos['cost']}, fee {expected_fee}")
    finally:
        pm.PAPER_TRADING, C.PAPER_TRADING = old_paper, old_cfg


def test_charge_fees_false_restores_old_accounting():
    """PARITY: the flag off must reproduce the historical fee-free behaviour."""
    from kalshi_btc_bot import portfolio as pm
    old_paper, old_cfg = pm.PAPER_TRADING, C.PAPER_TRADING
    pm.PAPER_TRADING = C.PAPER_TRADING = True
    try:
        with _Fees(False):
            p = pm.Portfolio(client=None)
            p.sync()
            p._fresh_quote = lambda tk, attempts=3: (0.62, 0.64)
            p._orderbook = lambda tk: {"yes": [[62, "500"]], "no": []}
            p._log_trade = lambda *a, **k: None
            cash0 = p.real_cash
            contract = {"ticker": "BTC-TEST-B64650", "signal": "BOUNDARY_NO",
                        "hours": 0.5, "type": "RANGE", "low": 64600,
                        "high": 64700, "itm": False, "otm_dist": -50}
            assert p.buy_no(contract, true_prob=0.34) is True
            spent = cash0 - p.real_cash
            assert abs(spent - p.positions["BTC-TEST-B64650"]["cost"]) < 1e-9
    finally:
        pm.PAPER_TRADING, C.PAPER_TRADING = old_paper, old_cfg


def test_settlement_is_free():
    """Kalshi does not charge on expiry — settle must not route through sell()."""
    src = open("kalshi_btc_bot/portfolio.py").read()
    body = src.split("def settle_paper_position")[1].split("def ")[0]
    assert "taker_fee" not in body, "settlement must not charge a fee"
    assert "self.real_cash += proceeds" in body


def test_config_is_read_module_qualified():
    src = open("kalshi_btc_bot/portfolio.py").read()
    assert "_C.CHARGE_FEES" in src
    assert "from .config import CHARGE_FEES" not in src


def test_shipped_defaults():
    assert C.CHARGE_FEES is True
    assert C.KALSHI_TAKER_FEE_RATE == 0.07
    assert C.KALSHI_FEE_MULTIPLIER == 1.0


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
