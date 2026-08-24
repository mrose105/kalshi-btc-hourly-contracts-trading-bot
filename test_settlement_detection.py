"""A CLOSED market is not a SETTLED market.

On Kalshi, `closed` means trading has stopped; the outcome is determined later.
Treating it as settled made the bot book an outcome from is_in_money(spot) at
the moment it noticed the close, using OUR spot instead of Kalshi's settlement
value.

Observed 2026-08-23: KXBTC-26AUG2323-B77250, close_time 23:00:00, still quoting
0.17/0.20 at 22:50:14, booked settled at 22:50:20 and credited the full $1.00
(+$1.87). Spot $77,359 against a band of [77,200, 77,300) — $59 outside with
ten minutes left. A coin flip recorded as a certainty.

`expired_settled` is 29 exits and -$1,187.51 across the paper history, a bigger
loss than the whole book, so how that branch decides matters.
"""
import re
import sys
sys.path.insert(0, ".")

SRC = open("kalshi_btc_bot/positions.py").read()


def _settled_set():
    m = re.search(r'_SETTLED = \{([^}]*)\}', SRC)
    assert m, "_SETTLED not found"
    return {x.strip().strip('"\'') for x in m.group(1).split(",") if x.strip()}


def test_closed_is_not_treated_as_settled():
    """THE BUG. `closed` must not short-circuit into settlement."""
    assert "closed" not in _settled_set(), (
        "a closed market has stopped trading but has NOT determined its "
        "outcome — booking it settles from our spot at an arbitrary moment")


def test_the_genuinely_settled_states_are_still_honoured():
    s = _settled_set()
    for good in ("settled", "determined", "finalized"):
        assert good in s, f"{good} must still settle the position"


def test_the_expired_fallback_survives():
    """Without it, a position whose status never updates would never close."""
    assert "_expired = _hours_from(close_time) < -0.05" in SRC
    assert "bid == 0 and ask == 0" in SRC, (
        "the fallback must require BOTH sides to have stopped quoting — a "
        "one-sided book near expiry is normal and still tradeable")


def test_the_fallback_requires_close_time_to_have_passed():
    """-0.05h past close_time. A live market must never hit this branch."""
    m = re.search(r'_expired = _hours_from\(close_time\) < (-?[\d.]+)', SRC)
    assert m, "expired guard not found"
    assert float(m.group(1)) < 0, (
        "the threshold must be NEGATIVE — i.e. close_time already passed. A "
        "positive value would settle positions that still have time left")


def test_settlement_branch_reads_both_conditions():
    assert "if status in _SETTLED or _expired:" in SRC


def test_the_2026_08_23_case_would_no_longer_settle_early():
    """Reproduce the live conditions: closed status, 10 minutes still to run."""
    settled = _settled_set()
    status = "closed"
    hours_to_close = 10 / 60.0        # ten minutes left
    bid, ask = 0.17, 0.20             # still quoting on both sides
    expired = hours_to_close < -0.05 and bid == 0 and ask == 0
    assert not (status in settled or expired), (
        "the 2026-08-23 position would still be booked early")


def test_a_genuinely_expired_market_still_closes_out():
    """The opposite failure: a position that can never be exited."""
    settled = _settled_set()
    status = ""                        # status never updated
    hours_to_close = -0.20             # twelve minutes past close
    bid, ask = 0.0, 0.0                # both sides gone
    expired = hours_to_close < -0.05 and bid == 0 and ask == 0
    assert status in settled or expired, "an expired position must close out"


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
