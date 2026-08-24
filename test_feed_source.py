"""BTCFeed price source — the exchange ticker, with a fallback that must work.

The bot trades TIMING against Kalshi, which lags spot by ~20s. It was reading
`api.coinbase.com/v2/prices/BTC-USD/spot`, a cached retail endpoint that itself
lags Coinbase's exchange feed by ~10s (corr +0.739). Half the head start was
being given away before the strategy saw a price.

These tests are offline — no network — because a feed test that needs the
network fails for reasons unrelated to the code.
"""
import sys
sys.path.insert(0, ".")
from kalshi_btc_bot.feed import BTCFeed


class _Resp:
    def __init__(self, payload, boom=False):
        self._p, self._boom = payload, boom

    def json(self):
        if self._boom:
            raise ValueError("bad json")
        return self._p


def _patch(monkey):
    """Swap requests.get inside feed.py for `monkey`. Returns the original."""
    from kalshi_btc_bot import feed as fm
    old = fm.requests.get
    fm.requests.get = monkey
    return old


def _restore(old):
    from kalshi_btc_bot import feed as fm
    fm.requests.get = old


def test_primary_is_the_exchange_ticker_not_the_retail_endpoint():
    assert "api.exchange.coinbase.com" in BTCFeed._TICKER
    assert "/products/BTC-USD/ticker" in BTCFeed._TICKER
    assert "v2/prices" not in BTCFeed._TICKER
    assert "v2/prices" in BTCFeed._FALLBACK, "fallback must be the old endpoint"


def test_reads_price_from_the_exchange_payload():
    seen = {}
    def fake(url, **kw):
        seen["url"] = url
        return _Resp({"price": "77123.45", "bid": "77123.0", "ask": "77124.0"})
    old = _patch(fake)
    try:
        f = BTCFeed()
        assert abs(f.fetch() - 77123.45) < 1e-9
        assert seen["url"] == BTCFeed._TICKER
        assert abs(f.last - 77123.45) < 1e-9
        assert len(f.prices) == 1
    finally:
        _restore(old)


def test_falls_back_when_the_exchange_feed_fails():
    """A feed change must never be able to stop the bot."""
    calls = []
    def fake(url, **kw):
        calls.append(url)
        if url == BTCFeed._TICKER:
            raise ConnectionError("exchange down")
        return _Resp({"data": {"amount": "76000.00"}})
    old = _patch(fake)
    try:
        f = BTCFeed()
        assert abs(f.fetch() - 76000.0) < 1e-9
        assert calls == [BTCFeed._TICKER, BTCFeed._FALLBACK]
    finally:
        _restore(old)


def test_both_sources_down_returns_last_and_records_nothing():
    def fake(url, **kw):
        raise ConnectionError("everything is down")
    old = _patch(fake)
    try:
        f = BTCFeed()
        f.last = 12345.0
        assert f.fetch() == 12345.0
        assert len(f.prices) == 0, "a failed fetch must not append a tick"
    finally:
        _restore(old)


def test_malformed_payload_falls_back_rather_than_crashing():
    def fake(url, **kw):
        if url == BTCFeed._TICKER:
            return _Resp({"no_price_field": 1})
        return _Resp({"data": {"amount": "75000.00"}})
    old = _patch(fake)
    try:
        f = BTCFeed()
        assert abs(f.fetch() - 75000.0) < 1e-9
    finally:
        _restore(old)


def test_nonsense_prices_are_rejected():
    """A zero or negative price would poison vol, regime and every gate."""
    for bad in ("0", "0.0", "-1"):
        def make(b):
            return lambda url, **kw: _Resp({"price": b})
        fake = make(bad)
        old = _patch(fake)
        try:
            f = BTCFeed()
            f.last = 70000.0
            assert f.fetch() == 70000.0, bad
            assert len(f.prices) == 0, bad
        finally:
            _restore(old)


def test_tick_history_stays_bounded():
    def fake(url, **kw):
        return _Resp({"price": "77000.00"})
    old = _patch(fake)
    try:
        f = BTCFeed()
        for _ in range(520):
            f.fetch()
        assert len(f.prices) == 500, len(f.prices)
    finally:
        _restore(old)


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
