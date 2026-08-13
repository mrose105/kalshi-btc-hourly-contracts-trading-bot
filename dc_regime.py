"""
Directional Change (DC) regime detection — Chen & Tsang framework.

WHY THIS EXISTS
---------------
Every regime bug this week came from the same root cause: fixed-interval
sampling meaning different things in different environments.

    momentum(60)      ->  60 SECONDS live (2s ticks), 2.5 HOURS in the backtest
    consecutive()     ->  prices[-10:] = 20 SECONDS live, 50 MINUTES in backtest
    zscore(300)       ->  looked identical, was actually fine (self-normalising)

Each was invisible until measured, and each meant nothing regime-dependent had
been validly backtested. Directional Change sidesteps the entire class: it
samples on PRICE MOVEMENT, not clock time. A theta=0.1% directional change is
the same event on 2-second ticks or 5-minute bars, provided the data is fine
enough to observe it. That is the property this module is built to test.

THE ALGORITHM (Tsang; see Chen's Essex thesis, ch. 2)
----------------------------------------------------
Track a running extreme. In a downward run, an UPTURN is confirmed when price
rises theta from the most recent extreme LOW; in an upward run, a DOWNTURN is
confirmed when price falls theta from the most recent extreme HIGH. Each
confirmed event closes a "run" = a DC event plus its subsequent overshoot.

Indicators, per completed run (Tsang 2017):
    TMV = (P_ext_now - P_ext_prev) / (P_ext_prev * theta)
          price movement between consecutive extremes, NORMALISED BY THETA so
          it is comparable across thresholds ("how many thetas did it move").
    T   = elapsed time between DC confirmations
    R   = TMV * theta / T
          time-adjusted return — movement per unit time.

CONVENTION NOTE, stated rather than hidden: sources describe R as "absolute TMV
divided by T", and whether the theta factor is folded in varies by paper. The
choice only rescales R by a constant for a fixed theta, so it cannot change any
relative comparison or regime ranking here. R is reported in return-per-second
units (theta included) because that is dimensionally interpretable.

NOT WIRED INTO TRADING. This is a measurement tool. Any use in live entry
decisions would need the same tune/validate treatment as everything else, and
would touch true_prob via the drift term.

Usage:
    python3 dc_regime.py --selftest
    python3 dc_regime.py --compare      # live ticks vs backtest bars, same theta
"""
import argparse
import glob
import math
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


@dataclass
class Run:
    """One completed DC run: the confirmed event plus its overshoot."""
    direction: str          # "UP" or "DN" — direction of the run just closed
    start_ext: float        # extreme price that began the run
    end_ext: float          # extreme price that ended it
    start_t: float          # seconds
    end_t: float
    confirm_t: float        # when the DC event was confirmed (before overshoot)

    @property
    def tmv_raw(self) -> float:
        return (self.end_ext - self.start_ext) / self.start_ext if self.start_ext else 0.0

    def tmv(self, theta: float) -> float:
        return self.tmv_raw / theta if theta else 0.0

    @property
    def T(self) -> float:
        return max(1e-9, self.end_t - self.start_t)

    def R(self, theta: float) -> float:
        return abs(self.tmv(theta)) * theta / self.T


def dc_runs(series, theta: float):
    """series: iterable of (t_seconds, price). Returns completed Runs.

    Standard two-mode DC scan. `mode` is the direction of the run currently
    being built; a confirmation flips it and closes the previous run at the
    extreme that was standing when the flip occurred.
    """
    it = iter(series)
    try:
        t0, p0 = next(it)
    except StopIteration:
        return []
    if theta <= 0:
        return []

    runs = []
    # Start in a nominal upward run; the first confirmation corrects the phase.
    # This MUST be a definite mode, not None: with a None state that falls
    # through both branches, the first sets ext to the running high and the
    # second immediately pulls it back to the current price, so ext tracks spot
    # instead of an extreme and nothing can ever confirm. (Caught by the
    # random-walk selftest, which returned 0 runs on a series with 5.1%
    # pullbacks.) The branches are mutually exclusive by construction.
    mode = "UP"
    ext_p, ext_t = p0, t0       # running extreme of the current run
    prev_ext_p, prev_ext_t = p0, t0

    for t, p in it:
        if mode == "UP":
            # building an upward run: track the high, watch for a downturn
            if p > ext_p:
                ext_p, ext_t = p, t
            elif ext_p > 0 and p <= ext_p * (1 - theta):
                runs.append(Run("UP", prev_ext_p, ext_p, prev_ext_t, ext_t, t))
                prev_ext_p, prev_ext_t = ext_p, ext_t
                mode = "DN"
                ext_p, ext_t = p, t
        else:
            # downward run: track the low, watch for an upturn
            if p < ext_p:
                ext_p, ext_t = p, t
            elif ext_p > 0 and p >= ext_p * (1 + theta):
                runs.append(Run("DN", prev_ext_p, ext_p, prev_ext_t, ext_t, t))
                prev_ext_p, prev_ext_t = ext_p, ext_t
                mode = "UP"
                ext_p, ext_t = p, t
    return runs


def summarize(runs, theta: float) -> dict:
    if not runs:
        return {"n": 0}
    tmvs = [abs(r.tmv(theta)) for r in runs]
    Ts   = [r.T for r in runs]
    Rs   = [r.R(theta) for r in runs]
    span = runs[-1].end_t - runs[0].start_t
    return {
        "n": len(runs),
        "NDC_per_hour": len(runs) / (span / 3600) if span > 0 else 0.0,
        "TMV_med": statistics.median(tmvs),
        "T_med": statistics.median(Ts),
        "R_med": statistics.median(Rs),
        "up_frac": sum(1 for r in runs if r.direction == "UP") / len(runs),
    }


