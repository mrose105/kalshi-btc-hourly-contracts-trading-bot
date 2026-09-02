"""Every thesis in this repo, through one identical test.

The reversion-snipe result was only trustworthy because the test was fixed
before the answer was known. This applies the same fixed test to every other
thesis the bot has ever acted on, so they are comparable to each other and to
that one — rather than each having its own bespoke measurement written by
whoever was arguing for it at the time.

THE TEST, applied identically to all of them:

  1. n, independent days, and independent expiries. Reported first, because
     every negative result in this repo has come down to cluster count.
  2. implied vs realized — is the market's price right on this population?
  3. net P&L per $1 after per-contract fees, with a DAY-CLUSTERED 95%
     percentile bootstrap. The interval decides it, not the mean.

Each thesis is a predicate over one shared row table plus a side (YES or NO).
Nothing is fitted, so there is nothing to overfit — this measures what the rule
would have paid, not what a model thinks of it.

SIDES. YES pays 1 if the band contains settlement, costs the ask.
NO pays 1 if it does not, and costs (1 - yes_bid), because selling YES at the
bid is buying NO at one minus it. Using (1 - ask) instead would credit NO with
the spread it actually has to cross, which is the single easiest way to
manufacture a NO edge that does not exist.

WHAT THIS CANNOT SAY. It scores entries at settlement. The live bot exits early
on a ladder, so a thesis that looks fine here can still lose money in
production, and one that looks poor here might be rescued by its exits. Exit
behaviour is no_exit_replay.py's job, not this file's.

Run:
  python3 thesis_bakeoff.py --start 2026-08-12 --end 2026-09-01
  python3 thesis_bakeoff.py --cache-only        # build the row cache and stop
"""
import argparse
import datetime as dt
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

from kalshi_btc_bot import config as C
from kalshi_btc_bot.fees import taker_fee
from kalshi_btc_bot.instrument import ACTIVE as _INSTRUMENT
from kalshi_btc_bot.contracts import otm_distance
from kalshi_btc_bot.model import DistModel

from wing_calibration import daterange, spot_at, spot_series
from boundary_no_quote_replay import (
    join_regimes,
    normalize_universe,
    tolerant_jsonl_gz,
)

_VOL_FLOOR = _INSTRUMENT.vol_h_floor
_VOL_CAP = _INSTRUMENT.vol_h_cap
_MODEL = DistModel()

CACHE = Path("thesis_rows.npz")
MIN_DAYS = 8


