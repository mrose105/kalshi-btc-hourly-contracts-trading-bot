"""
Calibrate the regime classifier's momentum window, with a tuning/validation split.

Why: RegimeEngine hardcoded feed.momentum(60). On live 2s ticks that is 60
SECONDS, and against 212k recorded live ticks |mom| clears TREND_THRESHOLD on
only 0.5% of them — hence live classified TRENDING 0.37% of the time and
BREAKOUT 0 times in 212,331 ticks. Meanwhile SyntheticFeed.recent() stretched
every window by TIME_SCALE (150x), so the SAME constant meant 2.5 HOURS in the
backtest, which reported 43% TRENDING / 18% BREAKOUT. Two environments, one
constant, opposite regimes — so nothing regime-dependent (drift in true_prob,
use_t directional gating) has ever been validly backtested.

This sweeps MOMENTUM_WINDOW_SECS with MOMENTUM_WINDOW_SCALED=False, i.e. both
sides measuring the same real elapsed time, against the historical baseline
(60s, scaled) as the control.

Usage:
    python3 momentum_window_sweep.py --capital 500
"""
import argparse, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from kalshi_btc_bot import config as C
from kalshi_btc_backtest import run_backtest

TUNE_START,  TUNE_END  = "2026-06-15", "2026-07-24"
VALID_START, VALID_END = "2026-07-24", "2026-08-12"


def run(label, start, end, capital, secs, scaled):
    C.MOMENTUM_WINDOW_SECS, C.MOMENTUM_WINDOW_SCALED = secs, scaled
    m = run_backtest(capital=capital, start_date=start, end_date=end,
                     verbose=False, use_kelly=True, use_vol_surface=False)
    print(f"  {label:>26} {m['total_trades']:>7} {m['win_rate']:>5.1f}% "
          f"{m['return_pct']:>+8.1f}% {m['sharpe']:>7.2f} {m['max_drawdown_pct']:>6.1f}% "
          f"{m.get('profit_factor',0):>5.2f}")
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=500.0)
    ap.add_argument("--grid", default="300,600,900,1800,3600")
    args = ap.parse_args()
    grid = [int(x) for x in args.grid.split(",")]
    o_s, o_sc = C.MOMENTUM_WINDOW_SECS, C.MOMENTUM_WINDOW_SCALED

    results = {}
    for wl, (s, e) in (("TUNING", (TUNE_START, TUNE_END)),
                       ("VALIDATION", (VALID_START, VALID_END))):
        print(f"\n=== {wl}  {s} -> {e} ===")
        print(f"  {'config':>26} {'trades':>7} {'WR':>6} {'return':>9} "
              f"{'Sharpe':>7} {'maxDD':>7} {'PF':>5}")
        results[wl] = {}
        results[wl]["baseline 60s (scaled)"] = run(
            "baseline 60s (scaled)", s, e, args.capital, 60, True)
        for w in grid:
            results[wl][f"{w}s real-time"] = run(
                f"{w}s real-time", s, e, args.capital, w, False)

    C.MOMENTUM_WINDOW_SECS, C.MOMENTUM_WINDOW_SCALED = o_s, o_sc
    print(f"\n{'='*64}\nREAD\n{'='*64}")
    for wl in ("TUNING", "VALIDATION"):
        best = max(results[wl], key=lambda k: results[wl][k]["sharpe"])
        print(f"  best on {wl.lower():<11}: {best}  "
              f"(Sharpe {results[wl][best]['sharpe']:.2f}, "
              f"return {results[wl][best]['return_pct']:+.1f}%)")
    bt = max(results["TUNING"], key=lambda k: results["TUNING"][k]["sharpe"])
    bv = max(results["VALIDATION"], key=lambda k: results["VALIDATION"][k]["sharpe"])
    print("  -> SAME config wins both windows." if bt == bv else
          "  -> Different winner out-of-sample; treat the tuning ranking as noise.")


if __name__ == "__main__":
    main()
