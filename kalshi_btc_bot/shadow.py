"""Price what the looser gate set would trade, without trading it.

The `universe` stream answers every SELECTION question — it is recorded before
any filter, so a gate can be moved and the whole history re-scored against real
settlement. It cannot answer anything about EXECUTION: whether the quote is
still there when the order lands, how far a size-N order walks the book, what
fraction of signals are fillable at all.

Those need an order attempt. The live gates deliberately produce about six
signals a day, so execution data accrues far too slowly to say anything — as of
2026-08-26 the entire estimate of decision-to-execution slippage rests on ONE
observation. This module widens the gates for RECORDING only, so that estimate
becomes a distribution while the real book keeps trading the tight set.

Cost control matters here, because this adds API calls to a 2-second loop:
SHADOW_MAX_PER_SCAN caps candidates per scan and SHADOW_TICKER_COOLDOWN stops
the same contract being re-sampled. At the shipped 1-per-scan / 120s values the
worst case is one extra quote and one extra book per scan, and in practice far
less because most scans surface nothing.
"""
import time

from . import config as _C
from . import recorder


class ShadowRecorder:
    """Not thread-safe. Driven from scan_step only."""

    def __init__(self):
        self._last: dict = {}      # ticker -> when it was last sampled

    def _fresh(self, ticker: str) -> bool:
        cd = getattr(_C, "SHADOW_TICKER_COOLDOWN", 120)
        return (time.time() - self._last.get(ticker, 0)) >= cd

    def scan(self, portfolio, dist, spot: float, vol: float, regime: dict,
             ladder: list) -> int:
        """Record up to SHADOW_MAX_PER_SCAN would-be entries. Returns the count.

        Deliberately swallows every exception. This is instrumentation; it must
        never be able to take down or slow the path that actually trades.
        """
        if not getattr(_C, "SHADOW_ENABLED", False) or not recorder.ENABLED:
            return 0
        if not ladder:
            return 0
        z = regime.get("zscore") or 0.0
        if regime.get("regime") not in ("RANGING", "REVERTING"):
            return 0
        if abs(z) < getattr(_C, "SHADOW_ZSCORE_MIN", 1.20):
            return 0

        cap = int(getattr(_C, "SHADOW_MAX_PER_SCAN", 1))
        hmax = getattr(_C, "SHADOW_HOURS_MAX", 1.0)
        rmin = getattr(_C, "SHADOW_OVERPRICING_MIN", 1.25)
        done = 0

        for c in ladder:
            if done >= cap:
                break
            try:
                tk = c["ticker"]
                if tk in portfolio.positions or not self._fresh(tk):
                    continue
                h = c.get("hours") or 0.0
                if not (_C.BOUNDARY_NO_HOURS_MIN <= h <= hmax):
                    continue
                a, b = c.get("ask"), c.get("bid")
                if not a or not b:
                    continue
                if not (_C.BOUNDARY_NO_YES_ASK_MIN <= a <= _C.BOUNDARY_NO_YES_ASK_MAX):
                    continue
                lo, hi = c.get("low"), c.get("high")
                if lo is None or hi is None:
                    continue
                # same side rule as find_boundary_no: fade the breakout direction
                if z > 0 and not spot < lo:
                    continue
                if z < 0 and not spot >= hi:
                    continue
                dd = -(abs(spot - hi) if spot >= hi else abs(lo - spot))
                if not (_C.BOUNDARY_NO_OTM_MIN <= dd <= _C.BOUNDARY_NO_OTM_MAX):
                    continue
                tp = dist.posterior_prob(c, spot, vol, max(h, 0.01), regime,
                                         bid=b, ask=a)["true_prob"]
                if not (0 < tp < _C.NO_TRUE_PROB_MAX):
                    continue
                if b / tp < rmin:
                    continue

                # Everything above is selection and `universe` could answer it.
                # Everything below is the point: what execution would have met.
                fresh = portfolio._fresh_quote(tk, attempts=1)
                book = portfolio._orderbook(tk) or {}
                size = 11
                filled, px = (0, None)
                try:
                    levels = (book.get("yes") or [])
                    filled, px = portfolio._walk_book(
                        levels, size, transform=lambda p: 1.0 - p)
                except Exception:
                    pass

                self._last[tk] = time.time()
                recorder.record_shadow(
                    ticker=tk, spot=spot, regime=regime, hours=h,
                    decision={"a": a, "b": b, "no_cost": round(1.0 - b, 4),
                              "true_prob": round(tp, 4),
                              "ratio": round(b / tp, 3),
                              "net_edge": round((1.0 - tp) - (1.0 - b), 4),
                              "otm": round(dd, 1)},
                    fresh=fresh, book=book,
                    would_fill={"want": size, "filled": filled,
                                "px": round(px, 4) if px else None},
                    reason="SHADOW_BOUNDARY_NO",
                )
                done += 1
            except Exception:
                continue
        return done
