"""Which terminal distribution prices Kalshi bands best — and does any beat the ask?

The live model is GBM: lognormal terminal price, mu = log(S) + drift - sigma^2*T/2,
with an optional variance-matched Student-t swap on the shape (config.DIST_TAIL_DF).
This scores that base against five alternatives on identical inputs.

EVERY CANDIDATE IS VARIANCE-MATCHED to the same vol_t. That is the whole design.
A distribution allowed to pick its own width can win by being effectively wider
rather than better-shaped, and we would learn nothing about shape. Standardising
each family to unit variance and scaling by the SAME vol_t isolates shape from
scale, the same separation that `_tail_scale` already makes in model.py and that
the drift-vs-vol decomposition made earlier. Whether vol_t itself is right is a
different experiment.

CANDIDATES
  gaussian    The GBM base. Reference point, not a contender.
  student_t   Symmetric fat tails. What the live model uses today.
  skew_t      Hansen skew-t. The symmetric t prices a band $X above spot exactly
              like one $X below; BTC hourly returns are not symmetric, and the
              reversion thesis is entirely about one side, so this is the first
              assumption worth relaxing.
  mixture     Two-component normal, calm + excited. Captures most of what the
              Levy families buy at a fraction of the parameters.
  nig         Normal Inverse Gaussian. Skew and kurtosis in one closed form.
  jump        Merton jump-diffusion. The only candidate with an explicit
              MECHANISM for far bands paying off, rather than a shape that
              happens to fit.
  empirical   No parametric form at all: the realized distribution of h-hour log
              returns, bucketed by vol decile. This is the NON-PARAMETRIC
              CEILING. If it cannot beat the ask, no family above it will,
              because each is a constrained approximation to this.

PARAMETERS ARE FIT OUT OF SAMPLE. Shape parameters are estimated on training
days and applied to held-out days, GroupKFold by calendar day. Fitting on the
full sample and scoring on it would let every flexible family beat the market by
construction. The empirical bucket boundaries and per-bucket samples are built
from training days only, for the same reason.

THE BASELINE IS THE ASK. Skill is reported against the market price, so 0.0
means "as good as Kalshi" and positive means better. Beating the sample mean is
not an edge and is not reported.

Run:
  python3 dist_bakeoff.py --start 2026-08-12 --end 2026-09-01
"""
import argparse
import datetime as dt
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy import optimize, stats

sys.path.insert(0, str(Path(__file__).parent))

from kalshi_btc_bot import config as C
from kalshi_btc_bot.fees import taker_fee
from kalshi_btc_bot.instrument import ACTIVE as _INSTRUMENT

from wing_calibration import daterange, spot_at, spot_series
from boundary_no_quote_replay import (
    join_regimes,
    normalize_universe,
    tolerant_jsonl_gz,
)
from sklearn.model_selection import GroupKFold

_VOL_FLOOR = _INSTRUMENT.vol_h_floor
_VOL_CAP = _INSTRUMENT.vol_h_cap


# ─────────────────────────────────────────────────────────────────────────────
# STANDARDIZED SHAPES. Each returns a CDF on a UNIT-VARIANCE, ZERO-MEAN variate.
# Scaling and the Ito correction are applied identically for all of them by
# band_prob(), so nothing here may introduce its own width.
# ─────────────────────────────────────────────────────────────────────────────
def cdf_gaussian(_params):
    return stats.norm.cdf


def cdf_student_t(params):
    df = max(2.05, float(params["df"]))
    s = math.sqrt(df / (df - 2.0))          # unit-variance rescale
    return lambda z: stats.t.cdf(np.asarray(z) * s, df)


def cdf_skew_t(params):
    """Jones-Faddy skew-t, standardized to zero mean and unit variance.

    scipy's jf_skew_t takes two SHAPE parameters (a, b); the distribution is
    symmetric when a == b and skews as they separate. Its k-th moment exists
    only when min(a, b) > k/2, so a variance requires min(a, b) > 1 — the naive
    (df, skew) parameterisation returns nan mean/std and silently poisons the
    fit.

    Parameterised here as tail weight `df` and asymmetry `s`:
        a = df/2 + s,  b = df/2 - s
    with both legs floored at 1.15 so the variance always exists. s = 0
    reproduces the symmetric Student-t, which makes this family a strict
    generalisation of the live model's shape and the comparison an honest one.
    """
    df = max(2.4, float(params["df"]))
    s = float(params["a"])
    half = df / 2.0
    lim = half - 1.15
    if lim <= 0:
        return stats.norm.cdf
    s = float(np.clip(s, -lim, lim))
    d = stats.jf_skew_t(half + s, half - s)
    m, sd = d.mean(), d.std()
    if not np.isfinite(m) or not np.isfinite(sd) or sd <= 0:
        return stats.norm.cdf
    return lambda z: d.cdf(np.asarray(z) * sd + m)


