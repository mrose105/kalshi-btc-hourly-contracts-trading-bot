"""/markets is paginated, and a truncated page is silent.

Kalshi caps the response at `limit` and returns a `cursor` when more exists.
Nothing checked that cursor. At limit=200 the response was cut in half: ONE
hourly window is up to 188 markets, so the closing window received exactly the
leftover 12 slots (200 - 188), and only in 2% of polls.

THAT is what defeated the expiring-window fix in 3b8459a. The ladder correctly
ASKED for the closing window; the transport could not carry it. Measured over
2026-08-28/29: windows went dark a median of 303s before close, against
MIN_HOURS = 300s — which is why it looked like the same [MIN_HOURS, MAX_HOURS]
bug rather than a truncated HTTP response.

Measured 2026-08-29 against the live API:
    limit=200   -> 200 rows, 2 windows, cursor PRESENT  (truncated)
    limit=1000  -> 318 rows, 3 windows, no cursor       (complete)
"""
import sys
sys.path.insert(0, ".")

SRC = open("kalshi_btc_bot/ladder.py").read()


class _Client:
    """Fake Kalshi that truncates at `limit` and hands back a cursor."""

    def __init__(self, total, page_cap):
        self.markets = [{"ticker": f"KXBTC-26AUG2922-B{i}"} for i in range(total)]
        self.page_cap = page_cap
        self.calls = []

    def _request(self, method, endpoint, params=None, timeout=None):
        params = params or {}
        self.calls.append(dict(params))
        start = int(params.get("cursor") or 0)
        n = min(int(params.get("limit", 100)), self.page_cap)
        page = self.markets[start:start + n]
        nxt = start + len(page)
        out = {"markets": page}
        if nxt < len(self.markets):
            out["cursor"] = str(nxt)
        return out


def _ladder(client):
    from kalshi_btc_bot.ladder import Ladder
    lb = Ladder.__new__(Ladder)
    lb.client = client
    return lb


def test_the_bug_a_single_page_silently_dropped_the_closing_window():
    """188 + 80 markets across two windows must not come back as 200."""
    c = _Client(total=268, page_cap=200)
    got = _ladder(c)._fetch_series("KXBTC")
    assert len(got) == 268, (
        f"got {len(got)} of 268 — the closing window is still being truncated")


def test_it_follows_the_cursor_until_exhausted():
    c = _Client(total=1400, page_cap=500)
    got = _ladder(c)._fetch_series("KXBTC")
    assert len(got) == 1400, len(got)
    assert len(c.calls) == 3, f"expected 3 pages, made {len(c.calls)}"


def test_no_cursor_means_one_call():
    """PARITY: the common case must not cost extra requests inside a 2s cycle."""
    c = _Client(total=318, page_cap=1000)
    got = _ladder(c)._fetch_series("KXBTC")
    assert len(got) == 318
    assert len(c.calls) == 1, f"made {len(c.calls)} calls for a complete page"


def test_pagination_is_bounded():
    """A runaway cursor must not stall the trading loop."""
    from kalshi_btc_bot.ladder import Ladder
    c = _Client(total=10_000_000, page_cap=10)
    got = _ladder(c)._fetch_series("KXBTC")
    assert len(c.calls) <= Ladder._MAX_PAGES, len(c.calls)
    assert got, "must still return what it managed to fetch"


def test_limit_is_high_enough_for_every_open_window():
    """318 markets across 3 windows was the live total on 2026-08-29."""
    import re
    m = re.search(r'params = \{"limit": (\d+)', SRC)
    assert m, "limit not found in _fetch_series"
    assert int(m.group(1)) >= 500, (
        f"limit={m.group(1)} — one window alone is 188 markets and all open "
        f"windows totalled 318; anything under ~400 truncates again")


def test_the_cursor_is_actually_read():
    """The regression that hid for weeks was an unread cursor field."""
    body = SRC.split("def _fetch_series")[1].split("\n    def ")[0]
    assert 'data.get("cursor")' in body, "cursor still ignored"
    assert 'params["cursor"]' in body, "cursor fetched but never sent back"


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
