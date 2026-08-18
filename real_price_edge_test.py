"""
Does the model find edge against REAL Kalshi prices?

This is the test the backtest structurally cannot do. build_ladder() synthesises
its ask from the same DistModel that then evaluates it, so calibration_check.py
could only refute the internal EWMA-vs-SMA vol-difference thesis — it never saw
a real quote. Here every ask is a real resting Kalshi offer captured live in
recordings/quotes_*, and every settlement is resolved from recorded spot.

Method
------
For each recorded ladder row: recompute true_prob with DistModel from the
regime snapshot recorded at that same tick, take the REAL ask, form
edge = true_prob - ask, then resolve the contract's actual settlement by
finding the recorded spot nearest its expiry. Bucket by edge and report EV per
$1 staked if you bought at that ask and held to expiry.

    EV/$1 = (realized ITM rate - mean ask) / mean ask

Caveats, stated rather than buried
----------------------------------
* `vol_regime` is not in the recorded regime dict, so true_prob is recomputed
  with its default ("NORMAL"). That scales vol_h by 1.0 instead of 1.15/0.92
  when the live engine had flagged HIGH/LOW — a small distortion, not a
  directional bias.
* Observations of the same contract at successive ticks are highly correlated,
  so the raw n overstates independent evidence. Both the all-observation view
  and a one-row-per-contract view are reported; trust the latter's n.
* Holding to settlement is not what the bot does (it exits on tiers). This
  measures whether the ENTRY signal selects contracts with positive
  expectancy, which is upstream of any exit logic.
* Recording covers only the days the recorder was enabled — a far smaller and
  more recent sample than the 59-day backtest. Treat as directional.

Usage:
    python3 real_price_edge_test.py
"""
import argparse
import bisect
import datetime as dt
import glob
import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from kalshi_btc_bot import config as C
from kalshi_btc_bot.model import DistModel


def tolerant(path: str) -> list:
    """Read a gzip JSONL that may still be mid-append."""
    out = []
    try:
        with gzip.open(path, "rt") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except Exception:
                    break
    except EOFError:
        pass
    return out


def load_ticks() -> list:
    rows = []
    for f in sorted(glob.glob("recordings/quotes_*.jsonl.gz")):
        rows += tolerant(f)
    rows.sort(key=lambda r: r["t"])
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tolerance-secs", type=int, default=180,
                    help="max gap between expiry and nearest recorded spot")
    args = ap.parse_args()

    ticks = load_ticks()
    print(f"recorded quote ticks: {len(ticks):,}")

    # dense spot timeline from every tick (not just ones carrying a ladder)
    stamps, spots = [], []
    for r in ticks:
        if r.get("spot"):
            stamps.append(dt.datetime.fromisoformat(r["t"]))
            spots.append(r["spot"])
    print(f"spot samples for settlement resolution: {len(stamps):,}")
    if not stamps:
        raise SystemExit("no spot data recorded")

    def spot_at(when: dt.datetime):
        i = bisect.bisect_left(stamps, when)
        best = None
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(stamps):
                gap = abs((stamps[j] - when).total_seconds())
                if best is None or gap < best[0]:
                    best = (gap, spots[j])
        if best is None or best[0] > args.tolerance_secs:
            return None
        return best[1]

    dist = DistModel()
    obs = []          # (prior_edge, posterior_edge, ask, itm, ticker)
    unresolved = 0
    for r in ticks:
        L = r.get("l") or []
        if not L:
            continue
        rg = r.get("rg") or {}
        vol = rg.get("v")
        if not vol:
            continue
        regime = {"regime": rg.get("r"), "direction": rg.get("d"),
                  "vol": vol, "zscore": rg.get("z") or 0.0,
                  "mom": rg.get("m") or 0.0}
        t0 = dt.datetime.fromisoformat(r["t"])
        spot = r["spot"]
        for c in L:
            ask, bid, lo, hi, h = c.get("a"), c.get("b"), c.get("lo"), c.get("hi"), c.get("h")
            if not (ask and bid and lo and hi and h) or ask <= 0 or bid <= 0 or h <= 0:
                continue
            settle = spot_at(t0 + dt.timedelta(hours=h))
            if settle is None:
                unresolved += 1
                continue
            con = {"type": "RANGE", "low": lo, "high": hi,
                   "strike": (lo + hi) / 2.0, "hours": h,
                   "itm": lo <= spot < hi}
            p_info = dist.posterior_prob(con, spot, vol, h, regime, bid=bid, ask=ask)
            prior = p_info["prior_prob"]
            posterior = p_info["true_prob"]
            if prior <= 0 or posterior <= 0:
                continue
            obs.append((prior - ask, posterior - ask, ask,
                        1 if lo <= settle < hi else 0, c.get("tk")))

    print(f"contract observations resolved: {len(obs):,}   "
          f"unresolved (no spot near expiry): {unresolved:,}")
    if not obs:
        raise SystemExit("nothing resolved — recorder may not span any expiry")

    def table(rows, label, edge_idx):
        print(f"\n=== {label} ===   n={len(rows):,}")
        print(f"  {'edge bucket':>18} {'n':>7} {'mean ask':>9} {'ITM rate':>9} {'EV/$1':>9}")
        for lo_e, hi_e in [(-9, 0), (0, .02), (.02, .05), (.05, .10), (.10, 9)]:
            g = [o for o in rows if lo_e <= o[edge_idx] < hi_e]
            if not g:
                continue
            ask = sum(o[2] for o in g) / len(g)
            itm = sum(o[3] for o in g) / len(g)
            print(f"  {lo_e:+.2f}..{hi_e:<+.2f} {len(g):>7,} {ask:>9.4f} {itm:>9.4f} "
                  f"{((itm-ask)/ask):>+8.1%}")
        sel = [o for o in rows if o[edge_idx] >= C.MIN_EDGE]
        rej = [o for o in rows if o[edge_idx] < C.MIN_EDGE]
        for nm, g in (("PASSES MIN_EDGE", sel), ("rejected", rej)):
            if not g:
                continue
            ask = sum(o[2] for o in g) / len(g)
            itm = sum(o[3] for o in g) / len(g)
            print(f"  {nm:>18} {len(g):>7,} {ask:>9.4f} {itm:>9.4f} {((itm-ask)/ask):>+8.1%}")

    table(obs, "PRIOR edge — ALL observations (correlated — same contract across ticks)", 0)
    table(obs, "POSTERIOR edge — ALL observations (correlated — same contract across ticks)", 1)

    # one row per contract: first sighting, the least-correlated view
    seen, uniq = set(), []
    for o in obs:
        if o[4] in seen:
            continue
        seen.add(o[4])
        uniq.append(o)
    table(uniq, "PRIOR edge — ONE row per contract (first sighting) — trust this n", 0)
    table(uniq, "POSTERIOR edge — ONE row per contract (first sighting) — trust this n", 1)

    print("\n  EV/$1 > 0 means buying at that real ask and holding to settlement")
    print("  made money. If 'PASSES MIN_EDGE' is not clearly above 'rejected',")
    print("  the entry signal is not finding real mispricing.")


if __name__ == "__main__":
    main()