def cdf_mixture(params):
    """Two zero-mean normals, weight w on the calm component.

    Standardized so w*s1^2 + (1-w)*s2^2 == 1; only the RATIO of the two widths
    is a free parameter, which is what makes this a shape family rather than a
    second vol estimate.
    """
    w = min(0.99, max(0.01, float(params["w"])))
    r = max(1.01, float(params["r"]))       # excited / calm width ratio
    s1 = math.sqrt(1.0 / (w + (1.0 - w) * r * r))
    s2 = s1 * r
    return lambda z: (w * stats.norm.cdf(np.asarray(z) / s1)
                      + (1.0 - w) * stats.norm.cdf(np.asarray(z) / s2))


def cdf_nig(params):
    a = max(0.05, float(params["a"]))
    b = float(np.clip(params["b"], -a * 0.95, a * 0.95))
    d = stats.norminvgauss(a, b)
    m, sd = d.mean(), d.std()
    if not np.isfinite(m) or not np.isfinite(sd) or sd <= 0:
        return stats.norm.cdf
    return lambda z: d.cdf(np.asarray(z) * sd + m)


def cdf_jump(params):
    """Merton jump-diffusion, Poisson-mixed over jump counts, standardized.

    lam jumps expected over the horizon, each N(0, js^2) in log space, on top of
    a diffusion of width sd. Total variance sd^2 + lam*js^2 is normalized to 1,
    so the free parameters are how much of the variance is jump and how it is
    split between frequency and size.
    """
    lam = max(1e-4, float(params["lam"]))
    js = max(1e-4, float(params["js"]))
    jump_var = lam * js * js
    if jump_var >= 0.99:
        js = math.sqrt(0.99 / lam)
        jump_var = 0.99
    sd = math.sqrt(1.0 - jump_var)
    kmax = max(3, int(lam + 5.0 * math.sqrt(lam)) + 1)
    ks = np.arange(0, kmax + 1)
    logw = -lam + ks * math.log(lam) - np.array(
        [math.lgamma(k + 1) for k in ks])
    wts = np.exp(logw)
    wts /= wts.sum()
    widths = np.sqrt(sd * sd + ks * js * js)

    def _cdf(z):
        z = np.atleast_1d(np.asarray(z, float))
        out = np.zeros_like(z, dtype=float)
        for w_, s_ in zip(wts, widths):
            out += w_ * stats.norm.cdf(z / s_)
        return out
    return _cdf


CANDIDATES = {
    "gaussian":  (cdf_gaussian,  {},                              []),
    "student_t": (cdf_student_t, {"df": 5.0},                     ["df"]),
    "skew_t":    (cdf_skew_t,    {"df": 6.0, "a": 0.0},           ["df", "a"]),
    "mixture":   (cdf_mixture,   {"w": 0.8, "r": 2.0},            ["w", "r"]),
    "nig":       (cdf_nig,       {"a": 1.0, "b": 0.0},            ["a", "b"]),
    "jump":      (cdf_jump,      {"lam": 0.3, "js": 0.8},         ["lam", "js"]),
}

BOUNDS = {
    "df": (2.1, 40.0), "a": (-4.0, 4.0), "w": (0.05, 0.95),
    "r": (1.05, 8.0), "b": (-3.0, 3.0), "lam": (0.01, 5.0),
    "js": (0.05, 3.0),
}


