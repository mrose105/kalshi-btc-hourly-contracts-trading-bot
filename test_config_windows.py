"""Regime windows must live in config, not as literals in regime.py.

`feed.zscore(300)` was hardcoded at regime.py:30 while MOMENTUM_WINDOW_SECS
next to it was configurable. A sweep against a literal silently does nothing —
the exact frozen-import failure this repo has already paid for, where seven
values of NO_OVERPRICING_MIN produced byte-identical trades because signals.py
had bound a name-local snapshot at import time.

Moving it changed no behaviour: the default is the same 300s.
"""
import sys
sys.path.insert(0, ".")

from kalshi_btc_bot import config as C

SRC = open("kalshi_btc_bot/regime.py").read()


def _code():
    return "\n".join(l for l in SRC.split("\n")
                      if not l.strip().startswith("#"))


def test_the_zscore_window_is_not_a_literal():
    """THE BUG. A hardcoded window cannot be swept."""
    body = _code()
    assert "feed.zscore(300)" not in body, (
        "zscore window is still hardcoded — a sweep would silently no-op")
    assert "feed.zscore(_C.ZSCORE_WINDOW_SECS)" in body


def test_it_is_read_module_qualified():
    """from .config import X binds a snapshot; sweeps mutate the module."""
    assert "_C.ZSCORE_WINDOW_SECS" in _code()
    assert "from .config import ZSCORE_WINDOW_SECS" not in SRC


def test_the_default_preserves_behaviour():
    """The move must be a no-op. 300 was the shipped value."""
    assert C.ZSCORE_WINDOW_SECS == 300


def test_it_is_a_separate_window_from_momentum():
    """mom and z are measured over different spans — that is deliberate."""
    assert C.MOMENTUM_WINDOW_SECS == 600
    assert C.ZSCORE_WINDOW_SECS != C.MOMENTUM_WINDOW_SECS


def test_a_sweep_actually_reaches_the_call():
    """End to end: mutating config must change what zscore() receives."""
    from kalshi_btc_bot import regime as R
    seen = []

    class _Feed:
        last = 78000.0
        def zscore(self, secs):
            seen.append(secs); return 0.0
        def momentum(self, secs=60): return 0.0
        def ewma_volatility(self, *a, **k): return 0.0001
        def volatility(self, *a, **k): return 0.0001
        def consecutive(self): return (0, 0)
        def recent(self, *a, **k): return []

    old = C.ZSCORE_WINDOW_SECS
    try:
        C.ZSCORE_WINDOW_SECS = 123
        try:
            R.RegimeEngine().detect(_Feed())
        except Exception:
            pass          # only the argument matters here
        assert 123 in seen, (
            f"config change never reached zscore(); saw {seen}")
    finally:
        C.ZSCORE_WINDOW_SECS = old


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
