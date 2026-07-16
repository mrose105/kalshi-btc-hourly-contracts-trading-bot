import datetime
import math
import requests
import statistics
from collections import deque


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
            end = datetime.datetime.now()
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
                    ts_naive = ts.replace(tzinfo=None) if ts.tzinfo else ts
                    self.bars_5min.append((ts_naive, float(c)))
            # Align the currently-forming bar to the last historical bar's boundary
            if self.bars_5min:
                last_ts = self.bars_5min[-1][0]
                self._current_bar_start = last_ts + datetime.timedelta(seconds=BAR_SECONDS)
            return len(self.bars_5min)
        except Exception as e:
            print(f"  ⚠️  5-min bar bootstrap failed: {e}")
            return 0

    def _maybe_close_5min_bar(self, ts: datetime.datetime, price: float) -> None:
        """Push a 5-min bar close whenever tick time crosses a 5-min boundary."""
        if self._current_bar_start is None:
            # Anchor first bar to the current 5-min slot boundary
            self._current_bar_start = ts.replace(second=0, microsecond=0)
            self._current_bar_start -= datetime.timedelta(
                minutes=self._current_bar_start.minute % 5
            )
            return
        bar_end = self._current_bar_start + datetime.timedelta(seconds=BAR_SECONDS)
        if ts >= bar_end:
            # The tick just before bar_end is our best available "close" — use
            # the previous tick if we have one, else fall back to current price.
            close_px = price
            if len(self.prices) >= 2:
                # Prev-tick was the last observation before the boundary
                close_px = self.prices[-2][1]
            self.bars_5min.append((self._current_bar_start, close_px))
            self._current_bar_start = bar_end

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

    def momentum(self, seconds: int = 60) -> float:
        r = self.recent(seconds)
        if len(r) < 2: return 0.0
        return (r[-1] - r[0]) / r[0]

    def acceleration(self) -> float:
        return self.momentum(30) - self.momentum(60)

    def volatility(self, seconds: int = 300) -> float:
        r = self.recent(seconds)
        if len(r) < 5: return 0.001
        rets = [math.log(r[i]/r[i-1]) for i in range(1, len(r)) if r[i-1] > 0]
        return statistics.stdev(rets) if len(rets) >= 2 else 0.001

    def ewma_volatility(self, lam: float = 0.99) -> float:
        """Fast EWMA vol — weights recent returns more than rolling stdev.
        λ=0.99 → ~69-bar half-life ≈ 4.6 min at 4s ticks. 2026-07-06: was
        λ=0.94, commented as "the standard daily decay factor from RiskMetrics" —
        that provenance is for daily bars (~11-day half-life); applied to 4s
        ticks it gave a ~45s half-life, letting one large tick flip the fast/slow
        vol_ratio and the HIGH/LOW regime read almost instantly. 0.99 keeps this
        genuinely "fast" relative to the ~46min slow EWMA below while damping
        single-tick noise."""
        prices = [p for _, p in self.prices[-300:]]
        if len(prices) < 3:
            return self.volatility(300)
        rets = [math.log(prices[i] / prices[i-1])
                for i in range(1, len(prices)) if prices[i-1] > 0]
        if len(rets) < 2:
            return 0.001
        var = rets[0] ** 2
        for r in rets[1:]:
            var = lam * var + (1.0 - lam) * r ** 2
        return max(1e-6, math.sqrt(var))

    def _bar_log_returns(self, window: int) -> list[float]:
        """Log-returns from the last (window+1) 5-min bar closes."""
        prices = [p for _, p in list(self.bars_5min)[-(window + 1):]]
        return [math.log(prices[i] / prices[i-1])
                for i in range(1, len(prices)) if prices[i-1] > 0]

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
        if len(self.bars_5min) < FAST_BARS + 2:
            return 1.0    # not enough history yet — treat as "no signal"
        slow = self.sma_volatility_5min(SLOW_BARS)
        fast = self.sma_volatility_5min(FAST_BARS)
        return fast / slow if slow > 0 else 1.0

    def zscore(self, seconds: int = 300) -> float:
        r = self.recent(seconds)
        if len(r) < 5: return 0.0
        mean = statistics.mean(r)
        std  = statistics.stdev(r)
        return (r[-1] - mean) / std if std > 0 else 0.0

    def consecutive(self) -> tuple:
        if len(self.prices) < 4: return 0, "FLAT"
        recent = [p for _, p in self.prices[-10:]]
        dirs = []
        for i in range(1, len(recent)):
            chg = (recent[i] - recent[i-1]) / recent[i-1]
            dirs.append("UP" if chg > 0.0001 else "DN" if chg < -0.0001 else "FLAT")
        if not dirs: return 0, "FLAT"
        last  = dirs[-1]
        count = sum(1 for _ in reversed(dirs) if _ == last)
        return count, last
