"""
Live P&L / equity monitor for the Kalshi BTC bot.

Reads trades.csv (written by Portfolio._log_trade) and renders an equity
curve, underwater drawdown plot, exit-tier breakdown, and a live-vs-backtest
comparison of per-tier win rates.

The last panel is the one that matters: the backtest's edge is concentrated in
momentum_locked (100% win rate) outrunning stop_loss. If that tier degrades
live, it shows up there first.

Usage:
    python3 live_pnl.py                                  # live trades, auto capital
    python3 live_pnl.py --mode paper                     # paper trades instead
    python3 live_pnl.py --since 2026-07-25               # only trades from a date
    python3 live_pnl.py --capital 44.39                  # set starting equity
    python3 live_pnl.py --watch 60                       # re-render every 60s
"""

import argparse
import csv
import datetime
import glob
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

# Match montecarlo.py's styling so the two charts read as one set.
plt.rcParams.update({
    "figure.facecolor": "#f8f8f8",
    "axes.facecolor":   "#ffffff",
    "axes.edgecolor":   "#cccccc",
    "axes.grid":        True,
    "grid.color":       "#e5e5e5",
    "grid.linewidth":   0.8,
    "font.family":      "DejaVu Sans",
    "font.size":        11,
    "axes.titlesize":   13,
    "axes.labelsize":   11,
    "legend.fontsize":  10,
    "xtick.color":      "#555555",
    "ytick.color":      "#555555",
    "axes.labelcolor":  "#333333",
    "axes.titlepad":    12,
})

EQUITY_COLOR = "#4C72B0"
LOSS_COLOR   = "#C44E52"
WIN_COLOR    = "#2ca02c"
NEUTRAL      = "#999999"

TRADES_CSV = Path(__file__).parent / "trades.csv"


def normalize_reason(reason: str) -> str:
    """Collapse a live exit reason to the backtest's tier name.

    Live reasons carry emoji and a formatted threshold ("stop_35% ", "gamma_lock
    📐"); the backtest records bare tier names and folds every stop into
    "stop_loss". Normalizing both sides is what makes the comparison panel
    meaningful rather than a list of near-duplicate labels.
    """
    r = (reason or "").strip()
    if not r:
        return "(none)"
    head = r.split()[0]
    if head.startswith("stop_"):
        return "stop_loss"
    if head in ("scalp_lock", "scalp_reversal"):
        return "scalp_reversal"
    return head


def load_trades(mode: str, since: str | None) -> list[dict]:
    if not TRADES_CSV.exists():
        sys.exit(f"No trade log at {TRADES_CSV}")
    with open(TRADES_CSV, newline="") as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        if mode != "all" and (r.get("mode") or "") != mode:
            continue
        ts = r.get("timestamp") or ""
        if since and ts[:10] < since:
            continue
        out.append(r)
    return out


def build_series(trades: list[dict], capital: float):
    """Realized equity curve from closing trades. Buys carry no pnl."""
    times, equity, pnls, reasons = [], [], [], []
    eq = capital
    for r in trades:
        if r.get("action") != "sell":
            continue
        raw = (r.get("pnl") or "").strip()
        if not raw:
            continue
        try:
            p = float(raw)
        except ValueError:
            continue
        eq += p
        try:
            t = datetime.datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S")
        except (ValueError, KeyError):
            t = times[-1] if times else datetime.datetime.now()
        times.append(t)
        equity.append(eq)
        pnls.append(p)
        reasons.append(normalize_reason(r.get("reason", "")))
    return times, np.array(equity), np.array(pnls), reasons


def drawdown(equity: np.ndarray, capital: float) -> np.ndarray:
    if equity.size == 0:
        return np.array([])
    curve = np.concatenate([[capital], equity])
    peak = np.maximum.accumulate(curve)
    return (curve - peak) / peak * 100


def backtest_tiers() -> dict:
    files = sorted(glob.glob(str(Path(__file__).parent / "results" / "backtest_*.json")))
    if not files:
        return {}
    try:
        with open(files[-1]) as f:
            return json.load(f).get("metrics", {}).get("by_exit_reason", {}) or {}
    except (OSError, ValueError):
        return {}


