"""
Sweep SNIPE_PEAK_GIVEBACK_MIN_BID with a tuning/validation split, same
discipline as sizing_sweep.py and cooldown_sweep.py.

Prompted by a real 2026-08-04 loss walkthrough: a snipe (entry $0.13) ran to
peak +42% then +46% (bid $0.17-$0.185) and gave it all back to zero.
peak_giveback never engaged because it never crossed PEAK_GIVEBACK_MIN_BID
($0.20) -- snipes enter at 10-25c (SNIPE_MIN/MAX_ENTRY_PRICE), so that
shared floor sits INSIDE their entry range. A real percentage-sized run can
still never clear the absolute-cents gate meant to protect it.

Scoped deliberately: sweeps ONLY SNIPE_PEAK_GIVEBACK_MIN_BID, the
snipe-specific split added alongside the original PEAK_GIVEBACK_MIN_BID
(kalshi_btc_bot/config.py). The general (non-snipe) floor is untouched --
it wasn't shown broken, and PEAK_GIVEBACK_MIN_BID's own "don't lock trivial
cents" rationale still applies to ordinary entries, which trade at
different, typically higher prices.

Usage:
    python3 peak_giveback_bid_sweep.py                    # default grid, $500 capital
    python3 peak_giveback_bid_sweep.py --grid 0.05,0.10,0.15,0.20
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from kalshi_btc_bot import config as C
from kalshi_btc_backtest import run_backtest

TUNE_START,  TUNE_END  = "2026-06-07", "2026-07-17"
VALID_START, VALID_END = "2026-07-17", "2026-08-05"


def run_grid(grid: list[float], capital: float, start: str, end: str) -> list[dict]:
    orig = C.SNIPE_PEAK_GIVEBACK_MIN_BID
    results = []
    for bid_floor in grid:
        C.SNIPE_PEAK_GIVEBACK_MIN_BID = bid_floor
        m = run_backtest(capital=capital, start_date=start, end_date=end,
                         verbose=False, use_kelly=True, use_vol_surface=False)
        pg = m.get("by_exit_reason", {}).get("peak_giveback", {})
        results.append({
            "bid": bid_floor, "sharpe": m.get("sharpe", 0.0),
            "return_pct": m.get("return_pct", 0.0),
            "profit_factor": m.get("profit_factor", 0.0),
            "max_dd": m.get("max_drawdown_pct", 0.0),
            "trades": m.get("total_trades", 0),
            "pg_trades": pg.get("count", 0), "pg_wr": pg.get("win_rate", 0.0),
        })
    C.SNIPE_PEAK_GIVEBACK_MIN_BID = orig
    return results


def print_table(results: list[dict], label: str) -> None:
    print(f"\n{label}")
    print(f"  {'bid':>6}  {'sharpe':>7}  {'return':>9}  {'PF':>5}  {'maxDD':>7}  "
          f"{'trades':>7}  {'pg_n':>5}  {'pg_wr':>6}")
    for r in results:
        print(f"  ${r['bid']:>4.2f}  {r['sharpe']:>7.2f}  {r['return_pct']:>+8.1f}%  "
              f"{r['profit_factor']:>5.2f}  {r['max_dd']:>6.1f}%  {r['trades']:>7}  "
              f"{r['pg_trades']:>5}  {r['pg_wr']:>5.0f}%")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--capital", type=float, default=500.0)
    ap.add_argument("--grid", default="0.02,0.05,0.08,0.10,0.15,0.20")
    ap.add_argument("--top-n", type=int, default=3)
    args = ap.parse_args()

    grid = [float(x) for x in args.grid.split(",")]
    print(f"SNIPE_PEAK_GIVEBACK_MIN_BID sweep at ${args.capital:,.0f} capital "
          f"(general PEAK_GIVEBACK_MIN_BID fixed at ${C.PEAK_GIVEBACK_MIN_BID:.2f})")
    print(f"  grid: {[f'${g:.2f}' for g in grid]}")
    print(f"  tune window:  {TUNE_START} -> {TUNE_END} (40d)")
    print(f"  valid window: {VALID_START} -> {VALID_END} (19d, held out during selection)")

    tune = run_grid(grid, args.capital, TUNE_START, TUNE_END)
    print_table(tune, "=== TUNING WINDOW (used to pick candidates) ===")

    by_sharpe = sorted(tune, key=lambda r: -r["sharpe"])[:args.top_n]
    candidates = sorted({r["bid"] for r in by_sharpe} | {C.SNIPE_PEAK_GIVEBACK_MIN_BID})
    print(f"\nRe-checking on the HELD-OUT window: {[f'${c:.2f}' for c in candidates]} "
          f"(top {args.top_n} by tuning Sharpe, plus current baseline)")

    valid = run_grid(candidates, args.capital, VALID_START, VALID_END)
    print_table(valid, "=== VALIDATION WINDOW (never touched during selection) ===")

    valid_sorted = sorted(valid, key=lambda r: -r["sharpe"])
    tune_best = sorted(tune, key=lambda r: -r["sharpe"])[0]["bid"]
    print(f"\n{'='*60}\nREAD\n{'='*60}")
    print(f"  current baseline: ${C.SNIPE_PEAK_GIVEBACK_MIN_BID:.2f}")
    print(f"  best on tuning window: ${tune_best:.2f}")
    print(f"  best on validation window: ${valid_sorted[0]['bid']:.2f}")
    if valid_sorted[0]["bid"] == tune_best:
        print("  -> SAME value wins on both windows. Real signal, not just an in-sample fit.")
    else:
        print("  -> Different value wins out-of-sample. The tuning-window winner did NOT")
        print("     generalize -- treat the tuning-only ranking as noise, not a recommendation.")
    print("\n  One 40/19 split, not a formal significance test. Re-run periodically as")
    print("  more paper/live data accrues.")


if __name__ == "__main__":
    main()
