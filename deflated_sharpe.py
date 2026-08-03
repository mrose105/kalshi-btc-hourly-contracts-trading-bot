"""
Deflated Sharpe Ratio (DSR) and Probability of Backtest Overfitting screen.

Implements Bailey & Lopez de Prado (2014), "The Deflated Sharpe Ratio:
Correcting for Selection Bias, Backtest Overfitting, and Non-Normality."
Journal of Portfolio Management, 40(5).

Why this exists: docs/QUANT_STANDARDS_AUDIT.md found that three production
config values (PEAK_GIVEBACK_FRACTION, NO_STOP, BOUNDARY_NO_ZSCORE_MIN) were
each chosen by grid-searching the SAME backtest window and keeping whichever
value maximized return or Sharpe, with no held-out validation and no
correction for the resulting selection bias. That is exactly the failure
mode this paper's formula exists to catch: the more parameter combinations
you try, the more the best-looking one is inflated by chance alone, even if
every individual trial were pure noise.

Two building blocks:

  1. Probabilistic Sharpe Ratio (PSR) — the probability the TRUE Sharpe
     ratio exceeds some benchmark SR*, given the estimation uncertainty of a
     Sharpe computed from a finite, possibly skewed/fat-tailed sample:

         PSR(SR*) = Phi( (SR_hat - SR*) * sqrt(T-1)
                          / sqrt(1 - skew*SR_hat + ((kurt-1)/4)*SR_hat^2) )

     T = number of return observations, skew/kurt = sample skewness and
     (non-excess, normal=3) kurtosis of those returns, Phi = standard normal
     CDF. All Sharpe-scale quantities (SR_hat, SR*) must be on the SAME
     period as T (e.g. daily if T counts daily returns) — mixing an
     annualized Sharpe with a daily T silently corrupts the result.

  2. Expected maximum Sharpe under N independent trials of pure noise —
     used as the benchmark SR* fed into PSR to get the DEFLATED Sharpe:

         E[max SR_n] ~= sigma_SR * ( (1-gamma)*Z^-1(1 - 1/N)
                                       + gamma*Z^-1(1 - 1/(N*e)) )

     gamma = Euler-Mascheroni constant, Z^-1 = inverse standard normal CDF,
     sigma_SR = standard deviation of the Sharpe ratio ACROSS the N trials
     (the practical proxy used here, per the paper, in place of a formal
     null simulation). This grows with N: trying more things raises the bar
     a real strategy must clear before its Sharpe is believable.

Usage:
    python3 deflated_sharpe.py                              # latest backtest JSON, latest sweep trials
    python3 deflated_sharpe.py results/backtest_X.json       # specific backtest file
    python3 deflated_sharpe.py --trials results/dsr_trials.json --sweep peak_giveback
    python3 deflated_sharpe.py --n-trials 8 --sr-std 0.071   # override trial stats manually
    python3 deflated_sharpe.py --selftest                    # run correctness self-checks, no data needed
"""
import argparse
import glob
import json
import math
import sys
from pathlib import Path

import numpy as np
from scipy.stats import norm

EULER_MASCHERONI = 0.5772156649015329


def daily_returns_from_trades(trades: list[dict], capital: float) -> np.ndarray:
    """Reconstruct the exact daily-return series kalshi_btc_backtest.py's
    Sharpe calc uses, from a saved backtest JSON's trade list. Kept as a
    literal mirror of that binning (group P&L by exited_at date, cumsum,
    percent-change day over day) so T/skew/kurtosis here are computed on
    the identical series the published Sharpe was computed from — deflating
    a Sharpe against statistics of a DIFFERENT return series would be
    meaningless.
    """
    daily_pnl: dict = {}
    for t in trades:
        ts = t["exited_at"]
        day = ts[:10] if isinstance(ts, str) else ts.date().isoformat()
        daily_pnl[day] = daily_pnl.get(day, 0.0) + t["pnl"]
    day_keys = sorted(daily_pnl)
    day_ends = np.array([daily_pnl[d] for d in day_keys]).cumsum() + capital
    day_start = np.concatenate(([capital], day_ends[:-1]))
    return (day_ends - day_start) / day_start


def sample_stats(daily_ret: np.ndarray) -> dict:
    """T, mean, std, skew, and (non-excess, normal=3) kurtosis of a daily
    return series, plus the resulting per-day and annualized Sharpe —
    recomputed independently as a check against the backtest's own reported
    Sharpe (see main()).
    """
    from scipy.stats import skew as _skew, kurtosis as _kurt
    T = len(daily_ret)
    mean = float(daily_ret.mean())
    # ddof=0 (population std, numpy's default) — matches
    # kalshi_btc_backtest.py's own daily_ret.std() exactly. Using ddof=1
    # here instead produced a Sharpe ~0.9% below the backtest's own reported
    # value at T=59, caught by the reconciliation check in main() below.
    std = float(daily_ret.std(ddof=0)) if T > 1 else 0.0
    sr_daily = mean / std if std > 0 else 0.0
    return {
        "T": T,
        "mean": mean,
        "std": std,
        "skew": float(_skew(daily_ret)) if T > 2 else 0.0,
        "kurtosis": float(_kurt(daily_ret, fisher=False)) if T > 3 else 3.0,
        "sr_daily": sr_daily,
        "sr_annualized": sr_daily * math.sqrt(365),
    }


