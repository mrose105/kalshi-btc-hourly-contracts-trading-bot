"""
Scale-in policy sweep for the NO side (BOUNDARY_NO) against the recorded book.

Why this exists: scale_in_policy_sweep.py rejected scale-in, but it tested YES
ONLY -- it skipped all 44 NO round-trips as "a different instrument". That
turned out to be testing the wrong side. trades.csv reports YES at -$1,111.70
against NO at +$97.41, and the live dashboard on 2026-08-19 shows the bot
trading NO exclusively: six BOUNDARY_NO round-trips, six wins, +$6.67, no YES
entry at all. So the scale-in question has to be re-asked on the side that
actually makes money.

NO price conventions (signals.py:323) -- getting these backwards silently
inverts the result:
    buy NO  costs 1 - yes_bid     (we are selling YES at the bid)
    sell NO receives 1 - yes_ask
    a held NO bought at cost e is up when (1 - yes_ask) > e

Gate: the production find_boundary_no on a one-contract ladder with empty
`existing`. start_total=0 is passed deliberately to bypass its cash-floor
branch (`if start_total > 0 and real_cash < ...`), isolating the signal from
portfolio cash state, which is not what we are measuring here.

CAVEAT -- this is OPTIMISTIC on fills, unlike the YES version. The live log
shows BOUNDARY_NO repeatedly skipped for "no book depth for NO at <=$0.82
(wanted 12)". Recorded quotes carry no depth ladder, so every add here is
assumed fillable at the quoted price. Real NO size is gated by liquidity, so
treat any positive result as an upper bound.

VERDICT 2026-08-19 -- scale-in REJECTED on NO too. 0 of 27 policies beat the
real NO entries, and the same monotone decay shows up: -9.9% at min_up 1%,
-16.5% at 5%. The diagnostic is cleaner here than on YES: add win rate is
58-64%, BELOW BOUNDARY_NO's own 75%. Scaling in degrades the exact property
that makes the strategy nearly work, and it does so while fills are assumed
free -- the real number is worse.

CORRECTION to an earlier read in this branch: "NO makes +$97.41, YES loses
$1,111" was wrong in the way that matters. That entire NO profit is ONE trade
(B64950 x333 @ $0.85 -> +$106.56, exit `misprice_time`), and it came from
MISPRICE_NO, which is disabled (ENABLE_MISPRICE_NO = False). Splitting the log
by the signal that opened each position:

    BOUNDARY_NO   44 trades   -$9.48    75% win rate   <- what runs live
    YES find_best 155 trades  -$413.00  42%
    snipe          76 trades  -$670.66  50%

So BOUNDARY_NO is near breakeven with a high hit rate -- it loses on payoff
asymmetry (buy NO at $0.82 risks 82c to make 18c), not on being wrong. It is
not the profit engine, but it is also not the problem. The problem is `snipe`:
76 trades for -$670.66, ~60% of all losses in the log. Any effort spent sizing
BOUNDARY_NO up or down is rounding error next to that.

Usage:
    python3 scale_in_no_sweep.py
"""
import itertools
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

MAIN = Path("/Users/michael/Downloads/Finance/Quant/kalshiArb")
# MAIN first -- see the note in scale_in_policy_sweep.py; the worktree copy of
# inspect_recording has no recordings/ and silently returns [].
sys.path.insert(0, str(MAIN))

from inspect_recording import load
from kalshi_btc_bot.model import DistModel
from kalshi_btc_bot.signals import SignalEngine
from replay_signals import ladder_from_quote, regime_from_rg
from scale_in_live_counterfactual import build_positions

UTC = ZoneInfo("UTC")

MIN_UNREAL = [0.01, 0.02, 0.05, 0.10]
MAX_ADDS   = [1, 2, 3]
SPACING    = [30, 60, 300]
DECAY      = 0.5