def render(trades, capital, mode, out):
    times, equity, pnls, reasons = build_series(trades, capital)
    n_buys = sum(1 for r in trades if r.get("action") == "buy")
    open_pos = n_buys - len(pnls)

    fig, axes = plt.subplots(2, 2, figsize=(16, 10))
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Plain text, not emoji — DejaVu Sans has no glyph for these and matplotlib
    # emits a UserWarning per render, which is noisy under --watch.
    tag = {"live": "LIVE", "paper": "PAPER"}.get(mode, "ALL MODES")
    fig.suptitle(f"Kalshi BTC bot — {tag}   |   {len(pnls)} closed, "
                 f"{max(0, open_pos)} open   |   {stamp}",
                 fontsize=14, fontweight="bold")

    # ── Equity curve ────────────────────────────────────────────────────────
    ax = axes[0][0]
    if len(pnls):
        x = np.arange(1, len(equity) + 1)
        ax.plot(x, equity, color=EQUITY_COLOR, linewidth=2.0)
        ax.fill_between(x, capital, equity, where=equity >= capital,
                        alpha=0.18, color=WIN_COLOR, interpolate=True)
        ax.fill_between(x, capital, equity, where=equity < capital,
                        alpha=0.18, color=LOSS_COLOR, interpolate=True)
        final = equity[-1]
        ax.annotate(f"${final:,.2f}\n{(final/capital-1)*100:+.1f}%",
                    xy=(len(equity), final), xytext=(-70, 6),
                    textcoords="offset points", fontweight="bold",
                    color=WIN_COLOR if final >= capital else LOSS_COLOR)
    else:
        ax.text(0.5, 0.5, "no closed trades yet", ha="center", va="center",
                transform=ax.transAxes, color=NEUTRAL, fontsize=12)
    ax.axhline(capital, color=NEUTRAL, linewidth=0.9, linestyle=":")
    ax.set_title("Realized equity", fontweight="bold")
    ax.set_xlabel("Closed trade #")
    ax.set_ylabel("Equity ($)")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"${v:,.2f}"))
    ax.spines[["top", "right"]].set_visible(False)

    # ── Underwater ──────────────────────────────────────────────────────────
    ax = axes[0][1]
    dd = drawdown(equity, capital)
    if dd.size:
        x = np.arange(dd.size)
        ax.fill_between(x, dd, 0, color=LOSS_COLOR, alpha=0.35)
        ax.plot(x, dd, color=LOSS_COLOR, linewidth=1.4)
        worst = dd.min()
        ax.axhline(worst, color=LOSS_COLOR, linestyle="--", linewidth=1.2,
                   label=f"max DD {worst:.1f}%")
        # Monte Carlo reference levels from the 10k-path bootstrap.
        for lvl, lbl in ((-18, "MC median -18%"), (-36, "MC p95 -36%")):
            if worst < lvl + 8:
                ax.axhline(lvl, color=NEUTRAL, linestyle=":", linewidth=1.0, label=lbl)
        ax.legend(loc="lower left", framealpha=0.9)
    else:
        ax.text(0.5, 0.5, "no closed trades yet", ha="center", va="center",
                transform=ax.transAxes, color=NEUTRAL, fontsize=12)
    ax.set_title("Drawdown (underwater)", fontweight="bold")
    ax.set_xlabel("Closed trade #")
    ax.set_ylabel("Drawdown (%)")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    ax.spines[["top", "right"]].set_visible(False)

    # ── P&L by exit tier ────────────────────────────────────────────────────
    ax = axes[1][0]
    tiers = {}
    for p, rs in zip(pnls, reasons):
        d = tiers.setdefault(rs, {"pnl": 0.0, "n": 0, "wins": 0})
        d["pnl"] += p
        d["n"] += 1
        d["wins"] += 1 if p > 0 else 0
    if tiers:
        order = sorted(tiers, key=lambda k: tiers[k]["pnl"])
        vals = [tiers[k]["pnl"] for k in order]
        cols = [WIN_COLOR if v >= 0 else LOSS_COLOR for v in vals]
        ax.barh(range(len(order)), vals, color=cols, alpha=0.85)
        ax.set_yticks(range(len(order)))
        ax.set_yticklabels([f"{k}  (n={tiers[k]['n']})" for k in order], fontsize=9)
        ax.axvline(0, color="#333333", linewidth=0.9)
    else:
        ax.text(0.5, 0.5, "no closed trades yet", ha="center", va="center",
                transform=ax.transAxes, color=NEUTRAL, fontsize=12)
    ax.set_title("P&L by exit tier", fontweight="bold")
    ax.set_xlabel("P&L ($)")
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"${v:,.0f}"))
    ax.spines[["top", "right"]].set_visible(False)

    # ── Live vs backtest win rate ───────────────────────────────────────────
    ax = axes[1][1]
    bt = backtest_tiers()
    shared = [k for k in tiers if k in bt]
    if shared:
        shared.sort(key=lambda k: -tiers[k]["n"])
        idx = np.arange(len(shared))
        live_wr = [tiers[k]["wins"] / tiers[k]["n"] * 100 for k in shared]
        bt_wr   = [bt[k].get("win_rate", 0) for k in shared]
        ax.barh(idx + 0.2, bt_wr,   height=0.38, color=NEUTRAL,      alpha=0.7, label="backtest")
        ax.barh(idx - 0.2, live_wr, height=0.38, color=EQUITY_COLOR, alpha=0.9, label="live")
        ax.set_yticks(idx)
        ax.set_yticklabels([f"{k}  (n={tiers[k]['n']})" for k in shared], fontsize=9)
        ax.set_xlim(0, 100)
        ax.legend(loc="lower right", framealpha=0.9)
        ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    else:
        ax.text(0.5, 0.5, "no overlapping tiers yet\n(need closed trades)",
                ha="center", va="center", transform=ax.transAxes,
                color=NEUTRAL, fontsize=12)
    ax.set_title("Win rate — live vs backtest", fontweight="bold")
    ax.set_xlabel("Win rate")
    ax.spines[["top", "right"]].set_visible(False)

    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)

    wins = int((pnls > 0).sum()) if len(pnls) else 0
    gross_w = float(pnls[pnls > 0].sum()) if len(pnls) else 0.0
    gross_l = float(-pnls[pnls < 0].sum()) if len(pnls) else 0.0
    print(f"\n  {tag}  |  {stamp}")
    print(f"  closed {len(pnls)}   open {max(0, open_pos)}   "
          f"win rate {(wins/len(pnls)*100 if len(pnls) else 0):.1f}%")
    print(f"  equity ${equity[-1] if len(equity) else capital:,.2f} "
          f"from ${capital:,.2f}   realized ${float(pnls.sum()) if len(pnls) else 0:+,.2f}")
    print(f"  profit factor {(gross_w/gross_l if gross_l else float('inf')):.2f}   "
          f"max DD {dd.min() if dd.size else 0:.1f}%")
    print(f"  → {out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="live", choices=["live", "paper", "all"])
    ap.add_argument("--since", help="only trades on/after this date (YYYY-MM-DD)")
    ap.add_argument("--capital", type=float, help="starting equity (default: infer)")
    ap.add_argument("--out", default="live_pnl.png")
    ap.add_argument("--watch", type=int, metavar="SECS",
                    help="re-render every SECS seconds until interrupted")
    args = ap.parse_args()

    while True:
        trades = load_trades(args.mode, args.since)
        capital = args.capital
        if capital is None:
            # Without an explicit figure, anchor at the total cost of the first
            # position so the curve starts somewhere meaningful rather than 0.
            capital = 100.0
            for r in trades:
                if r.get("action") == "buy":
                    try:
                        capital = max(100.0, float(r["price"]) * float(r["count"]) * 20)
                    except (KeyError, ValueError):
                        pass
                    break
        if not trades:
            print(f"  no {args.mode} trades in {TRADES_CSV.name}"
                  + (f" since {args.since}" if args.since else ""))
        render(trades, capital, args.mode, args.out)
        if not args.watch:
            break
        try:
            time.sleep(args.watch)
        except KeyboardInterrupt:
            print("\n  stopped")
            break


if __name__ == "__main__":
    main()
