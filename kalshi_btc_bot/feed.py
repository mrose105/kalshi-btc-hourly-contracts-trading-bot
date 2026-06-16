import datetime
import math
import requests
import statistics


# ─────────────────────────────────────────────
# PRICE FEED
# ─────────────────────────────────────────────
class BTCFeed:
    def __init__(self):
        self.prices = []
        self.last   = 0.0

    def fetch(self) -> float:
        try:
            r = requests.get(
                "https://api.coinbase.com/v2/prices/BTC-USD/spot",
                timeout=5
            )
            price = float(r.json()["data"]["amount"])
            self.last = price
            self.prices.append((datetime.datetime.now(), price))
            self.prices = self.prices[-500:]
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

    def ewma_volatility(self, lam: float = 0.94) -> float:
        """RiskMetrics EWMA vol — weights recent returns more than rolling stdev.
        λ=0.94 is the standard daily decay factor from J.P. Morgan RiskMetrics."""
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

    def ewma_volatility_slow(self, lam: float = 0.9990) -> float:
        """Slow EWMA vol (~115-bar half-life ≈ 8 min at 4s ticks).
        Proxy for Kalshi's lagged vol estimate — Kalshi reprices infrequently
        so this mimics a price that still reflects the last vol spike."""
        prices = [p for _, p in self.prices[-500:]]
        if len(prices) < 10:
            return self.ewma_volatility()
        rets = [math.log(prices[i] / prices[i-1])
                for i in range(1, len(prices)) if prices[i-1] > 0]
        if len(rets) < 5:
            return self.ewma_volatility()
        var = rets[0] ** 2
        for r in rets[1:]:
            var = lam * var + (1.0 - lam) * r ** 2
        return max(1e-6, math.sqrt(var))

    def vol_ratio(self) -> float:
        """Fast EWMA / Slow EWMA.
        < 0.55 → vol compressed: Kalshi's lagged model overestimates vol,
        RANGE contracts are systematically underpriced → structural buy edge."""
        slow = self.ewma_volatility_slow()
        fast = self.ewma_volatility()
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
