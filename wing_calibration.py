"""Does the market underprice the band spot is already in, at a BOUNDARY_NO moment?

THE CLAIM UNDER TEST. wing.py's rationale rests on one calibration table,
measured 2026-08-27 over 16,796 band-observations at BOUNDARY_NO-qualifying
moments:

    distance from spot   implied   realized   YES edge   NO edge
    $0-100                 0.326      0.416     +0.078     -0.102
    $100-200               0.127      0.077     -0.064     +0.038
    $200-300               0.038      0.010     -0.040     +0.016
    $300+                  0.018      0.000     -0.031     +0.005

If that holds, the near band is underpriced by 7.8c — twice the 3.8c
overpricing the NO leg harvests at $100-200 — and buying YES there is the
better expression of the same conditioning.

WHY IT NEEDS REDOING. Every figure above resolved contracts from the last
`universe` observation, which is ~T-5min rather than settlement: before
3b8459a the recorder could not see the final six minutes at all (median 307s
early, ZERO contracts observed within 60s of close). That biased realized rates
upward for bands spot happened to be sitting in near expiry — exactly the $0-100
row the whole thesis rests on. The companion ATM study went from 93% win /
+99.8% ROC to 40% / -26.7% under the same correction, and the wing leg was never
re-measured. wing.py has been disabled since 2026-08-28 pending this.

METHOD. Settlement resolves from the QUOTES stream, the only source continuous
through expiry (test_expiring_window.py). For each tick where the live
SignalEngine actually fires BOUNDARY_NO, every band on the ladder is bucketed by
distance from spot, its real recorded YES ask is taken as `implied`, and its
outcome is resolved from recorded spot at close_time. No model probability is
involved anywhere — this compares the market's own price to what happened.

Fee-adjusted, because a 7.8c edge does not survive an arbitrary cost: the Kalshi
taker fee peaks at mid-price, and the $0-100 band trades near 0.33.

Usage:
    python3 wing_calibration.py --start 2026-08-12 --end 2026-09-01
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import glob
import math
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from kalshi_btc_bot import config as C
from kalshi_btc_bot.fees import taker_fee
from kalshi_btc_bot.model import DistModel
from kalshi_btc_bot.signals import SignalEngine

from boundary_no_quote_replay import (
    join_regimes,
    normalize_universe,
    tolerant_jsonl_gz,
)


def daterange(stream: str, start: str, end: str) -> list[str]:
    out = []
    for p in sorted(glob.glob(f"recordings/{stream}_*.jsonl.gz")):
        day = Path(p).stem.split("_")[1][:10]
        if start <= day <= end:
            out.append(p)
    return out


def spot_series(paths: list[str]):
    ts, sp = [], []
    for path in paths:
        for row in tolerant_jsonl_gz(path):
            if row.get("spot") is None:
                continue
            try:
                ts.append(dt.datetime.fromisoformat(row["t"]).timestamp())
            except Exception:
                continue
            sp.append(float(row["spot"]))
    order = sorted(range(len(ts)), key=lambda i: ts[i])
    return [ts[i] for i in order], [sp[i] for i in order]


def spot_at(ts, sp, when, tolerance=120.0):
    """Last recorded spot AT OR BEFORE `when`. As-of, never nearest.

    The previous version took whichever neighbour was closer and so could
    resolve a contract's outcome from a price printed AFTER its close —
    measured 2026-08-30, that happened on 12 of 24 closes, median 0.4s after.
    On clean data the two samples were usually identical (median difference
    $0.00, max $6.08 against a $100 band), which is why it did not visibly
    distort the calibration.

    It is unbounded across a recording gap, though, and gaps are routine here.
    With a 120s tolerance a contract could be settled from a price two minutes
    into the future. Kalshi settles on spot AT the close, so the last sample at
    or before it is the honest estimate, and anything staler than `tolerance`
    is unresolved rather than guessed.
    """
    if not ts:
        return None
    i = bisect.bisect_right(ts, when) - 1
    if i < 0:
        return None
    return sp[i] if (when - ts[i]) <= tolerance else None


def band_spot_came_from(s_ts, s_sp, now_epoch: float, lookback: float):
    """The band spot ACTUALLY occupied `lookback` seconds ago.

    This is the thesis as stated: "the range BTC just came from ... they pop OTM
    then back in the money once reverted". That is ONE specific band, identified
    by where spot really was — not a direction.

    side_for() below answers a weaker question. It classifies every band by the
    SIGN of the z-score, so with z>0 every band under spot is "behind",
    including bands spot never touched and which are cheap for unrelated
    reasons. That dilutes the very effect being tested. Both are reported so the
    difference is visible rather than argued about.

    Returns (lo, hi) of the band containing the historical spot, or None when
    there is no sample within `lookback` +/- a tolerance. Strictly as-of: only
    samples at or before now_epoch are considered.
    """
    target = now_epoch - lookback
    i = bisect.bisect_right(s_ts, target) - 1
    if i < 0:
        return None
    if (target - s_ts[i]) > 120.0:
        return None
    return s_sp[i]


def bucket_for(dist: float) -> str | None:
    d = abs(dist)
    if d < 100:
        return "$0-100"
    if d < 200:
        return "$100-200"
    if d < 300:
        return "$200-300"
    return "$300+"


def side_for(contract: dict, spot: float, zscore: float) -> str:
    """Is this band BEHIND spot (where spot came from) or AHEAD of it?

    The original calibration bucketed by abs(distance), so a band $150 above
    spot and one $150 below landed in the same row. That erases the whole
    thesis: at a z-extreme in a mean-reverting regime, spot has just travelled
    AWAY from somewhere, and the claim is specifically that the band it LEFT is
    cheap because reversion brings spot back into it. A symmetric bucket
    averages that band together with the one spot is running toward, which is
    the opposite trade.

    z > 0 means spot is extended UP, so it came from BELOW: bands under spot are
    "behind". z < 0 mirrors it. The band spot is currently inside is neither.
    """
    lo, hi = float(contract["low"]), float(contract["high"])
    if lo <= spot < hi:
        return "in"
    above = lo > spot
    if zscore > 0:
        return "ahead" if above else "behind"
    return "behind" if above else "ahead"


MIN_CLUSTERS = 30      # independent expiries required before quoting an interval


def percentile_bootstrap_interval(values, iterations=2000, seed=7):
    """Ordinary percentile bootstrap. NOT BCa — no bias correction, no
    acceleration — and named accordingly after being mislabeled `bca_interval`.

    Gated on MIN_CLUSTERS independent expiry clusters, not on a raw count. The
    old `len(values) >= 8` guard would happily quote a "95% CI" from eight
    observations of a single expiry; with identical values that produces a
    degenerate [1.0, 1.0] that reads as certainty.
    """
    if len(values) < MIN_CLUSTERS:
        return None, None
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(iterations):
        means.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    return means[int(0.025 * iterations)], means[int(0.975 * iterations)]


LOOKBACKS = (120.0, 300.0, 600.0, 900.0, 1200.0)
TIME_BUCKETS = ((0, 10), (10, 20), (20, 30), (30, 45), (45, 70))


def snipe_sweep(ticks_iter, s_ts, s_sp, max_ask: float, settle_tol: float,
                min_ask: float = 0.10):
    """Cheap bands on the reversion side, across the whole hour. Two populations.

    CAME-FROM (returned as `cells`, keyed by lookback x time-bucket) is the
    thesis as literally stated: spot ACTUALLY occupied this band `lb` seconds
    ago, left it, and the claim is reversion carries spot back in.

    DIRECTIONAL (returned as `dir_cells`, keyed by time-bucket alone) is the
    weaker, broader version: any non-occupied band on the side spot must travel
    to in order to revert, whether or not spot was ever inside it. side_for()
    calls that side "behind" — z>0 means spot is extended up, so reversion is
    down, and the bands below spot are both where it came from and where it is
    heading. The two coincide in direction and differ in membership.

    Directional has no lookback axis because it does not consult the spot path,
    which is the entire difference between them. It is a strict superset of
    came-from: every came-from band is directional, most directional bands are
    not came-from. If directional matches came-from, the spot path carries no
    information and only the DIRECTION matters — which is a simpler and much
    more robust rule. If came-from is materially better, the path is the signal
    and the lookback becomes a real parameter that has to be fitted.

    PRICE WINDOW [min_ask, max_ask]. The ceiling is the snipe premise: a band at
    0.255 has not been written off. The FLOOR exists because the bottom of the
    book is not a cheaper version of the same trade — sub-0.10 bands are the
    ones far enough out that reversion cannot reach them inside the hour, and
    the docstring's own calibration shows the $300+ bucket realizing 0.000. They
    would otherwise dominate the population by count and drag every cell down
    for a reason that has nothing to do with the thesis.

    FEES ARE PER-OBSERVATION and computed from each band's own ask, never from a
    cell's mean. Kalshi's taker fee is ceil(0.07*N*P*(1-P)) — a concave function
    of price that peaks at the mid — so a 0.18 band pays ~1.0c and a 0.41 band
    ~1.7c. Averaging asks first and taking one fee overstates the cost of a
    cheap population, which is exactly the population under test here.

    Differs from the calibration above in three ways, all requested:

      ask <= max_ask     The thesis is a SNIPE: the band gets written off, goes
                         cheap, and reverts. A band at 0.255 is not written off.
                         The asymmetry only exists at a low price.
      whole hour         find_boundary_no() gates entries to 4.8-18 min, so the
                         earlier test could only ever see the tail of the hour.
                         Reversion needs runway; a band left 10 minutes ago may
                         have no time to revert before a close 5 minutes out.
                         The hours gate is dropped and time-to-expiry becomes an
                         axis instead of a filter.
      lookback swept     "just came from" was never specified as a number. 600s
                         was a guess. Swept from 2 to 20 minutes.

    The REGIME conditioning is kept — RANGING/REVERTING with |z| at or above
    BOUNDARY_NO_ZSCORE_MIN — because "when the NO side shows us YES is
    overpriced" is the premise. Unconditioned, the ATM band was measured at
    -30.4% ROC, so the conditioning is the claim, not the band.

    Returns (cells, dir_cells, qualifying, censored). `cells` is keyed
    (lookback, time_bucket); `dir_cells` by time_bucket. Both hold
    (ask, won, expiry) tuples.
    """
    cells: dict[tuple, list] = defaultdict(list)
    dir_cells: dict[tuple, list] = defaultdict(list)
    seen: set[tuple] = set()
    dir_seen: set[tuple] = set()
    censored_price = 0
    qualifying = 0

    for row, now, spot, regime in ticks_iter:
        if regime.get("regime") not in ("RANGING", "REVERTING"):
            continue
        z = regime.get("zscore") or 0.0
        if abs(z) < C.BOUNDARY_NO_ZSCORE_MIN:
            continue
        qualifying += 1
        lad = normalize_universe(row, now)
        now_e = now.timestamp()
        for c in lad:
            if not (min_ask <= float(c["ask"]) <= max_ask):
                censored_price += 1
                continue
            mins = float(c["hours"]) * 60.0
            tb = next((t for t in TIME_BUCKETS if t[0] <= mins < t[1]), None)
            if tb is None:
                continue
            try:
                close = dt.datetime.fromisoformat(
                    str(c["close_time"]).replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            ss = spot_at(s_ts, s_sp, close, settle_tol)
            if ss is None:
                continue
            lo, hi = float(c["low"]), float(c["high"])
            now_in = lo <= float(spot) < hi
            if now_in:
                continue                     # must have LEFT it
            won = 1.0 if lo <= ss < hi else 0.0
            exp = c["ticker"].rsplit("-", 1)[0]

            # DIRECTIONAL: on the reversion side, path ignored. side_for()
            # returns "behind" for the side spot came from, which is the same
            # side reversion travels toward — z>0 is extended up, so both point
            # down. No lookback axis: this deliberately does not look at where
            # spot has been.
            if side_for(c, float(spot), z) == "behind":
                dkey = (c["ticker"], int(mins) // 2)
                if dkey not in dir_seen:
                    dir_seen.add(dkey)
                    dir_cells[tb].append((float(c["ask"]), won, exp))

            for lb in LOOKBACKS:
                past = band_spot_came_from(s_ts, s_sp, now_e, lb)
                if past is None or not (lo <= past < hi):
                    continue
                key = (c["ticker"], lb, int(mins) // 2)
                if key in seen:
                    continue
                seen.add(key)
                cells[(lb, tb)].append((float(c["ask"]), won, exp))
    return cells, dir_cells, qualifying, censored_price


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2026-08-12")
    ap.add_argument("--end", default="2026-09-01")
    ap.add_argument("--every", type=int, default=5,
                    help="sample every Nth qualifying tick (dedup, default 5)")
    ap.add_argument("--settle-tolerance", type=float, default=120.0,
                    help="max seconds a settlement spot may predate close")
    ap.add_argument("--lookback", type=float, default=600.0,
                    help="seconds back to locate the band spot came from")
    ap.add_argument("--no-edge-map", action="store_true",
                    help="NO net edge by regime x time-to-expiry, gated vs ungated")
    ap.add_argument("--snipe-sweep", action="store_true",
                    help="sweep lookback x time-to-expiry for cheap came-from bands")
    ap.add_argument("--max-ask", type=float, default=0.21,
                    help="ask ceiling for the snipe sweep (default 0.21)")
    ap.add_argument("--min-ask", type=float, default=0.10,
                    help="ask floor for the snipe sweep (default 0.10). Below "
                         "this the band is too far out for reversion to reach "
                         "inside the hour, not a cheaper version of the trade.")
    args = ap.parse_args()
    if args.every < 1:
        ap.error("--every must be >= 1")
    if not (0.0 <= args.min_ask < args.max_ask <= 1.0):
        ap.error("--min-ask must be below --max-ask, both within [0, 1]")

    uni = daterange("universe", args.start, args.end)
    qs = daterange("quotes", args.start, args.end)
    if not uni:
        raise SystemExit(f"no universe recordings in {args.start}..{args.end}")

    # STREAM ONE DAY AT A TIME. Holding every universe row costs ~188 market
    # dicts per tick times ~38k ticks per day; loading twenty days at once was
    # killed by the OOM reaper (exit 137). The spot series is kept whole because
    # it is only timestamps and floats, and settlement for a 23:00 expiry has to
    # resolve into the following file.
    s_ts, s_sp = spot_series(qs)
    print(f"  {len(s_ts):,} spot samples across {len(qs)} quote days", flush=True)

    def day_ticks():
        by_day = {Path(p).stem.split("_")[1][:10]: p for p in qs}
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
            print(f"    {day}  {len(joined):,} ticks", flush=True)
            yield from joined
            del u, q, joined

    if args.no_edge_map:
        # IS THE MISPRICING HAPPENING WHERE WE ARE GATED OUT?
        #
        # BOUNDARY_NO fires only in RANGING/REVERTING, only at |z| >= 1.40, and
        # only 4.8-18 minutes from expiry. Every one of those is a filter on WHEN,
        # not on what. If the NO edge is real but lives outside them, the gates
        # are not selecting the edge, they are missing it.
        #
        # Measures the NO side directly on bands the strategy targets by price
        # (YES ask in [ASK_MIN, ASK_MAX]), with settlement as-of the close:
        #     NO net = (1 - realized) - (1 - implied_bid) - fee
        # and marks which cells the live gates would actually allow.
        cells: dict[tuple, list] = defaultdict(list)
        seen: set = set()
        for row in day_ticks():
            rg = row.get("rg") or {}
            spot, vol = row.get("spot"), rg.get("v")
            if spot is None or not vol:
                continue
            try:
                now = dt.datetime.fromisoformat(row["t"])
            except Exception:
                continue
            r = rg.get("r") or "?"
            z = abs(rg.get("z") or 0.0)
            for c in normalize_universe(row, now):
                ask = float(c["ask"])
                if not (C.BOUNDARY_NO_YES_ASK_MIN <= ask <= C.BOUNDARY_NO_YES_ASK_MAX):
                    continue
                mins = float(c["hours"]) * 60.0
                tb = next((t for t in TIME_BUCKETS if t[0] <= mins < t[1]), None)
                if tb is None:
                    continue
                try:
                    close = dt.datetime.fromisoformat(
                        str(c["close_time"]).replace("Z", "+00:00")).timestamp()
                except Exception:
                    continue
                ss = spot_at(s_ts, s_sp, close, args.settle_tolerance)
                if ss is None:
                    continue
                key = (c["ticker"], int(mins) // 2)
                if key in seen:
                    continue
                seen.add(key)
                lo, hi = float(c["low"]), float(c["high"])
                yes_won = 1.0 if lo <= ss < hi else 0.0
                no_cost = 1.0 - float(c["bid"])
                no_net = (1.0 - yes_won) - no_cost - taker_fee(1, no_cost)
                zb = "z>=1.4" if z >= C.BOUNDARY_NO_ZSCORE_MIN else "z<1.4"
                cells[(r, zb, tb)].append((no_net, c["ticker"].rsplit("-", 1)[0]))

        gate_lo = C.BOUNDARY_NO_HOURS_MIN * 60
        gate_hi = C.BOUNDARY_NO_HOURS_MAX * 60
        print(f"\n  NO NET EDGE per $1, by regime x |z| x time-to-expiry")
        print(f"  bands priced in the strategy's own window "
              f"(YES ask {C.BOUNDARY_NO_YES_ASK_MIN:.2f}-{C.BOUNDARY_NO_YES_ASK_MAX:.2f})")
        print(f"  [G] = a cell the live gates ALLOW "
              f"(RANGING/REVERTING, z>=1.4, {gate_lo:.0f}-{gate_hi:.0f} min)\n")
        hdr = "  " + "regime / z".ljust(22) + "".join(
            f"{lo}-{hi}m".rjust(15) for lo, hi in TIME_BUCKETS)
        print(hdr)
        for r in ("RANGING", "REVERTING", "TRENDING"):
            for zb in ("z>=1.4", "z<1.4"):
                line = "  " + f"{r} {zb}".ljust(22)
                for tb in TIME_BUCKETS:
                    rows = cells.get((r, zb, tb)) or []
                    if len(rows) < 25:
                        line += f"{'n=' + str(len(rows)):>15s}"
                        continue
                    m = sum(x[0] for x in rows) / len(rows)
                    allowed = (r in ("RANGING", "REVERTING") and zb == "z>=1.4"
                               and tb[0] >= gate_lo - 5 and tb[1] <= gate_hi + 12)
                    line += f"{f'{m:+.3f}{"[G]" if allowed else ""}':>15s}"
                print(line)

        print("\n  cells with >= MIN_CLUSTERS expiries:")
        for k in sorted(cells, key=lambda k: (k[0], k[1], k[2])):
            rows = cells[k]
            if len(rows) < 40:
                continue
            by_e: dict[str, list] = defaultdict(list)
            for net, exp in rows:
                by_e[exp].append(net)
            cm = [sum(v) / len(v) for v in by_e.values()]
            lo_, hi_ = percentile_bootstrap_interval(cm)
            if lo_ is None:
                continue
            m = sum(cm) / len(cm)
            v = ("POSITIVE" if lo_ > 0 else "NEGATIVE" if hi_ < 0 else "spans 0")
            r, zb, tb = k
            print(f"    {r:10s} {zb:7s} {tb[0]:>2}-{tb[1]:<3}m  n={len(rows):5d}  "
                  f"{len(by_e):3d} exp  mean {m:+.4f}  CI [{lo_:+.4f}, {hi_:+.4f}]  {v}")
        return

    if args.snipe_sweep:
        def prepared():
            for row in day_ticks():
                rg = row.get("rg") or {}
                spot, vol = row.get("spot"), rg.get("v")
                if spot is None or not vol:
                    continue
                try:
                    now = dt.datetime.fromisoformat(row["t"])
                except Exception:
                    continue
                yield row, now, float(spot), {
                    "regime": rg.get("r"), "direction": rg.get("d"), "vol": vol,
                    "zscore": rg.get("z") or 0.0, "mom": rg.get("m") or 0.0}

        cells, dir_cells, qualifying, censored = snipe_sweep(
            prepared(), s_ts, s_sp, args.max_ask, args.settle_tolerance,
            args.min_ask)
        print(f"\n  SNIPE SWEEP — reversion-side bands, ask in "
              f"[{args.min_ask:.2f}, {args.max_ask:.2f}], whole hour")
        print(f"  regime-qualifying ticks: {qualifying:,}   "
              f"band-observations censored by the price window: {censored:,}")
        print(f"  REGIME conditioning kept (RANGING/REVERTING, |z| >= "
              f"{C.BOUNDARY_NO_ZSCORE_MIN}); hours gate dropped.\n")
        print(f"  {len(LOOKBACKS) * len(TIME_BUCKETS)} cells are swept — treat any "
              f"single positive cell as a hypothesis, not a result.\n")
        hdr = "  " + "lookback".ljust(10) + "".join(
            f"{lo}-{hi}m".rjust(16) for lo, hi in TIME_BUCKETS)
        print(hdr)
        for lb in LOOKBACKS:
            line = "  " + f"{lb/60:.0f} min".ljust(10)
            for tb in TIME_BUCKETS:
                rows = cells.get((lb, tb)) or []
                if len(rows) < 20:
                    line += f"{'n=' + str(len(rows)):>16s}"
                    continue
                n = len(rows)
                net = sum(r[1] - r[0] - taker_fee(1, r[0]) for r in rows) / n
                line += f"{f'{net:+.3f} (n{n})':>16s}"
            print(line)

        print("\n  cells with enough independent expiries for an interval:")
        any_ci = False
        for lb in LOOKBACKS:
            for tb in TIME_BUCKETS:
                rows = cells.get((lb, tb)) or []
                if len(rows) < 40:
                    continue
                by_e: dict[str, list] = defaultdict(list)
                for ask, won, exp in rows:
                    by_e[exp].append(won - ask - taker_fee(1, ask))
                cm = [sum(v) / len(v) for v in by_e.values()]
                lo_, hi_ = percentile_bootstrap_interval(cm)
                if lo_ is None:
                    continue
                any_ci = True
                m = sum(cm) / len(cm)
                v = ("POSITIVE" if lo_ > 0 else "NEGATIVE" if hi_ < 0
                     else "INCLUDES ZERO")
                print(f"    lookback {lb/60:>4.0f}min  {tb[0]}-{tb[1]}m  "
                      f"n={len(rows):4d}  {len(by_e):3d} expiries  "
                      f"mean {m:+.4f}  CI [{lo_:+.4f}, {hi_:+.4f}]  {v}")
        if not any_ci:
            print(f"    none reached MIN_CLUSTERS={MIN_CLUSTERS} independent expiries")

        # DIRECTIONAL. One row, no lookback axis — the point of it is that it
        # never consults the spot path. Read it against the came-from grid
        # above: if this row matches the best came-from cells, the path adds
        # nothing and the rule collapses to "buy the reversion side", which
        # needs no fitted lookback and cannot overfit one.
        print(f"\n  DIRECTIONAL — reversion-side bands, spot path IGNORED "
              f"(no lookback axis)")
        print(f"  {'window':<10}{'n':>7}{'expiries':>10}{'implied':>10}"
              f"{'realized':>10}{'net/$1':>10}   95% CI (expiry-clustered)")
        for tb in TIME_BUCKETS:
            rows = dir_cells.get(tb) or []
            label = f"{tb[0]}-{tb[1]}m"
            if len(rows) < 20:
                print(f"  {label:<10}{len(rows):>7}   (too few)")
                continue
            n = len(rows)
            implied = sum(r[0] for r in rows) / n
            realized = sum(r[1] for r in rows) / n
            by_e: dict[str, list] = defaultdict(list)
            for ask, won, exp in rows:
                by_e[exp].append(won - ask - taker_fee(1, ask))
            cm = [sum(v) / len(v) for v in by_e.values()]
            m = sum(cm) / len(cm)
            lo_, hi_ = percentile_bootstrap_interval(cm)
            if lo_ is None:
                ci = "(too few expiries)"
            else:
                v = ("POSITIVE" if lo_ > 0 else "NEGATIVE" if hi_ < 0
                     else "INCLUDES ZERO")
                ci = f"[{lo_:+.4f}, {hi_:+.4f}]  {v}"
            print(f"  {label:<10}{n:>7}{len(by_e):>10}{implied:>10.3f}"
                  f"{realized:>10.3f}{m:>+10.4f}   {ci}")
        return

    engine = SignalEngine(DistModel(), use_market_posterior=True)

    # bucket -> list of (implied_ask, realized_0_or_1, expiry_key)
    obs: dict[str, list] = defaultdict(list)
    fired = 0
    seen: set[tuple] = set()

    for row in day_ticks():
        rg = row.get("rg") or {}
        spot, vol = row.get("spot"), rg.get("v")
        if spot is None or not vol:
            continue
        regime = {"regime": rg.get("r"), "direction": rg.get("d"), "vol": vol,
                  "zscore": rg.get("z") or 0.0, "mom": rg.get("m") or 0.0}
        try:
            now = dt.datetime.fromisoformat(row["t"])
        except Exception:
            continue
        lad = normalize_universe(row, now)
        if not lad:
            continue
        # Only moments the LIVE predicate actually fires on. The unconditioned
        # sweep is a different (and measured-negative) question: an
        # unconditioned ATM band came out at -30.4% ROC, so the conditioning is
        # the claim, not the band.
        sig = engine.find_boundary_no(
            float(spot), float(vol), regime, lad, {},
            real_cash=500.0, start_total=500.0,
        )
        if sig is None:
            continue
        fired += 1
        if fired % args.every:
            continue

        for c in lad:
            b = bucket_for(c["otm_dist"])
            if b is None:
                continue
            try:
                close = dt.datetime.fromisoformat(
                    str(c["close_time"]).replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            # One observation per (contract, minute-bucket) so a contract quoted
            # for twenty minutes does not vote twenty times.
            key = (c["ticker"], int(close - now.timestamp()) // 120)
            if key in seen:
                continue
            seen.add(key)
            ss = spot_at(s_ts, s_sp, close, args.settle_tolerance)
            if ss is None:
                continue
            yes_won = 1.0 if float(c["low"]) <= ss < float(c["high"]) else 0.0
            sd = side_for(c, float(spot), regime["zscore"])
            # WHAT THE WING ACTUALLY BUYS vs WHERE THE EDGE SITS.
            # wing.toward_spot() picks one strike TOWARD spot from the NO leg,
            # which is usually the band ADJACENT to the one spot occupies, not
            # the occupied band itself. The only positive cell in this study is
            # the occupied band. Separate them explicitly.
            lo_c, hi_c = float(c["low"]), float(c["high"])
            width = max(1.0, hi_c - lo_c)
            if lo_c <= float(spot) < hi_c:
                obs["OCCUPIED band (spot inside)"].append(
                    (float(c["ask"]), yes_won, c["ticker"].rsplit("-", 1)[0]))
            elif abs(c["otm_dist"]) <= width:
                obs["ADJACENT band (1 strike away)"].append(
                    (float(c["ask"]), yes_won, c["ticker"].rsplit("-", 1)[0]))
            rec = (float(c["ask"]), yes_won, c["ticker"].rsplit("-", 1)[0])
            obs[b].append(rec)
            obs[f"{b} {sd}"].append(rec)
            # THE SPOT-PATH DEFINITION: is this the band spot was actually
            # sitting in `lookback` seconds ago, and has it since left it?
            past = band_spot_came_from(s_ts, s_sp, now.timestamp(), args.lookback)
            if past is not None:
                was_in = float(c["low"]) <= past < float(c["high"])
                now_in = float(c["low"]) <= float(spot) < float(c["high"])
                if was_in and not now_in:
                    obs["CAME-FROM (spot path)"].append(rec)
                elif was_in and now_in:
                    obs["never left"].append(rec)

    print(f"  BOUNDARY_NO fired on {fired:,} ticks; sampled every {args.every}\n")

    print(f"{'distance':12s} {'n':>6s} {'implied':>8s} {'realized':>9s} "
          f"{'YES edge':>9s} {'NO edge':>8s} {'YES net':>8s}")
    order = ["$0-100", "$100-200", "$200-300", "$300+"]
    for b in order:
        rows = obs.get(b) or []
        if not rows:
            print(f"{b:12s} {'—':>6s}")
            continue
        n = len(rows)
        implied = sum(r[0] for r in rows) / n
        realized = sum(r[1] for r in rows) / n
        # Fee per OBSERVATION, then averaged. taker_fee peaks at mid-price, so
        # taker_fee(mean_ask) != mean(taker_fee(ask)) — computing one fee from
        # the mean ask quietly mis-states the net edge.
        yes_net = sum(r[1] - r[0] - taker_fee(1, r[0]) for r in rows) / n
        print(f"{b:12s} {n:6d} {implied:8.3f} {realized:9.3f} "
              f"{realized - implied:+9.3f} {implied - realized:+8.3f} {yes_net:+8.3f}")

    print(f"\n  SPLIT BY DIRECTION — 'behind' = the band spot CAME FROM "
          f"(reversion target), 'ahead' = the band it is running toward")
    print(f"{'distance':12s} {'side':>7s} {'n':>6s} {'implied':>8s} {'realized':>9s} "
          f"{'YES net':>8s}")
    for b in order:
        for sd in ("behind", "in", "ahead"):
            rows = obs.get(f"{b} {sd}") or []
            if len(rows) < 20:
                continue
            n = len(rows)
            implied = sum(r[0] for r in rows) / n
            realized = sum(r[1] for r in rows) / n
            net = realized - implied - taker_fee(1, implied)
            print(f"{b:12s} {sd:>7s} {n:6d} {implied:8.3f} {realized:9.3f} {net:+8.3f}")

    print(f"\n  SPOT-PATH TEST — the band spot was really in {args.lookback:.0f}s "
          f"ago and has since left (the stated thesis)")
    for cell in ("CAME-FROM (spot path)", "never left"):
        rows = obs.get(cell) or []
        if not rows:
            print(f"    {cell:24s} no observations")
            continue
        n = len(rows)
        implied = sum(r[0] for r in rows) / n
        realized = sum(r[1] for r in rows) / n
        net = sum(r[1] - r[0] - taker_fee(1, r[0]) for r in rows) / n
        print(f"    {cell:24s} n={n:4d}  implied {implied:.3f}  "
              f"realized {realized:.3f}  net {net:+.4f}")

    print()
    for cell in ("$0-100 behind", "$0-100 in", "$0-100 ahead",
                 "CAME-FROM (spot path)",
                 "OCCUPIED band (spot inside)", "ADJACENT band (1 strike away)"):
        rows = obs.get(cell) or []
        if len(rows) < 40:
            continue
        by_e: dict[str, list] = defaultdict(list)
        for ask, won, exp in rows:
            by_e[exp].append(won - ask - taker_fee(1, ask))
        cm = [sum(v) / len(v) for v in by_e.values()]
        lo, hi = percentile_bootstrap_interval(cm)
        m = sum(cm) / len(cm)
        v = ("POSITIVE" if lo is not None and lo > 0
             else "NEGATIVE" if lo is not None and hi < 0 else "INCLUDES ZERO")
        ci = f"[{lo:+.4f}, {hi:+.4f}]" if lo is not None else "(too few expiries)"
        print(f"  {cell:16s} n={len(rows):4d}  {len(by_e):3d} expiries  "
              f"mean {m:+.4f}  95% CI {ci}  {v}")

    # The load-bearing row, with an expiry-clustered interval.
    near = obs.get("$0-100") or []
    if near:
        by_exp: dict[str, list] = defaultdict(list)
        for ask, won, exp in near:
            by_exp[exp].append(won - ask - taker_fee(1, ask))
        cluster_means = [sum(v) / len(v) for v in by_exp.values()]
        lo, hi = percentile_bootstrap_interval(cluster_means)
        m = sum(cluster_means) / len(cluster_means)
        print(f"\n  $0-100 net YES edge, clustered by expiry ({len(by_exp)} expiries)")
        print(f"    mean {m:+.4f} per $1 contract", end="")
        if lo is not None:
            verdict = ("POSITIVE" if lo > 0
                       else "NEGATIVE" if hi < 0
                       else "INCLUDES ZERO")
            print(f"   95% CI [{lo:+.4f}, {hi:+.4f}]   {verdict}")
        else:
            print("   (too few expiries for an interval)")


if __name__ == "__main__":
    main()
