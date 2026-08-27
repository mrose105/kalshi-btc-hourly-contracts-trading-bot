"""The shadow recorder must never be able to affect trading.

It exists to answer the EXECUTION questions `universe` cannot: is the quote
still there a moment later, is there depth at size, what fraction of candidates
are fillable. Those need a fresh quote and a book fetch, which is API load
inside a 2-second loop — so the caps matter, and so does the guarantee that a
failure here is invisible to the path that places orders.

As of 2026-08-26 the entire measured estimate of decision-to-execution slippage
rests on ONE observation. That is what this is for.
"""
import sys
sys.path.insert(0, ".")

from kalshi_btc_bot import config as C
from kalshi_btc_bot.shadow import ShadowRecorder


class _Dist:
    def __init__(self, tp=0.05):
        self.tp = tp

    def posterior_prob(self, *a, **k):
        return {"true_prob": self.tp, "prior_prob": self.tp,
                "market_prob": None, "market_weight": 0.0}


class _Port:
    def __init__(self, boom=False):
        self.positions = {}
        self.quotes = 0
        self.books = 0
        self.boom = boom

    def _fresh_quote(self, tk, attempts=3):
        self.quotes += 1
        if self.boom:
            raise RuntimeError("network died")
        return (0.20, 0.22)

    def _orderbook(self, tk):
        self.books += 1
        return {"yes": [[20, 500], [19, 300]]}

    @staticmethod
    def _walk_book(levels, qty, transform=None, **k):
        return qty, 0.80


SPOT = 79000.0
# z>0 fades bands ABOVE spot (requires spot < lo); z<0 fades bands BELOW
# (requires spot >= hi). Getting that backwards is how a first draft of this
# file "proved" the recorder was broken when the fixture was.
REG = {"regime": "RANGING", "direction": "NEUTRAL", "zscore": +1.90,
       "vol": 0.0001, "mom": 0.0}


def _row(tk="KXBTC-T-B79150", hours=0.60):
    # band ABOVE spot, z>0 -> the valid combination
    return {"ticker": tk, "ask": 0.20, "bid": 0.18, "hours": hours,
            "type": "RANGE", "low": 79100.0, "high": 79200.0, "vol": 900}


def _rec(**over):
    r = ShadowRecorder()
    return r


def test_records_a_candidate_the_live_gates_would_reject():
    """THE POINT. 0.60h is outside the 15-min live window but inside shadow's."""
    from kalshi_btc_bot import recorder
    old = recorder.ENABLED
    recorder.ENABLED = True
    seen = []
    old_fn = recorder.record_shadow
    recorder.record_shadow = lambda **kw: seen.append(kw)
    try:
        p = _Port()
        n = _rec().scan(p, _Dist(0.05), SPOT, 0.0001, REG, [_row(hours=0.60)])
        assert n == 1, "a candidate outside the live window was not recorded"
        assert len(seen) == 1
        rec = seen[0]
        assert rec["decision"]["no_cost"] == 0.82
        assert rec["fresh"] == (0.20, 0.22), "must capture the FRESH quote"
        assert rec["book"], "must capture the book"
        assert rec["would_fill"]["filled"] == 11
    finally:
        recorder.record_shadow = old_fn
        recorder.ENABLED = old


def test_it_never_trades():
    """No order path may be reachable from here."""
    src = open("kalshi_btc_bot/shadow.py").read()
    for banned in ("buy_no", "buy(", "sell(", "place_order", "_log_trade"):
        assert banned not in src, f"shadow.py references {banned!r}"


def test_a_throwing_portfolio_cannot_break_the_scan():
    """Instrumentation must never take down trading."""
    p = _Port(boom=True)
    n = _rec().scan(p, _Dist(0.05), SPOT, 0.0001, REG, [_row()])
    assert n == 0


def test_a_throwing_pricer_cannot_break_the_scan():
    class _Boom:
        def posterior_prob(self, *a, **k):
            raise RuntimeError("model blew up")
    n = _rec().scan(_Port(), _Boom(), SPOT, 0.0001, REG, [_row()])
    assert n == 0


def test_per_scan_cap_is_enforced():
    from kalshi_btc_bot import recorder
    old, recorder.ENABLED = recorder.ENABLED, True
    old_fn = recorder.record_shadow
    recorder.record_shadow = lambda **kw: None
    try:
        p = _Port()
        rows = [_row(f"KXBTC-T-B{79150+i}") for i in range(10)]
        n = _rec().scan(p, _Dist(0.05), SPOT, 0.0001, REG, rows)
        assert n <= C.SHADOW_MAX_PER_SCAN, f"recorded {n}, cap is {C.SHADOW_MAX_PER_SCAN}"
        assert p.quotes <= C.SHADOW_MAX_PER_SCAN, "one fresh quote per recorded row, max"
        assert p.books <= C.SHADOW_MAX_PER_SCAN
    finally:
        recorder.record_shadow = old_fn
        recorder.ENABLED = old


def test_the_same_ticker_is_not_resampled_within_the_cooldown():
    from kalshi_btc_bot import recorder
    old, recorder.ENABLED = recorder.ENABLED, True
    old_fn = recorder.record_shadow
    recorder.record_shadow = lambda **kw: None
    try:
        p, r = _Port(), ShadowRecorder()
        assert r.scan(p, _Dist(0.05), SPOT, 0.0001, REG, [_row()]) == 1
        assert r.scan(p, _Dist(0.05), SPOT, 0.0001, REG, [_row()]) == 0, (
            "same ticker resampled immediately — that is unbounded API load")
    finally:
        recorder.record_shadow = old_fn
        recorder.ENABLED = old


def test_disabled_costs_nothing():
    old = C.SHADOW_ENABLED
    C.SHADOW_ENABLED = False
    try:
        p = _Port()
        assert _rec().scan(p, _Dist(0.05), SPOT, 0.0001, REG, [_row()]) == 0
        assert p.quotes == 0 and p.books == 0, "disabled must make zero API calls"
    finally:
        C.SHADOW_ENABLED = old


def test_it_skips_contracts_already_held():
    p = _Port()
    p.positions["KXBTC-T-B79150"] = {"contract": {}}
    assert _rec().scan(p, _Dist(0.05), SPOT, 0.0001, REG, [_row()]) == 0


def test_it_respects_the_regime_and_side_rules():
    """Shadow loosens thresholds, not the strategy's logic."""
    p = _Port()
    # trending regime — BOUNDARY_NO never fires there, shadow must not either
    assert _rec().scan(p, _Dist(0.05), SPOT, 0.0001,
                       dict(REG, regime="TRENDING"), [_row()]) == 0
    # z>0 fades bands ABOVE spot; a band BELOW spot is the wrong side
    below = dict(_row(), low=78800.0, high=78900.0)
    assert _rec().scan(p, _Dist(0.05), SPOT, 0.0001, REG, [below]) == 0


def test_config_is_read_at_call_time():
    src = open("kalshi_btc_bot/shadow.py").read()
    assert "from . import config as _C" in src
    assert "from .config import" not in src, "frozen import — see test_frozen_config.py"


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
