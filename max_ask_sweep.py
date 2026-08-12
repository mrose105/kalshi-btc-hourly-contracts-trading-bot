"""
Sweep MAX_ASK with a tuning/validation split.

Motivation: the market is RANGING ~70% / REVERTING ~30% of the time, and of the
ticks that have a tradeable ladder, 86.3% match NEITHER entry strategy — normal
vol (vr median 1.00) and mild z (median 0.98). In a ranging market the natural
trade is the band CONTAINING spot, but MAX_ASK=0.45 blocks it: a quiet ATM band
trades 50-85c.

This cannot be tested against recordings — every recorded ladder row already
passed MAX_ASK, so the data is censored by the exact filter under test (which is
why record_universe now captures the pre-filter window). The backtest builds its
own ladder unbounded, so it can answer the gate question, with the caveat that
its quotes are synthetic.

RESULT (2026-08-11): INCONCLUSIVE — the backtest cannot answer this either.
Its ladder tops out around $0.44 (verified: at 10 min to expiry the highest ask
build_ladder generates is 0.44), because MIN_HOURS excludes anything inside 6
minutes and a 100-wide band cannot get more probable than that further out.
Raising MAX_ASK 0.45 -> 0.85 admits nothing, so every value scores byte
-identically (207 trades / +72.7% / Sharpe 5.61 on tuning; 37 / +22.9% / 15.30
on validation). That identical-results pattern is the same tell that exposed
the cooldown and momentum sweeps.

Both data sources are therefore blind here, for opposite reasons:
  recordings — censored, every row already passed MAX_ASK
  backtest   — its synthetic ladder never prices above ~0.45
Only recorder.record_universe (added the same day, captures the pre-filter
window) can settle it. Re-run this against real universe data once a few days
have accrued.

Usage:
    python3 max_ask_sweep.py --capital 500
"""
import argparse, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from kalshi_btc_bot import config as C
from kalshi_btc_backtest import run_backtest

TUNE_START,  TUNE_END  = "2026-06-15", "2026-07-24"
VALID_START, VALID_END = "2026-07-24", "2026-08-12"


def run(v, start, end, capital):
    C.MAX_ASK = v
    m = run_backtest(capital=capital, start_date=start, end_date=end,
                     verbose=False, use_kelly=True, use_vol_surface=False)
    print(f"  {v:>6.2f} {m['total_trades']:>7} {m['win_rate']:>6.1f}% "
          f"{m['return_pct']:>+9.1f}% {m['sharpe']:>7.2f} "
          f"{m['max_drawdown_pct']:>7.1f}% {m.get('profit_factor',0):>6.2f}")
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=500.0)
    ap.add_argument("--grid", default="0.45,0.55,0.65,0.75,0.85")
    args = ap.parse_args()
    grid = [float(x) for x in args.grid.split(",")]
    orig = C.MAX_ASK
    out = {}
    for lbl,(s,e) in (("TUNING",(TUNE_START,TUNE_END)),
                      ("VALIDATION",(VALID_START,VALID_END))):
        print(f"\n=== {lbl}  {s} -> {e} ===")
        print(f"  {'MAX_ASK':>6} {'trades':>7} {'WR':>7} {'return':>10} {'Sharpe':>7} {'maxDD':>8} {'PF':>6}")
        out[lbl] = {v: run(v, s, e, args.capital) for v in grid}
    C.MAX_ASK = orig
    bt = max(out["TUNING"], key=lambda v: out["TUNING"][v]["sharpe"])
    bv = max(out["VALIDATION"], key=lambda v: out["VALIDATION"][v]["sharpe"])
    print(f"\n{'='*60}\nREAD\n{'='*60}")
    print(f"  current baseline: {orig:.2f}")
    print(f"  best on tuning:     {bt:.2f}  (Sharpe {out['TUNING'][bt]['sharpe']:.2f})")
    print(f"  best on validation: {bv:.2f}  (Sharpe {out['VALIDATION'][bv]['sharpe']:.2f})")
    print("  -> SAME value wins both windows." if bt==bv else
          "  -> Different winner out-of-sample; treat the tuning ranking as noise.")


if __name__ == "__main__":
    main()
