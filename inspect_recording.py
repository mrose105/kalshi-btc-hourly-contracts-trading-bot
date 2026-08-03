"""
Inspect market-data recordings written by kalshi_btc_bot/recorder.py.

Usage:
    python3 inspect_recording.py                       # today, all streams, summary
    python3 inspect_recording.py --date 2026-07-28      # specific UTC day
    python3 inspect_recording.py --stream marks         # one stream only
    python3 inspect_recording.py --ticker KXBTC-...-B63550   # filter to one contract
    python3 inspect_recording.py --tail 20              # last N raw records
    python3 inspect_recording.py --gaps                 # find recording gaps (dropped/stalled)
"""
import argparse
import datetime
import glob
import gzip
import json
import statistics
import sys
from pathlib import Path

_DIR = Path(__file__).parent / "recordings"
STREAMS = ["quotes", "marks", "books", "orders"]


def load(stream: str, date: str) -> list[dict]:
    """Read one recorder stream, tolerating a file still being written.

    gzip only writes its footer (CRC + size) on close(), and recorder.py holds
    its handles open for the whole session — see recorder.py's _handle(). So
    reading today's file while the bot is still running always hits an
    unterminated gzip member and raises EOFError on the read *after* the last
    complete line, even though every line up to that point decoded fine. That
    is expected, not corruption: read line-by-line and stop cleanly at the
    truncation instead of losing every row already read.
    """
    path = _DIR / f"{stream}_{date}.jsonl.gz"
    if not path.exists():
        return []
    rows = []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        while True:
            try:
                line = f.readline()
            except EOFError:
                break  # in-progress file — stream not finalized yet
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a partial last line from a killed process
    return rows


def available_dates() -> list[str]:
    dates = set()
    for p in glob.glob(str(_DIR / "*.jsonl.gz")):
        name = Path(p).stem.replace(".jsonl", "")
        parts = name.rsplit("_", 1)
        if len(parts) == 2:
            dates.add(parts[1])
    return sorted(dates)


def parse_t(rec: dict) -> datetime.datetime:
    return datetime.datetime.fromisoformat(rec["t"])


def cmd_summary(date: str) -> None:
    print(f"=== {date} ===\n")
    total_bytes = 0
    for stream in STREAMS:
        path = _DIR / f"{stream}_{date}.jsonl.gz"
        rows = load(stream, date)
        size = path.stat().st_size if path.exists() else 0
        total_bytes += size
        if not rows:
            print(f"  {stream:<8} 0 records")
            continue
        t0, t1 = parse_t(rows[0]), parse_t(rows[-1])
        span_min = (t1 - t0).total_seconds() / 60
        rate = len(rows) / span_min if span_min > 0 else 0
        tickers = {r.get("tk") for r in rows if "tk" in r}
        extra = f", {len(tickers)} tickers" if tickers else ""
        print(f"  {stream:<8} {len(rows):>6} records  {t0.strftime('%H:%M:%S')}"
              f" -> {t1.strftime('%H:%M:%S')}  ({span_min:.1f} min, {rate:.1f}/min){extra}"
              f"  [{size/1024:.0f} KB]")
    print(f"\n  total on disk: {total_bytes/1024/1024:.2f} MB")

    orders = load("orders", date)
    if orders:
        buys = [o for o in orders if o["ev"] == "buy"]
        sells = [o for o in orders if o["ev"] == "sell"]
        no_fill = [o for o in orders if o["fill"] == 0]
        print(f"\n  orders: {len(buys)} buy, {len(sells)} sell, {len(no_fill)} zero-fill")
        slips = []
        for o in orders:
            if o["fill"] > 0 and o.get("lim"):
                slip = (o["px"] - o["lim"]) if o["ev"] == "buy" else (o["lim"] - o["px"])
                slips.append(slip)
        if slips:
            print(f"  realized slippage vs limit: mean {statistics.mean(slips):+.4f}  "
                  f"median {statistics.median(slips):+.4f}  "
                  f"worst {max(slips, key=abs):+.4f}")


def cmd_gaps(date: str, stream: str) -> None:
    rows = load(stream, date)
    if len(rows) < 2:
        print(f"not enough {stream} records on {date} to find gaps")
        return
    times = [parse_t(r) for r in rows]
    diffs = [(times[i] - times[i-1]).total_seconds() for i in range(1, len(times))]
    med = statistics.median(diffs)
    gaps = [(times[i-1], times[i], diffs[i-1]) for i in range(1, len(diffs)+1)
            if diffs[i-1] > max(med * 5, med + 30)]
    print(f"{stream} on {date}: {len(rows)} records, median interval {med:.1f}s")
    if not gaps:
        print("  no gaps found (nothing > 5x median or +30s over median)")
        return
    print(f"  {len(gaps)} gap(s):")
    for t0, t1, secs in gaps:
        print(f"    {t0.strftime('%H:%M:%S')} -> {t1.strftime('%H:%M:%S')}  "
              f"({secs/60:.1f} min silent — restart, network stall, or queue drop)")


def cmd_stream(stream: str, date: str, ticker: str | None, tail: int | None) -> None:
    rows = load(stream, date)
    if ticker:
        rows = [r for r in rows if r.get("tk") == ticker]
    if tail:
        rows = rows[-tail:]
    if not rows:
        print("no matching records")
        return
    for r in rows:
        print(json.dumps(r, separators=(",", ":")))


def cmd_marks_for(ticker: str, date: str) -> None:
    """Full mark history for one contract — the series a replay would use."""
    rows = [r for r in load("marks", date) if r.get("tk") == ticker]
    if not rows:
        print(f"no marks for {ticker} on {date}")
        return
    entry = rows[0].get("entry")
    print(f"{ticker}  entry=${entry}  {len(rows)} marks\n")
    print(f"{'time':<10}{'bid':>7}{'ask':>7}{'mins left':>10}{'pnl%':>8}{'true_p':>8}")
    for r in rows:
        t = parse_t(r)
        mins = (r.get("h") or 0) * 60
        pnl = ((r["b"] - entry) / entry * 100) if entry else 0
        print(f"{t.strftime('%H:%M:%S'):<10}{r['b']:>7.3f}{r['a']:>7.3f}"
              f"{mins:>10.1f}{pnl:>+7.0f}%{(r.get('tp') or 0):>8.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="UTC date (YYYY-MM-DD), default: latest available")
    ap.add_argument("--stream", choices=STREAMS, help="show one stream's raw records")
    ap.add_argument("--ticker", help="filter to one contract")
    ap.add_argument("--tail", type=int, help="show only the last N records")
    ap.add_argument("--gaps", action="store_true", help="find recording gaps")
    ap.add_argument("--marks-for", metavar="TICKER",
                    help="print the full mark history for one contract")
    args = ap.parse_args()

    dates = available_dates()
    if not dates:
        sys.exit(f"no recordings found in {_DIR}/ — run with KALSHI_RECORD=1 first")
    date = args.date or dates[-1]
    if date not in dates:
        sys.exit(f"no data for {date}. Available: {', '.join(dates)}")

    if args.marks_for:
        cmd_marks_for(args.marks_for, date)
    elif args.gaps:
        for s in ([args.stream] if args.stream else STREAMS):
            cmd_gaps(date, s)
            print()
    elif args.stream:
        cmd_stream(args.stream, date, args.ticker, args.tail)
    else:
        cmd_summary(date)


if __name__ == "__main__":
    main()
