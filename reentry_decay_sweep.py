"""
Sweep REENTRY_SIZE_DECAY with a tuning/validation split.

Caps each repeat entry on a ticker at DECAY x the dollars deployed on the
previous entry in that same ticker. See config.REENTRY_SIZE_DECAY for the
mechanism; in short, Kelly's f* = (true_prob - ask)/(1 - ask) RISES as a
contract collapses, so a model that merely fails to update gets bigger exactly
where it is losing.

The live instance (B63625, 2026-08-13) escalated 19 -> 33 -> 138 contracts
across three stopped-out attempts and produced the session's worst loss.

WHAT WOULD FALSIFY IT: re-entering bigger at a better price is what an averaging
strategy is SUPPOSED to do, and the whole-book split by attempt number is not
one-sided — attempt 2 is clearly worst (-$13.39 avg, n=36, robust to dropping
the single largest outlier) but attempt 3+ is positive (+$21.11, n=17). If the
cheap re-entries are where the money is, capping them costs real return and this
sweep will show it. 0 (off) is in the grid and competes on equal terms.

Usage:
    python3 reentry_decay_sweep.py --capital 500
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from kalshi_btc_bot import config as C
from kalshi_btc_backtest import run_backtest

TUNE_START,  TUNE_END  = "2026-06-18", "2026-07-27"
VALID_START, VALID_END = "2026-07-27", "2026-08-14"


def run(v, start, end, capital):
    C.REENTRY_SIZE_DECAY = v
    m = run_backtest(capital=capital, start_date=start, end_date=end,
                     verbose=False, use_kelly=True, use_vol_surface=False)
    print(f"  {v:>6.2f} {m['total_trades']:>7} {m['win_rate']:>6.1f}% "
          f"{m['return_pct']:>+9.1f}% {m['sharpe']:>7.2f} "
          f"{m['max_drawdown_pct']:>7.1f}% {m.get('profit_factor', 0):>6.2f}")
    return m


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--capital", type=float, default=500.0)
    ap.add_argument("--grid", default="0,1.0,0.75,0.5,0.25")
    args = ap.parse_args()
    grid = [float(x) for x in args.grid.split(",")]
    orig = C.REENTRY_SIZE_DECAY

    out = {}
    for lbl, (s, e) in (("TUNING", (TUNE_START, TUNE_END)),
                        ("VALIDATION", (VALID_START, VALID_END))):
        print(f"\n=== {lbl}  {s} -> {e} ===")
        print(f"  {'DECAY':>6} {'trades':>7} {'WR':>7} {'return':>10} "
              f"{'Sharpe':>7} {'maxDD':>8} {'PF':>6}")
        out[lbl] = {v: run(v, s, e, args.capital) for v in grid}
    C.REENTRY_SIZE_DECAY = orig

    bt = max(out["TUNING"],     key=lambda v: out["TUNING"][v]["sharpe"])
    bv = max(out["VALIDATION"], key=lambda v: out["VALIDATION"][v]["sharpe"])
    print(f"\n{'=' * 60}\nREAD\n{'=' * 60}")
    print(f"  current baseline:    {orig:.2f}  (0 = disabled)")
    print(f"  best on TUNING:      {bt:.2f}  "
          f"(Sharpe {out['TUNING'][bt]['sharpe']:.2f}, "
          f"return {out['TUNING'][bt]['return_pct']:+.1f}%)")
    print(f"  best on VALIDATION:  {bv:.2f}  "
          f"(Sharpe {out['VALIDATION'][bv]['sharpe']:.2f}, "
          f"return {out['VALIDATION'][bv]['return_pct']:+.1f}%)")
    if bt == bv and bt != 0.0:
        print(f"\n  AGREES — {bt:.2f} wins both windows. Actionable.")
    elif bt == 0.0 or bv == 0.0:
        print("\n  A window prefers the cap OFF. Do not enable on this evidence "
              "alone; a structural argument would have to carry it.")
    else:
        print(f"\n  DISAGREES ({bt:.2f} vs {bv:.2f}). Not tunable out-of-sample.")

    base_t = out["TUNING"][0.0]["sharpe"]
    base_v = out["VALIDATION"][0.0]["sharpe"]
    both = [v for v in grid if v != 0.0
            and out["TUNING"][v]["sharpe"] > base_t
            and out["VALIDATION"][v]["sharpe"] > base_v]
    print(f"\n  values beating OFF on BOTH windows: {both if both else 'none'}")

    # Tail risk is the point of this guard, so report it explicitly rather than
    # leaving it inside Sharpe: an escalating re-entry is a drawdown problem
    # first and a return problem second.
    print(f"\n  {'DECAY':>6} {'tuning maxDD':>14} {'valid maxDD':>13}")
    for v in grid:
        print(f"  {v:>6.2f} {out['TUNING'][v]['max_drawdown_pct']:>13.1f}% "
              f"{out['VALIDATION'][v]['max_drawdown_pct']:>12.1f}%")


if __name__ == "__main__":
    main()
