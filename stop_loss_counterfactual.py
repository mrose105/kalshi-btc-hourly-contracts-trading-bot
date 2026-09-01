"""
Counterfactual for the "convex stop" hypothesis: does removing the 35%
stop_loss for far-OTM/cheap contracts let them recover, while it's still
needed for closer/pricier ones?

For every real non-snipe stop_loss exit in a run, fork a shadow position at
the moment the stop would fire: copy the exact position state into an
isolated BacktestPortfolio, mark it to skip ONLY the stop_loss tier (every
other tier -- peak_giveback, boundary_risk, expiry_settle -- still applies
normally), and let it ride the same price path forward using the real
manage_exits()/update() logic (not reimplemented) until it closes some other
way. The real position closes normally in the root run either way, so this
doesn't change the root run's own numbers -- it's a pure "what if" replay.

Buckets results by entry price (cheap ~ far OTM / high leverage, matching
the hypothesis) to see whether removing the stop helps disproportionately
for cheap entries or is a wash/negative across the board.

Usage:
    python3 stop_loss_counterfactual.py --start 2026-06-08 --end 2026-07-18
"""
import argparse
import sys
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))
import kalshi_btc_backtest as B


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--capital", type=float, default=500.0)
    args = ap.parse_args()

    shadows = []  # list of dicts: real_pnl, entry_price, itm_entry, portfolio

    orig_close = B.BacktestPortfolio._close
    orig_update = B.BacktestPortfolio.update
    orig_manage = B.BacktestPortfolio.manage_exits
    orig_init = B.BacktestPortfolio.__init__

    root_holder = {}

    def patched_init(self, *a, **kw):
        orig_init(self, *a, **kw)
        if "root" not in root_holder:
            root_holder["root"] = self
            self._is_root = True

    # Which stop tier to fork on. The YES ladder closes with "stop_loss"; the NO
    # ladder closes with "no_stop" (kalshi_btc_backtest.py, NO exit ladder). This
    # script only ever knew the YES name, so pointed at the live book — which has
    # run ENABLE_YES=False since the NO-only paper test — it forked nothing and
    # printed a +0.00 null result. Follow whichever side is enabled.
    STOP_REASON = "stop_loss" if B.C.ENABLE_YES else "no_stop"
    WANT_NO     = not B.C.ENABLE_YES

    def patched_close(self, ticker, bid, reason, bar_ts):
        if reason == STOP_REASON and getattr(self, "_is_root", False):
            pos = self.positions.get(ticker)
            if (pos is not None and not pos.get("is_snipe")
                    and bool(pos.get("is_no")) == WANT_NO):
                shp = B.BacktestPortfolio(capital=args.capital, use_kelly=True)
                shp.positions[ticker] = dict(pos)
                shp.positions[ticker]["_no_stop"] = True
                shp.cash = max(0.0, shp.capital - pos["cost"])
                shadows.append({
                    "ticker": ticker, "entry_price": pos["entry"],
                    "itm_entry": pos["contract"]["itm"], "count": pos["count"],
                    "portfolio": shp,
                })
        if reason == STOP_REASON and getattr(self, "_no_stop_active", None) is ticker:
            return  # shadow's own stop tier -- skip, let it ride
        return orig_close(self, ticker, bid, reason, bar_ts)

    def patched_update(self, *a, **kw):
        r = orig_update(self, *a, **kw)
        if getattr(self, "_is_root", False):
            for sh in shadows:
                if sh["portfolio"].positions:
                    orig_update(sh["portfolio"], *a, **kw)
        return r

    def patched_manage(self, *a, **kw):
        if self.positions and next(iter(self.positions.values())).get("_no_stop"):
            ticker = next(iter(self.positions.keys()))
            self._no_stop_active = ticker
        r = orig_manage(self, *a, **kw)
        self._no_stop_active = None
        if getattr(self, "_is_root", False):
            for sh in shadows:
                if sh["portfolio"].positions:
                    shp = sh["portfolio"]
                    t = next(iter(shp.positions.keys()))
                    shp._no_stop_active = t
                    orig_manage(shp, *a, **kw)
                    shp._no_stop_active = None
        return r

    B.BacktestPortfolio.__init__ = patched_init
    B.BacktestPortfolio._close = patched_close
    B.BacktestPortfolio.update = patched_update
    B.BacktestPortfolio.manage_exits = patched_manage

    m = B.run_backtest(capital=args.capital, start_date=args.start, end_date=args.end,
                       verbose=False, use_kelly=True, use_vol_surface=False)

    B.BacktestPortfolio.__init__ = orig_init
    B.BacktestPortfolio._close = orig_close
    B.BacktestPortfolio.update = orig_update
    B.BacktestPortfolio.manage_exits = orig_manage

    root = root_holder["root"]
    real_stop_trades = {t["ticker"]: t for t in root.trades if t["reason"] == STOP_REASON}

    print(f"\nReal {STOP_REASON} trades: {len(real_stop_trades)}  Shadows forked: {len(shadows)}")

    rows = []
    still_open = 0
    for sh in shadows:
        real = real_stop_trades.get(sh["ticker"])
        if real is None:
            continue
        shp = sh["portfolio"]
        if shp.positions:
            still_open += 1
            continue
        if not shp.trades:
            continue
        shadow_pnl = shp.trades[0]["pnl"]
        rows.append({
            "entry_price": sh["entry_price"], "itm_entry": sh["itm_entry"],
            "real_pnl": real["pnl"], "shadow_pnl": shadow_pnl,
            "improvement": shadow_pnl - real["pnl"],
            "shadow_reason": shp.trades[0]["reason"],
        })

    print(f"Resolved: {len(rows)}  Still open at window end (excluded): {still_open}")

    total_real = sum(r["real_pnl"] for r in rows)
    total_shadow = sum(r["shadow_pnl"] for r in rows)
    print(f"\nSum real (stopped at 35%) P&L:      {total_real:+.2f}")
    print(f"Sum shadow (stop removed) P&L:       {total_shadow:+.2f}")
    print(f"Net effect of removing the stop:     {total_shadow - total_real:+.2f}")

    # bucket by entry price tercile (cheap = high leverage/far-OTM proxy)
    rows_sorted = sorted(rows, key=lambda r: r["entry_price"])
    n = len(rows_sorted)
    if n >= 6:
        third = n // 3
        buckets = {
            "cheap (bottom 1/3 entry price)": rows_sorted[:third],
            "mid": rows_sorted[third:2*third],
            "expensive (top 1/3 entry price)": rows_sorted[2*third:],
        }
        print(f"\n{'bucket':>32}  {'n':>4}  {'avg entry':>10}  {'real pnl':>10}  {'shadow pnl':>11}  {'improvement':>12}")
        for label, rs in buckets.items():
            if not rs:
                continue
            avg_entry = sum(r["entry_price"] for r in rs) / len(rs)
            tr = sum(r["real_pnl"] for r in rs)
            ts = sum(r["shadow_pnl"] for r in rs)
            print(f"{label:>32}  {len(rs):>4}  ${avg_entry:>8.3f}  {tr:>+9.2f}  {ts:>+10.2f}  {ts-tr:>+11.2f}")

    by_itm = defaultdict(list)
    for r in rows:
        by_itm["ITM entry" if r["itm_entry"] else "OTM entry"].append(r)
    print()
    for label, rs in by_itm.items():
        tr = sum(r["real_pnl"] for r in rs)
        ts = sum(r["shadow_pnl"] for r in rs)
        print(f"{label:>12}: n={len(rs):>3}  real={tr:+.2f}  shadow={ts:+.2f}  improvement={ts-tr:+.2f}")

    by_shadow_reason = defaultdict(lambda: [0, 0.0])
    for r in rows:
        by_shadow_reason[r["shadow_reason"]][0] += 1
        by_shadow_reason[r["shadow_reason"]][1] += r["improvement"]
    print("\nWhat the shadow eventually exited via (if stop had been removed):")
    for reason, (cnt, imp) in sorted(by_shadow_reason.items(), key=lambda kv: -kv[1][0]):
        print(f"  {reason:>16}: n={cnt:>3}  total improvement vs real={imp:+.2f}")


if __name__ == "__main__":
    main()
