"""
Sweep the two exit-tier gap fixes from exit_coverage_analysis.py, each with
its own tuning/validation split, same discipline as sizing_sweep.py /
cooldown_sweep.py / peak_giveback_bid_sweep.py.

Two independent one-parameter sweeps (not swept jointly) -- each holds the
other fix at its no-op default (1.50) so the two mechanisms don't confound
each other's read:

  1. SNIPE_STOP_PCT -- TIER 6-snipe catastrophe floor. Snipes currently skip
     TIER 5.25/6 entirely; 179 of 196 losing snipe exits in a 59d/$500
     backtest averaged -94.7% pnl_pct (vs -45.7% for non-snipe stopped/
     boundary_risk losses).

  2. PEAK_GIVEBACK_HARD_LOSS_PCT -- TIER 0.75b bypass of the peak_giveback
     bid floor once pnl has cratered past this threshold. time_exit_OTM
     trades averaged peak +105.5% -> exit -94.9% (200pp giveback, $2,848
     total) from crashes that fell below the bid floor in a single bar.

Usage:
    python3 exit_gap_fixes_sweep.py                      # both sweeps, $500 capital
    python3 exit_gap_fixes_sweep.py --which snipe_stop    # just the first
    python3 exit_gap_fixes_sweep.py --which hard_loss     # just the second
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from kalshi_btc_bot import config as C
from kalshi_btc_backtest import run_backtest

TUNE_START,  TUNE_END  = "2026-06-07", "2026-07-17"
VALID_START, VALID_END = "2026-07-17", "2026-08-05"

NOOP = 1.50  # both params floor pnl_pct's own -100% cap -- true no-op baseline


def run_grid(attr: str, grid: list[float], capital: float, start: str, end: str) -> list[dict]:
    orig = getattr(C, attr)
    results = []
    for val in grid:
        setattr(C, attr, val)
        m = run_backtest(capital=capital, start_date=start, end_date=end,
                         verbose=False, use_kelly=True, use_vol_surface=False)
        by_reason = m.get("by_exit_reason", {})
        results.append({
            "val": val, "sharpe": m.get("sharpe", 0.0),
            "return_pct": m.get("return_pct", 0.0),
            "profit_factor": m.get("profit_factor", 0.0),
            "max_dd": m.get("max_drawdown_pct", 0.0),
            "trades": m.get("total_trades", 0),
            "new_tier_n": by_reason.get(
                "snipe_stop" if attr == "SNIPE_STOP_PCT" else "peak_giveback", {}
            ).get("count", 0),
        })
    setattr(C, attr, orig)
    return results


def print_table(results: list[dict], label: str, tier_label: str) -> None:
    print(f"\n{label}")
    print(f"  {'val':>6}  {'sharpe':>7}  {'return':>9}  {'PF':>5}  {'maxDD':>7}  "
          f"{'trades':>7}  {tier_label:>10}")
    for r in results:
        print(f"  {r['val']:>5.2f}  {r['sharpe']:>7.2f}  {r['return_pct']:>+8.1f}%  "
              f"{r['profit_factor']:>5.2f}  {r['max_dd']:>6.1f}%  {r['trades']:>7}  "
              f"{r['new_tier_n']:>10}")


def run_one_sweep(attr: str, grid: list[float], capital: float, top_n: int, tier_label: str) -> None:
    print(f"\n{'#'*70}\n{attr} sweep (other fix held at no-op {NOOP})\n{'#'*70}")
    print(f"  grid: {grid}")
    print(f"  tune window:  {TUNE_START} -> {TUNE_END} (40d)")
    print(f"  valid window: {VALID_START} -> {VALID_END} (19d, held out during selection)")

    tune = run_grid(attr, grid, capital, TUNE_START, TUNE_END)
    print_table(tune, "=== TUNING WINDOW (used to pick candidates) ===", tier_label)

    by_sharpe = sorted(tune, key=lambda r: -r["sharpe"])[:top_n]
    candidates = sorted({r["val"] for r in by_sharpe} | {NOOP})
    print(f"\nRe-checking on the HELD-OUT window: {candidates} "
          f"(top {top_n} by tuning Sharpe, plus no-op baseline)")

    valid = run_grid(attr, candidates, capital, VALID_START, VALID_END)
    print_table(valid, "=== VALIDATION WINDOW (never touched during selection) ===", tier_label)

    valid_sorted = sorted(valid, key=lambda r: -r["sharpe"])
    tune_best = sorted(tune, key=lambda r: -r["sharpe"])[0]["val"]
    print(f"\n{'='*60}\nREAD: {attr}\n{'='*60}")
    print(f"  current (no-op) baseline: {NOOP}")
    print(f"  best on tuning window: {tune_best:.2f}")
    print(f"  best on validation window: {valid_sorted[0]['val']:.2f}")
    if valid_sorted[0]["val"] == tune_best:
        print("  -> SAME value wins on both windows. Real signal, not just an in-sample fit.")
    else:
        print("  -> Different value wins out-of-sample. The tuning-window winner did NOT")
        print("     generalize -- treat the tuning-only ranking as noise, not a recommendation.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--capital", type=float, default=500.0)
    ap.add_argument("--which", choices=["both", "snipe_stop", "hard_loss"], default="both")
    ap.add_argument("--top-n", type=int, default=3)
    args = ap.parse_args()

    print(f"Exit-tier gap fixes sweep at ${args.capital:,.0f} capital")

    if args.which in ("both", "snipe_stop"):
        run_one_sweep("SNIPE_STOP_PCT", [0.50, 0.65, 0.80, 0.95, NOOP],
                      args.capital, args.top_n, "snipe_stop_n")

    if args.which in ("both", "hard_loss"):
        run_one_sweep("PEAK_GIVEBACK_HARD_LOSS_PCT", [0.30, 0.50, 0.65, 0.80, NOOP],
                      args.capital, args.top_n, "pkgvbk_n")

    print("\n  Each is a one 40/19 split, not a formal significance test. Re-run")
    print("  periodically as more paper/live data accrues.")


if __name__ == "__main__":
    main()
