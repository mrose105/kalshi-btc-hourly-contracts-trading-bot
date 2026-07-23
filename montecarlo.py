"""
Monte Carlo simulation of backtest equity curves.

Usage:
    python3 montecarlo.py                                     # uses latest result
    python3 montecarlo.py results/backtest_20260722_1946.json # specific file
    python3 montecarlo.py --n 2000 --capital 5000             # options
"""

import json
import sys
import argparse
import glob
import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

# ── RStudio / ggplot2-like style ──────────────────────────────────────────────
plt.rcParams.update({
    "figure.facecolor":  "#f8f8f8",
    "axes.facecolor":    "#ffffff",
    "axes.edgecolor":    "#cccccc",
    "axes.grid":         True,
    "grid.color":        "#e5e5e5",
    "grid.linewidth":    0.8,
    "font.family":       "DejaVu Sans",
    "font.size":         11,
    "axes.titlesize":    13,
    "axes.labelsize":    11,
    "legend.fontsize":   10,
    "xtick.color":       "#555555",
    "ytick.color":       "#555555",
    "axes.labelcolor":   "#333333",
    "axes.titlepad":     12,
})

BAND_COLOR  = "#4C72B0"
ACTUAL_COLOR = "#C44E52"
MEDIAN_COLOR = "#2ca02c"


def load_pnls(path: str) -> tuple[list[float], float]:
    with open(path) as f:
        d = json.load(f)
    trades = d["trades"]
    capital = d.get("config", {}).get("capital", 5000)
    return [t["pnl"] for t in trades], capital


def build_equity(pnls: list[float], capital: float) -> np.ndarray:
    curve = np.empty(len(pnls) + 1)
    curve[0] = capital
    for i, p in enumerate(pnls):
        curve[i + 1] = curve[i] + p
    return curve


def run_montecarlo(pnls: list[float], capital: float, n: int, seed: int = 42) -> np.ndarray:
    rng = np.random.default_rng(seed)
    arr = np.array(pnls)
    n_trades = len(arr)
    sims = np.empty((n, n_trades + 1))
    sims[:, 0] = capital
    for i in range(n):
        shuffled = rng.choice(arr, size=n_trades, replace=True)
        sims[i, 1:] = capital + np.cumsum(shuffled)
    return sims


def max_drawdown(curve: np.ndarray) -> float:
    peak = np.maximum.accumulate(curve)
    dd = (curve - peak) / peak
    return float(dd.min())


