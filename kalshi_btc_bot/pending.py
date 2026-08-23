"""Delayed entry: hold a fired NO signal back and buy it only after a dip.

The bot's break-even win rate is set almost entirely by entry price — a NO
bought at cost X pays (1-X)/X and needs X to break even. Buying the same
contract 15% cheaper moves break-even from 74% to 60%. See
config.DELAYED_ENTRY_DIP for the settlement-resolved evidence, including the
two reasons it may not pay.

The design point that makes this more than "buy the dip": a queued ticker is
only ever filled on a loop where the signal engine RE-FIRES it. `gate()` is fed
from the live scan, so a pending entry is implicitly revalidated against fresh
spot, true_prob, z-score, regime and net edge on every tick. A dip caused by
spot walking toward the strike band stops re-firing and expires unfilled; a dip
that is quote noise keeps re-firing and gets bought. Nothing here re-implements
those gates, and nothing here should — that would be the backtest/live parity
bug class this repo keeps paying for.

Reference price is the FIRST sighting, never updated downward. Re-anchoring to
each new low would turn "wait for a 10% dip" into "chase it to the floor",
which is the mirror image of the scale-in failure recorded in memory: a
threshold measured from a moving reference always eventually triggers.
"""
import time

from . import config as _C


class PendingEntries:
    """Signal-price memory for tickers awaiting a dip. Not thread-safe."""

    def __init__(self):
        # ticker -> {"ref": first-sighting cost, "t": queued at, "n": sightings}
        self._pending = {}

    def __len__(self):
        return len(self._pending)

    def pending(self):
        """(ticker, ref_cost, waited_secs) for the live view / diagnostics."""
        now = time.time()
        return [(tk, p["ref"], now - p["t"])
                for tk, p in sorted(self._pending.items())]

    def discard(self, ticker):
        """Forget a ticker — call after a fill or an abandoned entry."""
        self._pending.pop(ticker, None)

    def expire(self, now=None):
        """Drop entries past DELAYED_ENTRY_MAX_WAIT_MINS. Returns dropped."""
        now = time.time() if now is None else now
        limit = _C.DELAYED_ENTRY_MAX_WAIT_MINS * 60.0
        dead = [tk for tk, p in self._pending.items() if now - p["t"] > limit]
        for tk in dead:
            del self._pending[tk]
        return dead

    def gate(self, sig, cost_key="no_cost", now=None):
        """Return (signal_to_buy_or_None, status).

        status is one of: "off" (feature disabled or signal not covered),
        "queued" (first sighting, do not buy), "waiting" (dip not deep enough),
        "triggered" (dip reached, buy it now).

        Called with EVERY fresh signal from the scan. Passing through unchanged
        when DELAYED_ENTRY_DIP <= 0 is what makes the default byte-identical to
        the old behaviour.
        """
        dip = _C.DELAYED_ENTRY_DIP
        if dip <= 0 or sig.get("signal") not in _C.DELAYED_ENTRY_SIGNALS:
            return sig, "off"

        now = time.time() if now is None else now
        ticker = sig["ticker"]
        cost = float(sig[cost_key])
        p = self._pending.get(ticker)

        if p is None:
            self._pending[ticker] = {"ref": cost, "t": now, "n": 1}
            return None, "queued"

        p["n"] += 1
        if cost <= p["ref"] * (1.0 - dip):
            cap = _C.DELAYED_ENTRY_DIP_MAX
            if cap is not None and cost < p["ref"] * (1.0 - cap):
                # Straight through the band. A contract that fell this far did
                # so because spot moved toward the strike, and those settle at
                # 40-50% against a 67% base rate. Abandon rather than buy the
                # knife, and do NOT keep waiting for it to bounce back into the
                # band — by then the information has already arrived.
                del self._pending[ticker]
                return None, "abandoned"
            del self._pending[ticker]
            return sig, "triggered"
        return None, "waiting"

    def watchlist_fills(self, rows_by_ticker, dist, spot, vol, regime,
                        now=None) -> list:
        """Re-price every ARMED ticker off the CURRENT ladder and fire the ones
        whose discount and model value both clear. Returns [(ticker, row)].

        This is the opposite of `gate()`. gate() only ever re-checks a ticker on
        a scan where the SIGNAL re-fires, i.e. where every entry gate still
        passes. But the gates going stale IS the discount: as spot drifts toward
        the band, true_prob rises, the overpricing ratio collapses, and the
        signal stops firing — precisely while the contract gets cheap. Requiring
        the full gate set to re-pass caps the reachable discount at ~2.2%
        (median). Dropping to the model's own valuation reaches ~16%.

        So the arming gate stays strict and the FILL condition is only:
          1. price has dipped WATCHLIST_ENTRY_DIP below the arming cost, and
          2. the model still values NO above that price by
             WATCHLIST_ENTRY_NET_EDGE.

        Measured 2026-08-23, settlement-resolved, net of fees, 15-min window:
            policy                          n    WR    cost      ROC   TUNE  VALID
            buy at arming (live)          110   80%  $0.803   -1.7%  -3.4%  -0.2%
            dip 10%, net_edge >= 0.05      39   72%  $0.669   +4.9%  +3.4%  +6.3%
        Win rate falls 8pp but break-even falls 13pp, so the discount more than
        pays for the worse hit rate. Requiring net_edge >= 0.05 rather than
        >= 0.00 is what makes it work (+4.9% vs +1.7%) — the model's valuation
        is the filter, the stale gates are the discount.

        NOT AN ESTABLISHED EDGE. n=39 over 9 days, 4 of them negative, clustered
        95% CI [-16.6%, +24.7%], P(ROC>0) = 69%. Below the bar this repo uses to
        act; shipped OFF, as a live measurement.

        KNOWN INCONSISTENCY: this re-prices with dist.true_prob (the raw
        Student-t prior), because that is what the measurement above used.
        find_boundary_no arms using _posterior() (the market-blended value). Two
        different probability estimates in one strategy. Re-measure with the
        posterior before treating either as settled.
        """
        dip = getattr(_C, "WATCHLIST_ENTRY_DIP", 0.0)
        if dip <= 0 or not self._pending:
            return []
        need = getattr(_C, "WATCHLIST_ENTRY_NET_EDGE", 0.05)
        out = []
        for ticker, p in list(self._pending.items()):
            row = rows_by_ticker.get(ticker)
            if not row:
                continue
            bid = row.get("bid")
            if not bid or bid <= 0:
                continue
            cost = 1.0 - float(bid)
            if cost > p["ref"] * (1.0 - dip):
                continue
            try:
                true_p = dist.true_prob(row, spot, vol, row["hours"], regime)
            except Exception:
                continue
            if not (0.0 < true_p < 1.0):
                continue
            if (1.0 - true_p) - cost < need:
                continue
            del self._pending[ticker]
            out.append((ticker, {**row, "signal": "BOUNDARY_NO",
                                 "true_prob": true_p, "no_cost": cost,
                                 "watchlist_ref": p["ref"]}))
        return out

    def describe(self, sig, status, cost_key="no_cost"):
        """One-line human summary of a gate() decision, or None if not needed."""
        if status in ("off", None):
            return None
        ticker = sig["ticker"]
        cost = float(sig[cost_key])
        cap = _C.DELAYED_ENTRY_DIP_MAX
        if status == "queued":
            hi = cost * (1.0 - _C.DELAYED_ENTRY_DIP)
            lo = cost * (1.0 - cap) if cap is not None else None
            band = f"${lo:.3f}-${hi:.3f}" if lo is not None else f"<= ${hi:.3f}"
            return (f"⏳ QUEUED {ticker[-18:]} at ${cost:.3f} — "
                    f"want {band} "
                    f"(-{_C.DELAYED_ENTRY_DIP:.0%}"
                    + (f" to -{cap:.0%}" if cap is not None else "")
                    + f", {_C.DELAYED_ENTRY_MAX_WAIT_MINS:.0f}m limit)")
        p = self._pending.get(ticker)
        if status == "waiting" and p:
            hi = p["ref"] * (1.0 - _C.DELAYED_ENTRY_DIP)
            lo = p["ref"] * (1.0 - cap) if cap is not None else None
            band = f"${lo:.3f}-${hi:.3f}" if lo is not None else f"<= ${hi:.3f}"
            return (f"⏳ waiting {ticker[-18:]} ${cost:.3f} "
                    f"(want {band}, ref ${p['ref']:.3f}, "
                    f"{(cost - p['ref']) / p['ref']:+.1%})")
        if status == "triggered":
            return f"⏳→✅ DIP HIT {ticker[-18:]} buying at ${cost:.3f}"
        if status == "abandoned":
            return (f"⏳✗ ABANDONED {ticker[-18:]} ${cost:.3f} — "
                    f"blew through the -{cap:.0%} cap, spot moved")
        return None
