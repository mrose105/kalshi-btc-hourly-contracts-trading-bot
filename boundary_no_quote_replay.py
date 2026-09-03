"""Validate BOUNDARY_NO entries against recorded Kalshi quotes.

This is an entry-quality test, not an execution backtest. It replays the live
SignalEngine over recorded quote ticks, enters at the contemporaneous top-of-
book NO cost (1 - YES bid), and holds to settlement. Settlement is consulted
only after a signal has been selected.

The output can support claims about whether the live entry predicate selected
positive-expectancy NO contracts in the recorded sample. It cannot support
claims about depth, latency, stop performance, capacity, or executable return.
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import math
import glob
import gzip
import json
import random
import zlib
from dataclasses import dataclass

from kalshi_btc_bot import config as C
from kalshi_btc_bot.contracts import is_in_money, otm_distance, parse_contract
from kalshi_btc_bot.model import DistModel
from kalshi_btc_bot.signals import SignalEngine


@dataclass(frozen=True)
class Selection:
    ticker: str
    expiry_key: str
    entered_at: dt.datetime
    expiry: dt.datetime
    no_cost: float
    entry_fee: float
    payout: float | None
    zscore: float
    distance: float
    net_edge: float

    @property
    def pnl(self) -> float | None:
        return None if self.payout is None else self.payout - self.no_cost - self.entry_fee

    @property
    def all_in_cost(self) -> float:
        return self.no_cost + self.entry_fee


def tolerant_jsonl_gz(path: str) -> list[dict]:
    """Read an append-in-progress gzip JSONL file without losing prior rows.

    Also survives a CORRUPT member, not just a truncated one. A file whose
    writer was killed mid-block (macOS sleep, or the process being stopped)
    raises zlib.error rather than EOFError, and catching only EOFError turned
    that into a hard crash that took the whole replay down — for recordings that
    are unrecoverable, so dropping the day is not an option. Measured
    2026-08-31: recordings/*_2026-08-23 and the in-progress current day both hit
    this. Keep whatever decompressed cleanly and stop at the damage.

    RESUMES past a corrupt member instead of stopping at it. The recorder opens
    the file in append mode, so each session writes its own gzip MEMBER; one
    damaged member used to cost every member after it, silently and with no
    error. Measured 2026-09-02: universe_2026-09-01 is 5.86 MB and yielded
    18,660 rows ending 12:05 UTC, against 37,444 rows to 23:54 for a comparable
    full day — half the file discarded. 2026-09-02 lost ~90%, 2026-08-23 ~40%.

    That loss is NOT random. Damage stops the reader partway through a day, so
    what goes missing is always the BACK of the day — a systematic
    time-of-day/time-to-expiry bias in the one axis these strategies turn on,
    which is far worse than an equivalent amount of random missingness.

    Scans for member boundaries by gzip magic and decompresses each
    independently. The magic bytes can occur inside compressed data by chance,
    so a candidate offset counts only if it actually decompresses to something.

    Rows are returned sorted by timestamp: recovered members are not guaranteed
    to be in file order, and several callers do as-of joins that assume sorted
    input.
    UNION of two passes, deduplicated. The sequential pass is what gzip.open
    does and it is correct and fast on healthy files; the member-scan pass
    recovers what lies beyond damage. Taking the union means the recovery path
    can only ever ADD rows — an early version replaced the sequential read with
    a scan and cut a healthy 37,444-row day to 123, because a false magic-byte
    match inside compressed data split a good member mid-stream. A repair that
    can silently delete data is worse than the truncation it fixes.
    """
    seen: set[bytes] = set()
    lines: list[bytes] = []

    def take(blob: bytes) -> None:
        for ln in blob.split(b"\n"):
            ln = ln.strip()
            if ln and ln not in seen:
                seen.add(ln)
                lines.append(ln)

    # Pass 1 — sequential, stopping at the damage (the original behaviour).
    try:
        with gzip.open(path, "rb") as fh:
            take(fh.read())
    except (EOFError, zlib.error, OSError):
        try:
            with gzip.open(path, "rb") as fh:
                buf = []
                while True:
                    try:
                        chunk = fh.read(1 << 20)
                    except (EOFError, zlib.error, OSError):
                        break
                    if not chunk:
                        break
                    buf.append(chunk)
                take(b"".join(buf))
        except (EOFError, zlib.error, OSError):
            pass

    # Pass 2 — resume past damaged members. Only reached when pass 1 could not
    # read the file whole, so healthy files never pay for it.
    with open(path, "rb") as fh:
        data = fh.read()
    pos, n = 0, len(data)
    while 0 <= pos < n:
        start = data.find(b"\x1f\x8b\x08", pos)
        if start < 0:
            break
        dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
        out = []
        try:
            i, step = start, 1 << 20
            while i < n and not dec.eof:
                out.append(dec.decompress(data[i:i + step]))
                i += step
        except zlib.error:
            pass                      # keep what decompressed before the damage
        if out:
            take(b"".join(out))
        nxt = (n - len(dec.unused_data)) if dec.eof and dec.unused_data else -1
        pos = nxt if nxt > start else start + 3

    rows = []
    for ln in lines:
        try:
            rows.append(json.loads(ln))
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            continue                  # a torn line, not a reason to drop the rest
    rows.sort(key=lambda r: r.get("t", ""))
    return rows


def load_ticks(pattern: str) -> list[dict]:
    rows = []
    for path in sorted(glob.glob(pattern)):
        rows.extend(tolerant_jsonl_gz(path))
    rows.sort(key=lambda row: row["t"])
    return rows


def parse_grid(raw: str) -> list[float]:
    values = [float(item.strip()) for item in raw.split(",") if item.strip()]
    if not values or any(value < 0 for value in values):
        raise argparse.ArgumentTypeError("z-values must contain non-negative numbers")
    return values


def normalize_ladder(row: dict, now: dt.datetime) -> list[dict]:
    ladder = []
    for compact in row.get("l") or []:
        required = ("tk", "a", "b", "lo", "hi", "h", "d")
        if any(compact.get(key) is None for key in required):
            continue
        hours = float(compact["h"])
        ladder.append({
            "ticker": compact["tk"],
            "ask": float(compact["a"]),
            "bid": float(compact["b"]),
            "low": float(compact["lo"]),
            "high": float(compact["hi"]),
            "strike": (float(compact["lo"]) + float(compact["hi"])) / 2.0,
            "hours": hours,
            "otm_dist": float(compact["d"]),
            "itm": bool(compact.get("itm")),
            "type": "RANGE",
            "vol": compact.get("v", 0),
            "close_time": (now + dt.timedelta(hours=hours)).isoformat(),
        })
    return ladder


def normalize_universe(row: dict, now: dt.datetime) -> list[dict]:
    """Rebuild the production ladder from the uncensored raw market stream."""
    ladder = []
    spot = float(row["spot"])
    for market in row.get("m") or []:
        ask = float(market.get("a") or 0.0)
        bid = float(market.get("b") or 0.0)
        volume = float(market.get("v") or 0.0)
        if ask <= 0 or bid <= 0 or volume < C.MIN_VOLUME:
            continue
        # Mirror the production ladder filter exactly (ladder.py uses MAX_ASK),
        # so this replay predicts what the live bot would actually have seen.
        #
        # KNOWN CONSEQUENCE, not a bug in this tool: MAX_ASK is 0.45 while
        # BOUNDARY_NO_YES_ASK_MAX is 0.65, so BOUNDARY_NO candidates with a YES
        # ask between those two are filtered out of the shared ladder before the
        # NO scan ever sees them. That censorship predates the reverted 38c
        # policy and is still live. Raising the ladder ceiling is a real strategy
        # change and needs an explicit decision, so it is reported rather than
        # silently patched here.
        if ask > C.MAX_ASK:
            continue
        spread = ask - bid
        if spread > C.MAX_SPREAD or spread / ask > C.MAX_SPREAD_PCT:
            continue
        contract = parse_contract(market["tk"], spot, {
            "floor_strike": market.get("lo"),
            "cap_strike": market.get("hi"),
        })
        if contract["type"] == "UNKNOWN":
            continue
        try:
            expiry = dt.datetime.fromisoformat(str(market["ct"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        hours = max(0.01, (expiry - now).total_seconds() / 3600.0)
        ladder.append({
            **contract,
            "ticker": market["tk"],
            "ask": ask,
            "bid": bid,
            "spread": spread,
            "vol": volume,
            "hours": round(hours, 3),
            "close_time": market["ct"],
            "itm": is_in_money(contract, spot),
            "otm_dist": otm_distance(contract, spot),
        })
    return ladder


def join_regimes(universe: list[dict], quotes: list[dict],
                 tolerance_secs: int) -> list[dict]:
    """Attach the latest regime snapshot AT OR BEFORE each universe row.

    AS-OF, never nearest. The previous version took whichever neighbour was
    closer, which meant a snapshot from the FUTURE whenever one happened to be
    nearer — measured 2026-08-30, that was 96.5% of 38,583 joins. The leak was
    small on clean data (median 0.00s, max 0.75s: the recorder writes universe
    and quotes within the same scan cycle) and a z-score computed over minutes
    cannot move on 0.75s of extra price, which is why nothing visibly broke.

    It was unbounded during a recording GAP, though, and gaps are routine here —
    18.6 minutes on 2026-08-31 alone. Across a gap "nearest" can reach forward
    by up to tolerance_secs of genuinely unknowable information, and
    `zscore`/`regime`/`mom` from this join are what decide whether
    find_boundary_no() fires. A signal selected on a regime that did not exist
    yet is not a signal.

    Rows with no snapshot at or before the tick, or one older than
    tolerance_secs, are dropped rather than back-filled.
    """
    regime_rows = [row for row in quotes if row.get("rg")]
    stamps = [dt.datetime.fromisoformat(row["t"]) for row in regime_rows]
    joined = []
    for row in universe:
        when = dt.datetime.fromisoformat(row["t"])
        # bisect_right - 1 is the last stamp <= when; a stamp exactly equal to
        # the tick is contemporaneous, not future, so it is allowed.
        index = bisect.bisect_right(stamps, when) - 1
        if index < 0:
            continue
        age = (when - stamps[index]).total_seconds()
        if age > tolerance_secs:
            continue
        joined.append({**row, "rg": regime_rows[index]["rg"]})
    return joined


def build_spot_lookup(ticks: list[dict], tolerance_secs: int):
    stamps, spots = [], []
    for row in ticks:
        if row.get("spot") is not None:
            stamps.append(dt.datetime.fromisoformat(row["t"]))
            spots.append(float(row["spot"]))

    def spot_at(when: dt.datetime) -> float | None:
        index = bisect.bisect_left(stamps, when)
        best = None
        for candidate in (index - 1, index, index + 1):
            if 0 <= candidate < len(stamps):
                gap = abs((stamps[candidate] - when).total_seconds())
                if best is None or gap < best[0]:
                    best = (gap, spots[candidate])
        if best is None or best[0] > tolerance_secs:
            return None
        return best[1]

    return spot_at


def _one_contract_taker_fee(price: float, rate: float = 0.07,
                            multiplier: float = 1.0) -> float:
    """Conservative single-contract KXBTC taker fee, rounded up to the cent.

    Local on purpose. This is research ACCOUNTING — an EV term so a reported
    edge is not quietly gross of costs — not an entry rule. It is deliberately
    not wired to any config threshold, so no sweep or policy change can silently
    move it, and removing an entry-price policy cannot break this tool's numbers.
    """
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return math.ceil(rate * multiplier * price * (1.0 - price) * 100.0 - 1e-12) / 100.0


def replay(ticks: list[dict], zscore_min: float, tolerance_secs: int,
           settlement_ticks: list[dict] | None = None) -> list[Selection]:
    """Replay one best live-style BOUNDARY_NO signal per scan tick."""
    spot_at = build_spot_lookup(settlement_ticks or ticks, tolerance_secs)
    engine = SignalEngine(DistModel(), use_market_posterior=True)
    existing: dict[str, dict] = {}
    selections = []
    original_z = C.BOUNDARY_NO_ZSCORE_MIN
    C.BOUNDARY_NO_ZSCORE_MIN = zscore_min
    try:
        for row in ticks:
            now = dt.datetime.fromisoformat(row["t"])
            existing = {
                ticker: position
                for ticker, position in existing.items()
                if position["expiry"] > now
            }
            if len(existing) >= C.MAX_POSITIONS:
                continue

            rg = row.get("rg") or {}
            spot, vol = row.get("spot"), rg.get("v")
            if spot is None or not vol:
                continue
            regime = {
                "regime": rg.get("r"),
                "direction": rg.get("d"),
                "vol": vol,
                "zscore": rg.get("z") or 0.0,
                "mom": rg.get("m") or 0.0,
            }
            ladder = (normalize_universe(row, now) if row.get("m") is not None
                      else normalize_ladder(row, now))
            signal = engine.find_boundary_no(
                float(spot), float(vol), regime, ladder, existing,
                real_cash=500.0, start_total=500.0,
            )
            if signal is None:
                continue

            expiry = now + dt.timedelta(hours=float(signal["hours"]))
            settle_spot = spot_at(expiry)
            payout = None
            if settle_spot is not None:
                yes_settled = signal["low"] <= settle_spot < signal["high"]
                payout = 0.0 if yes_settled else 1.0
            selection = Selection(
                ticker=signal["ticker"],
                expiry_key=signal["ticker"].rsplit("-", 1)[0],
                entered_at=now,
                expiry=expiry,
                no_cost=1.0 - float(signal["bid"]),
                entry_fee=_one_contract_taker_fee(1.0 - float(signal["bid"])),
                payout=payout,
                zscore=float(signal["zscore"]),
                distance=float(signal["otm_dist"]),
                net_edge=float(signal["net_edge"]),
            )
            selections.append(selection)
            existing[signal["ticker"]] = {
                "contract": signal,
                "expiry": expiry,
            }
    finally:
        C.BOUNDARY_NO_ZSCORE_MIN = original_z
    return selections


def clustered_interval(rows: list[Selection], iterations: int,
                       seed: int) -> tuple[float, float, float, int]:
    """Bootstrap EV/$1 by expiry, preserving correlated contracts together."""
    clusters: dict[str, list[Selection]] = {}
    for row in rows:
        clusters.setdefault(row.expiry_key, []).append(row)
    keys = sorted(clusters)
    if not keys or iterations <= 0:
        return float("nan"), float("nan"), float("nan"), len(keys)

    rng = random.Random(seed)
    samples = []
    for _ in range(iterations):
        sampled = [clusters[rng.choice(keys)] for _ in keys]
        cost = sum(row.all_in_cost for cluster in sampled for row in cluster)
        pnl = sum(row.pnl for cluster in sampled for row in cluster)
        samples.append(pnl / cost if cost else 0.0)
    samples.sort()
    lo = samples[int(0.025 * (iterations - 1))]
    hi = samples[int(0.975 * (iterations - 1))]
    p_nonpositive = sum(value <= 0 for value in samples) / iterations
    return lo, hi, p_nonpositive, len(keys)


def summarize(rows: list[Selection], iterations: int, seed: int) -> dict:
    resolved = [row for row in rows if row.payout is not None]
    if not resolved:
        return {"n": 0, "unresolved": len(rows)}
    cost = sum(row.all_in_cost for row in resolved)
    pnl = sum(row.pnl for row in resolved)
    wins = sum(row.payout == 1.0 for row in resolved)
    lo, hi, p_nonpositive, clusters = clustered_interval(
        resolved, iterations, seed,
    )
    return {
        "n": len(resolved),
        "unresolved": len(rows) - len(resolved),
        "expiry_clusters": clusters,
        "win_rate": wins / len(resolved),
        "mean_no_cost": cost / len(resolved),
        "one_contract_pnl": pnl,
        "ev_per_dollar": pnl / cost,
        "ci_95": [lo, hi],
        "p_ev_nonpositive": p_nonpositive,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quotes", default="recordings/quotes_*.jsonl.gz")
    parser.add_argument("--universe", default="recordings/universe_*.jsonl.gz")
    parser.add_argument("--z-values", type=parse_grid,
                        default=parse_grid("0.5,0.75,0.9,1.0,1.2,1.4,1.5,1.75,2.0,2.5,3.0"))
    parser.add_argument("--tolerance-secs", type=int, default=180)
    parser.add_argument("--regime-tolerance-secs", type=int, default=10)
    parser.add_argument("--bootstrap", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20260818)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    ticks = load_ticks(args.quotes)
    if not ticks:
        raise SystemExit(f"no quote ticks matched {args.quotes!r}")

    universe = load_ticks(args.universe)
    decision_ticks = join_regimes(universe, ticks, args.regime_tolerance_secs)
    if not decision_ticks:
        raise SystemExit(
            "no raw universe rows could be joined to contemporaneous regime snapshots"
        )

    results = []
    for index, zscore in enumerate(args.z_values):
        rows = replay(
            decision_ticks, zscore, args.tolerance_secs,
            settlement_ticks=ticks,
        )
        metrics = summarize(rows, args.bootstrap, args.seed + index)
        results.append({"zscore_min": zscore, **metrics})

    if args.json:
        print(json.dumps({
            "method": "real_quote_boundary_no_hold_to_settlement",
            "quote_ticks": len(ticks),
            "universe_ticks": len(universe),
            "joined_decision_ticks": len(decision_ticks),
            "seed": args.seed,
            "results": results,
        }, indent=2))
        return

    print(f"recorded quote ticks: {len(ticks):,}")
    print(f"raw universe ticks: {len(universe):,}; joined decisions: {len(decision_ticks):,}")
    print("REAL QUOTES: one live-style BOUNDARY_NO selection per tick; hold to settlement")
    print("Top-of-book entry with conservative one-contract taker fee; "
          "excludes latency, depth, stops, and capacity.")
    print("  z      n  exp  unres      WR   mean cost    EV/$1       95% cluster CI   P(EV<=0)")
    for result in results:
        if not result.get("n"):
            print(f"{result['zscore_min']:>4.2f}      0")
            continue
        lo, hi = result["ci_95"]
        print(
            f"{result['zscore_min']:>4.2f} {result['n']:>6} "
            f"{result['expiry_clusters']:>4} {result['unresolved']:>6} "
            f"{result['win_rate']:>7.1%} {result['mean_no_cost']:>11.3f} "
            f"{result['ev_per_dollar']:>+9.1%} "
            f"[{lo:>+7.1%}, {hi:>+7.1%}] "
            f"{result['p_ev_nonpositive']:>9.1%}"
        )


if __name__ == "__main__":
    main()