def main():
    positions = build_positions(MAIN / "trades.csv")
    no_pos = [p for p in positions if p["side"] == "no"]
    days = sorted(f.name.split("quotes_")[1].split(".")[0]
                  for f in (MAIN / "recordings").glob("quotes_*.jsonl.gz"))

    sig_e = SignalEngine(DistModel())

    by_day = defaultdict(list)
    for p in no_pos:
        d = p["entry_ts"].date()
        while d <= p["exit_ts"].date():
            by_day[d.isoformat()].append(p)
            d += timedelta(days=1)

    policies = list(itertools.product(MIN_UNREAL, MAX_ADDS, SPACING))
    res = {pol: [0, 0.0, 0.0, 0] for pol in policies}
    covered, probed = set(), 0

    for day in days:
        day_pos = by_day.get(day)
        if not day_pos:
            continue
        try:
            quotes = load("quotes", day)
        except Exception as e:
            print(f"  ! {day}: {e}")
            continue
        state = {(pol, id(p)): {"adds": 0, "last": None}
                 for pol in policies for p in day_pos}

        for q in quotes:
            if not q.get("l"):
                continue
            t = datetime.fromisoformat(q["t"]).astimezone(UTC)
            live = [p for p in day_pos if p["entry_ts"] <= t < p["exit_ts"]]
            if not live:
                continue
            by_tk = {c["ticker"]: c for c in ladder_from_quote(q)}
            regime = regime_from_rg(q.get("rg", {}))
            spot = q["spot"]
            gate_cache = {}

            for p in live:
                c = by_tk.get(p["ticker"])
                if c is None:
                    continue
                covered.add(id(p))
                probed += 1
                tk = p["ticker"]
                if tk not in gate_cache:
                    gate_cache[tk] = bool(sig_e.find_boundary_no(
                        spot, regime["vol"], regime, [c], {}, 1e9, 0.0))
                if not gate_cache[tk]:
                    continue
                no_bid = 1.0 - c["ask"]     # what we could sell the held NO at
                add_cost = 1.0 - c["bid"]   # what buying more NO costs
                unreal = (no_bid - p["entry"]) / p["entry"]
                for pol in policies:
                    min_u, max_a, spacing = pol
                    if unreal < min_u:
                        continue
                    st = state[(pol, id(p))]
                    if st["adds"] >= max_a:
                        continue
                    if st["last"] and (t - st["last"]).total_seconds() < spacing:
                        continue
                    n = max(1, int(round(p["count"] * DECAY ** (st["adds"] + 1))))
                    r = res[pol]
                    r[0] += 1
                    r[1] += add_cost * n
                    r[2] += (p["exit"] - add_cost) * n
                    r[3] += 1 if (p["exit"] - add_cost) > 0 else 0
                    st["adds"] += 1
                    st["last"] = t

    # baseline: what the real NO entries returned per dollar, same positions
    base_cap = sum(p["entry"] * p["count"] for p in no_pos if id(p) in covered)
    base_pnl = sum((p["exit"] - p["entry"]) * p["count"]
                   for p in no_pos if id(p) in covered)
    base_ret = base_pnl / base_cap if base_cap else 0.0

    print(f"\n{'='*76}")
    print("  SCALE-IN POLICY SWEEP -- NO SIDE (BOUNDARY_NO), live recorded book")
    print(f"{'='*76}")
    print(f"  NO round-trips: {len(no_pos)}   with quote coverage: {len(covered)}"
          f"   position-ticks: {probed}")
    print(f"  baseline (real NO entries, covered positions): "
          f"${base_pnl:+.2f} on ${base_cap:.2f} = {base_ret:+.1%} per dollar")
    print(f"\n  {'min_up':>7} {'adds':>5} {'gap_s':>6} {'n':>5} {'capital':>9} "
          f"{'pnl':>9} {'return':>8} {'winrate':>8}  vs base")

    rows = []
    for pol in policies:
        n, cap, pnl, wins = res[pol]
        if n:
            rows.append((pnl / cap if cap else 0.0, pol, n, cap, pnl, wins))
    for ret, pol, n, cap, pnl, wins in sorted(rows, reverse=True):
        mark = "BETTER" if ret > base_ret else "worse"
        print(f"  {pol[0]:>6.0%} {pol[1]:>5d} {pol[2]:>6d} {n:>5d} ${cap:>8.2f} "
              f"${pnl:>+8.2f} {ret:>+7.1%} {wins/n:>7.1%}  {mark}")

    if rows:
        best = max(rows)
        n_better = sum(1 for r in rows if r[0] > base_ret)
        print(f"\n  Best: min_up={best[1][0]:.0%} max_adds={best[1][1]} "
              f"spacing={best[1][2]}s -> {best[0]:+.1%} on {best[2]} adds "
              f"(${best[4]:+.2f})")
        print(f"  Policies beating the real NO entries: {n_better}/{len(rows)}")
        print("\n  Reminder: fills assumed available. The live log shows NO adds"
              "\n  getting skipped for lack of book depth, so this is an UPPER bound.")
    else:
        print("\n  No adds triggered under any policy.")


if __name__ == "__main__":
    main()
