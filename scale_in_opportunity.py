"""
Measure the scale-in opportunity BEFORE building anything.

Today the bot can never add to an open position: both BacktestPortfolio.buy()
and the live Portfolio.buy() return False when `ticker in self.positions`, and
find_best/find_snipe skip held tickers before scoring them. So the bot has
never evaluated whether a contract it already owns is still worth buying.

This script answers the prior question: does that opportunity exist, and is it
on WINNERS or LOSERS? "Scale in" is only distinguishable from "average down" if
the re-qualifying moments skew to positions that are currently up.

Method -- reuse the real gate, don't reimplement it. For each open position at
each bar, find that ticker's current contract in the ladder and call the
production SignalEngine.find_best with a single-contract ladder and empty
`existing`. If it returns the contract, the bot would buy it again on its own
merits right now. Record the position's unrealized P&L at that moment, then
match against how the position actually resolved.

Read-only: patches nothing that changes trading behaviour, only observes.

Usage:
    python3 scale_in_opportunity.py --start 2026-06-19 --end 2026-08-16
"""
import argparse
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from kalshi_btc_bot.signals import SignalEngine
import kalshi_btc_backtest as B


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--capital", type=float, default=500.0)
    args = ap.parse_args()

    # key: (ticker, entered_at) -> list of {bars_held, unreal_pct, edge}
    events = defaultdict(list)
    bars_with_positions = 0
    position_bars = 0

    orig_find_best = SignalEngine.find_best

    def probing_find_best(self, spot, vol, regime, ladder, existing, vol_term=None):
        nonlocal bars_with_positions, position_bars
        if existing:
            bars_with_positions += 1
        by_ticker = {c["ticker"]: c for c in ladder}
        for tk, pos in existing.items():
            position_bars += 1
            c = by_ticker.get(tk)
            if c is None:
                continue  # contract no longer in the ladder (spot moved away)
            # Reuse the production gate verbatim on a one-contract ladder with
            # no existing positions -- returns the contract iff it would be
            # bought again right now on its own merits.
            hit = orig_find_best(self, spot, vol, regime, [c], {}, vol_term=None)
            if not hit:
                continue
            entry = pos["entry"]
            bid   = pos.get("bid_now", entry)
            unreal_pct = (bid - entry) / entry if entry else 0.0
            events[(tk, pos["entered_at"])].append({
                "bars_held":  pos["bars_held"],
                "unreal_pct": unreal_pct,
                "edge":       hit.get("edge", 0.0),
                "is_snipe":   pos.get("is_snipe", False),
            })
        return orig_find_best(self, spot, vol, regime, ladder, existing,
                              vol_term=vol_term)

    SignalEngine.find_best = probing_find_best
    try:
        B.run_backtest(capital=args.capital, start_date=args.start,
                       end_date=args.end, verbose=False, use_kelly=True,
                       use_vol_surface=False)
    finally:
        SignalEngine.find_best = orig_find_best

    print(f"\n{'='*66}")
    print("  SCALE-IN OPPORTUNITY")
    print(f"{'='*66}")
    print(f"  Position-bars observed:        {position_bars}")
    print(f"  Positions that ever re-qualify: {len(events)}")
    total_events = sum(len(v) for v in events.values())
    print(f"  Re-qualify events (add chances): {total_events}")
    if position_bars:
        print(f"  Re-qualify rate per position-bar: {total_events/position_bars:.1%}")

    if not total_events:
        print("\n  No re-qualification events -- scale-in has nothing to act on.")
        return

    flat = [e for v in events.values() for e in v]
    up   = [e for e in flat if e["unreal_pct"] > 0.001]
    dn   = [e for e in flat if e["unreal_pct"] < -0.001]
    fl   = [e for e in flat if abs(e["unreal_pct"]) <= 0.001]

    print(f"\n  At the moment of re-qualification, the position was:")
    print(f"    UP    {len(up):5d}  ({len(up)/len(flat):5.1%})   "
          f"median unreal {statistics.median([e['unreal_pct'] for e in up]):+.1%}"
          if up else "    UP        0")
    print(f"    FLAT  {len(fl):5d}  ({len(fl)/len(flat):5.1%})")
    print(f"    DOWN  {len(dn):5d}  ({len(dn)/len(flat):5.1%})   "
          f"median unreal {statistics.median([e['unreal_pct'] for e in dn]):+.1%}"
          if dn else "    DOWN      0")

    print(f"\n  --> {len(dn)/len(flat):.0%} of add-chances are on LOSING positions.")
    print("      Those are the ones to refuse: adding there is averaging down,")
    print("      which is exactly what we do not want.")

    adds_per_pos = [len(v) for v in events.values()]
    print(f"\n  Add-chances per qualifying position: "
          f"median {statistics.median(adds_per_pos):.0f}, max {max(adds_per_pos)}")
    print("  (a cap on adds/position matters -- see max)")


if __name__ == "__main__":
    main()
