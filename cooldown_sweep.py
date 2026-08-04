"""
Sweep EXIT_COOLDOWN_SECS — the re-entry cooldown applied after a PROFITABLE
exit — with a tuning/validation split, same discipline as sizing_sweep.py.

Prompted by: "maybe the cooldown for re-entry is good for losers but the
winners, if it keeps winning this is when you want to keep going." Real
question: does a real edge that's still there right after a winning exit
get needlessly handcuffed by the same-style cooldown meant to stop chasing
a bad setup?

STOP_COOLDOWN_SECS (loss-cut cooldown, 300s) is held fixed — the user's
framing explicitly accepted that one as correct, and kalshi_btc_backtest.py
now classifies losses by realized pnl sign rather than exit-reason string
(fixes time_exit_OTM being mislabeled as a non-loss — it's 0% WR in every
run this session, but its reason string doesn't match "stop_"/"boundary_risk").

Usage:
    python3 cooldown_sweep.py                      # default grid, $500 capital
    python3 cooldown_sweep.py --grid 0,30,60,120,300
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from kalshi_btc_bot import config as C
from kalshi_btc_backtest import run_backtest

TUNE_START,  TUNE_END  = "2026-06-06", "2026-07-16"
VALID_START, VALID_END = "2026-07-16", "2026-08-04"


def run_grid(grid: list[int], capital: float, start: str, end: str) -> list[dict]:
    orig = C.EXIT_COOLDOWN_SECS
    results = []
    for secs in grid:
        C.EXIT_COOLDOWN_SECS = secs
        m = run_backtest(capital=capital, start_date=start, end_date=end,
                         verbose=False, use_kelly=True, use_vol_surface=False)
        results.append({
            "secs": secs, "sharpe": m.get("sharpe", 0.0),
            "return_pct": m.get("return_pct", 0.0),
            "profit_factor": m.get("profit_factor", 0.0),
            "max_dd": m.get("max_drawdown_pct", 0.0),
            "trades": m.get("total_trades", 0),
        })
    C.EXIT_COOLDOWN_SECS = orig
    return results


def print_table(results: list[dict], label: str) -> None:
    print(f"\n{label}")
    print(f"  {'secs':>6}  {'sharpe':>7}  {'return':>9}  {'PF':>5}  {'maxDD':>7}  {'trades':>7}")
    for r in results:
        print(f"  {r['secs']:>5}s  {r['sharpe']:>7.2f}  {r['return_pct']:>+8.1f}%  "
              f"{r['profit_factor']:>5.2f}  {r['max_dd']:>6.1f}%  {r['trades']:>7}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--capital", type=float, default=500.0)
    ap.add_argument("--grid", default="0,15,30,60,120,300")
    ap.add_argument("--top-n", type=int, default=3)
    args = ap.parse_args()

    grid = [int(x) for x in args.grid.split(",")]
    print(f"Win-exit cooldown sweep at ${args.capital:,.0f} capital "
          f"(loss cooldown fixed at {C.STOP_COOLDOWN_SECS}s)")
    print(f"  grid: {grid}s")
    print(f"  tune window:  {TUNE_START} -> {TUNE_END} (40d)")
    print(f"  valid window: {VALID_START} -> {VALID_END} (19d, held out during selection)")

    tune = run_grid(grid, args.capital, TUNE_START, TUNE_END)
    print_table(tune, "=== TUNING WINDOW (used to pick candidates) ===")

    by_sharpe = sorted(tune, key=lambda r: -r["sharpe"])[:args.top_n]
    candidates = sorted({r["secs"] for r in by_sharpe} | {C.EXIT_COOLDOWN_SECS})
    print(f"\nRe-checking on the HELD-OUT window: {candidates} "
          f"(top {args.top_n} by tuning Sharpe, plus current baseline)")

    valid = run_grid(candidates, args.capital, VALID_START, VALID_END)
    print_table(valid, "=== VALIDATION WINDOW (never touched during selection) ===")

    valid_sorted = sorted(valid, key=lambda r: -r["sharpe"])
    tune_best = sorted(tune, key=lambda r: -r["sharpe"])[0]["secs"]
    print(f"\n{'='*60}\nREAD\n{'='*60}")
    print(f"  current baseline: {C.EXIT_COOLDOWN_SECS}s")
    print(f"  best on tuning window: {tune_best}s")
    print(f"  best on validation window: {valid_sorted[0]['secs']}s")
    if valid_sorted[0]["secs"] == tune_best:
        print("  -> SAME value wins on both windows. Real signal, not just an in-sample fit.")
    else:
        print("  -> Different value wins out-of-sample. The tuning-window winner did NOT")
        print("     generalize -- treat the tuning-only ranking as noise, not a recommendation.")
    print("\n  One 40/19 split, not a formal significance test. Re-run periodically as")
    print("  more paper/live data accrues.")


if __name__ == "__main__":
    main()