def build_rows(start, end, settle_tol):
    """One row per (ticker, 2-minute bucket), with everything any thesis needs.

    true_prob is computed with the CURRENT model — drift config-gated, so
    DRIFT_REVERTING_COEF = 0.0. Any thesis whose gate reads true_prob (that is
    BOUNDARY_NO and SNIPE) is therefore scored under the corrected model, not
    the one that manufactured the original signal.
    """
    uni = daterange("universe", start, end)
    qs = daterange("quotes", start, end)
    if not uni:
        raise SystemExit(f"no universe recordings in {start}..{end}")
    s_ts, s_sp = spot_series(qs)
    print(f"  {len(s_ts):,} spot samples across {len(qs)} quote days", flush=True)

    by_day = {Path(p).stem.split("_")[1][:10]: p for p in qs}
    cols = defaultdict(list)
    seen = set()

    for up in uni:
        day = Path(up).stem.split("_")[1][:10]
        qp = by_day.get(day)
        if qp is None:
            continue
        u = tolerant_jsonl_gz(up)
        u.sort(key=lambda r: r.get("t", ""))
        q = tolerant_jsonl_gz(qp)
        q.sort(key=lambda r: r.get("t", ""))
        joined = join_regimes(u, q, tolerance_secs=5)
        before = len(cols["y"])

        for row in joined:
            rg = row.get("rg") or {}
            spot, vol = row.get("spot"), rg.get("v")
            if spot is None or not vol:
                continue
            try:
                now = dt.datetime.fromisoformat(row["t"])
            except Exception:
                continue
            spot, vol = float(spot), float(vol)
            r = rg.get("r") or "?"
            z = float(rg.get("z") or 0.0)
            regime = {
                "regime": r, "zscore": z,
                "mom": float(rg.get("m") or 0.0),
                "vol_regime": "NORMAL",
                "direction": rg.get("d") or "",
                "vol_compression": bool(rg.get("vc")),
            }
            vol_h = min(max(vol * math.sqrt(C.BARS_PER_HOUR), _VOL_FLOOR),
                        _VOL_CAP)

            for c in normalize_universe(row, now):
                hours = float(c["hours"])
                mins = hours * 60.0
                if not (0.0 < mins < 70.0):
                    continue
                key = (c["ticker"], int(mins) // 2)
                if key in seen:
                    continue
                try:
                    close = dt.datetime.fromisoformat(
                        str(c["close_time"]).replace("Z", "+00:00")).timestamp()
                except Exception:
                    continue
                ss = spot_at(s_ts, s_sp, close, settle_tol)
                if ss is None:
                    continue
                seen.add(key)

                lo, hi = float(c["low"]), float(c["high"])
                ask = float(c["ask"])
                bid = float(c["bid"] or 0.0)
                tp = _MODEL.true_prob(c, spot, vol, hours, regime)
                vol_t = vol_h * math.sqrt(max(1e-4, hours))
                center = 0.5 * (lo + hi)
                sigma = spot * vol_t

                cols["y"].append(1.0 if lo <= ss < hi else 0.0)
                cols["ask"].append(ask)
                cols["bid"].append(bid)
                cols["mins"].append(mins)
                cols["hours"].append(hours)
                cols["z"].append(z)
                cols["true_prob"].append(tp)
                cols["spot"].append(spot)
                cols["lo"].append(lo)
                cols["hi"].append(hi)
                cols["dist"].append(center - spot)
                # The bot's own signed convention: NEGATIVE when out of the
                # money, magnitude = distance from the nearer edge, POSITIVE
                # when spot is inside. BOUNDARY_NO_OTM_MIN/MAX are -250/-10, so
                # a positive-magnitude reimplementation silently matches nothing
                # and the live strategy scores zero observations.
                cols["otm"].append(otm_distance(c, spot))
                cols["dist_norm"].append((center - spot) / sigma
                                         if sigma > 0 else 0.0)
                cols["vol_ratio"].append(float(rg.get("vr") or 1.0))
                cols["compressed"].append(1.0 if rg.get("vc") else 0.0)
                cols["mom"].append(float(rg.get("m") or 0.0))
                cols["occupied"].append(1.0 if lo <= spot < hi else 0.0)
                # +1 = band is on the side spot came from (= where a reversion
                # would carry it); -1 = the continuation side; 0 = occupied.
                if lo <= spot < hi:
                    side = 0.0
                else:
                    above = lo > spot
                    side = 1.0 if ((not above) if z > 0 else above) else -1.0
                cols["side_sign"].append(side)
                cols["regime"].append(r)
                cols["day"].append(day)
                cols["expiry"].append(c["ticker"].rsplit("-", 1)[0])
        print(f"    {day}  {len(joined):,} ticks  -> "
              f"{len(cols['y']) - before:,} rows", flush=True)
        del u, q, joined

    out = {}
    for k, v in cols.items():
        out[k] = np.asarray(v) if k in ("regime", "day", "expiry") \
            else np.asarray(v, float)
    return out


def clustered_ci(pnl, groups, iterations=4000, seed=13):
    """Day-clustered percentile bootstrap on mean net P&L per $1.

    Resamples DAYS, not rows. Bands inside one session share a spot path and a
    settlement; treating them as independent shrinks the interval by roughly
    sqrt(rows/days) and is how a null becomes a discovery.
    """
    by_day = defaultdict(list)
    for v, g in zip(pnl, groups):
        by_day[g].append(v)
    days = list(by_day)
    if len(days) < MIN_DAYS:
        return None
    rng = np.random.default_rng(seed)
    means = np.empty(iterations)
    for i in range(iterations):
        pick = rng.integers(0, len(days), len(days))
        means[i] = float(np.mean([v for j in pick for v in by_day[days[j]]]))
    means.sort()
    return (float(means[int(0.025 * iterations)]),
            float(means[int(0.975 * iterations)]), len(days))


def score(d, mask, side):
    """Net P&L per $1 for one thesis, plus its interval."""
    if not mask.any():
        return None
    y = d["y"][mask]
    ask, bid = d["ask"][mask], d["bid"][mask]
    if side == "yes":
        cost = ask
        payoff = y
    else:
        # Buying NO means selling YES at the BID. Using (1 - ask) here would
        # hand the strategy the spread it actually has to cross.
        cost = 1.0 - bid
        payoff = 1.0 - y
    valid = (cost > 0.0) & (cost < 1.0)
    if not valid.any():
        return None
    cost, payoff = cost[valid], payoff[valid]
    fees = np.array([taker_fee(1, cc) for cc in cost], float)
    pnl = payoff - cost - fees
    days = d["day"][mask][valid]
    exps = d["expiry"][mask][valid]
    ci = clustered_ci(pnl, days)
    return {
        "n": int(valid.sum()),
        "days": len(set(days.tolist())),
        "expiries": len(set(exps.tolist())),
        "implied": float(np.mean(cost)),
        "realized": float(np.mean(payoff)),
        "net": float(np.mean(pnl)),
        "ci": ci,
    }


PAIRS = [
    ("BOUNDARY_NO (live gates)", "BOUNDARY_NO minus model gates",
     "do the model's overprice/net-edge gates select anything?"),
    ("YES in compression", "YES outside compression",
     "does vol compression actually make YES cheap?"),
    ("REVERSION side (directional)", "CONTINUATION side (mirror)",
     "is the reversion side better than its opposite?"),
    ("SNIPE + compression gate", "SNIPE (live gates)",
     "does the compression gate improve the snipe?"),
]


def paired_diff(d, mask_a, side_a, mask_b, side_b, iterations=4000, seed=17):
    """Day-paired bootstrap on (mean A - mean B).

    Resamples the SAME days for both arms on every iteration. That is the whole
    point: a day that was bad for everything moves both means together, so the
    common market noise cancels and what is left is the difference the gate
    actually makes. Bootstrapping the two arms independently and differencing
    them would carry both arms' day-to-day variance into the interval and could
    hide a real effect behind shared noise.

    Days where either arm has no observations are dropped, so this is a
    like-for-like comparison on the days both rules could have traded.
    """
    def per_day(mask, side):
        y = d["y"][mask]
        cost = d["ask"][mask] if side == "yes" else 1.0 - d["bid"][mask]
        payoff = y if side == "yes" else 1.0 - y
        ok = (cost > 0.0) & (cost < 1.0)
        cost, payoff = cost[ok], payoff[ok]
        fees = np.array([taker_fee(1, cc) for cc in cost], float)
        out = defaultdict(list)
        for v, g in zip(payoff - cost - fees, d["day"][mask][ok]):
            out[g].append(v)
        return out

    a, b = per_day(mask_a, side_a), per_day(mask_b, side_b)
    days = sorted(set(a) & set(b))
    if len(days) < MIN_DAYS:
        return None
    rng = np.random.default_rng(seed)
    diffs = np.empty(iterations)
    for i in range(iterations):
        pick = rng.integers(0, len(days), len(days))
        va = [v for j in pick for v in a[days[j]]]
        vb = [v for j in pick for v in b[days[j]]]
        diffs[i] = float(np.mean(va)) - float(np.mean(vb))
    diffs.sort()
    obs = (float(np.mean([v for g in days for v in a[g]]))
           - float(np.mean([v for g in days for v in b[g]])))
    return (obs, float(diffs[int(0.025 * iterations)]),
            float(diffs[int(0.975 * iterations)]), len(days))


def theses(d):
    """(name, mask, side, one-line description of what the rule claims).

    Gate constants are read from config so these track the live bot rather than
    a snapshot of it.
    """
    z, mins, ask, tp = d["z"], d["mins"], d["ask"], d["true_prob"]
    otm, side, occ = d["otm"], d["side_sign"], d["occupied"]
    reg, comp, bid = d["regime"], d["compressed"], d["bid"]
    ranging = (reg == "RANGING") | (reg == "REVERTING")
    hrs = d["hours"]
    with np.errstate(divide="ignore", invalid="ignore"):
        over_r = np.where(tp > 1e-9, ask / np.maximum(tp, 1e-9), 0.0)
        edge_r = np.where(ask > 1e-9, tp / np.maximum(ask, 1e-9) - 1.0, 0.0)
    no_net_edge = (1.0 - tp) - (1.0 - bid)

    out = []

    # ── the live strategy ────────────────────────────────────────────────────
    out.append((
        "BOUNDARY_NO (live gates)",
        ranging & (np.abs(z) >= C.BOUNDARY_NO_ZSCORE_MIN)
        & (hrs >= C.BOUNDARY_NO_HOURS_MIN) & (hrs <= C.BOUNDARY_NO_HOURS_MAX)
        & (ask >= C.BOUNDARY_NO_YES_ASK_MIN) & (ask <= C.BOUNDARY_NO_YES_ASK_MAX)
        & (otm >= C.BOUNDARY_NO_OTM_MIN) & (otm <= C.BOUNDARY_NO_OTM_MAX)
        & (over_r >= C.BOUNDARY_NO_OVERPRICING_MIN)
        & (no_net_edge >= C.BOUNDARY_NO_MIN_NET_EDGE),
        "no",
        "sell the OTM band at a z-extreme, 4.8-18m out"))

    # Same rule without the model's opinion. If this scores like the gated
    # version, the true_prob gates are selecting nothing and the edge (if any)
    # is in the price/time/regime window alone.
    out.append((
        "BOUNDARY_NO minus model gates",
        ranging & (np.abs(z) >= C.BOUNDARY_NO_ZSCORE_MIN)
        & (hrs >= C.BOUNDARY_NO_HOURS_MIN) & (hrs <= C.BOUNDARY_NO_HOURS_MAX)
        & (ask >= C.BOUNDARY_NO_YES_ASK_MIN) & (ask <= C.BOUNDARY_NO_YES_ASK_MAX)
        & (otm >= C.BOUNDARY_NO_OTM_MIN) & (otm <= C.BOUNDARY_NO_OTM_MAX),
        "no",
        "the same window, with overprice/net-edge gates removed"))

    # ── MISPRICE_NO: disabled for an ops bug, not for performance ────────────
    out.append((
        "MISPRICE_NO",
        (ask >= 0.10) & (ask <= 0.40) & (over_r >= 1.25) & (mins <= 30.0),
        "no",
        "sell any band the model calls overpriced by 25%+"))

    # ── SNIPE: cheap YES near expiry ─────────────────────────────────────────
    out.append((
        "SNIPE (live gates)",
        (ask >= C.SNIPE_MIN_ENTRY_PRICE) & (ask <= C.SNIPE_MAX_ENTRY_PRICE)
        & (edge_r >= C.SNIPE_MIN_EDGE_RATIO) & (mins <= 20.0),
        "yes",
        "buy cheap YES the model calls underpriced, near expiry"))
    out.append((
        "SNIPE + compression gate",
        (ask >= C.SNIPE_MIN_ENTRY_PRICE) & (ask <= C.SNIPE_MAX_ENTRY_PRICE)
        & (edge_r >= C.SNIPE_MIN_EDGE_RATIO) & (mins <= 20.0) & (comp > 0),
        "yes",
        "the same, only while vol is compressed"))

    # ── the compression thesis, on its own ───────────────────────────────────
    out.append((
        "YES in compression",
        (ask >= 0.10) & (ask <= 0.60) & (comp > 0),
        "yes",
        "vol compression makes Kalshi overstate vol -> YES is cheap"))
    out.append((
        "YES outside compression",
        (ask >= 0.10) & (ask <= 0.60) & (comp <= 0),
        "yes",
        "the control group for the line above"))

    # ── the wing ─────────────────────────────────────────────────────────────
    out.append((
        "WING occupied band",
        (occ > 0) & ranging & (np.abs(z) >= C.BOUNDARY_NO_ZSCORE_MIN)
        & (ask > 0) & (ask <= C.MAX_ASK),
        "yes",
        "buy the band spot is sitting in, as a NO companion"))

    # ── the reversion snipe, for continuity with the earlier run ─────────────
    out.append((
        "REVERSION side (directional)",
        (side > 0) & ranging & (np.abs(z) >= C.BOUNDARY_NO_ZSCORE_MIN)
        & (ask >= 0.10) & (ask <= 0.21),
        "yes",
        "buy cheap bands on the side a reversion travels toward"))
    out.append((
        "CONTINUATION side (mirror)",
        (side < 0) & ranging & (np.abs(z) >= C.BOUNDARY_NO_ZSCORE_MIN)
        & (ask >= 0.10) & (ask <= 0.21),
        "yes",
        "the opposite side, as the control"))

    # ── the hour-shape claim: YES early, NO late ────────────────────────────
    out.append((
        "YES early in the hour",
        (mins >= 30.0) & (ask >= 0.10) & (ask <= 0.40),
        "yes",
        "YES has runway when there is time left"))
    out.append((
        "NO late in the hour",
        (mins <= 18.0) & (ask >= 0.10) & (ask <= 0.30),
        "no",
        "sell premium once there is no time to recover"))

    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2026-08-12")
    ap.add_argument("--end", default="2026-09-01")
    ap.add_argument("--settle-tolerance", type=float, default=120.0)
    ap.add_argument("--rebuild", action="store_true",
                    help="ignore the row cache and re-extract")
    ap.add_argument("--cache-only", action="store_true")
    args = ap.parse_args()

    if CACHE.exists() and not args.rebuild:
        print(f"  loading cached rows from {CACHE}")
        z = np.load(CACHE, allow_pickle=False)
        d = {k: z[k] for k in z.files}
    else:
        d = build_rows(args.start, args.end, args.settle_tolerance)
        np.savez_compressed(CACHE, **d)
        print(f"  cached {len(d['y']):,} rows to {CACHE}")
    if args.cache_only:
        return

    print(f"\n  rows {len(d['y']):,}   days {len(set(d['day'].tolist()))}   "
          f"expiries {len(set(d['expiry'].tolist()))}")
    print(f"  Net P&L per $1 after per-contract fees. The 95% CI is "
          f"DAY-clustered.\n  A mean without an interval that clears zero is "
          f"not a result.\n")

    hdr = (f"  {'thesis':<30}{'side':>5}{'n':>7}{'d':>4}{'exp':>5}"
           f"{'impl':>7}{'real':>7}{'net/$1':>9}   95% CI")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    results = []
    for name, mask, side, why in theses(d):
        s = score(d, mask, side)
        if s is None or s["n"] < 30:
            print(f"  {name:<30}{side:>5}{(s or {}).get('n', 0):>7}"
                  f"   too few observations")
            continue
        ci = s["ci"]
        if ci is None:
            tag, cis = "NO INTERVAL", f"(<{MIN_DAYS} days)"
        else:
            lo_, hi_, nd = ci
            tag = ("POSITIVE" if lo_ > 0 else "NEGATIVE" if hi_ < 0
                   else "INCLUDES ZERO")
            cis = f"[{lo_:+.4f}, {hi_:+.4f}]"
        print(f"  {name:<30}{side:>5}{s['n']:>7}{s['days']:>4}"
              f"{s['expiries']:>5}{s['implied']:>7.3f}{s['realized']:>7.3f}"
              f"{s['net']:>+9.4f}   {cis}  {tag}")
        results.append((name, why, s, tag))

    print(f"\n  what each rule claims:")
    for name, why, _, _ in results:
        print(f"    {name:<30} {why}")

    pos = [r for r in results if r[3] == "POSITIVE"]
    print(f"\n  {len(pos)} of {len(results)} theses have an interval that "
          f"clears zero.")
    if pos:
        for name, _, s, _ in pos:
            print(f"    {name}  {s['net']:+.4f}/$1 on {s['expiries']} expiries")
    # The paired differences are the questions the table above cannot answer.
    # Two rows can both include zero while the GAP between them is solid,
    # because the shared day-to-day market noise dominates each arm separately
    # and cancels in the difference.
    lookup = {name: (mask, side) for name, mask, side, _ in theses(d)}
    print(f"\n  PAIRED DIFFERENCES (same days resampled for both arms)")
    print(f"  {'A - B':<58}{'diff':>9}   95% CI")
    for a_name, b_name, question in PAIRS:
        if a_name not in lookup or b_name not in lookup:
            continue
        ma, sa = lookup[a_name]
        mb, sb = lookup[b_name]
        res = paired_diff(d, ma, sa, mb, sb)
        label = f"{a_name} - {b_name}"
        if res is None:
            print(f"  {label:<58}{'--':>9}   too few shared days")
            continue
        obs, lo_, hi_, nd = res
        tag = ("A BETTER" if lo_ > 0 else "B BETTER" if hi_ < 0
               else "NO DIFFERENCE")
        print(f"  {label:<58}{obs:>+9.4f}   [{lo_:+.4f}, {hi_:+.4f}]  "
              f"{tag}  ({nd}d)")
        print(f"    -> {question}")

    print(f"\n  Entries are scored at SETTLEMENT. The live bot exits early, so "
          f"this\n  measures the entry rule, not the strategy. Exits are "
          f"no_exit_replay.py.")


if __name__ == "__main__":
    main()
