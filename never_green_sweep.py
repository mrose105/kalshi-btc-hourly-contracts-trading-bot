"""
Sweep CUT_NEVER_GREEN_MINS with a tuning/validation split.

THE OBSERVATION
---------------
In the corrected-instrument backtest (RANGE_WIDTH=100), 58 of 313 trades never
traded above their entry price at any point in their life. Those 58 lost
-$324.47 between them, and only 7 (12.1%) ended positive. Their median hold was
15 minutes — so nothing in the existing exit ladder is catching them early:
peak_giveback needs a peak to give back, and stop_loss waits for a fixed %
adverse move that a slowly-bleeding contract takes a long time to reach.

The live book says the same thing. In the 2026-08-13 post-restart session, 5 of
the 8 losing trades never went green, costing -$16.67 of the -$17.96 session.

THE RULE
--------
If a position has been open >= N minutes and has never shown an unrealised
gain, close it. This is a distinct failure mode from the two the ladder already
covers — it is the position that was wrong from the first tick.

WHAT WOULD FALSIFY IT
---------------------
That 12.1% recovery rate is measured on trades the bot HELD, so it is not
survivorship-free: a trade cut at 5 minutes might have recovered later, and
those recoveries are exactly what this rule gives up. The backtest replays the
real price path past the cut, so the sweep prices that giveup honestly. If the
recovered winners are large enough, the rule loses money despite killing losers
at an 88% hit rate. That is the question here, and the grid includes 0 (off) so
the baseline competes on equal terms.

Granularity caveat, stated: the backtest steps in 5-minute bars, so N is
resolved to whole bars. 5 = 1 bar, 10 = 2 bars. Live runs on 2s ticks and can
honour any N; a value that only wins at exactly one bar boundary should be
treated as noise, not signal.

Usage:
    python3 never_green_sweep.py --capital 500
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
    C.CUT_NEVER_GREEN_MINS = v
    m = run_backtest(capital=capital, start_date=start, end_date=end,
                     verbose=False, use_kelly=True, use_vol_surface=False)
    ng = m.get("by_exit_reason", {}).get("never_green", {})
    print(f"  {v:>5} {m['total_trades']:>7} {m['win_rate']:>6.1f}% "
          f"{m['return_pct']:>+9.1f}% {m['sharpe']:>7.2f} "
          f"{m['max_drawdown_pct']:>7.1f}% {m.get('profit_factor', 0):>6.2f} "
          f"{ng.get('count', 0):>6} {ng.get('pnl', 0):>+9.2f}")
    return m


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--capital", type=float, default=500.0)
    ap.add_argument("--grid", default="0,5,10,15,20,30")
    args = ap.parse_args()
    grid = [int(x) for x in args.grid.split(",")]
    orig = C.CUT_NEVER_GREEN_MINS

    out = {}
    for lbl, (s, e) in (("TUNING", (TUNE_START, TUNE_END)),
                        ("VALIDATION", (VALID_START, VALID_END))):
        print(f"\n=== {lbl}  {s} -> {e} ===")
        print(f"  {'MINS':>5} {'trades':>7} {'WR':>7} {'return':>10} "
              f"{'Sharpe':>7} {'maxDD':>8} {'PF':>6} {'ng_n':>6} {'ng_pnl':>9}")
        out[lbl] = {v: run(v, s, e, args.capital) for v in grid}
    C.CUT_NEVER_GREEN_MINS = orig

    bt = max(out["TUNING"],     key=lambda v: out["TUNING"][v]["sharpe"])
    bv = max(out["VALIDATION"], key=lambda v: out["VALIDATION"][v]["sharpe"])
    print(f"\n{'=' * 66}\nREAD\n{'=' * 66}")
    print(f"  current baseline:      {orig} min (0 = disabled)")
    print(f"  best on TUNING:        {bt} min  "
          f"(Sharpe {out['TUNING'][bt]['sharpe']:.2f}, "
          f"return {out['TUNING'][bt]['return_pct']:+.1f}%)")
    print(f"  best on VALIDATION:    {bv} min  "
          f"(Sharpe {out['VALIDATION'][bv]['sharpe']:.2f}, "
          f"return {out['VALIDATION'][bv]['return_pct']:+.1f}%)")
    if bt == bv and bt != 0:
        print(f"\n  AGREES — {bt} min wins both windows. Actionable.")
    elif bt == 0 or bv == 0:
        print("\n  A window prefers the rule OFF. Do not enable on this evidence.")
    else:
        print(f"\n  DISAGREES ({bt} vs {bv}). Not tunable out-of-sample; if the "
              "rule is kept it must be justified structurally, not by this sweep.")

    # A structural rule can be right even when the sweep will not pick a value.
    # Report whether ANY setting beats baseline on BOTH windows, which is a
    # weaker and more honest claim than "N is optimal".
    base_t = out["TUNING"][0]["sharpe"]
    base_v = out["VALIDATION"][0]["sharpe"]
    both = [v for v in grid if v != 0
            and out["TUNING"][v]["sharpe"] > base_t
            and out["VALIDATION"][v]["sharpe"] > base_v]
    print(f"\n  values beating OFF on BOTH windows: "
          f"{both if both else 'none'}")


if __name__ == "__main__":
    main()
