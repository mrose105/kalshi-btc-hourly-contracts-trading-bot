"""SPX index feed — S&P 500 spot for the KXINXU/KXINX ladders.

Everything about vol estimation, bar aggregation, momentum, z-score and regime
detection is instrument-agnostic and inherited from BTCFeed unchanged. Only two
things are BTC-specific and overridden here: where a live tick comes from, and
where bootstrap history comes from.

Why Yahoo's v8 chart endpoint and not the obvious alternatives (measured
2026-08-17 during market hours):

  * yfinance 5-minute bars for ^GSPC lag 3.2 MINUTES. Unusable as a tick source
    for a bot that polls every 2s and enters contracts with MIN_HOURS=0.10 (6
    min) left — the spot would be half the contract's remaining life out of date.
  * Alpaca has no index data at all: /v1beta1/indices/* and /v2/stocks/SPX/*
    both 404 with valid credentials.
  * Deriving SPX from live Alpaca SPY works but carries real basis risk. A fixed
    ratio drifts 4.38 SPX points (0.88 strikes) over 5 days. Anchoring on the
    last ^GSPC bar and scaling by SPY's move does better — median error 0.25
    points — but p99 is 2.17 and max 2.49. Against 5-point strikes and a
    ~3.9-point 6-minute sigma at 15% annualized, a 2-point spot error is over
    half a standard deviation, which is tens of probability points on a
    near-money digital. Not acceptable for pricing.
  * This endpoint's meta.regularMarketPrice carries ^GSPC itself at 1.1-3.3s
    lag, polls in 106-469ms, and updated on all 8 consecutive 2s polls without
    rate-limiting. It is the actual index Kalshi settles against, so there is no
    basis at all.

For reference, the abandoned kalshi_spx_hf_paper_trader.py estimated SPX as
`SPY * 10.0`, which read 7758 against a true 7779.53 — 21 points, over 4 strikes.
"""

import datetime
import json
import urllib.request

from .feed import BAR_SECONDS, SLOW_BARS, BTCFeed

# Yahoo rejects the default urllib agent.
_UA = "Mozilla/5.0"
_QUOTE_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/"
              "%5EGSPC?range=1d&interval=1m")

# A quote older than this means the session is closed or the feed has stalled.
# Observed lag during market hours was 1.1-3.3s across 8 polls; 90s is loose
# enough to ride out a slow refresh and tight enough that a closed market is
# never mistaken for a live one.
MAX_QUOTE_AGE_SEC = 90


class SPXFeed(BTCFeed):
    """^GSPC spot, drop-in for BTCFeed.

    Inherits fetch()'s contract exactly: return a price, append a tick only when
    the price is genuinely fresh, and return self.last without recording
    anything when it is not. That last part matters more here than for BTC —
    when the equity session closes, Yahoo keeps serving the final close
    indefinitely. Recording those as ticks would feed a stream of zero returns
    into ewma_volatility() and collapse measured vol to the floor, which reads
    as maximum vol compression: exactly the regime that lowers MIN_EDGE and
    widens the OTM gate. Stale quotes are dropped instead, and _tick_log_returns
    already skips pairs that span the resulting hole.
    """

    def __init__(self):
        super().__init__()
        self.last_quote_age: float | None = None
        self.session_open: bool = False

    def fetch(self) -> float:
        try:
            req = urllib.request.Request(_QUOTE_URL, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=5) as resp:
                meta = json.load(resp)["chart"]["result"][0]["meta"]
            price = float(meta["regularMarketPrice"])
            quote_ts = float(meta["regularMarketTime"])
        except Exception:
            self.session_open = False
            return self.last

        if price <= 0:
            self.session_open = False
            return self.last

        now = datetime.datetime.now()
        age = now.timestamp() - quote_ts
        self.last_quote_age = age

        # Stale quote — closed session or stalled feed. Publish the price so
        # callers can display it, but do not let it into the vol history.
        if age > MAX_QUOTE_AGE_SEC:
            self.session_open = False
            self.last = price
            return price

        self.session_open = True
        self.last = price
        self.prices.append((now, price))
        self.prices = self.prices[-500:]
        self._maybe_close_5min_bar(now, price)
        return price

    def bootstrap_history(self, hours: int = 24) -> int:
        """Populate bars_5min with ^GSPC 5-min closes.

        `hours` is TRADING hours here, not wall-clock hours, which is the whole
        difference from the BTC path. BTC trades 24/7 so 24 wall-clock hours is
        24 hours of bars; the S&P trades 6.5h a day, so filling SLOW_BARS=288
        bars (24 trading hours) spans about 3.7 sessions. Requesting 24
        calendar hours would return ~78 bars and leave the 24h SMA — the
        denominator of vol_ratio() — measuring barely one session.

        Overnight and weekend gaps are left in place deliberately. Both
        _bar_log_returns() and _maybe_close_5min_bar() already drop
        non-contiguous pairs, so a gap costs a return rather than injecting a
        fake one spanning 17 hours.
        """
        try:
            import yfinance as yf

            sessions = hours / 6.5
            # Pad for weekends and holidays, floor at a week so the slow window
            # is meaningful even when called with a small `hours`.
            days = max(7, int(sessions * 1.6) + 3)
            end = datetime.datetime.now(datetime.timezone.utc)
            start = end - datetime.timedelta(days=days)
            df = yf.download("^GSPC", start=start, end=end, interval="5m",
                             progress=False, auto_adjust=False)
            if df is None or df.empty:
                return 0
            closes = df["Close"].values.flatten()
            times = df.index.to_pydatetime()
            self.bars_5min.clear()
            for ts, c in zip(times, closes):
                if c > 0:
                    # Same tz handling as BTCFeed: yfinance stamps are tz-aware
                    # UTC, live ticks are naive local. Convert before stripping
                    # tzinfo or the next bar boundary sits hours in the future
                    # and no bars ever close.
                    ts_naive = (ts.astimezone().replace(tzinfo=None)
                                if ts.tzinfo else ts)
                    self.bars_5min.append((ts_naive, float(c)))
            # Keep only what the slow window can use, matching the deque bound.
            while len(self.bars_5min) > SLOW_BARS + 10:
                self.bars_5min.popleft()
            if self.bars_5min:
                last_ts = self.bars_5min[-1][0]
                self._current_bar_start = last_ts + datetime.timedelta(
                    seconds=BAR_SECONDS)
                stale_min = (datetime.datetime.now() - last_ts).total_seconds() / 60
                # Unlike BTC, staleness here is usually just "the market is
                # shut", which is expected rather than a warning sign.
                if stale_min > 15:
                    print(f"  ℹ️  ^GSPC history ends {stale_min:.0f} min ago "
                          f"(closed session?) — vol_ratio stays neutral until "
                          f"the fast window refills")
            return len(self.bars_5min)
        except Exception as e:
            print(f"  ⚠️  ^GSPC 5-min bar bootstrap failed: {e}")
            return 0
