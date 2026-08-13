"""
Sweep PEAK_GIVEBACK_FRACTION with a tuning/validation split.

Motivation, from real post-exit prices (exit_timing_study.py): peak_giveback
books +35.3% on contracts that afterwards reach +114.4%, and snipe_lock books
+65.9% on ones reaching +134.6%. The tiers fire early.

Mechanism: peak_giveback exits once pnl fades to FRACTION of its own peak.
At 0.75 that is a 25% giveback — on a binary repricing violently between entry
and expiry, 25% is well inside ordinary noise, so the rule exits on the wobble
rather than the reversal. Lower FRACTION = more room before it fires.

Note the original 0.75 came from a single-window sweep with no holdout
(docs/QUANT_STANDARDS_AUDIT.md sec 1), so it has never been validated
out-of-sample.

Usage:
    python3 giveback_fraction_sweep.py --capital 500
"""
import argparse, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from kalshi_btc_bot import config as C
from kalshi_btc_backtest import run_backtest

TUNE_START,  TUNE_END  = "2026-06-18", "2026-07-27"
VALID_START, VALID_END = "2026-07-27", "2026-08-14"


def run(v, start, end, capital):
    C.PEAK_GIVEBACK_FRACTION = v
    m = run_backtest(capital=capital, start_date=start, end_date=end,
                     verbose=False, use_kelly=True, use_vol_surface=False)
    pg = m.get("by_exit_reason", {}).get("peak_giveback", {})
    print(f"  {v:>6.2f} {m['total_trades']:>7} {m['win_rate']:>6.1f}% "
          f"{m['return_pct']:>+9.1f}% {m['sharpe']:>7.2f} "
          f"{m['max_drawdown_pct']:>7.1f}% {m.get('profit_factor',0):>6.2f} "
          f"{pg.get('count',0):>6}")
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=500.0)
    ap.add_argument("--grid", default="0.40,0.50,0.60,0.70,0.75,0.85")
    args = ap.parse_args()
    grid = [float(x) for x in args.grid.split(",")]
    orig = C.PEAK_GIVEBACK_FRACTION
    out = {}
    for lbl,(s,e) in (("TUNING",(TUNE_START,TUNE_END)),
                      ("VALIDATION",(VALID_START,VALID_END))):
        print(f"\n=== {lbl}  {s} -> {e} ===")
        print(f"  {'FRAC':>6} {'trades':>7} {'WR':>7} {'return':>10} "
              f"{'Sharpe':>7} {'maxDD':>8} {'PF':>6} {'pg_n':>6}")
        out[lbl] = {v: run(v, s, e, args.capital) for v in grid}
    C.PEAK_GIVEBACK_FRACTION = orig
    bt = max(out["TUNING"], key=lambda v: out["TUNING"][v]["sharpe"])
    bv = max(out["VALIDATION"], key=lambda v: out["VALIDATION"][v]["sharpe"])
    print(f"\n{'='*60}\nREAD\n{'='*60}")
    print(f"  current baseline:   {orig:.2f}")
    print(f"  best on tuning:     {bt:.2f}  (Sharpe {out['TUNING'][bt]['sharpe']:.2f}, "
          f"return {out['TUNING'][bt]['return_pct']:+.1f}%)")
    print(f"  best on validation: {bv:.2f}  (Sharpe {out['VALIDATION'][bv]['sharpe']:.2f}, "
          f"return {out['VALIDATION'][bv]['return_pct']:+.1f}%)")
    print("  -> SAME value wins both windows." if bt==bv else
          "  -> Different winner out-of-sample; treat the tuning ranking as noise.")


if __name__ == "__main__":
    main()
