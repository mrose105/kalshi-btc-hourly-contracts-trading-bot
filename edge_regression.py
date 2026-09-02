"""Does ANY model beat the Kalshi ask at predicting whether a band settles in?

THE BASELINE IS THE MARKET, NOT THE MEAN. A model that beats the sample mean has
shown nothing — the ask already encodes almost all of it. The only question that
matters for a trading edge is whether a model beats `p_hat = ask`, out of sample,
after fees. Every score here is reported as a skill score against that baseline,
where 0.0 means "exactly as good as the market" and positive means better.

WHY LINEAR AND KNN TOGETHER. They fail differently, which is the point:

  linear   can only express a monotone weighted sum. If edge exists as "cheaper
           bands revert more", linear finds it. If edge is real but lives in an
           interaction — cheap AND late AND high-|z| — linear averages it away
           and reports nothing.
  KNN      makes no functional-form assumption and picks up interactions and
           non-monotonicity for free. What it cannot do is extrapolate, and it
           degrades fast as feature count grows.

Agreement between them is informative. Both flat means there is no signal to
find under either assumption. KNN >> linear means the structure is nonlinear and
worth pursuing. Linear >> KNN usually means KNN is starved by dimensionality,
not that the market is beaten.

SPLITS ARE GROUPED BY DAY. Observations inside one expiry are near-duplicates —
same spot path, same settlement, bands correlated across the ladder. A random
row split puts siblings on both sides of the fold and inflates every score
toward "we predict the market perfectly". GroupKFold on the calendar day is the
conservative choice: it also absorbs same-session autocorrelation that expiry
clustering alone would leave in.

TWO FRAMINGS, because they answer different questions:

  (a) predict `won`            Does anything beat the ask? Brier skill vs market.
  (b) predict `won - ask`      Does anything predict the market's ERROR? This is
                               the parametric twin of feature_separation.py,
                               which found nothing non-parametrically. Out-of-
                               sample R^2 <= 0 is the null: no edge.

NO LOOK-AHEAD. Features are entry-time only. Settlement resolves as-of the close
from the quotes stream via spot_at(), never nearest-neighbour.

Run:
  python3 edge_regression.py --start 2026-08-12 --end 2026-09-01
  python3 edge_regression.py --all-regimes          # drop the RANGING/REVERTING gate
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

_VOL_FLOOR = _INSTRUMENT.vol_h_floor
_VOL_CAP = _INSTRUMENT.vol_h_cap

from wing_calibration import (
    band_spot_came_from,
    daterange,
    spot_at,
    spot_series,
)
from boundary_no_quote_replay import (
    join_regimes,
    normalize_universe,
    tolerant_jsonl_gz,
)

from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.model_selection import GroupKFold
from sklearn.neighbors import KNeighborsRegressor
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

REGIMES = ("RANGING", "REVERTING", "TRENDING", "BREAKOUT")

# The recorded regime block is {r, d, v, vh, vr, vc, z, m, ac}. `vr` is the
# fast/slow EWMA vol RATIO (a float, compressed below VOL_RATIO_COMPRESSION),
# and `vc` is the vol-compression boolean — NOT a vol-regime label. An earlier
# version of this file one-hot encoded `vr` against the strings LOW/NORMAL/HIGH,
# which never matched, leaving three constant-zero columns and keeping both the
# vol ratio and the compression flag out of the model entirely.
FEATURES = [
    "ask", "mins", "z", "abs_z", "dist_norm", "abs_dist_norm",
    "side_sign", "came_from", "mom", "accel", "vol", "vol_ratio",
    "compressed", "dir_sign", "band_w_norm",
] + [f"rg_{r}" for r in REGIMES]

# Features excluding the price itself. Framing (b) asks whether these predict the
# market's error; leaving `ask` in would let the model rediscover the price and
# report skill that is really just the baseline.
NON_PRICE = [f for f in FEATURES if f != "ask"]


def build_rows(start: str, end: str, all_regimes: bool, settle_tol: float,
               lookback: float, min_ask: float, max_ask: float):
    """One row per (ticker, 2-minute bucket). Streams a day at a time.

    The dedup bucket matters. The recorder ticks every ~2s, so an undeduped
    population is ~35x oversampled per band and every interval and every
    cross-validation score is computed on near-identical siblings.
    """
    uni = daterange("universe", start, end)
    qs = daterange("quotes", start, end)
    if not uni:
        raise SystemExit(f"no universe recordings in {start}..{end}")
    s_ts, s_sp = spot_series(qs)
    print(f"  {len(s_ts):,} spot samples across {len(qs)} quote days", flush=True)

    by_day = {Path(p).stem.split("_")[1][:10]: p for p in qs}
    X, y, groups, asks = [], [], [], []
    seen: set = set()
    kept_days = []
    nonfinite = [0]

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
        before = len(X)

        for row in joined:
            rg = row.get("rg") or {}
            spot, vol = row.get("spot"), rg.get("v")
            if spot is None or not vol:
                continue
            r = rg.get("r") or "?"
            z = float(rg.get("z") or 0.0)
            if not all_regimes:
                if r not in ("RANGING", "REVERTING"):
                    continue
                if abs(z) < C.BOUNDARY_NO_ZSCORE_MIN:
                    continue
            try:
                now = dt.datetime.fromisoformat(row["t"])
            except Exception:
                continue
            now_e = now.timestamp()
            spot = float(spot)
            vol = float(vol)
            mom = float(rg.get("m") or 0.0)
            accel = float(rg.get("ac") or 0.0)
            vol_ratio = float(rg.get("vr") or 1.0)
            compressed = 1.0 if rg.get("vc") else 0.0
            d = rg.get("d") or ""
            dir_sign = 1.0 if d == "UP" else -1.0 if d == "DN" else 0.0
            past = band_spot_came_from(s_ts, s_sp, now_e, lookback)

            for c in normalize_universe(row, now):
                ask = float(c["ask"])
                if not (min_ask <= ask <= max_ask):
                    continue
                mins = float(c["hours"]) * 60.0
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
                won = 1.0 if lo <= ss < hi else 0.0
                center = 0.5 * (lo + hi)

                # Distance in EXPECTED-MOVE units, not dollars. A $150 gap is a
                # different trade at 5 minutes than at 50, and in dollars the
                # model would have to learn that interaction from scratch.
                #
                # vol_h is clamped to the SAME floor/cap model.py applies before
                # pricing. Unclamped, a near-zero EWMA vol drives sigma toward
                # zero and dist_norm to +/-inf, which is both non-finite and a
                # distance the live bot would never see — it prices off the
                # clamped vol too.
                hours = max(1e-4, float(c["hours"]))
                vol_h = min(max(vol * math.sqrt(C.BARS_PER_HOUR), _VOL_FLOOR),
                            _VOL_CAP)
                sigma = spot * vol_h * math.sqrt(hours)
                dist_norm = (center - spot) / sigma if sigma > 0 else 0.0

                if lo <= spot < hi:
                    side_sign = 0.0
                else:
                    above = lo > spot
                    # "behind" = the side spot came from = where reversion heads.
                    behind = (not above) if z > 0 else above
                    side_sign = 1.0 if behind else -1.0

                came = 1.0 if (past is not None and lo <= past < hi) else 0.0

                feat = {
                    "ask": ask, "mins": mins, "z": z, "abs_z": abs(z),
                    "dist_norm": dist_norm, "abs_dist_norm": abs(dist_norm),
                    "side_sign": side_sign, "came_from": came,
                    "mom": mom, "accel": accel, "vol": vol,
                    "vol_ratio": vol_ratio, "compressed": compressed,
                    "dir_sign": dir_sign,
                    "band_w_norm": (hi - lo) / sigma if sigma > 0 else 0.0,
                }
                for rr in REGIMES:
                    feat[f"rg_{rr}"] = 1.0 if r == rr else 0.0

                vec = [feat[k] for k in FEATURES]
                # A single non-finite value aborts the whole fit, so drop the
                # row and count it rather than letting it poison the run. This
                # should be zero once vol is clamped; the counter is here so a
                # silent population loss is visible instead of assumed absent.
                if not all(math.isfinite(v) for v in vec):
                    nonfinite[0] += 1
                    continue
                X.append(vec)
                y.append(won)
                asks.append(ask)
                groups.append(day)

        if len(X) > before:
            kept_days.append(day)
        print(f"    {day}  {len(joined):,} ticks  -> {len(X) - before:,} rows",
              flush=True)
        del u, q, joined

    if nonfinite[0]:
        print(f"  dropped {nonfinite[0]:,} rows with non-finite features")
    return (np.asarray(X, float), np.asarray(y, float),
            np.asarray(asks, float), np.asarray(groups), kept_days)


def brier(p, y):
    return float(np.mean((np.clip(p, 0.0, 1.0) - y) ** 2))


def skill(p, y, base):
    """Brier skill score vs the market. 0 = matches the ask, 1 = perfect."""
    b0 = brier(base, y)
    return (b0 - brier(p, y)) / b0 if b0 > 0 else float("nan")


def net_pnl_interval(p, y, ask, groups, iterations=4000, seed=11):
    """Day-clustered percentile bootstrap on net P&L per $1 of the TRADED subset.

    Clustered by day for the same reason every other interval in this repo is:
    bands within a session share a spot path, so treating 4,344 selected rows as
    4,344 independent bets shrinks the interval by roughly sqrt(rows/days) and
    turns noise into a result. With ~18 days the honest interval is wide, and it
    should be allowed to say so.

    Resamples DAYS with replacement, recomputing the trade-weighted mean each
    time, so a day with many selections carries its real weight.
    """
    fees = np.array([taker_fee(1, a) for a in ask], float)
    take = p - ask - fees > 0.0
    if not take.any():
        return None
    pnl = (y - ask - fees)[take]
    gs = groups[take]
    by_day: dict = defaultdict(list)
    for v, g in zip(pnl, gs):
        by_day[g].append(v)
    days = list(by_day)
    if len(days) < 8:
        return None
    rng = np.random.default_rng(seed)
    means = np.empty(iterations)
    for i in range(iterations):
        pick = rng.integers(0, len(days), len(days))
        vals = [v for j in pick for v in by_day[days[j]]]
        means[i] = float(np.mean(vals))
    means.sort()
    return (float(means[int(0.025 * iterations)]),
            float(means[int(0.975 * iterations)]), len(days))


def net_pnl(p, y, ask):
    """P&L per opportunity of buying YES wherever the model says it is cheap.

    Fees are per-contract off that contract's own ask — Kalshi's taker fee is
    concave in price, so one fee from a mean ask misprices a cheap population.
    """
    fees = np.array([taker_fee(1, a) for a in ask], float)
    take = p - ask - fees > 0.0
    if not take.any():
        return 0.0, 0
    return float(np.mean((y - ask - fees)[take])), int(take.sum())


def evaluate(X, y, ask, groups, n_splits, ks):
    idx = {f: i for i, f in enumerate(FEATURES)}
    np_cols = [idx[f] for f in NON_PRICE]
    gkf = GroupKFold(n_splits=n_splits)

    oof = {name: np.full(len(y), np.nan) for name in
           ["linear", "knn", "resid_linear", "resid_knn"]}
    knn_k = {}

    for tr, te in gkf.split(X, y, groups):
        # (a) predict `won` from everything, price included.
        lin = make_pipeline(StandardScaler(),
                            LogisticRegression(max_iter=2000, C=1.0))
        lin.fit(X[tr], y[tr])
        oof["linear"][te] = lin.predict_proba(X[te])[:, 1]

        best_k, best_b, best_p = None, np.inf, None
        for k in ks:
            m = make_pipeline(StandardScaler(), KNeighborsRegressor(n_neighbors=k))
            m.fit(X[tr], y[tr])
            p = m.predict(X[te])
            b = brier(p, y[te])
            if b < best_b:
                best_k, best_b, best_p = k, b, p
        oof["knn"][te] = best_p
        knn_k[len(knn_k)] = best_k

        # (b) predict the market's ERROR from non-price features only.
        res_tr = y[tr] - ask[tr]
        rl = make_pipeline(StandardScaler(), Ridge(alpha=1.0))
        rl.fit(X[tr][:, np_cols], res_tr)
        oof["resid_linear"][te] = rl.predict(X[te][:, np_cols])

        rk = make_pipeline(StandardScaler(),
                           KNeighborsRegressor(n_neighbors=max(ks)))
        rk.fit(X[tr][:, np_cols], res_tr)
        oof["resid_knn"][te] = rk.predict(X[te][:, np_cols])

    return oof, knn_k


def r2_oos(pred, truth):
    """Out-of-sample R^2 against predicting zero error — i.e. against the market
    being unbiased. Negative means the model is worse than assuming no edge."""
    ss_res = float(np.sum((truth - pred) ** 2))
    ss_tot = float(np.sum(truth ** 2))
    return 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2026-08-12")
    ap.add_argument("--end", default="2026-09-01")
    ap.add_argument("--all-regimes", action="store_true",
                    help="drop the RANGING/REVERTING + |z| gate")
    ap.add_argument("--settle-tolerance", type=float, default=120.0)
    ap.add_argument("--lookback", type=float, default=600.0)
    ap.add_argument("--min-ask", type=float, default=0.05)
    ap.add_argument("--max-ask", type=float, default=0.60)
    ap.add_argument("--splits", type=int, default=5)
    ap.add_argument("--k", default="25,50,100,200",
                    help="KNN neighbour counts to select among, inner-free "
                         "(selected on the test fold — optimistic by design, "
                         "so a flat result is a STRONG null)")
    args = ap.parse_args()

    ks = [int(v) for v in args.k.split(",") if v.strip()]

    X, y, ask, groups, days = build_rows(
        args.start, args.end, args.all_regimes, args.settle_tolerance,
        args.lookback, args.min_ask, args.max_ask)

    print(f"\n  rows {len(y):,}   days {len(days)}   "
          f"base rate {y.mean():.4f}   mean ask {ask.mean():.4f}")
    if len(y) < 500 or len(set(groups)) < args.splits:
        raise SystemExit("  not enough data for a grouped split")

    b_market = brier(ask, y)
    print(f"  MARKET baseline Brier {b_market:.5f}   "
          f"(mean ask {ask.mean():.4f} vs realized {y.mean():.4f})")

    oof, knn_k = evaluate(X, y, ask, groups, args.splits, ks)

    print(f"\n  (a) PREDICT `won`  — out-of-sample, GroupKFold by day "
          f"({args.splits} folds)")
    print(f"  {'model':<14}{'Brier':>10}{'skill vs market':>18}"
          f"{'net/$1 when it trades':>24}{'trades':>9}")
    m_p, m_n = net_pnl(ask, y, ask)
    print(f"  {'market (ask)':<14}{b_market:>10.5f}{0.0:>18.4f}"
          f"{m_p:>+24.4f}{m_n:>9}")
    for name in ("linear", "knn"):
        p = oof[name]
        pnl, n = net_pnl(p, y, ask)
        print(f"  {name:<14}{brier(p, y):>10.5f}{skill(p, y, ask):>+18.4f}"
              f"{pnl:>+24.4f}{n:>9}")
    print(f"  KNN k chosen per fold: {sorted(knn_k.values())}")

    print(f"\n  net P&L per $1, day-clustered 95% bootstrap "
          f"(the number that decides it):")
    for name in ("linear", "knn"):
        ci = net_pnl_interval(oof[name], y, ask, groups)
        pnl, n = net_pnl(oof[name], y, ask)
        if ci is None:
            print(f"    {name:<14} too few days for an interval")
            continue
        lo_, hi_, nd = ci
        v = ("POSITIVE" if lo_ > 0 else "NEGATIVE" if hi_ < 0
             else "INCLUDES ZERO")
        print(f"    {name:<14} mean {pnl:+.4f}  n={n:5d}  {nd:2d} days  "
              f"95% CI [{lo_:+.4f}, {hi_:+.4f}]  {v}")

    print(f"\n  (b) PREDICT THE MARKET'S ERROR (`won - ask`), non-price "
          f"features only")
    truth = y - ask
    print(f"  {'model':<14}{'OOS R^2':>12}   (<= 0 means no predictable error)")
    for name in ("resid_linear", "resid_knn"):
        print(f"  {name:<14}{r2_oos(oof[name], truth):>12.5f}")

    # Coefficients are only worth reading if the linear model showed skill, but
    # print them either way so a null is inspectable rather than asserted.
    lin = make_pipeline(StandardScaler(), Ridge(alpha=1.0))
    idx = {f: i for i, f in enumerate(FEATURES)}
    lin.fit(X[:, [idx[f] for f in NON_PRICE]], truth)
    coef = lin[-1].coef_
    order = np.argsort(-np.abs(coef))
    print(f"\n  standardized coefficients on `won - ask` (full sample, "
          f"in-sample — direction only, not evidence):")
    for i in order[:8]:
        print(f"    {NON_PRICE[i]:<16}{coef[i]:+.5f}")


if __name__ == "__main__":
    main()
