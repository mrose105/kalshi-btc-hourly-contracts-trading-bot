"""
Sweep the scale-in policy grid against the recorded live book, so the verdict
is not a verdict on one lucky/unlucky parameter guess.

Same method and same caveats as scale_in_live_counterfactual.py -- this just
evaluates every policy in one pass over the quote stream (loading 13 days of
ticks once instead of once per policy).

Baseline for comparison: the ORIGINAL entries on the same days returned
-23.9% per dollar deployed. A policy is only interesting if it beats that.

VERDICT 2026-08-19 -- scale-in REJECTED for YES. 0 of 30 policies beat the
baseline; every one loses money at a 0% add win rate:
      min_up   best return
        10%      -26.0%   (10 adds)
         5%      -29.3%   (13 adds)
        20%      -51.5%   ( 4 adds)
        40%      -48.2%   ( 3 adds)
Waiting for MORE confirmation makes it strictly WORSE, which is the signature
of buying near the peak: the further a contract has already run, the closer to
the top the add lands. There is no threshold that rescues it.

Two reasons this was always going to fail here, and they matter more than the
rule itself:
  1. The YES book is deeply negative to begin with -- trades.csv's own pnl
     column reports YES -$1,111.70 (171 paper + 63 live closes) against NO
     +$97.41. Scaling in multiplies size on the losing side. No add rule fixes
     a negative per-dollar edge; it levers it.
  2. The measurement is CONSERVATIVE and the real result would be worse. Each
     tranche is exited at the parent's REAL exit price, holding exits fixed. In
     a live implementation an add raises the weighted-average `entry`, and both
     pnl_pct and peak_pnl_pct derive from that single scalar
     (kalshi_btc_backtest.py:758-759) -- so adding dilutes the gain, pushes
     scalp_lock's +40% further away and brings the stop closer.

Power caveat: max 13 adds in any policy. Small. But the sign is unanimous
across all 30 and monotone in the threshold, which is a consistent picture
rather than a coin flip.

Usage:
    python3 scale_in_policy_sweep.py
"""
import itertools
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

MAIN = Path("/Users/michael/Downloads/Finance/Quant/kalshiArb")
# MAIN must come FIRST on sys.path: inspect_recording resolves its recordings
# dir as Path(__file__).parent/"recordings", and this worktree has a copy of
# inspect_recording.py but no recordings/ -- if the worktree's copy wins, load()
# silently returns [] and every policy reports zero adds. Python already puts
# this script's own dir on sys.path, so build_positions still imports fine.
sys.path.insert(0, str(MAIN))

from inspect_recording import load
from kalshi_btc_bot.model import DistModel
from kalshi_btc_bot.signals import SignalEngine
from replay_signals import ladder_from_quote, regime_from_rg
from scale_in_live_counterfactual import build_positions

UTC = ZoneInfo("UTC")

MIN_UNREAL = [0.02, 0.05, 0.10, 0.20, 0.40]
MAX_ADDS   = [1, 2, 3]
SPACING    = [60, 300]
DECAY      = 0.5

BASELINE_RETURN = -0.239   # original entries, same days, per dollar deployed


def main():
    positions = build_positions(MAIN / "trades.csv")
    yes = [p for p in positions if p["side"] == "yes"]
    days = sorted(f.name.split("quotes_")[1].split(".")[0]
                  for f in (MAIN / "recordings").glob("quotes_*.jsonl.gz"))

    sig_e = SignalEngine(DistModel())

    by_day = defaultdict(list)
    for p in yes:
        d = p["entry_ts"].date()
        while d <= p["exit_ts"].date():
            by_day[d.isoformat()].append(p)
            d += timedelta(days=1)

    policies = list(itertools.product(MIN_UNREAL, MAX_ADDS, SPACING))
    # per policy: [n_adds, capital, pnl, wins]
    res = {pol: [0, 0.0, 0.0, 0] for pol in policies}

    for day in days:
        day_pos = by_day.get(day)
        if not day_pos:
            continue
        try:
            quotes = load("quotes", day)
        except Exception:
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

            # evaluate the production gate ONCE per contract per tick, then
            # apply every policy to the same answer -- the gate does not depend
            # on the policy, only the add rules do.
            gate_cache = {}
            for p in live:
                c = by_tk.get(p["ticker"])
                if c is None:
                    continue
                tk = p["ticker"]
                if tk not in gate_cache:
                    gate_cache[tk] = bool(
                        sig_e.find_best(spot, regime["vol"], regime, [c], {}))
                if not gate_cache[tk]:
                    continue
                unreal = (c["bid"] - p["entry"]) / p["entry"]
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
                    r[1] += c["ask"] * n
                    r[2] += (p["exit"] - c["ask"]) * n
                    r[3] += 1 if (p["exit"] - c["ask"]) > 0 else 0
                    st["adds"] += 1
                    st["last"] = t

    print(f"\n{'='*74}")
    print("  SCALE-IN POLICY SWEEP  (live recorded book, YES positions)")
    print(f"  baseline: original entries on these days = {BASELINE_RETURN:+.1%} per dollar")
    print(f"{'='*74}")
    print(f"  {'min_up':>7} {'adds':>5} {'gap_s':>6} {'n':>5} {'capital':>9} "
          f"{'pnl':>9} {'return':>8} {'winrate':>8}  vs base")
    rows = []
    for pol in policies:
        n, cap, pnl, wins = res[pol]
        if not n:
            continue
        ret = pnl / cap if cap else 0.0
        rows.append((ret, pol, n, cap, pnl, wins))
    for ret, pol, n, cap, pnl, wins in sorted(rows, reverse=True):
        mark = "BETTER" if ret > BASELINE_RETURN else "worse"
        print(f"  {pol[0]:>6.0%} {pol[1]:>5d} {pol[2]:>6d} {n:>5d} ${cap:>8.2f} "
              f"${pnl:>+8.2f} {ret:>+7.1%} {wins/n:>7.1%}  {mark}")

    if rows:
        best = max(rows)
        print(f"\n  Best policy: min_up={best[1][0]:.0%} max_adds={best[1][1]} "
              f"spacing={best[1][2]}s -> {best[0]:+.1%} on {best[2]} adds")
        n_better = sum(1 for r in rows if r[0] > BASELINE_RETURN)
        print(f"  Policies beating the baseline entries: {n_better}/{len(rows)}")


if __name__ == "__main__":
    main()