def plot(sims: np.ndarray, actual: np.ndarray, capital: float, out: str):
    n_trades = actual.shape[0] - 1
    x = np.arange(n_trades + 1)

    pct5  = np.percentile(sims, 5,  axis=0)
    pct25 = np.percentile(sims, 25, axis=0)
    pct50 = np.percentile(sims, 50, axis=0)
    pct75 = np.percentile(sims, 75, axis=0)
    pct95 = np.percentile(sims, 95, axis=0)

    sim_drawdowns = np.array([max_drawdown(sims[i]) for i in range(sims.shape[0])])
    actual_dd = max_drawdown(actual)

    fig, axes = plt.subplots(1, 2, figsize=(16, 6))

    # ── Left: equity fan ─────────────────────────────────────────────────────
    ax = axes[0]
    ax.fill_between(x, pct5,  pct95, alpha=0.15, color=BAND_COLOR, label="5–95th pct")
    ax.fill_between(x, pct25, pct75, alpha=0.30, color=BAND_COLOR, label="25–75th pct")
    ax.plot(x, pct50,  color=MEDIAN_COLOR, linewidth=1.8, linestyle="--", label="Median sim")
    ax.plot(x, actual, color=ACTUAL_COLOR,  linewidth=2.2, label="Actual backtest")
    ax.axhline(capital, color="#999999", linewidth=0.9, linestyle=":")

    final_actual = actual[-1]
    ret_actual = (final_actual / capital - 1) * 100
    ax.annotate(f"${final_actual:,.0f}\n+{ret_actual:.0f}%",
                xy=(n_trades, final_actual), xytext=(-65, 8),
                textcoords="offset points", color=ACTUAL_COLOR,
                fontsize=9, fontweight="bold")

    ax.set_title(f"Equity fan  |  {sims.shape[0]:,} sims  |  ${capital/1000:.0f}K start", fontweight="bold")
    ax.set_xlabel("Trade #")
    ax.set_ylabel("Portfolio value ($)")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"${v:,.0f}"))
    ax.legend(loc="upper left", framealpha=0.9)
    ax.spines[["top", "right"]].set_visible(False)

    # ── Right: drawdown distribution ─────────────────────────────────────────
    ax2 = axes[1]
    dd_pct = sim_drawdowns * 100
    ax2.hist(dd_pct, bins=40, color=BAND_COLOR, alpha=0.75, edgecolor="white", linewidth=0.4)
    ax2.axvline(np.percentile(dd_pct, 5),  color="#e07b39", linewidth=1.5, linestyle="--",
                label=f"5th pct: {np.percentile(dd_pct,5):.1f}%")
    ax2.axvline(np.percentile(dd_pct, 50), color=MEDIAN_COLOR, linewidth=1.5, linestyle="--",
                label=f"Median: {np.percentile(dd_pct,50):.1f}%")
    ax2.axvline(actual_dd * 100, color=ACTUAL_COLOR, linewidth=2.0,
                label=f"Actual: {actual_dd*100:.1f}%")

    p_gt20 = (dd_pct < -20).mean() * 100
    p_gt30 = (dd_pct < -30).mean() * 100
    ax2.set_title("Max drawdown distribution", fontweight="bold")
    ax2.set_xlabel("Max drawdown (%)")
    ax2.set_ylabel("Simulations")
    ax2.xaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    ax2.legend(framealpha=0.9)
    ax2.spines[["top", "right"]].set_visible(False)

    # Drawdown risk text
    ax2.text(0.98, 0.97, f"P(DD > 20%): {p_gt20:.1f}%\nP(DD > 30%): {p_gt30:.1f}%",
             transform=ax2.transAxes, ha="right", va="top",
             fontsize=10, bbox=dict(boxstyle="round,pad=0.4", facecolor="white", alpha=0.8))

    fig.tight_layout()
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"Saved → {out}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("file", nargs="?", help="Backtest JSON result file")
    parser.add_argument("--n",       type=int, default=1000, help="Number of simulations (default 1000)")
    parser.add_argument("--capital", type=float, default=None, help="Override starting capital")
    parser.add_argument("--out",     default="montecarlo.png", help="Output PNG path")
    args = parser.parse_args()

    if args.file:
        path = args.file
    else:
        files = sorted(glob.glob("results/backtest_*.json"))
        if not files:
            sys.exit("No result files found in results/")
        path = files[-1]
        print(f"Using latest: {os.path.basename(path)}")

    pnls, capital = load_pnls(path)
    if args.capital:
        capital = args.capital

    print(f"  {len(pnls)} trades  |  ${capital:,.0f} starting capital  |  {args.n} sims")

    actual = build_equity(pnls, capital)
    sims   = run_montecarlo(pnls, capital, args.n)
    plot(sims, actual, capital, args.out)

    # Summary stats
    finals = sims[:, -1]
    sim_dds = np.array([max_drawdown(sims[i]) for i in range(sims.shape[0])]) * 100
    print(f"\n  Actual final:     ${actual[-1]:>10,.0f}  ({(actual[-1]/capital - 1)*100:.1f}%)")
    print(f"  Median sim:       ${np.median(finals):>10,.0f}  ({(np.median(finals)/capital - 1)*100:.1f}%)")
    print(f"  5th pct final:    ${np.percentile(finals, 5):>10,.0f}")
    print(f"  95th pct final:   ${np.percentile(finals, 95):>10,.0f}")
    print(f"\n  Actual max DD:    {max_drawdown(actual)*100:.1f}%")
    print(f"  Median sim DD:    {np.median(sim_dds):.1f}%")
    print(f"  P(DD > 20%):      {(sim_dds < -20).mean()*100:.1f}%")
    print(f"  P(DD > 30%):      {(sim_dds < -30).mean()*100:.1f}%")
    print(f"\n  Note: P(loss on final equity) is misleading for bootstrapped returns —")
    print(f"  use drawdown metrics above for realistic risk estimates.")


if __name__ == "__main__":
    main()