def probabilistic_sharpe_ratio(sr_hat: float, sr_benchmark: float, T: int,
                               skew: float, kurtosis: float) -> float:
    """PSR(SR*): probability the true Sharpe exceeds sr_benchmark, given a
    sample Sharpe sr_hat estimated from T observations with the given
    skew/(non-excess) kurtosis. All Sharpe values must be on the SAME
    per-period scale as T (see module docstring)."""
    if T <= 1:
        return float("nan")
    denom = 1.0 - skew * sr_hat + ((kurtosis - 1.0) / 4.0) * sr_hat ** 2
    if denom <= 0:
        # Degenerate: reported only for pathological (T, skew, kurtosis,
        # sr_hat) combinations that shouldn't occur with real return data.
        return float("nan")
    z = (sr_hat - sr_benchmark) * math.sqrt(T - 1) / math.sqrt(denom)
    return float(norm.cdf(z))


def expected_max_sharpe(n_trials: int, sr_std: float) -> float:
    """E[max SR_n] under n_trials independent draws of Sharpe ratios with
    standard deviation sr_std (Bailey & Lopez de Prado 2014, eq. 7-ish;
    the practical form using the empirical cross-trial std of realized
    Sharpes in place of a formal null distribution). Returns 0.0 for
    n_trials <= 1 (no selection-bias correction applies to a single trial;
    the underlying inverse-CDF term is undefined at N=1)."""
    if n_trials <= 1 or sr_std <= 0:
        return 0.0
    n = float(n_trials)
    term1 = (1.0 - EULER_MASCHERONI) * norm.ppf(1.0 - 1.0 / n)
    term2 = EULER_MASCHERONI * norm.ppf(1.0 - 1.0 / (n * math.e))
    return sr_std * (term1 + term2)


def deflated_sharpe_ratio(sr_hat: float, T: int, skew: float, kurtosis: float,
                          n_trials: int, sr_std: float) -> dict:
    """Full pipeline: compute E[max SR|N trials] as the benchmark, then PSR
    against that benchmark. sr_hat and sr_std must both be on the SAME
    per-period scale as T (e.g. both daily). Returns a dict with every
    intermediate value so the result is auditable, not just a final number."""
    sr_benchmark = expected_max_sharpe(n_trials, sr_std)
    dsr = probabilistic_sharpe_ratio(sr_hat, sr_benchmark, T, skew, kurtosis)
    psr_vs_zero = probabilistic_sharpe_ratio(sr_hat, 0.0, T, skew, kurtosis)
    return {
        "sr_hat": sr_hat, "T": T, "skew": skew, "kurtosis": kurtosis,
        "n_trials": n_trials, "sr_std": sr_std,
        "sr_benchmark": sr_benchmark,
        "psr_vs_zero": psr_vs_zero,
        "dsr": dsr,
    }


# ── Self-checks — algebraic properties any correct implementation must have ──
def _selftest() -> bool:
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        ok = ok and cond

    # PSR(SR* == SR_hat) must be exactly 0.5: the z-score numerator is 0,
    # Phi(0) = 0.5 regardless of T/skew/kurtosis, as long as the sample is
    # large enough for the denominator to be positive.
    p = probabilistic_sharpe_ratio(1.0, 1.0, T=100, skew=0.3, kurtosis=4.0)
    check("PSR(SR*=SR_hat) == 0.5", abs(p - 0.5) < 1e-9)

    # expected_max_sharpe must be strictly increasing in N for fixed sr_std:
    # trying more things raises the bar a real strategy must clear.
    vals = [expected_max_sharpe(n, sr_std=0.1) for n in (2, 5, 10, 50, 100, 500)]
    check("E[max SR|N] strictly increasing in N",
          all(vals[i] < vals[i+1] for i in range(len(vals)-1)))

    # N<=1 must not apply any deflation (formula undefined at N=1; no
    # selection-bias correction is meaningful for a single trial).
    check("E[max SR|N<=1] == 0.0",
          expected_max_sharpe(1, 0.1) == 0.0 and expected_max_sharpe(0, 0.1) == 0.0)

    # Under normal returns (skew=0, kurtosis=3) the PSR denominator reduces
    # to Lo (2002)'s asymptotic Sharpe-ratio variance term 1 + SR^2/2.
    denom_formula = 1.0 - 0.0 * 1.5 + ((3.0 - 1.0) / 4.0) * 1.5 ** 2
    lo_2002 = 1.0 + 0.5 * 1.5 ** 2
    check("normal-case denominator matches Lo (2002) 1 + SR^2/2",
          abs(denom_formula - lo_2002) < 1e-9)

    # A strategy with sr_hat far above a modest E[max SR|N] should look
    # convincing (DSR near 1); far below should look like noise (DSR near 0).
    strong = deflated_sharpe_ratio(sr_hat=0.30, T=250, skew=0.0, kurtosis=3.0,
                                   n_trials=10, sr_std=0.02)
    weak = deflated_sharpe_ratio(sr_hat=0.01, T=250, skew=0.0, kurtosis=3.0,
                                 n_trials=10, sr_std=0.02)
    check("far-above-benchmark case has DSR > 0.99", strong["dsr"] > 0.99)
    check("far-below-benchmark case has DSR < 0.5", weak["dsr"] < weak["sr_benchmark"] or weak["dsr"] < 0.5)

    return ok


