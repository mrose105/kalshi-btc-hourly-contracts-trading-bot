"""Kalshi trading fees.

Reinstated 2026-08-22. Fee accounting was written on 2026-08-18 as part of the
"60/40" work, then deleted wholesale when that policy was reverted — because
the fee arithmetic had been entangled with a $0.38 entry-price cap that was a
misreading of the request. The cap was wrong; the fee arithmetic never was, and
it should not have gone out with it.

Consequence of it being absent: every backtest, replay and paper P&L in this
repo has been overstating results by roughly 1.8-2.5% of deployed capital. At
the strategy's measured -4.1% ROC that is not a rounding error — it is more
than half the gap again.

FORMULA. Kalshi's general taker fee, per order:

    fee = ceil(rate * multiplier * count * price * (1 - price))

rounded UP to the whole cent, where `price` is the price of the contract being
bought, in dollars. The price*(1-price) term means the fee peaks at 50c and
falls toward either end — a 70c NO costs about 1.5c/contract, a 5c longshot
about 0.3c.

Measured against the 39 settlement-resolved NO entries at a realistic 14-lot
size: median 1.83% of capital deployed, p90 2.76%. At 1 lot the rounding-up
dominates and it is 2.70% median, which is why small orders are
disproportionately expensive.

SETTLEMENT IS FREE. Kalshi does not charge on expiry, so a contract held to
settlement pays the fee once, on entry. Anything exited early — a stop,
edge_gone, a profit capture — pays a second time on the way out.
"""
import math

from . import config as _C


def taker_fee(count: int, price: float,
              rate: float | None = None,
              multiplier: float | None = None) -> float:
    """Dollar fee for taking `count` contracts at `price` dollars each.

    Returns 0.0 for a non-positive size, and for a price at or outside [0, 1]
    where the quadratic term is meaningless rather than merely small.
    """
    if count <= 0 or not (0.0 < price < 1.0):
        return 0.0
    rate = _C.KALSHI_TAKER_FEE_RATE if rate is None else rate
    multiplier = (_C.KALSHI_FEE_MULTIPLIER if multiplier is None else multiplier)
    raw_cents = rate * multiplier * count * price * (1.0 - price) * 100.0
    # Round to 9dp BEFORE the ceil. price*(1-price) is symmetric about 0.50 in
    # exact arithmetic but not in binary floating point: at price=0.70,
    # (1 - 0.70) evaluates to 0.30000000000000004, so the product lands at
    # 147.00000000000003 cents where price=0.30 gives exactly 147.0 — and the
    # ceil turns 3e-14 of noise into a whole extra cent. Without this the fee
    # is asymmetric across the 50c line for economically identical orders.
    return math.ceil(round(raw_cents, 9)) / 100.0


def fee_pct_of_notional(count: int, price: float) -> float:
    """Fee as a fraction of the capital the order deploys. 0.0 if size is 0."""
    notional = count * price
    if notional <= 0:
        return 0.0
    return taker_fee(count, price) / notional
