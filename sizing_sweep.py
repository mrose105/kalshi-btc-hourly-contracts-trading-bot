"""
Sweep KELLY_CAP/MAX_TRADE_PCT (the per-trade position-size ceiling) to find
a defensible baseline, with a genuine tuning/validation split rather than
picking the in-sample maximum.

Why the split matters: docs/QUANT_STANDARDS_AUDIT.md's #1 finding was that
prior sweeps (PEAK_GIVEBACK_FRACTION, NO_STOP, BOUNDARY_NO_ZSCORE_MIN) were
all grid-searched against a single window and kept whichever value scored
best on that SAME window — exactly the selection-bias failure mode the
deflated Sharpe ratio exists to catch (deflated_sharpe.py). This script
tunes on one window and re-checks candidates on a separate, untouched one;
a value that only looks good on the window that picked it is noise, not a
baseline.

Two more things this specific sweep needs that earlier ones didn't:
  - Position sizing interacts with the capacity constraint
    (docs/BACKTEST_INTEGRITY.md §7) — a bigger per-trade cap does not
    straightforwardly mean bigger returns anymore, since larger fills eat
    more of _size_impact_penalty(). Run at the ACTUAL capital in question,
    not $10K, or the interaction is invisible.
  - Every value in the grid is a genuinely different question ("how much
    should Kelly be allowed to scale up to"), not points on a smooth,
    obviously-unimodal curve — report the whole curve, not just the winner.

Usage:
    python3 sizing_sweep.py                      # default grid, $500 capital
    python3 sizing_sweep.py --capital 500 --grid 0.01,0.02,0.025,0.03,0.05
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from kalshi_btc_bot import config as C
from kalshi_btc_backtest import run_backtest

TUNE_START,  TUNE_END  = "2026-06-07", "2026-07-17"   # 40 days
VALID_START, VALID_END = "2026-07-17", "2026-08-05"   # 19 days, held out
# yfinance's 5m interval hard-limits any request to the last 60 days from
# now (confirmed empirically: 59 days back succeeds, 60 fails). TUNE_START
# is pinned just inside that boundary — update both constants together if
# re-running this later, since the boundary itself moves with "now".


def run_grid(grid: list[float], capital: float, start: str, end: str) -> list[dict]:
    orig_kelly_cap, orig_max_trade = C.KELLY_CAP, C.MAX_TRADE_PCT
    results = []
    for pct in grid:
        C.KELLY_CAP = pct
        C.MAX_TRADE_PCT = pct
        m = run_backtest(capital=capital, start_date=start, end_date=end,
                         verbose=False, use_kelly=True, use_vol_surface=False)
        results.append({
            "pct": pct, "sharpe": m.get("sharpe", 0.0),
            "return_pct": m.get("return_pct", 0.0),
            "profit_factor": m.get("profit_factor", 0.0),
            "max_dd": m.get("max_drawdown_pct", 0.0),
            "trades": m.get("total_trades", 0),
        })
    C.KELLY_CAP, C.MAX_TRADE_PCT = orig_kelly_cap, orig_max_trade
    return results


def print_table(results: list[dict], label: str) -> None:
    print(f"\n{label}")
    print(f"  {'pct':>6}  {'sharpe':>7}  {'return':>9}  {'PF':>5}  {'maxDD':>7}  {'trades':>7}")
    for r in results:
        print(f"  {r['pct']:>5.1%}  {r['sharpe']:>7.2f}  {r['return_pct']:>+8.1f}%  "
              f"{r['profit_factor']:>5.2f}  {r['max_dd']:>6.1f}%  {r['trades']:>7}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--capital", type=float, default=500.0)
    ap.add_argument("--grid", default="0.010,0.015,0.020,0.025,0.030,0.040,0.050,0.075,0.100")
    ap.add_argument("--top-n", type=int, default=3,
                    help="how many tuning-window candidates to re-check on the held-out window")
    args = ap.parse_args()

    grid = [float(x) for x in args.grid.split(",")]
    print(f"Position-size sweep at ${args.capital:,.0f} capital")
    print(f"  grid: {[f'{g:.1%}' for g in grid]}")
    print(f"  tune window:  {TUNE_START} -> {TUNE_END} (40d)")
    print(f"  valid window: {VALID_START} -> {VALID_END} (20d, held out during selection)")

    tune = run_grid(grid, args.capital, TUNE_START, TUNE_END)
    print_table(tune, "=== TUNING WINDOW (used to pick candidates) ===")

    by_sharpe = sorted(tune, key=lambda r: -r["sharpe"])[:args.top_n]
    candidates = sorted({r["pct"] for r in by_sharpe} | {C.KELLY_CAP})  # always include current baseline
    print(f"\nRe-checking on the HELD-OUT window: "
          f"{[f'{c:.1%}' for c in candidates]} (top {args.top_n} by tuning Sharpe, plus current baseline)")

    valid = run_grid(candidates, args.capital, VALID_START, VALID_END)
    print_table(valid, "=== VALIDATION WINDOW (never touched during selection) ===")

    tune_rank = {r["pct"]: i for i, r in enumerate(sorted(tune, key=lambda r: -r["sharpe"]))}
    valid_sorted = sorted(valid, key=lambda r: -r["sharpe"])
    print(f"\n{'='*60}\nREAD\n{'='*60}")
    print(f"  current baseline: {C.KELLY_CAP:.1%}")
    print(f"  best on tuning window: {sorted(tune, key=lambda r: -r['sharpe'])[0]['pct']:.1%}")
    print(f"  best on validation window: {valid_sorted[0]['pct']:.1%}")
    if valid_sorted[0]["pct"] == sorted(tune, key=lambda r: -r["sharpe"])[0]["pct"]:
        print("  -> SAME value wins on both windows. Real signal, not just an in-sample fit.")
    else:
        print("  -> Different value wins out-of-sample. The tuning-window winner did NOT")
        print("     generalize -- treat the tuning-only ranking as noise, not a recommendation.")
    print("\n  This is one 40/20 split, not a formal significance test (see deflated_sharpe.py")
    print("  for that machinery). Treat agreement between the two windows as supportive")
    print("  evidence, not proof -- and re-run periodically as more paper/live data accrues.")


if __name__ == "__main__":
    main()
