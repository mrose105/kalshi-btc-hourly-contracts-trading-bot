"""
Is DistModel.true_prob calibrated — and is any miscalibration width-dependent?

Motivation: after fixing RANGE_WIDTH 250 -> 100 (docs/QUANT_STANDARDS_AUDIT.md
sec 1d) the corrected backtest turned deeply unprofitable, and the diagnostic
that stood out was that edge at entry is ANTI-predictive: losers carried more
model edge (0.0573) than winners (0.0357). That is the signature of a
miscalibrated probability model, not of bad exit tiers.

This script tests calibration directly and independently of any trading logic:
walk the real BTC 5m series, and at each sampled bar build the contract ladder
exactly as the backtest does, record each contract's predicted true_prob, then
look forward to the contract's actual expiry and record whether spot really
landed inside the band. Bucket by predicted probability and compare predicted
vs realized frequency.

Reads a forward outcome, which is lookahead — deliberately, and safely: this
measures the model, it never places a trade. Nothing here feeds the backtest.

Runs the same test at multiple band widths, because that is the open question:
if the model is well calibrated at 250 but overconfident at 100, its
parameters were implicitly fitted to the wrong instrument and need refitting.
If it is overconfident at both, the vol-edge thesis itself is the problem.

Usage:
    python3 calibration_check.py                       # widths 100 and 250
    python3 calibration_check.py --widths 100 --days 59
"""
import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import kalshi_btc_backtest as B
from kalshi_btc_bot import config as C
from kalshi_btc_bot.model import DistModel
from kalshi_btc_bot.regime import RegimeEngine


def run(days: int, width: int, sample_every: int, max_hours: float) -> list:
    """Return [(predicted_prob, settled_itm)] for one band width."""
    bars = B.fetch_bars(days=days) if hasattr(B, "fetch_bars") else None
    if bars is None:
        import yfinance as yf
        df = yf.download("BTC-USD", period=f"{days}d", interval="5m",
                         progress=False, auto_adjust=False)
        if df.empty:
            raise SystemExit("no data")
        if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            df.columns = df.columns.droplevel(1)
        bars = [(ts.to_pydatetime(), float(r["Close"]), float(r["High"]),
                 float(r["Low"]), float(r["Open"])) for ts, r in df.iterrows()]

    # spot at each timestamp, for resolving settlement
    closes = {ts: c for ts, c, _h, _l, _o in bars}
    opens = {ts: o for ts, _c, _h, _l, o in bars}
    times = [b[0] for b in bars]

    feed = B.SyntheticFeed()
    regime_e = RegimeEngine()
    dist = DistModel()
    scale = math.sqrt(B.TIME_SCALE)

    warm = min(B.SMA_VOL_WINDOW, len(bars) // 3)
    for ts, c, h, l, _o in bars[:warm]:
        feed.push(ts, c, h, l)

    orig_width, orig_span = B.RANGE_WIDTH, B.RANGE_SPAN
    B.RANGE_WIDTH = width          # build_ladder reads these at call time
    out = []
    try:
        for i, (ts, c, h, l, _o) in enumerate(bars[warm:]):
            feed.push(ts, c, h, l)
            if i % sample_every:
                continue
            regime = regime_e.detect(feed)
            regime_bt = {**regime, "vol": regime["vol"] / scale}
            kalshi_vol = feed.sma_volatility(B.SMA_VOL_WINDOW) / scale
            for con in B.build_ladder(c, ts, dist, regime_bt, kalshi_vol):
                if con["hours"] > max_hours:
                    continue
                # resolve at expiry: use the bar OPEN nearest the expiry stamp,
                # matching how the backtest settles (bar_open, not bar_close)
                exp = ts + __import__("datetime").timedelta(hours=con["hours"])
                nxt = [t for t in times if t >= exp]
                if not nxt:
                    continue
                settle = opens.get(nxt[0], closes.get(nxt[0]))
                if settle is None:
                    continue
                p = dist.true_prob(con, c, regime_bt["vol"], con["hours"], regime_bt)
                if p <= 0:
                    continue
                out.append((p, 1 if con["low"] <= settle < con["high"] else 0))
    finally:
        B.RANGE_WIDTH, B.RANGE_SPAN = orig_width, orig_span
    return out


def report(rows: list, label: str) -> None:
    print(f"\n=== {label} ===   n={len(rows):,}")
    if not rows:
        return
    buckets = [(0.0, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 0.20),
               (0.20, 0.30), (0.30, 0.45), (0.45, 1.01)]
    print(f"  {'predicted':>14} {'n':>7} {'mean pred':>10} {'realized':>10} {'ratio':>8}")
    tp = tr = 0.0
    for lo, hi in buckets:
        g = [r for r in rows if lo <= r[0] < hi]
        if not g:
            continue
        mp = sum(p for p, _ in g) / len(g)
        mr = sum(o for _, o in g) / len(g)
        tp += sum(p for p, _ in g)
        tr += sum(o for _, o in g)
        flag = ""
        if mr > 0 and mp / mr > 1.25:
            flag = "  <-- model TOO HIGH"
        elif mr > 0 and mp / mr < 0.8:
            flag = "  <-- model too low"
        elif mr == 0:
            flag = "  <-- never settled ITM"
        print(f"  {lo:.2f}-{hi:<8.2f} {len(g):>7,} {mp:>10.4f} {mr:>10.4f} "
              f"{(mp/mr if mr else float('inf')):>8.2f}{flag}")
    print(f"  {'OVERALL':>14} {len(rows):>7,} {tp/len(rows):>10.4f} {tr/len(rows):>10.4f} "
          f"{(tp/tr if tr else float('inf')):>8.2f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=59)
    ap.add_argument("--widths", default="100,250")
    ap.add_argument("--sample-every", type=int, default=6, help="bars between samples")
    ap.add_argument("--max-hours", type=float, default=1.05)
    args = ap.parse_args()

    print("DistModel.true_prob calibration — predicted vs realized settlement")
    print(f"  {args.days}d of BTC 5m, sampling every {args.sample_every} bars, "
          f"contracts <= {args.max_hours}h to expiry")
    for w in [int(x) for x in args.widths.split(",")]:
        report(run(args.days, w, args.sample_every, args.max_hours),
               f"RANGE_WIDTH = {w}" + ("  (real market)" if w == 100 else "  (old buggy value)"))
    print("\n  ratio > 1 means the model predicts a HIGHER chance than actually occurs")
    print("  (overconfident -> we overpay -> more 'edge' on worse contracts).")


if __name__ == "__main__":
    main()
