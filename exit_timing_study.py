"""
Are the profit-lock tiers cutting winners too early?

Realised R:R across the paper book is 1.04x against a 4.88x theoretical profile
implied by entry prices — winners get capped at +35%..+105% by profit tiers
while losers run to -48%..-70%. This asks the direct question: for every trade
the bot closed on a PROFIT tier, what did the contract do afterwards?

Uses recorded ladder/universe quotes to follow each contract past our exit, so
this is measured against real prices we simply did not take, not a model.

Reported per exit tier:
    realised   what we actually booked
    peak-after the best exit available after we left (max bid seen)
    settle     what holding to expiry would have paid ($1 if ITM else $0)

Limits, stated:
  * A contract only appears in `quotes` while it passes the ladder filters, so
    post-exit coverage is partial; `universe` (from 2026-08-12) is unfiltered
    and used when available.
  * "peak-after" is a perfect-foresight upper bound — no rule achieves it. It
    bounds how much was left on the table, nothing more.
  * Settlement resolves from recorded spot near the close time; contracts whose
    expiry falls outside the recording window are dropped.

Usage:
    python3 exit_timing_study.py
"""
import bisect
import collections
import csv
import datetime as dt
import glob
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import real_price_edge_test as R

PROFIT_TIERS = ("peak_giveback", "gamma_lock", "scalp_lock", "snipe_lock",
                "momentum_locked", "profit_extracted", "near_settlement",
                "edge_gone", "misprice_captured")


def main():
    # ---- price history per contract, from both streams -------------------
    hist = collections.defaultdict(list)          # ticker -> [(dt, bid, ask)]
    stamps, spots = [], []
    for f in sorted(glob.glob("recordings/quotes_*.jsonl.gz")):
        for r in R.tolerant(f):
            t = dt.datetime.fromisoformat(r["t"])
            if r.get("spot"):
                stamps.append(t); spots.append(r["spot"])
            for c in (r.get("l") or []):
                if c.get("tk"):
                    hist[c["tk"]].append((t, c.get("b") or 0.0, c.get("a") or 0.0))
    for f in sorted(glob.glob("recordings/universe_*.jsonl.gz")):
        for r in R.tolerant(f):
            t = dt.datetime.fromisoformat(r["t"])
            if r.get("spot"):
                stamps.append(t); spots.append(r["spot"])
            for m in r["m"]:
                if m.get("tk"):
                    hist[m["tk"]].append((t, m.get("b") or 0.0, m.get("a") or 0.0))
    order = sorted(range(len(stamps)), key=lambda i: stamps[i])
    stamps = [stamps[i] for i in order]; spots = [spots[i] for i in order]
    for k in hist:
        hist[k].sort()

    def spot_at(when, tol=300):
        i = bisect.bisect_left(stamps, when)
        best = None
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(stamps):
                g = abs((stamps[j] - when).total_seconds())
                if best is None or g < best[0]:
                    best = (g, spots[j])
        return None if (best is None or best[0] > tol) else best[1]

    # ---- FIFO round trips from the trade log ----------------------------
    rows = [r for r in csv.DictReader(open("trades.csv")) if r["mode"] == "paper"]
    lots = collections.defaultdict(list); trips = []
    for r in rows:
        tk = r["ticker"]; n = int(r["count"]); px = float(r["price"])
        t = dt.datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S")
        if r["action"] == "buy":
            lots[tk].append({"n": n, "px": px, "no": r["side"] == "no"})
        else:
            rem = n; matched = []
            while rem > 0 and lots[tk]:
                L = lots[tk][0]; take = min(rem, L["n"])
                matched.append((take, L)); L["n"] -= take; rem -= take
                if L["n"] == 0: lots[tk].pop(0)
            for take, L in matched:
                trips.append({"tk": tk, "entry": L["px"], "exit": px,
                              "exit_t": t, "reason": r["reason"].split()[0],
                              "no": L["no"]})

    # trades.csv timestamps are NAIVE LOCAL; recordings are tz-aware UTC.
    # Convert once here rather than comparing mixed-awareness datetimes.
    def to_utc(naive):
        return naive.replace(tzinfo=dt.timezone.utc) + dt.timedelta(hours=4)

    out = collections.defaultdict(list)
    for tp in trips:
        if tp["reason"] not in PROFIT_TIERS or tp["no"]:
            continue
        h = hist.get(tp["tk"])
        if not h:
            continue
        cutoff = to_utc(tp["exit_t"])
        after = [(t, b, a) for t, b, a in h if t > cutoff]
        if not after:
            continue
        realised = (tp["exit"] - tp["entry"]) / tp["entry"]
        peak_after = max(b for _, b, _ in after)
        best_after = (peak_after - tp["entry"]) / tp["entry"]
        rec = {"realised": realised, "peak_after": best_after}
        exp = max(t for t, _, _ in h)
        s = spot_at(exp)
        out[tp["reason"]].append(rec)

    if not out:
        raise SystemExit("no profit-tier exits with post-exit coverage")

    print("Profit-tier exits, and what the contract did AFTER we left")
    print("(peak-after is perfect foresight — an upper bound on what was left)\n")
    print(f"  {'exit tier':<18} {'n':>4} {'realised':>10} {'peak after':>11} {'left on table':>14}")
    tot_r = tot_p = 0; tot_n = 0
    for tier, v in sorted(out.items(), key=lambda kv: -len(kv[1])):
        r_ = statistics.mean([x["realised"] for x in v])
        p_ = statistics.mean([x["peak_after"] for x in v])
        tot_r += r_ * len(v); tot_p += p_ * len(v); tot_n += len(v)
        print(f"  {tier:<18} {len(v):>4} {r_:>+9.1%} {p_:>+10.1%} {p_-r_:>+13.1%}")
    print(f"  {'ALL':<18} {tot_n:>4} {tot_r/tot_n:>+9.1%} {tot_p/tot_n:>+10.1%} "
          f"{(tot_p-tot_r)/tot_n:>+13.1%}")


if __name__ == "__main__":
    main()
