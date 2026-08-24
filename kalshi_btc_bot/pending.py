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

TWO CONSUMERS, ONE QUEUE. `gate()` arms a ticker; either `gate()` itself or
`watchlist_fills()` may fill it, and they use different fill conditions on
purpose (see each method). Arming is enabled by `_arming_on()`, which is true
if EITHER DELAYED_ENTRY_DIP or WATCHLIST_ENTRY_DIP is set.

That used to be `DELAYED_ENTRY_DIP > 0` alone, and the shipped config had
DELAYED_ENTRY_DIP = 0.0 with WATCHLIST_ENTRY_DIP = 0.05 — so nothing was ever
armed, `watchlist_fills()` returned [] forever, and the bot bought at arming
(measured -1.7% ROC) while every doc claimed it was buying the dip (+12.0%).
Found 2026-08-24 after a 3h41m live session placed zero orders despite 122
observations clearing every model gate. If you add a third consumer, add its
flag to `_arming_on()` — and see `test_watchlist_entry.py` for the test that
now fails when a switched-on feature is unreachable.
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

    @staticmethod
    def _arming_on() -> bool:
        """Does anything downstream need tickers armed?

        THE BUG THIS FIXES. `_pending` is written in exactly one place — the
        first-sighting branch of `gate()` — and `gate()` used to return early
        whenever DELAYED_ENTRY_DIP <= 0. So with DELAYED_ENTRY_DIP = 0.0 and
        WATCHLIST_ENTRY_DIP = 0.05, which is what shipped, nothing was ever
        armed and `watchlist_fills()` returned [] on every call for the life of
        the process. The watchlist was inert while the config, the tests and
        the README all described it running.

        Two switched-on-looking flags where one silently gated the other, which
        is the same failure shape as the frozen-import bug. Both consumers now
        declare their need here instead of sharing one flag's guard.
        """
        return (_C.DELAYED_ENTRY_DIP > 0
                or getattr(_C, "WATCHLIST_ENTRY_DIP", 0.0) > 0)

    def gate(self, sig, cost_key="no_cost", now=None):
        """Return (signal_to_buy_or_None, status).

        status is one of: "off" (feature disabled or signal not covered),
        "queued" (first sighting, do not buy), "waiting" (holding for a fill
        that gate() itself will not make), "triggered" (dip reached, buy now),
        "abandoned" (fell through DELAYED_ENTRY_DIP_MAX).

        Called with EVERY fresh signal from the scan. Passing through unchanged
        when nothing needs arming is what makes the off case byte-identical to
        the old behaviour.

        Both delayed entry and the watchlist WITHHOLD the buy at arming time —
        that is the policy being measured in each case, and the arming price is
        the reference the dip is measured from. What differs is who fills:
        gate() fills only when the whole signal re-fires (below), while
        `watchlist_fills()` fills off the raw ladder on the model's valuation.
        With only WATCHLIST_ENTRY_DIP set, gate() arms and then always defers —
        every fill comes from watchlist_fills().
        """
        if not self._arming_on() or sig.get("signal") not in _C.DELAYED_ENTRY_SIGNALS:
            return sig, "off"

        now = time.time() if now is None else now
        ticker = sig["ticker"]
        cost = float(sig[cost_key])
        p = self._pending.get(ticker)

        if p is None:
            self._pending[ticker] = {"ref": cost, "t": now, "n": 1}
            return None, "queued"

        p["n"] += 1

        dip = _C.DELAYED_ENTRY_DIP
        if dip <= 0:
            # Watchlist-only. gate() never fills, and deliberately does not
            # apply DELAYED_ENTRY_DIP_MAX: the measured watchlist policy
            # (dip 5%, net_edge >= 0.05, n=14) carries no lower cap, because
            # the model's own valuation is the filter — a dip caused by spot
            # walking into the band raises true_prob, collapses net edge, and
            # fails the fill test on its own. Adding a cap here would gate a
            # policy on a threshold its measurement never contained.
            return None, "waiting"

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

        Measured 2026-08-23, settlement-resolved, net of fees, 15-min window,
        arming on the posterior exactly as find_boundary_no does:
            policy                        n    WR    cost      ROC   VALID  P(>0)
            buy at arming (live)        110   80%  $0.803   -1.7%   -0.2%     --
            dip 5%, net_edge >= 0.05     14   79%  $0.689  +12.0%  +26.2%    79%
        Win rate falls 1pp while cost falls 11pp, so break-even drops far more
        than the hit rate does. That is the whole mechanism.

        NOT AN ESTABLISHED EDGE — n=14 across 14 expiries and 6 days, P(ROC>0)
        = 79%. Fourteen trades. Judge it on days the grid search never saw.

        Prices with the POSTERIOR, matching the arming gate. An earlier draft
        used the raw prior and measured +4.9% at n=39 — but that run also ARMED
        on the prior, which live does not do. Re-armed correctly on the
        posterior the sample collapses to n~14, and the prior-vs-posterior
        choice for the fill is worth about 1pp. Consistency wins.
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
                # POSTERIOR, matching what find_boundary_no arms on. Using the
                # raw prior here would put two different probability estimates
                # inside one strategy. Measured cost of the consistency: ~1pp
                # (+12.9% prior vs +12.0% posterior at dip 5%).
                true_p = dist.posterior_prob(
                    row, spot, vol, row["hours"], regime,
                    bid=row.get("bid"), ask=row.get("ask"),
                )["true_prob"]
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
        # Whichever consumer is actually going to fill this decides what the
        # target band is. Rendering the delayed-entry band in watchlist-only
        # mode printed "want <= $X (-0%)", i.e. the current price, which reads
        # as a bot about to buy at no discount at all.
        wl = getattr(_C, "WATCHLIST_ENTRY_DIP", 0.0)
        if _C.DELAYED_ENTRY_DIP > 0:
            dip, cap, who = _C.DELAYED_ENTRY_DIP, _C.DELAYED_ENTRY_DIP_MAX, ""
        else:
            dip, cap, who = wl, None, " [watchlist]"

        def _band(ref):
            hi = ref * (1.0 - dip)
            lo = ref * (1.0 - cap) if cap is not None else None
            return f"${lo:.3f}-${hi:.3f}" if lo is not None else f"<= ${hi:.3f}"

        if status == "queued":
            return (f"⏳ QUEUED{who} {ticker[-18:]} at ${cost:.3f} — "
                    f"want {_band(cost)} "
                    f"(-{dip:.0%}"
                    + (f" to -{cap:.0%}" if cap is not None else "")
                    + f", {_C.DELAYED_ENTRY_MAX_WAIT_MINS:.0f}m limit)")
        p = self._pending.get(ticker)
        if status == "waiting" and p:
            return (f"⏳ waiting{who} {ticker[-18:]} ${cost:.3f} "
                    f"(want {_band(p['ref'])}, ref ${p['ref']:.3f}, "
                    f"{(cost - p['ref']) / p['ref']:+.1%})")
        if status == "triggered":
            return f"⏳→✅ DIP HIT {ticker[-18:]} buying at ${cost:.3f}"
        if status == "abandoned":
            return (f"⏳✗ ABANDONED {ticker[-18:]} ${cost:.3f} — "
                    f"blew through the -{cap:.0%} cap, spot moved")
        return None