# ─────────────────────────────────────────────
# SELF-TEST
# ─────────────────────────────────────────────
def selftest() -> int:
    fails = []

    def check(name, cond, detail=""):
        print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
        if not cond:
            fails.append(name)

    print("dc_regime selftest")

    # 1. a clean zigzag produces the expected number of runs
    #    up to 110, down to 99, up to 110 ... with theta=5%
    prices = [100, 105, 110, 104, 99, 104, 110, 104, 99]
    s = [(float(i), float(p)) for i, p in enumerate(prices)]
    runs = dc_runs(s, 0.05)
    check("zigzag yields runs", len(runs) >= 2, f"got {len(runs)}")

    # 2. a monotonic ramp produces no reversals
    ramp = [(float(i), 100.0 * (1 + 0.001 * i)) for i in range(200)]
    check("monotonic ramp -> no completed runs", len(dc_runs(ramp, 0.05)) == 0)

    # 3. noise below theta is ignored entirely
    noise = [(float(i), 100.0 + (0.1 if i % 2 else -0.1)) for i in range(500)]
    check("sub-threshold noise ignored", len(dc_runs(noise, 0.05)) == 0)

    # 4. smaller theta detects at least as many runs as a larger one
    import random
    random.seed(11)
    walk, p = [], 100.0
    for i in range(4000):
        p *= (1 + random.gauss(0, 0.0008))
        walk.append((float(i), p))
    n_small = len(dc_runs(walk, 0.005))
    n_big   = len(dc_runs(walk, 0.02))
    check("smaller theta -> more runs", n_small > n_big, f"{n_small} vs {n_big}")

    # 5. TMV is normalised by theta: same move, half theta -> ~double TMV
    r = dc_runs(walk, 0.01)
    r2 = dc_runs(walk, 0.02)
    if r and r2:
        check("TMV normalisation is theta-relative",
              statistics.median([abs(x.tmv(0.01)) for x in r]) >
              statistics.median([abs(x.tmv(0.02)) for x in r2]) * 0.8)
    else:
        check("TMV normalisation is theta-relative", False, "no runs")

    # 6. THE KEY PROPERTY — resolution agnosticism.
    #    Subsample the same walk 10x coarser; DC should find a similar number of
    #    runs, unlike a fixed-window momentum statistic which would change by ~10x.
    coarse = walk[::10]
    n_fine = len(dc_runs(walk, 0.01))
    n_coar = len(dc_runs(coarse, 0.01))
    ratio = n_coar / n_fine if n_fine else 0
    check("10x coarser data -> similar run count (resolution-agnostic)",
          0.5 <= ratio <= 1.5, f"fine={n_fine} coarse={n_coar} ratio={ratio:.2f}")

    # 7. R has sane units and is positive
    if r:
        check("R positive and finite", all(x.R(0.01) > 0 and math.isfinite(x.R(0.01)) for x in r))
    else:
        check("R positive and finite", False, "no runs")

    # 8. degenerate inputs
    check("empty / single point -> no runs",
          dc_runs([], 0.01) == [] and dc_runs([(0.0, 100.0)], 0.01) == [])
    check("theta<=0 -> no runs", dc_runs(walk, 0.0) == [])

    print(f"\n{len(fails)} failure(s)" if fails else "\nall checks passed")
    return 1 if fails else 0


def load_live():
    import real_price_edge_test as R
    import datetime as dt
    out = []
    for f in sorted(glob.glob("recordings/quotes_*.jsonl.gz")):
        for rec in R.tolerant(f):
            if rec.get("spot"):
                out.append((dt.datetime.fromisoformat(rec["t"]).timestamp(), rec["spot"]))
    out.sort()
    return out


def load_bars(days=30):
    import yfinance as yf
    df = yf.download("BTC-USD", period=f"{days}d", interval="5m",
                     progress=False, auto_adjust=False)
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.droplevel(1)
    return [(ts.timestamp(), float(r["Close"])) for ts, r in df.iterrows()]


def compare():
    live = load_live()
    bars = load_bars()
    if not live:
        raise SystemExit("no recordings found")
    lo, hi = live[0][0], live[-1][0]
    bars_ov = [(t, p) for t, p in bars if lo <= t <= hi]
    print(f"live ticks  : {len(live):,}  (~{(hi-lo)/3600:.1f}h, ~2s spacing)")
    print(f"5-min bars  : {len(bars_ov):,}  over the SAME span (150x coarser)\n")
    print("Same theta applied to both. If DC is resolution-agnostic, the run")
    print("counts per hour should be close — this is what fixed-window momentum")
    print("could NOT do (60s live vs 2.5h backtest off one constant).\n")
    print(f"  {'theta':>7} {'live runs/h':>12} {'bar runs/h':>12} {'ratio':>7} "
          f"{'live TMV':>9} {'bar TMV':>9}")
    for theta in (0.0005, 0.001, 0.002, 0.005):
        a = summarize(dc_runs(live, theta), theta)
        b = summarize(dc_runs(bars_ov, theta), theta)
        if not a["n"] or not b["n"]:
            print(f"  {theta:>7.4f} {'(insufficient runs)':>32}")
            continue
        ratio = b["NDC_per_hour"] / a["NDC_per_hour"] if a["NDC_per_hour"] else 0
        print(f"  {theta:>7.4f} {a['NDC_per_hour']:>12.2f} {b['NDC_per_hour']:>12.2f} "
              f"{ratio:>7.2f} {a['TMV_med']:>9.2f} {b['TMV_med']:>9.2f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--compare", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        sys.exit(selftest())
    if args.compare:
        compare()
        return
    ap.print_help()


if __name__ == "__main__":
    main()
