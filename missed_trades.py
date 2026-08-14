"""
What are the entry gates costing us?

Answerable only since record_universe (2026-08-12) started capturing the raw
expiry window BEFORE any filter. Every earlier attempt at this question died on
censorship: the `quotes` stream contains only rows that already passed MAX_ASK,
MIN_VOLUME and MAX_SPREAD, so asking "should MAX_ASK be higher?" against it
returns 0% blocked by construction. Two separate analyses on 2026-08-11 hit
exactly that wall.

Method: for every contract seen in the universe, take a representative quote,
resolve what it actually settled at from recorded spot, and compute what buying
it would have returned — then bucket by which gate (if any) blocked it.

    EV_yes = (settled_itm - ask) / ask
    EV_no  = ((1 - settled_itm) - no_cost) / no_cost,  no_cost = 1 - bid
             (1 - bid is the NO ASK — what buying NO actually costs. Using
              1 - ask there is the bug fixed in 731be85.)

Honest limits, stated rather than buried:
  * A representative quote is not an entry price. The bot enters on a signal at
    a moment, not at the median of a contract's life. This measures whether a
    gate is systematically blocking profitable contracts, not what the bot would
    literally have earned.
  * Holding to settlement is not what the bot does — it exits on tiers.
  * Settlement resolves from recorded spot nearest the close time, so contracts
    whose expiry falls outside the recording window are dropped. On the first
    run that dropped 1,048 of 1,960 contracts, which was ATTRIBUTED TO BOT
    RESTARTS. That was wrong. Measured 2026-08-14: 68 gaps >5min totalling
    43.8h — 21% of the whole recording window — almost all exactly 2.0h and
    clustered 01:00-09:00 UTC. They are macOS Maintenance Sleep cycles. The bot
    runs under `caffeinate -dimsu`, but -s only prevents system sleep ON AC
    POWER; on battery the machine sleeps anyway and the process stays alive
    receiving nothing. Fix is AC power, or `sudo pmset -b sleep 0 disablesleep 1`.
    The gaps are also non-random — they concentrate overnight, biasing every
    study here toward US-session conditions.
  * Independent unit is the contract, not the observation.

Usage:
    python3 missed_trades.py
    python3 missed_trades.py --min-obs 20
"""
import argparse
import bisect
import datetime as dt
import glob
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import real_price_edge_test as R
from kalshi_btc_bot import config as C