def _load_trials(path: str, sweep: str | None) -> list[float]:
    trials = json.load(open(path))
    if sweep:
        trials = [t for t in trials if t.get("sweep") == sweep]
    return [t["sharpe"] for t in trials]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("backtest_json", nargs="?",
                    help="Backtest result file (default: latest in results/)")
    ap.add_argument("--trials", default="results/dsr_trials.json",
                    help="JSON file of {sweep, sharpe} trial records")
    ap.add_argument("--sweep", default="peak_giveback",
                    help="Which sweep's trials to use for N/sr_std "
                         "(default: peak_giveback — the no_threshold sweep "
                         "is currently DEGENERATE, see QUANT_STANDARDS_AUDIT.md)")
    ap.add_argument("--n-trials", type=int, help="Override trial count N")
    ap.add_argument("--sr-std", type=float,
                    help="Override cross-trial Sharpe std (ANNUALIZED scale)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        print("Self-check: algebraic correctness properties\n")
        sys.exit(0 if _selftest() else 1)

    path = args.backtest_json
    if not path:
        files = sorted(glob.glob("results/backtest_*.json"))
        if not files:
            sys.exit("No backtest result files found in results/")
        path = files[-1]
    d = json.load(open(path))
    trades = d["trades"]
    capital = d.get("config", {}).get("capital", 10000.0)
    reported_sharpe = d["metrics"].get("sharpe")

    daily_ret = daily_returns_from_trades(trades, capital)
    stats = sample_stats(daily_ret)

    print(f"=== {Path(path).name} ===")
    print(f"  trades: {len(trades)}   daily obs (T): {stats['T']}")
    print(f"  reconstructed annualized Sharpe: {stats['sr_annualized']:.3f}"
          f"   (backtest reported: {reported_sharpe})")
    if abs(stats['sr_annualized'] - (reported_sharpe or 0)) > 0.02:
        print("  ⚠️  reconstructed Sharpe does not match the reported value — "
              "daily binning or capital may not match the original run")
    print(f"  skew: {stats['skew']:+.3f}   kurtosis: {stats['kurtosis']:.3f} "
          f"(normal = 3.0)")

    if args.n_trials is not None and args.sr_std is not None:
        n_trials, sr_std_annual = args.n_trials, args.sr_std
        trial_source = "manual override"
    else:
        try:
            trial_sharpes = _load_trials(args.trials, args.sweep)
        except FileNotFoundError:
            sys.exit(f"No trials file at {args.trials}. Run a sweep first, "
                     f"or pass --n-trials/--sr-std directly.")
        if len(trial_sharpes) < 2:
            sys.exit(f"Only {len(trial_sharpes)} trial(s) found for sweep "
                     f"'{args.sweep}' — need >= 2 to estimate cross-trial variance.")
        n_trials = len(trial_sharpes)
        sr_std_annual = float(np.std(trial_sharpes, ddof=1))
        trial_source = f"{args.trials} [{args.sweep}]"

    # Convert annualized sr_std to daily scale to match T (daily observations).
    sr_std_daily = sr_std_annual / math.sqrt(365)

    print(f"\n  N trials: {n_trials}  (source: {trial_source})")
    print(f"  cross-trial Sharpe std: {sr_std_annual:.3f} annualized "
          f"({sr_std_daily:.5f} daily)")

    result = deflated_sharpe_ratio(
        sr_hat=stats["sr_daily"], T=stats["T"],
        skew=stats["skew"], kurtosis=stats["kurtosis"],
        n_trials=n_trials, sr_std=sr_std_daily,
    )

    print(f"\n  E[max Sharpe | {n_trials} trials of pure noise]: "
          f"{result['sr_benchmark'] * math.sqrt(365):.3f} annualized")
    print(f"  PSR (vs. SR*=0, no trial correction): {result['psr_vs_zero']:.1%}")
    print(f"  DEFLATED SHARPE RATIO (vs. E[max SR|N]): {result['dsr']:.1%}")

    print(f"\n  Interpretation: given {n_trials} parameter trials were run "
          f"against this same\n  backtest window, there is a {result['dsr']:.1%} "
          f"probability the strategy's true\n  Sharpe ratio genuinely exceeds "
          f"what {n_trials} trials of pure noise would\n  be expected to "
          f"produce by chance alone.")


if __name__ == "__main__":
    main()