def band_prob(cdf, lo, hi, spot, vol_t):
    """P(band) under a standardized shape, scaled by vol_t. Drift is ZERO.

    DRIFT IS DELIBERATELY OFF for every candidate. DRIFT_REVERTING_COEF was
    measured this session to have manufactured the BOUNDARY_NO signal by
    understating true_prob on exactly the bands the strategy bought; leaving any
    drift in would let a shape family inherit or cancel that error and be
    credited for it. Shape is the only thing under test here.
    """
    mu = np.log(spot) - 0.5 * vol_t * vol_t
    z_lo = (np.log(np.maximum(1.0, lo)) - mu) / vol_t
    z_hi = (np.log(np.maximum(1.0, hi)) - mu) / vol_t
    return np.clip(cdf(z_hi) - cdf(z_lo), 1e-6, 1.0 - 1e-6)


def log_loss(p, y):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def brier(p, y):
    return float(np.mean((np.clip(p, 0.0, 1.0) - y) ** 2))


def fit_shape(name, lo, hi, spot, vol_t, y):
    """Fit shape parameters by minimizing log loss on the training rows.

    Log loss rather than Brier because it is the proper score that punishes
    confident errors in the tails, which is exactly where these families differ
    and exactly where a binary's payoff lives.
    """
    fn, init, keys = CANDIDATES[name]
    if not keys:
        return dict(init)

    def obj(v):
        params = dict(zip(keys, v))
        try:
            p = band_prob(fn(params), lo, hi, spot, vol_t)
        except Exception:
            return 1e6
        if not np.all(np.isfinite(p)):
            return 1e6
        return log_loss(p, y)

    res = optimize.minimize(
        obj, [init[k] for k in keys], method="Nelder-Mead",
        bounds=[BOUNDS[k] for k in keys],
        options={"maxiter": 220, "xatol": 1e-3, "fatol": 1e-5})
    return dict(zip(keys, res.x)) if res.success or np.isfinite(res.fun) \
        else dict(init)


def empirical_cdf_factory(train_ret, train_volb, n_buckets):
    """Per-vol-bucket empirical CDF of STANDARDIZED realized log returns.

    Returns of different horizons and vol levels are pooled only after dividing
    by their own vol_t, so the bucket describes SHAPE, matching every parametric
    candidate. A bucket with too few samples falls back to the pooled sample
    rather than to a Gaussian, so `empirical` never silently becomes `gaussian`.
    """
    pooled = np.sort(train_ret)
    by_b = {}
    for b in range(n_buckets):
        sel = train_ret[train_volb == b]
        by_b[b] = np.sort(sel) if len(sel) >= 400 else pooled

    def make(b):
        s = by_b.get(b, pooled)
        n = len(s)
        return lambda z: np.searchsorted(s, np.asarray(z), "right") / n
    return make


