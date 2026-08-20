"""
Live-book counterfactual for SCALING INTO winning positions.

Why this and not a backtest A/B: config.py:337 says of the re-entry cap
"Treat any future re-entry rule as untestable on bars" -- 5-min bars showed 14
re-entries where live 2s ticks showed 53. Scale-in is the same class of rule
(an intra-position add), so bars structurally under-represent it. Recorded
quotes give ~16.7k usable ticks/day against 288 bars, which is the resolution
the rule actually lives at. This mirrors the methodology that decided
REENTRY_SIZE_DECAY: same real fills, hypothetical contract counts.

Method:
  1. Rebuild real positions from trades.csv (FIFO buy->sell per ticker).
  2. Replay the recorded quote stream over each position's actual life.
  3. At each tick, ask the PRODUCTION SignalEngine.find_best -- via a
     one-contract ladder with empty `existing` -- whether the contract we
     already hold would be bought again right now on its own merits.
  4. Add only when the position is UP by >= --min-unreal (never average down),
     at most --max-adds times, no more often than --spacing seconds apart,
     each tranche --decay^k of the original size.
  5. Price each hypothetical tranche at the tick's real ask and exit it at the
     position's REAL exit price. Nothing compounds; each tranche is scored
     independently, so no path-dependence contaminates the result.

YES positions only. BOUNDARY_NO is a different instrument (buy_no matches the
YES bid) and is reported as skipped rather than silently mixed in.

Usage:
    python3 scale_in_live_counterfactual.py
    python3 scale_in_live_counterfactual.py --min-unreal 0.10 --max-adds 2
"""
import argparse
import csv
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

MAIN = Path("/Users/michael/Downloads/Finance/Quant/kalshiArb")
sys.path.insert(0, str(MAIN))

from inspect_recording import load
from kalshi_btc_bot.model import DistModel
from kalshi_btc_bot.signals import SignalEngine
from replay_signals import ladder_from_quote, regime_from_rg

LOCAL = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def build_positions(path: Path):
    """FIFO-match buy->sell per ticker into closed round-trips."""
    rows = list(csv.DictReader(open(path)))
    for r in rows:
        r["_ts"] = (datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S")
                    .replace(tzinfo=LOCAL).astimezone(UTC))
    rows.sort(key=lambda r: r["_ts"])

    open_lots = defaultdict(list)
    positions = []
    for r in rows:
        tk = r["ticker"]
        if r["action"] == "buy":
            open_lots[tk].append(r)
        elif r["action"] == "sell" and open_lots[tk]:
            b = open_lots[tk].pop(0)
            positions.append({
                "ticker": tk,
                "side": b["side"],
                "mode": b["mode"],
                "entry_ts": b["_ts"],
                "exit_ts": r["_ts"],
                "entry": float(b["price"]),
                "exit": float(r["price"]),
                "count": int(b["count"]),
                "reason": r["reason"],
            })
    return positions


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-unreal", type=float, default=0.05,
                    help="only add when position is up at least this much (never average down)")
    ap.add_argument("--max-adds", type=int, default=2)
    ap.add_argument("--decay", type=float, default=0.5,
                    help="tranche k is decay**k of the original contract count")
    ap.add_argument("--spacing", type=int, default=60,
                    help="minimum seconds between adds on one position")
    args = ap.parse_args()

    positions = build_positions(MAIN / "trades.csv")
    yes = [p for p in positions if p["side"] == "yes"]
    no_n = len(positions) - len(yes)

    days = sorted(f.name.split("quotes_")[1].split(".")[0]
                  for f in (MAIN / "recordings").glob("quotes_*.jsonl.gz"))

    dist = DistModel()
    sig_e = SignalEngine(dist)

    # index positions by UTC date(s) they span
    by_day = defaultdict(list)
    for p in yes:
        d = p["entry_ts"].date()
        while d <= p["exit_ts"].date():
            by_day[d.isoformat()].append(p)
            d += timedelta(days=1)

    covered = 0
    adds = []          # each: {pos, ask, count, pnl}
    probed_ticks = 0

    for day in days:
        day_pos = by_day.get(day)
        if not day_pos:
            continue
        try:
            quotes = load("quotes", day)
        except Exception as e:
            print(f"  ! {day}: {e}")
            continue
        state = {id(p): {"adds": 0, "last": None} for p in day_pos}
        seen = set()

        for q in quotes:
            if not q.get("l"):
                continue
            t = datetime.fromisoformat(q["t"]).astimezone(UTC)
            live = [p for p in day_pos if p["entry_ts"] <= t < p["exit_ts"]]
            if not live:
                continue
            ladder = ladder_from_quote(q)
            by_tk = {c["ticker"]: c for c in ladder}
            regime = regime_from_rg(q.get("rg", {}))
            spot = q["spot"]

            for p in live:
                c = by_tk.get(p["ticker"])
                if c is None:
                    continue
                seen.add(id(p))
                probed_ticks += 1
                st = state[id(p)]
                if st["adds"] >= args.max_adds:
                    continue
                if st["last"] and (t - st["last"]).total_seconds() < args.spacing:
                    continue
                # never average down
                if c["bid"] < p["entry"] * (1 + args.min_unreal):
                    continue
                # reuse the production gate verbatim on a one-contract ladder
                if not sig_e.find_best(spot, regime["vol"], regime, [c], {}):
                    continue
                n = max(1, int(round(p["count"] * args.decay ** (st["adds"] + 1))))
                ask = c["ask"]
                adds.append({
                    "ticker": p["ticker"],
                    "ask": ask,
                    "count": n,
                    "unreal": (c["bid"] - p["entry"]) / p["entry"],
                    "pnl": (p["exit"] - ask) * n,
                    "reason": p["reason"],
                })
                st["adds"] += 1
                st["last"] = t
        covered += len(seen)

    print(f"\n{'='*68}")
    print("  LIVE-BOOK SCALE-IN COUNTERFACTUAL")
    print(f"{'='*68}")
    print(f"  policy: add when up >={args.min_unreal:.0%}, max {args.max_adds} adds, "
          f"decay {args.decay}, >={args.spacing}s apart")
    print(f"  closed round-trips in trades.csv: {len(positions)}  "
          f"(YES {len(yes)}, NO skipped {no_n})")
    print(f"  YES positions with quote coverage: {covered}")
    print(f"  position-ticks probed: {probed_ticks}")

    if not adds:
        print("\n  No adds triggered under this policy.")
        return

    pnl = sum(a["pnl"] for a in adds)
    cost = sum(a["ask"] * a["count"] for a in adds)
    wins = [a for a in adds if a["pnl"] > 0]
    print(f"\n  Adds triggered:        {len(adds)}")
    print(f"  Extra capital deployed: ${cost:.2f}")
    print(f"  P&L from adds:          ${pnl:+.2f}")
    print(f"  Return on added capital:{pnl/cost:+.1%}" if cost else "")
    print(f"  Add win rate:           {len(wins)/len(adds):.1%}")
    print(f"  Median unreal at add:   {statistics.median(a['unreal'] for a in adds):+.1%}")

    print("\n  By exit reason of the parent position:")
    agg = defaultdict(lambda: [0, 0.0])
    for a in adds:
        k = (a["reason"] or "?").replace("✅", "").replace("❌", "").strip()
        agg[k][0] += 1
        agg[k][1] += a["pnl"]
    for k, (n, v) in sorted(agg.items(), key=lambda kv: kv[1][1]):
        print(f"    {k:28} {n:4d} adds  ${v:+9.2f}")


if __name__ == "__main__":
    main()
