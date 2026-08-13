import datetime
import math
import requests
import statistics
from collections import deque

from .config import PRICE_FETCH


# ─────────────────────────────────────────────
# PRICE FEED
# ─────────────────────────────────────────────
# 5-minute bar aggregation mirrors the backtest so vol_ratio() measures the same
# thing in live as in sim: fast realized (12 five-min bars = 1h) vs Kalshi's
# lagged 24h SMA (288 bars). See vol_ratio() docstring for the parity rationale.
BAR_SECONDS = 300
FAST_BARS   = 12
SLOW_BARS   = 288


class BTCFeed:
    def __init__(self):
        self.prices = []
        self.last   = 0.0
        # 5-minute bars (close prices). Bootstrapped from yfinance so the 24h SMA
        # is meaningful from the first scan tick instead of after 24h of runtime.
        self.bars_5min: deque[tuple[datetime.datetime, float]] = deque(maxlen=SLOW_BARS + 10)
        self._current_bar_start: datetime.datetime | None = None

    def bootstrap_history(self, hours: int = 24) -> int:
        """Populate bars_5min with `hours` of historical BTC 5-min closes so the
        24h SMA vol signal is live from tick #1. Returns bar count populated."""
        try:
            import yfinance as yf
            # Timezone-aware UTC. Passing naive local datetimes made yfinance
            # read them as UTC, so in EDT (UTC-4) the window closed four hours
            # early and the "24h of history" actually ended four hours ago —
            # measured 243 min of lag vs 2 min once tz-aware. That hole is what
            # fed the synthetic-bar gap in _maybe_close_5min_bar().
            end = datetime.datetime.now(datetime.timezone.utc)
            start = end - datetime.timedelta(hours=hours + 1)
            df = yf.download("BTC-USD", start=start, end=end, interval="5m",
                             progress=False, auto_adjust=False)
            if df is None or df.empty:
                return 0
            closes = df["Close"].values.flatten()
            times = df.index.to_pydatetime()
            self.bars_5min.clear()
            for ts, c in zip(times, closes):
                if c > 0:
                    # yfinance timestamps are tz-aware UTC; live ticks use naive
                    # local time. Convert to local before stripping tzinfo, else
                    # the next 5-min bar boundary sits hours in the "future" and
                    # no bars close (vol_ratio frozen) until local clock catches
                    # up to the UTC clock value.
                    ts_naive = ts.astimezone().replace(tzinfo=None) if ts.tzinfo else ts
                    self.bars_5min.append((ts_naive, float(c)))
            # Align the currently-forming bar to the last historical bar's boundary
            if self.bars_5min:
                last_ts = self.bars_5min[-1][0]
                self._current_bar_start = last_ts + datetime.timedelta(seconds=BAR_SECONDS)
                stale_min = (datetime.datetime.now() - last_ts).total_seconds() / 60
                if stale_min > 15:
                    print(f"  ⚠️  Bootstrapped history ends {stale_min:.0f} min ago — "
                          f"vol_ratio stays neutral until the fast window refills")
            return len(self.bars_5min)
        except Exception as e:
            print(f"  ⚠️  5-min bar bootstrap failed: {e}")
            return 0

    @staticmethod
    def _slot_start(ts: datetime.datetime) -> datetime.datetime:
        """Floor a timestamp to the 5-min bar boundary containing it."""
        ts = ts.replace(second=0, microsecond=0)
        return ts - datetime.timedelta(minutes=ts.minute % 5)

    def _maybe_close_5min_bar(self, ts: datetime.datetime, price: float) -> None:
        """Push a 5-min bar close whenever tick time crosses a 5-min boundary.

        Advancing exactly one bar per call replayed a feed gap (restart,
        outage, laptop sleep) as one synthetic bar per tick: a four-hour hole
        produced 48 bars stamped five minutes apart, each holding the ~2
        seconds of price movement between consecutive ticks. Their near-zero
        returns collapsed the fast vol window — measured vol_ratio 0.079
        against a 0.55 compression threshold — so the vol-compression regime
        fired continuously for roughly an hour after every restart, lowering
        MIN_EDGE and widening the OTM gate on fabricated data.

        Skip missing bars rather than inventing them, and resume on the live
        boundary.
        """
        if self._current_bar_start is None:
            self._current_bar_start = self._slot_start(ts)
            return
        bar_end = self._current_bar_start + datetime.timedelta(seconds=BAR_SECONDS)
        if ts < bar_end:
            return
        if ts < bar_end + datetime.timedelta(seconds=BAR_SECONDS):
            # Normal roll: the forming bar just completed. The tick before the
            # boundary is our best available close.
            close_px = price
            if len(self.prices) >= 2:
                close_px = self.prices[-2][1]
            self.bars_5min.append((self._current_bar_start, close_px))
            self._current_bar_start = bar_end
            return
        # Gap. The forming bar is not a five-minute bar — its start is far
        # older than the tick that would close it — so recording it injects a
        # single enormous return. Drop it and re-anchor to the current slot.
        self._current_bar_start = self._slot_start(ts)

    def fetch(self) -> float:
        try:
            r = requests.get(
                "https://api.coinbase.com/v2/prices/BTC-USD/spot",
                timeout=5
            )
            price = float(r.json()["data"]["amount"])
            now   = datetime.datetime.now()
            self.last = price
            self.prices.append((now, price))
            self.prices = self.prices[-500:]
            self._maybe_close_5min_bar(now, price)
            return price
        except:
            return self.last

    def recent(self, seconds: int) -> list:
        cutoff = datetime.datetime.now() - datetime.timedelta(seconds=seconds)
        return [p for t, p in self.prices if t >= cutoff]

    def _recent_ticks(self, seconds: int) -> list:
        cutoff = datetime.datetime.now() - datetime.timedelta(seconds=seconds)
        return [(t, p) for t, p in self.prices if t >= cutoff]

    @staticmethod
    def _tick_log_returns(ticks: list) -> list[float]:
        """Log-returns between consecutive ticks, skipping any pair that spans
        a feed outage.

        fetch() returns the last known price without recording a tick when the
        HTTP call fails, so a dropped poll leaves a hole in self.prices. The
        next return then covers the whole outage but is treated as one
        PRICE_FETCH interval. Squared into the EWMA that prices every contract,
        a single 5-min hole inflated vol 5.6x (9.5x for 15 min) and decayed
        only over the EWMA half-life — mispricing the entire ladder meanwhile.
        """
        max_dt = PRICE_FETCH * 3
        rets = []
        for i in range(1, len(ticks)):
            (t_prev, p_prev), (t_now, p_now) = ticks[i-1], ticks[i]
            if p_prev <= 0 or p_now <= 0:
                continue
            if (t_now - t_prev).total_seconds() > max_dt:
                continue
            rets.append(math.log(p_now / p_prev))
        return rets

    def momentum(self, seconds: int = 60) -> float:
        r = self.recent(seconds)
        if len(r) < 2: return 0.0
        return (r[-1] - r[0]) / r[0]

    def acceleration(self) -> float:
        return self.momentum(30) - self.momentum(60)

    def volatility(self, seconds: int = 300) -> float:
        ticks = self._recent_ticks(seconds)
        if len(ticks) < 5: return 0.001
        rets = self._tick_log_returns(ticks)
        return statistics.stdev(rets) if len(rets) >= 2 else 0.001

    def ewma_volatility(self, lam: float = 0.99) -> float:
        """Fast EWMA vol — weights recent returns more than rolling stdev.
        λ=0.99 → ~69-tick half-life ≈ 2.3 min at the current 2s PRICE_FETCH
        (the "4.6 min" this note used to quote assumed the old 4s poll, which
        halved when PRICE_FETCH dropped to 2s). 2026-07-06: was
        λ=0.94, commented as "the standard daily decay factor from RiskMetrics" —
        that provenance is for daily bars (~11-day half-life); applied to 4s
        ticks it gave a ~45s half-life, letting one large tick flip the fast/slow
        vol_ratio and the HIGH/LOW regime read almost instantly. 0.99 keeps this
        genuinely "fast" relative to the ~46min slow EWMA below while damping
        single-tick noise."""
        ticks = self.prices[-300:]
        if len(ticks) < 3:
            return self.volatility(300)
        rets = self._tick_log_returns(ticks)
        if len(rets) < 2:
            return 0.001
        var = rets[0] ** 2
        for r in rets[1:]:
            var = lam * var + (1.0 - lam) * r ** 2
        return max(1e-6, math.sqrt(var))

    def _bar_log_returns(self, window: int) -> list[float]:
        """Log-returns from the last (window+1) 5-min bar closes.

        Only bars exactly BAR_SECONDS apart contribute. A pair spanning a gap
        measures hours of price movement as though it were five minutes — one
        outlier large enough to distort both vol windows — so those pairs are
        dropped rather than annualized as if they were ordinary bars.
        """
        bars = list(self.bars_5min)[-(window + 1):]
        rets = []
        for i in range(1, len(bars)):
            (t_prev, p_prev), (t_now, p_now) = bars[i-1], bars[i]
            if p_prev <= 0 or p_now <= 0:
                continue
            if abs((t_now - t_prev).total_seconds() - BAR_SECONDS) > 1:
                continue
            rets.append(math.log(p_now / p_prev))
        return rets

    def sma_volatility_5min(self, window: int = SLOW_BARS) -> float:
        """Rolling-window realized vol on 5-min bars — matches the backtest's
        `sma_volatility(SMA_VOL_WINDOW)` exactly. `SLOW_BARS=288` = 24h, mirroring
        Kalshi's publicly-stated lagged vol window."""
        rets = self._bar_log_returns(window)
        if len(rets) < 2:
            return 0.0
        return max(1e-6, statistics.stdev(rets))

    def vol_ratio(self) -> float:
        """Fast 1h realized / Slow 24h realized — both from 5-min bar log-returns,
        identical to the backtest's `vol_ratio()`. < 0.55 → vol compressed:
        Kalshi's lagged 24h model still prices as if vol were high while realized
        vol has already dropped → RANGE contracts underpriced → structural edge.

        Was previously an EWMA(46min half-life)/EWMA(4.6min half-life) ratio on
        4-second ticks — that measured a 46-min lag, not the 24h lag Kalshi
        actually uses, so it fired compression far more often than the backtest
        suggested. Rewritten 2026-07-16 to match backtest exactly. Requires
        `bootstrap_history()` at startup so the 24h SMA is meaningful from tick 1.
        """
        # Gate on contiguous returns actually available, not raw bar count. A
        # partially filled fast window understates realized vol, and
        # understated fast vol is precisely what reads as compression — the
        # regime that lowers MIN_EDGE and widens the OTM gate. Stay neutral
        # until there is enough clean data to support a real reading.
        fast_rets = self._bar_log_returns(FAST_BARS)
        slow_rets = self._bar_log_returns(SLOW_BARS)
        if len(fast_rets) < FAST_BARS - 1 or len(slow_rets) < FAST_BARS:
            return 1.0    # not enough contiguous history — treat as "no signal"
        slow = self.sma_volatility_5min(SLOW_BARS)
        fast = self.sma_volatility_5min(FAST_BARS)
        return fast / slow if slow > 0 else 1.0

    def zscore(self, seconds: int = 300) -> float:
        """(last - mean) / stdev over the window.

        Single pass instead of statistics.mean + statistics.stdev, which walk
        the list twice and carry Fraction-based exactness this does not need.
        Measured 2026-08-13 on a live-sized buffer: 545us -> ~35us, and this is
        the most expensive call in RegimeEngine.detect(), which runs every tick.
        Uses the sample stdev (n-1) the original did, so values are unchanged.
        """
        r = self.recent(seconds)
        n = len(r)
        if n < 5:
            return 0.0
        total = 0.0
        for x in r:
            total += x
        mean = total / n
        sq = 0.0
        for x in r:
            d = x - mean
            sq += d * d
        var = sq / (n - 1)
        if var <= 0:
            return 0.0
        return (r[-1] - mean) / math.sqrt(var)

    def consecutive(self) -> tuple:
        """Consecutive same-direction moves, measured on 5-MINUTE BARS.

        This read self.prices[-10:] — the last 10 raw ticks. At PRICE_FETCH=2s
        that is a 20-SECOND window, and the 0.0001 direction threshold is ~$6 on
        BTC, which a 2-second move almost never clears. Measured over 218k
        recorded live ticks: 89.9% classified FLAT, only 10.1% directional at
        all, so P(3 consecutive same-direction) ~ 0.03% and TREND_BARS=3 was
        effectively unreachable — TRENDING sat at 0.29% of ticks even after the
        momentum window was corrected (a576b80).

        The backtest's `prices` ARE 5-minute bars, so the same code meant 50
        MINUTES there — the same sample-count-vs-wall-clock divergence as the
        momentum window. Reading bars_5min (already maintained for the SMA vol,
        298 bars deep and bootstrapped at startup) makes both sides measure
        identical 5-minute steps, and applies the 0.0001 threshold to a 5-minute
        move (median 0.00032) rather than a 2-second one.
        """
        src = [p for _, p in list(self.bars_5min)[-10:]]
        if len(src) < 4: return 0, "FLAT"
        recent = src
        dirs = []
        for i in range(1, len(recent)):
            chg = (recent[i] - recent[i-1]) / recent[i-1]
            dirs.append("UP" if chg > 0.0001 else "DN" if chg < -0.0001 else "FLAT")
        if not dirs: return 0, "FLAT"
        last  = dirs[-1]
        count = sum(1 for _ in reversed(dirs) if _ == last)
        return count, last