def build_rows(start, end, settle_tol, min_ask, max_ask):
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
            spot = float(spot)
            vol_h = min(max(float(vol) * math.sqrt(C.BARS_PER_HOUR),
                            _VOL_FLOOR), _VOL_CAP)

            for c in normalize_universe(row, now):
                ask = float(c["ask"])
                if not (min_ask <= ask <= max_ask):
                    continue
                hours = float(c["hours"])
                if not (0.0 < hours * 60.0 < 70.0):
                    continue
                key = (c["ticker"], int(hours * 60.0) // 2)
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
                vol_t = vol_h * math.sqrt(max(1e-4, hours))
                lo, hi = float(c["low"]), float(c["high"])
                # Realized standardized log return to settlement — the empirical
                # candidate's raw material, and never used by the parametric fits.
                z_real = (math.log(ss / spot) + 0.5 * vol_t * vol_t) / vol_t
                cols["lo"].append(lo)
                cols["hi"].append(hi)
                cols["spot"].append(spot)
                cols["vol_t"].append(vol_t)
                cols["ask"].append(ask)
                cols["y"].append(1.0 if lo <= ss < hi else 0.0)
                cols["z_real"].append(z_real)
                cols["day"].append(day)
        print(f"    {day}  {len(joined):,} ticks  -> "
              f"{len(cols['y']) - before:,} rows", flush=True)
        del u, q, joined

    out = {k: (np.asarray(v, float) if k != "day" else np.asarray(v))
           for k, v in cols.items()}
    good = np.isfinite(out["vol_t"]) & (out["vol_t"] > 0) & \
        np.isfinite(out["z_real"])
    if (~good).any():
        print(f"  dropped {int((~good).sum()):,} rows with non-finite scale")
    return {k: v[good] for k, v in out.items()}


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2026-08-12")
    ap.add_argument("--end", default="2026-09-01")
    ap.add_argument("--settle-tolerance", type=float, default=120.0)
    ap.add_argument("--min-ask", type=float, default=0.02)
    ap.add_argument("--max-ask", type=float, default=0.98)
    ap.add_argument("--splits", type=int, default=5)
    ap.add_argument("--vol-buckets", type=int, default=4)
    args = ap.parse_args()

    d = build_rows(args.start, args.end, args.settle_tolerance,
                   args.min_ask, args.max_ask)
    y, ask, day = d["y"], d["ask"], d["day"]
    n_days = len(set(day))
    print(f"\n  rows {len(y):,}   days {n_days}   base rate {y.mean():.4f}   "
          f"mean ask {ask.mean():.4f}")
    if len(y) < 1000 or n_days < args.splits:
        raise SystemExit("  not enough data for a grouped split")

    names = list(CANDIDATES) + ["empirical"]
    oof = {n: np.full(len(y), np.nan) for n in names}
    fitted = defaultdict(list)

    gkf = GroupKFold(n_splits=args.splits)
    for fold, (tr, te) in enumerate(gkf.split(y, y, day)):
        for name in CANDIDATES:
            params = fit_shape(name, d["lo"][tr], d["hi"][tr], d["spot"][tr],
                               d["vol_t"][tr], y[tr])
            fitted[name].append(params)
            oof[name][te] = band_prob(CANDIDATES[name][0](params),
                                      d["lo"][te], d["hi"][te],
                                      d["spot"][te], d["vol_t"][te])

        # Empirical: bucket edges from TRAINING vol only, so the test fold
        # cannot influence its own bucketing.
        edges = np.quantile(d["vol_t"][tr],
                            np.linspace(0, 1, args.vol_buckets + 1)[1:-1])
        b_tr = np.searchsorted(edges, d["vol_t"][tr])
        b_te = np.searchsorted(edges, d["vol_t"][te])
        make = empirical_cdf_factory(d["z_real"][tr], b_tr, args.vol_buckets)
        p = np.empty(len(te))
        for b in range(args.vol_buckets):
            m = b_te == b
            if not m.any():
                continue
            p[m] = band_prob(make(b), d["lo"][te][m], d["hi"][te][m],
                             d["spot"][te][m], d["vol_t"][te][m])
        oof["empirical"][te] = p
        print(f"    fold {fold + 1}/{args.splits} done", flush=True)

    b_mkt, l_mkt = brier(ask, y), log_loss(ask, y)
    print(f"\n  MARKET baseline   Brier {b_mkt:.5f}   log loss {l_mkt:.5f}")
    print(f"  (mean ask {ask.mean():.4f} vs realized {y.mean():.4f})\n")
    print(f"  {'distribution':<13}{'Brier':>10}{'skill':>9}{'log loss':>11}"
          f"{'LL skill':>10}{'net/$1':>10}{'trades':>8}")

    fees = np.array([taker_fee(1, a) for a in ask], float)
    rows = []
    for name in names:
        p = oof[name]
        b, ll = brier(p, y), log_loss(p, y)
        take = p - ask - fees > 0.0
        pnl = float(np.mean((y - ask - fees)[take])) if take.any() else 0.0
        rows.append((name, b, (b_mkt - b) / b_mkt, ll, (l_mkt - ll) / l_mkt,
                     pnl, int(take.sum())))
    for name, b, s, ll, ls, pnl, n in rows:
        print(f"  {name:<13}{b:>10.5f}{s:>+9.4f}{ll:>11.5f}{ls:>+10.4f}"
              f"{pnl:>+10.4f}{n:>8}")

    print(f"\n  fitted shape parameters by fold (stability matters as much as "
          f"the score — a family whose parameters swing fold to fold is fitting "
          f"the sample, not the market):")
    for name in CANDIDATES:
        if not CANDIDATES[name][2]:
            continue
        keys = CANDIDATES[name][2]
        s = "   ".join(
            k + "=" + "/".join(f"{f[k]:.2f}" for f in fitted[name])
            for k in keys)
        print(f"    {name:<11}{s}")

    print(f"\n  Skill is measured against the ASK. A distribution can win this "
          f"table\n  and still have no edge: beating the market's Brier by "
          f"0.001 is a\n  calibration result, and only the net/$1 column is a "
          f"trading result.")


if __name__ == "__main__":
    main()