def build_spot_index():
    stamps, spots = [], []
    for f in sorted(glob.glob("recordings/quotes_*.jsonl.gz")):
        for r in R.tolerant(f):
            if r.get("spot"):
                stamps.append(dt.datetime.fromisoformat(r["t"]))
                spots.append(r["spot"])
    for f in sorted(glob.glob("recordings/universe_*.jsonl.gz")):
        for r in R.tolerant(f):
            if r.get("spot"):
                stamps.append(dt.datetime.fromisoformat(r["t"]))
                spots.append(r["spot"])
    order = sorted(range(len(stamps)), key=lambda i: stamps[i])
    return [stamps[i] for i in order], [spots[i] for i in order]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-obs", type=int, default=10,
                    help="ignore contracts seen fewer times than this")
    ap.add_argument("--tolerance-secs", type=int, default=300)
    ap.add_argument("--anchor-mins", type=float, default=15.0,
                    help="evaluate each contract this many minutes before its expiry")
    args = ap.parse_args()

    stamps, spots = build_spot_index()
    if not stamps:
        raise SystemExit("no recordings")

    def spot_at(when):
        i = bisect.bisect_left(stamps, when)
        best = None
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(stamps):
                gap = abs((stamps[j] - when).total_seconds())
                if best is None or gap < best[0]:
                    best = (gap, spots[j])
        return None if (best is None or best[0] > args.tolerance_secs) else best[1]

    # gather every observation per contract
    per = {}
    for f in sorted(glob.glob("recordings/universe_*.jsonl.gz")):
        for r in R.tolerant(f):
            for m in r["m"]:
                tk = m.get("tk")
                if not tk or m.get("lo") is None or m.get("hi") is None:
                    continue
                per.setdefault(tk, {"obs": [], "lo": m["lo"],
                                    "hi": m["hi"], "ct": m.get("ct")})
                per[tk]["obs"].append((r["t"], m.get("a") or 0.0,
                                       m.get("b") or 0.0, m.get("v") or 0.0))

    rows = []
    unresolved = 0
    anchor = dt.timedelta(minutes=args.anchor_mins)
    for tk, d in per.items():
        if len(d["obs"]) < args.min_obs or not d["ct"]:
            continue
        try:
            exp = dt.datetime.fromisoformat(d["ct"].replace("Z", "+00:00"))
        except Exception:
            continue
        s = spot_at(exp)
        if s is None:
            unresolved += 1
            continue
        # Evaluate at a consistent point in the contract's life rather than the
        # median of its whole history. The bot enters at a moment, and a
        # contract's quote changes character completely as expiry approaches, so
        # a lifetime median describes no decision anyone ever faced.
        target = exp - anchor
        best = min(d["obs"],
                   key=lambda o: abs((dt.datetime.fromisoformat(o[0]) - target).total_seconds()))
        if abs((dt.datetime.fromisoformat(best[0]) - target).total_seconds()) > 600:
            continue
        _, ask, bid, vol = best
        if ask <= 0:
            continue
        itm = 1 if d["lo"] <= s < d["hi"] else 0
        spread = ask - bid

        blocked = []
        if ask > C.MAX_ASK:                      blocked.append("MAX_ASK")
        if vol < C.MIN_VOLUME:                   blocked.append("MIN_VOLUME")
        if bid <= 0:                             blocked.append("no bid")
        elif spread > C.MAX_SPREAD:              blocked.append("MAX_SPREAD")
        elif ask > 0 and spread / ask > C.MAX_SPREAD_PCT:
            blocked.append("MAX_SPREAD_PCT")

        no_cost = 1.0 - bid
        rows.append({
            "tk": tk, "ask": ask, "bid": bid, "vol": vol, "itm": itm,
            "gate": blocked[0] if blocked else "PASSES",
            "blocked": bool(blocked),
            "ev_yes": (itm - ask) / ask,
            "ev_no": (((1 - itm) - no_cost) / no_cost) if 0 < no_cost < 1 else None,
        })

    print(f"contracts evaluated {args.anchor_mins:.0f} min before expiry, with a\n"
      f"resolvable settlement: {len(rows):,}   (unresolved: {unresolved:,})\n")

    def block(label, sel):
        if not sel:
            return
        n = len(sel)
        ask = statistics.median([r["ask"] for r in sel])
        itm = sum(r["itm"] for r in sel) / n
        ey = sum(r["ev_yes"] for r in sel) / n
        nos = [r["ev_no"] for r in sel if r["ev_no"] is not None]
        en = sum(nos) / len(nos) if nos else float("nan")
        print(f"  {label:<18} {n:>5} {ask:>9.3f} {itm:>9.1%} {ey:>+10.1%} {en:>+10.1%}")

    print(f"  {'gate':<18} {'n':>5} {'med ask':>9} {'settled':>9} "
          f"{'EV buy YES':>10} {'EV buy NO':>10}")
    block("PASSES (tradeable)", [r for r in rows if not r["blocked"]])
    for g in ("MAX_ASK", "MIN_VOLUME", "no bid", "MAX_SPREAD", "MAX_SPREAD_PCT"):
        block(f"blocked: {g}", [r for r in rows if r["gate"] == g])

    # the headline question: is anything being blocked that we'd want?
    print("\n  Contracts blocked by MAX_ASK, split by price band:")
    print(f"  {'ask band':<18} {'n':>5} {'med ask':>9} {'settled':>9} {'EV buy YES':>10}")
    ma = [r for r in rows if r["gate"] == "MAX_ASK"]
    for lo, hi in ((0.45, 0.60), (0.60, 0.75), (0.75, 0.90), (0.90, 1.01)):
        sel = [r for r in ma if lo <= r["ask"] < hi]
        if len(sel) < 3:
            continue
        n = len(sel)
        print(f"  {f'{lo:.2f}-{hi:.2f}':<18} {n:>5} "
              f"{statistics.median([r['ask'] for r in sel]):>9.3f} "
              f"{sum(r['itm'] for r in sel)/n:>9.1%} "
              f"{sum(r['ev_yes'] for r in sel)/n:>+10.1%}")


if __name__ == "__main__":
    main()
