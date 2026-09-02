"""Record Deribit BTC option implied vol, so it can be TESTED against our own.

WHY. DistModel.true_prob prices every Kalshi band off one number: EWMA realized
vol from our own feed, scaled sqrt(BARS_PER_HOUR) and then sqrt(hours). Nothing
external validates that estimate, and the sqrt-of-time step assumes IID returns
that the regime engine's own existence denies.

Deribit publishes a live option surface. Whether its IV is a better forward vol
estimate than our EWMA is an empirical question — and one we currently CANNOT
answer, because there is no history: `recordings/walls_*` holds a single 4 KB
day from 2026-08-06, and it is open-interest walls, not IV.

So this records. It does not price, does not trade, and is not imported by the
bot. Wiring an unvalidated vol input into the live pricer is the failure mode
docs/BACKTEST_INTEGRITY.md catalogues nine times; the fix is weeks of data
first, comparison second, wiring third.

WHAT IT WRITES. recordings/deribit_iv_YYYY-MM-DD.jsonl.gz, UTC-dated to match
every other stream (see the note in feedback: recordings are UTC, logs are
local). One row per poll:

    t          ISO-8601 UTC
    spot       Deribit underlying_price, front expiry (forward-basis minimal)
    n          instruments in the snapshot
    exp        per-expiry [{e, h, atm_iv, k, n}]  hours, ATM IV %, strike, count
    iv1h       ATM IV interpolated to a 1-hour horizon, or null

ATM IV per expiry is the open-interest-weighted mark_iv of the strikes nearest
the forward, which is more robust than picking a single nearest strike when the
grid is coarse. Interpolation to 1h is linear in TOTAL VARIANCE (iv^2 * t), not
in IV — variance is what adds across time; interpolating IV directly
systematically misprices the short end, which is the only end we trade.

Usage:
    python3 deribit_iv_recorder.py                  # poll forever, 60s
    python3 deribit_iv_recorder.py --once           # single snapshot, print it
    python3 deribit_iv_recorder.py --interval 30
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import math
import os
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

URL = ("https://www.deribit.com/api/v2/public/"
       "get_book_summary_by_currency?currency=BTC&kind=option")
REC_DIR = Path(__file__).parent / "recordings"
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def fetch(timeout: int = 45, retries: int = 3) -> list:
    """Deribit returns ~1 MB here and a truncated read is common through a
    proxy, so a partial body is retried rather than treated as an outage."""
    last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(URL, timeout=timeout) as fh:
                return json.load(fh)["result"]
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def parse_expiry(name: str) -> dt.datetime | None:
    """BTC-26SEP26-90000-C -> expiry datetime. Deribit expires at 08:00 UTC."""
    try:
        _, d, _strike, _cp = name.split("-")
        day = int(d[:-5]) if len(d) == 7 else int(d[:-5])
        mon = _MONTHS[d[-5:-2]]
        yr = 2000 + int(d[-2:])
        return dt.datetime(yr, mon, day, 8, 0, tzinfo=dt.timezone.utc)
    except Exception:
        return None


def strike_of(name: str) -> float | None:
    try:
        return float(name.split("-")[2])
    except Exception:
        return None


def snapshot(raw: list, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    by_exp: dict[dt.datetime, list] = defaultdict(list)
    for s in raw:
        iv = s.get("mark_iv")
        name = s.get("instrument_name") or ""
        if not iv or iv <= 0:
            continue
        e = parse_expiry(name)
        k = strike_of(name)
        u = s.get("underlying_price")
        if e is None or k is None or not u:
            continue
        if e <= now:
            continue
        by_exp[e].append((k, float(iv), float(u), float(s.get("open_interest") or 0.0)))

    rows = []
    for e in sorted(by_exp):
        legs = by_exp[e]
        fwd = sum(x[2] for x in legs) / len(legs)
        # ATM = the nearest strike level; both C and P at that strike carry the
        # same IV in a consistent surface, and averaging them is standard.
        nearest = min(abs(x[0] - fwd) for x in legs)
        atm = [x for x in legs if abs(abs(x[0] - fwd) - nearest) < 1e-9]
        wsum = sum(x[3] for x in atm)
        iv = (sum(x[1] * x[3] for x in atm) / wsum if wsum > 0
              else sum(x[1] for x in atm) / len(atm))
        rows.append({
            "e": e.isoformat(),
            "h": round((e - now).total_seconds() / 3600.0, 4),
            "atm_iv": round(iv, 4),
            "k": atm[0][0],
            "n": len(legs),
            "fwd": round(fwd, 2),
        })

    return {
        "t": now.isoformat(),
        "spot": rows[0]["fwd"] if rows else None,
        "n": len(raw),
        "exp": rows,
        "iv1h": interp_iv(rows, 1.0),
    }


def interp_iv(rows: list, hours: float) -> float | None:
    """ATM IV at `hours`, interpolated in TOTAL VARIANCE.

    Variance is additive in time; IV is not. Interpolating IV linearly across
    expiries overstates the short end whenever the term structure slopes, and
    the short end is the only part a 1-hour Kalshi contract cares about. Below
    the front expiry this necessarily extrapolates flat in variance — recorded
    so the assumption is visible rather than buried.
    """
    pts = [(r["h"], r["atm_iv"]) for r in rows if r["h"] > 0 and r["atm_iv"] > 0]
    if not pts:
        return None
    pts.sort()
    if hours <= pts[0][0]:
        return round(pts[0][1], 4)
    for (h0, v0), (h1, v1) in zip(pts, pts[1:]):
        if h0 <= hours <= h1:
            w0, w1 = v0 * v0 * h0, v1 * v1 * h1
            tv = w0 + (w1 - w0) * ((hours - h0) / (h1 - h0)) if h1 > h0 else w0
            return round(math.sqrt(max(0.0, tv / hours)), 4)
    return round(pts[-1][1], 4)


def write(row: dict) -> Path:
    REC_DIR.mkdir(exist_ok=True)
    # UTC-dated, like every other stream in recordings/.
    day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    path = REC_DIR / f"deribit_iv_{day}.jsonl.gz"
    with gzip.open(path, "at") as fh:
        fh.write(json.dumps(row) + "\n")
    return path


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    if args.interval < 5:
        ap.error("--interval must be >= 5s; Deribit rate-limits public endpoints")

    print(f"  Deribit IV recorder -> recordings/deribit_iv_<UTC date>.jsonl.gz")
    print(f"  every {args.interval:.0f}s   (records only; the bot does not read this)")
    n = 0
    while True:
        try:
            row = snapshot(fetch())
            p = write(row)
            n += 1
            front = row["exp"][0] if row["exp"] else {}
            print(f"  [{row['t'][11:19]}Z] spot=${row['spot'] or 0:,.0f} "
                  f"front={front.get('h', 0):.2f}h iv={front.get('atm_iv', 0):.1f}% "
                  f"iv1h={row['iv1h']} expiries={len(row['exp'])} "
                  f"rows={n} -> {p.name}", flush=True)
        except Exception as e:
            print(f"  ⚠️  {type(e).__name__}: {e}", flush=True)
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
