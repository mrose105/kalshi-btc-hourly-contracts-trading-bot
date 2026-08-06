"""
Clean, non-compounding counterfactual for the one-snipe-per-expiry filter
(kalshi_btc_bot/signals.py find_snipe).

Why this exists: the tuning/validation split showed the filter changing 39
real decisions in the 40-day tuning window, yet final Sharpe/return/trade
count came out byte-identical to baseline -- while the SAME calendar span
run continuously (not chopped into tune/valid halves) showed a huge return
difference (+139pp). That gap is almost certainly Kelly-sized compounding
path-dependence (a few early diverted trades change capital at every later
trade), not the filter's real effect. This script isolates the filter's
true per-decision effect without letting anything compound:

For every point where the filter blocks what would have been find_snipe's
winning candidate, open a SEPARATE, fixed-capital "shadow" BacktestPortfolio
containing ONLY that one hypothetical position, evolve it forward bar-by-bar
in lockstep with the real run using the exact same exit-tier logic
(BacktestPortfolio.update/manage_exits -- not reimplemented, reused), and
compare its eventual P&L to whatever the real run actually did in its place
(a different ticker, or nothing). Sum the differences -- that sum is the
filter's real, uncontaminated effect.

Usage:
    python3 snipe_concentration_counterfactual.py --start 2026-06-08 --end 2026-07-18
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from kalshi_btc_bot import config as C
from kalshi_btc_bot.signals import (
    SignalEngine, SNIPE_MIN_EDGE_RATIO, SNIPE_MIN_ENTRY_PRICE,
    SNIPE_MAX_ENTRY_PRICE, MIN_HOURS, MAX_HOURS, _clustered,
)
import kalshi_btc_backtest as B


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--capital", type=float, default=500.0)
    args = ap.parse_args()

    diversions = []   # dicts: bar_ts, blocked contract, true_prob, actual result ticker
    shadows = []       # dicts: diversion + 'portfolio' (BacktestPortfolio)

    orig_find_snipe = SignalEngine.find_snipe

    def patched_find_snipe(self, spot, vol, regime, ladder, existing):
        use_t     = regime["use_t"]
        direction = regime["direction"]
        snipe_expiries = {t.rsplit("-", 1)[0] for t, p in existing.items() if p.get("is_snipe")}

        best_ratio_nf = 1.0 + SNIPE_MIN_EDGE_RATIO
        best_nf = None
        for c in ladder:
            if c["ticker"] in existing:
                continue
            if _clustered(c["ticker"], c["strike"], existing):
                continue
            if c["hours"] < MIN_HOURS or c["hours"] > MAX_HOURS:
                continue
            if c["ask"] < SNIPE_MIN_ENTRY_PRICE or c["ask"] > SNIPE_MAX_ENTRY_PRICE:
                continue
            ctype = c["type"]
            if use_t:
                if ctype == "ABOVE" and direction == "DN":
                    continue
                if ctype == "BELOW" and direction == "UP":
                    continue
            true_p = self.dist.true_prob(c, spot, vol, c["hours"], regime)
            if true_p <= c["ask"]:
                continue
            ratio = true_p / c["ask"]
            if ratio > best_ratio_nf:
                best_ratio_nf = ratio
                best_nf = c

        result = orig_find_snipe(self, spot, vol, regime, ladder, existing)

        if best_nf is not None and best_nf["ticker"].rsplit("-", 1)[0] in snipe_expiries:
            result_ticker = result["ticker"] if result else None
            if result_ticker != best_nf["ticker"]:
                diversions.append({
                    "blocked_contract": dict(best_nf),
                    "true_prob": self.dist.true_prob(best_nf, spot, vol, best_nf["hours"], regime),
                    "result_ticker": result_ticker,
                })
        return result

    SignalEngine.find_snipe = patched_find_snipe

    orig_update = B.BacktestPortfolio.update
    orig_manage = B.BacktestPortfolio.manage_exits

    def patched_update(self, *a, **kw):
        r = orig_update(self, *a, **kw)
        if getattr(self, "_is_root", False):
            for sh in shadows:
                if sh["portfolio"].positions:
                    orig_update(sh["portfolio"], *a, **kw)
        return r

    def patched_manage(self, *a, **kw):
        r = orig_manage(self, *a, **kw)
        if getattr(self, "_is_root", False):
            for sh in shadows:
                if sh["portfolio"].positions:
                    orig_manage(sh["portfolio"], *a, **kw)
            # after this bar's diversions are recorded, open shadows for any new ones
            while len(shadows) < len(diversions):
                d = diversions[len(shadows)]
                shp = B.BacktestPortfolio(capital=args.capital, use_kelly=True)
                shp.buy(d["blocked_contract"], d["true_prob"], kw.get("bar_ts", a[1] if len(a) > 1 else None),
                        is_snipe=True)
                shadows.append({**d, "portfolio": shp})
        return r

    B.BacktestPortfolio.update = patched_update
    B.BacktestPortfolio.manage_exits = patched_manage

    orig_init = B.BacktestPortfolio.__init__
    root_holder = {}

    def patched_init(self, *a, **kw):
        orig_init(self, *a, **kw)
        if "root" not in root_holder:
            root_holder["root"] = self
            self._is_root = True
        else:
            self._is_shadow = True

    B.BacktestPortfolio.__init__ = patched_init

    m = B.run_backtest(capital=args.capital, start_date=args.start, end_date=args.end,
                       verbose=False, use_kelly=True, use_vol_surface=False)

    SignalEngine.find_snipe = orig_find_snipe
    B.BacktestPortfolio.update = orig_update
    B.BacktestPortfolio.manage_exits = orig_manage
    B.BacktestPortfolio.__init__ = orig_init

    print(f"\nDiversions found: {len(diversions)}  Shadows opened: {len(shadows)}")

    root = root_holder["root"]
    # match each diversion's actual replacement trade by ticker (nearest entered_at)
    total_blocked_pnl = 0.0
    total_actual_pnl = 0.0
    resolved = 0
    still_open = 0
    for sh in shadows:
        shp = sh["portfolio"]
        if shp.trades:
            blocked_pnl = shp.trades[0]["pnl"]
        elif shp.positions:
            still_open += 1
            continue
        else:
            blocked_pnl = 0.0  # buy() itself failed (e.g. cash-constrained shadow)

        actual_pnl = 0.0
        if sh["result_ticker"]:
            matches = [t for t in root.trades if t["ticker"] == sh["result_ticker"]]
            if matches:
                actual_pnl = matches[0]["pnl"]

        total_blocked_pnl += blocked_pnl
        total_actual_pnl += actual_pnl
        resolved += 1

    print(f"Resolved: {resolved}  Still-open at window end (excluded): {still_open}")
    print(f"\nSum P&L if blocked candidates had been bought instead: {total_blocked_pnl:+.2f}")
    print(f"Sum P&L of what was actually bought (or $0 if nothing): {total_actual_pnl:+.2f}")
    print(f"\nNet effect of the one-snipe-per-expiry filter: {total_actual_pnl - total_blocked_pnl:+.2f}")
    print("  (positive = filter helped, i.e. what was actually bought beat the blocked pick;")
    print("   negative = filter hurt, i.e. the blocked pick would have done better)")


if __name__ == "__main__":
    main()
