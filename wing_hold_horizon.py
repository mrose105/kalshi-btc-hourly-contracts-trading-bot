"""How long should the YES wing be held?

The wing is bought at the ASK and currently managed by the YES exit ladder —
gamma_lock, boundary_risk, scalp_lock, time_exit_OTM — tiers designed for a
different strategy. Live on 2026-09-01 two of three wings exited on those tiers
inside four minutes for +$0.58 and +$2.52; the third held six minutes and lost
$3.52. n=3 decides nothing, so this measures the whole value path.

METHOD. For every OCCUPIED band (spot inside it) at a BOUNDARY_NO-qualifying
moment, take the real YES ask as the entry, then follow that contract forward in
the universe stream and mark it at each horizon by the real BID — what selling
would actually have received. Settlement resolves as-of close from the quotes
stream. Fees charged on entry always; on the early exits a SECOND taker fee is
charged, because settlement is free and an early exit is not (fees.py).

    P&L(t)   = bid(t) - ask(0) - fee(ask) - fee(bid)
    P&L(exp) = payout - ask(0) - fee(ask)          payout in {0, 1}

An entry is only counted at a horizon where a real quote exists, so the columns
are directly comparable on the SAME contracts (the intersection is reported).

Usage:
    python3 wing_hold_horizon.py --start 2026-08-12 --end 2026-09-01
"""
from __future__ import annotations
import argparse, bisect, datetime as dt, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from kalshi_btc_bot import config as C
from kalshi_btc_bot.fees import taker_fee
from boundary_no_quote_replay import join_regimes, normalize_universe, tolerant_jsonl_gz
from wing_calibration import (MIN_CLUSTERS, percentile_bootstrap_interval,
                              daterange, spot_at, spot_series)

HORIZONS = (2, 5, 10, 15, 20, 30)      # minutes held


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-08-12")
    ap.add_argument("--end", default="2026-09-01")
    ap.add_argument("--every", type=int, default=8)
    ap.add_argument("--max-ask", type=float, default=C.MAX_ASK)
    a = ap.parse_args()
    if a.every < 1:
        ap.error("--every must be >= 1")

    uni = daterange("universe", a.start, a.end)
    qs = daterange("quotes", a.start, a.end)
    if not uni:
        raise SystemExit("no universe recordings")
    s_ts, s_sp = spot_series(qs)
    print(f"  {len(s_ts):,} spot samples / {len(qs)} quote days", flush=True)

    results = defaultdict(list)     # horizon (or 'exp') -> [(pnl, expiry, key)]
    byday = {Path(p).stem.split("_")[1][:10]: p for p in qs}
    for up in uni:
        day = Path(up).stem.split("_")[1][:10]
        if day not in byday:
            continue
        u = tolerant_jsonl_gz(up); u.sort(key=lambda r: r.get("t", ""))
        q = tolerant_jsonl_gz(byday[day]); q.sort(key=lambda r: r.get("t", ""))
        ticks = join_regimes(u, q, tolerance_secs=5)
        print(f"    {day} {len(ticks):,}", flush=True)

        # ticker -> sorted [(epoch, bid)] for forward marking, built once per day
        path = defaultdict(list)
        stamps = []
        for row in ticks:
            try:
                e = dt.datetime.fromisoformat(row["t"]).timestamp()
            except Exception:
                continue
            stamps.append(e)
            for m in row.get("m") or []:
                b = m.get("b")
                if b is not None:
                    path[m["tk"]].append((e, float(b)))
        for tk in path:
            path[tk].sort()

        seen = set()
        for i, row in enumerate(ticks):
            if i % a.every:
                continue
            rg = row.get("rg") or {}
            spot, vol = row.get("spot"), rg.get("v")
            if spot is None or not vol:
                continue
            if rg.get("r") not in ("RANGING", "REVERTING"):
                continue
            if abs(rg.get("z") or 0.0) < C.BOUNDARY_NO_ZSCORE_MIN:
                continue
            try:
                now = dt.datetime.fromisoformat(row["t"])
            except Exception:
                continue
            e0 = now.timestamp()
            for c in normalize_universe(row, now):
                lo, hi = float(c["low"]), float(c["high"])
                if not (lo <= float(spot) < hi):        # OCCUPIED band only
                    continue
                ask = float(c["ask"])
                if ask <= 0 or ask > a.max_ask:
                    continue
                key = (c["ticker"], int(e0) // 300)     # one entry per 5 min
                if key in seen:
                    continue
                seen.add(key)
                try:
                    close = dt.datetime.fromisoformat(
                        str(c["close_time"]).replace("Z", "+00:00")).timestamp()
                except Exception:
                    continue
                ss = spot_at(s_ts, s_sp, close, 120.0)
                if ss is None:
                    continue
                exp = c["ticker"].rsplit("-", 1)[0]
                entry_fee = taker_fee(1, ask)
                # hold to settlement: free at expiry
                payout = 1.0 if lo <= ss < hi else 0.0
                results["exp"].append((payout - ask - entry_fee, exp, key))
                # early exits, marked at the real bid
                p = path.get(c["ticker"]) or []
                ts_ = [x[0] for x in p]
                for mins in HORIZONS:
                    tgt = e0 + mins * 60
                    if tgt > close:
                        continue
                    j = bisect.bisect_right(ts_, tgt) - 1
                    if j < 0 or (tgt - ts_[j]) > 120:
                        continue
                    bid = p[j][1]
                    if bid <= 0:
                        continue
                    results[mins].append(
                        (bid - ask - entry_fee - taker_fee(1, bid), exp, key))

    # compare on the INTERSECTION so columns are the same contracts
    common = None
    for k in list(HORIZONS) + ["exp"]:
        ks = {r[2] for r in results.get(k, [])}
        common = ks if common is None else (common & ks)
    print(f"\n  {len(results['exp']):,} occupied-band entries; "
          f"{len(common):,} priced at every horizon\n")
    print(f"  {'hold':>8s} {'n':>6s} {'mean P&L/$1':>12s} {'win rate':>9s} "
          f"{'95% CI (expiry-clustered)':>30s}")
    for k in list(HORIZONS) + ["exp"]:
        rows = [r for r in results.get(k, []) if r[2] in common]
        if not rows:
            continue
        n = len(rows)
        m = sum(r[0] for r in rows) / n
        w = sum(1 for r in rows if r[0] > 0) / n
        be = defaultdict(list)
        for pnl, e, _ in rows:
            be[e].append(pnl)
        cm = [sum(v) / len(v) for v in be.values()]
        lo, hi = percentile_bootstrap_interval(cm)
        ci = (f"[{lo:+.4f}, {hi:+.4f}] {len(be)} exp" if lo is not None
              else f"({len(be)} exp < MIN_CLUSTERS={MIN_CLUSTERS})")
        label = "settle" if k == "exp" else f"{k}m"
        print(f"  {label:>8s} {n:6d} {m:+12.4f} {w:9.1%} {ci:>30s}")


if __name__ == "__main__":
    main()
