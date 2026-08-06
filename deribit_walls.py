"""
Deribit BTC options walls — gamma-weighted open-interest concentration.

The idea (user-proposed 2026-08-06): equity/index traders watch "call walls"
and "put walls" — strikes with huge dealer open interest that act as
resistance/support, because dealers hedging their gamma exposure buy dips and
sell rips near those strikes, pinning spot. Kalshi's own book is far too small
to produce that effect, but the real BTC options market (Deribit, ~$2B notional
on the front expiry alone) is where actual BTC dealer gamma lives. If those
walls pin BTC spot, that's directly tradeable through Kalshi hourly contracts.

TWO THINGS THIS MODULE IS CAREFUL ABOUT
---------------------------------------
1. Raw OI is a misleading wall metric near expiry. On 2026-08-06 the front
   expiry (7AUG26) showed 6,509 BTC of call OI at $70,000 vs 1,014 BTC at
   $64,000 — but spot was $64,371, so the $70k strike was ~9% OTM with
   essentially zero gamma one day from expiry. It cannot pin anything. The
   $64k strike, with 1/6th the OI, carried far more gamma. So wall strength
   here is gamma-weighted, not raw OI. This is the difference between a real
   signal and a chart that looks impressive.

2. Wall strength is reported UNSIGNED. Signed gamma exposure (GEX) requires
   assuming which side dealers are on (the usual retail convention is dealers
   long calls / short puts), and that assumption is not verifiable from public
   data. The pin/magnet effect depends on gamma CONCENTRATION, not on its
   sign, so the unsigned version is what's defensible. A signed GEX is
   reported separately and labelled as convention-dependent — do not build a
   directional signal on it without validating the convention first.

VALIDATION STATUS: NOT VALIDATED. Deribit's public API exposes only a current
OI snapshot — there is no historical open-interest endpoint — so this CANNOT
be backtested against the 59 days of BTC history the rest of the repo uses.
Any claim that walls predict reversion has to be earned forward, by recording
snapshots (record_walls() in kalshi_btc_bot/recorder.py) and testing once
enough history accrues. Nothing here feeds a trading decision yet, by design.
See docs/QUANT_STANDARDS_AUDIT.md for why this repo doesn't ship unvalidated
signals.

Usage:
    python3 deribit_walls.py                 # current walls, front expiry
    python3 deribit_walls.py --expiry 14AUG26
    python3 deribit_walls.py --selftest
"""
import argparse
import json
import math
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

DERIBIT_URL = ("https://www.deribit.com/api/v2/public/"
               "get_book_summary_by_currency?currency=BTC&kind=option")

# Deribit BTC options are 1 BTC per contract and open_interest is already
# denominated in BTC, so no multiplier is needed.
CONTRACT_SIZE = 1.0


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_gamma(spot: float, strike: float, t_years: float, iv: float,
             r: float = 0.0) -> float:
    """Black-Scholes gamma. Identical for a call and a put at the same strike,
    which is why wall strength can sum both without double-counting sign."""
    if spot <= 0 or strike <= 0 or t_years <= 0 or iv <= 0:
        return 0.0
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / (iv * sqrt_t)
    return _norm_pdf(d1) / (spot * iv * sqrt_t)


def dollar_gamma(spot: float, gamma: float, oi: float) -> float:
    """Dollar gamma exposure: $ change in delta-hedge per 1% spot move."""
    return gamma * oi * CONTRACT_SIZE * spot * spot * 0.01


def parse_instrument(name: str):
    """'BTC-7AUG26-65500-C' -> ('7AUG26', 65500.0, 'C')"""
    parts = name.split("-")
    if len(parts) != 4:
        raise ValueError(f"unexpected instrument name: {name}")
    return parts[1], float(parts[2]), parts[3]


def expiry_to_dt(expiry: str) -> datetime:
    """Deribit expiries settle 08:00 UTC on the stated day."""
    d = datetime.strptime(expiry, "%d%b%y").replace(tzinfo=timezone.utc)
    return d.replace(hour=8)


def fetch_raw(url: str = DERIBIT_URL, timeout: int = 30) -> list:
    with urllib.request.urlopen(url, timeout=timeout) as fh:
        return json.load(fh)["result"]


def build_walls(raw: list, expiry: str = None, now: datetime = None) -> dict:
    """Aggregate per-strike OI and gamma-weighted wall strength for one expiry.

    Returns a dict with spot, expiry, hours_to_expiry, and a per-strike list
    sorted by strike. Wall strength is unsigned dollar gamma (see module
    docstring); signed_gex uses the dealer-long-call/short-put convention and
    is labelled accordingly.
    """
    now = now or datetime.now(timezone.utc)

    # Deribit reports underlying_price per instrument (the forward for that
    # expiry). Use the front-expiry spot proxy: the option nearest to ATM on
    # the target expiry, which minimises forward-basis distortion.
    per_expiry = defaultdict(list)
    for d in raw:
        try:
            exp, strike, cp = parse_instrument(d["instrument_name"])
        except ValueError:
            continue
        per_expiry[exp].append((strike, cp, d))

    if not per_expiry:
        raise ValueError("no parseable option instruments in payload")

    if expiry is None:
        future = [e for e in per_expiry if expiry_to_dt(e) > now]
        if not future:
            raise ValueError("no unexpired expiries in payload")
        expiry = min(future, key=expiry_to_dt)

    if expiry not in per_expiry:
        raise ValueError(f"expiry {expiry} not present; have "
                         f"{sorted(per_expiry, key=expiry_to_dt)[:6]}")

    rows = per_expiry[expiry]
    underlyings = [d.get("underlying_price") for _, _, d in rows
                   if d.get("underlying_price")]
    if not underlyings:
        raise ValueError("no underlying_price on any instrument")
    spot = sorted(underlyings)[len(underlyings) // 2]   # median: robust to stale quotes

    t_years = max((expiry_to_dt(expiry) - now).total_seconds(), 0.0) / (365.25 * 24 * 3600)

    agg = defaultdict(lambda: {"call_oi": 0.0, "put_oi": 0.0,
                               "iv_sum": 0.0, "iv_n": 0, "volume": 0.0})
    for strike, cp, d in rows:
        oi = d.get("open_interest") or 0.0
        a = agg[strike]
        a["call_oi" if cp == "C" else "put_oi"] += oi
        a["volume"] += (d.get("volume") or 0.0)
        iv = d.get("mark_iv")
        if iv:
            a["iv_sum"] += iv / 100.0     # Deribit quotes IV in percent
            a["iv_n"] += 1

    strikes = []
    for k in sorted(agg):
        a = agg[k]
        iv = (a["iv_sum"] / a["iv_n"]) if a["iv_n"] else 0.0
        total_oi = a["call_oi"] + a["put_oi"]
        g = bs_gamma(spot, k, t_years, iv)
        # Unsigned wall strength: gamma is identical for calls and puts at a
        # strike, so total OI is the right weight for pin/magnet strength.
        strength = dollar_gamma(spot, g, total_oi)
        # Convention-dependent: dealers long calls, short puts.
        signed = dollar_gamma(spot, g, a["call_oi"] - a["put_oi"])
        strikes.append({
            "strike": k, "call_oi": a["call_oi"], "put_oi": a["put_oi"],
            "total_oi": total_oi, "iv": iv, "gamma": g,
            "wall_strength": strength, "signed_gex": signed,
            "volume": a["volume"], "dist_pct": (k - spot) / spot * 100.0,
        })

    return {
        "ts": now.isoformat(), "expiry": expiry, "spot": spot,
        "hours_to_expiry": t_years * 365.25 * 24,
        "strikes": strikes,
        "total_wall_strength": sum(s["wall_strength"] for s in strikes),
        "total_signed_gex": sum(s["signed_gex"] for s in strikes),
    }


def top_walls(walls: dict, n: int = 5, max_dist_pct: float = None) -> list:
    """Strongest walls by gamma-weighted strength, optionally within a band."""
    ss = walls["strikes"]
    if max_dist_pct is not None:
        ss = [s for s in ss if abs(s["dist_pct"]) <= max_dist_pct]
    return sorted(ss, key=lambda s: -s["wall_strength"])[:n]


def summarize(walls: dict) -> str:
    out = []
    out.append(f"Deribit BTC walls — expiry {walls['expiry']}  "
               f"({walls['hours_to_expiry']:.1f}h out)")
    out.append(f"  spot ${walls['spot']:,.0f}")
    out.append("")
    out.append(f"  {'strike':>9} {'dist':>7} {'call OI':>9} {'put OI':>9} "
               f"{'IV':>6} {'wall $gamma':>13}")
    peak = max((s["wall_strength"] for s in walls["strikes"]), default=0.0)
    for s in walls["strikes"]:
        if s["total_oi"] < 1 and s["wall_strength"] <= 0:
            continue
        bar = "#" * int(20 * s["wall_strength"] / peak) if peak > 0 else ""
        near = "  <-- SPOT" if abs(s["dist_pct"]) < 0.4 else ""
        out.append(f"  {s['strike']:>9,.0f} {s['dist_pct']:>+6.1f}% "
                   f"{s['call_oi']:>9,.0f} {s['put_oi']:>9,.0f} "
                   f"{s['iv']*100:>5.1f}% {s['wall_strength']:>13,.0f}  {bar}{near}")
    out.append("")
    out.append("  Strongest walls within ±3% of spot (the band hourly contracts live in):")
    near_walls = top_walls(walls, n=5, max_dist_pct=3.0)
    if not near_walls:
        out.append("    (none — all OI is far OTM, no pin candidates)")
    for s in near_walls:
        side = "call-heavy" if s["call_oi"] > s["put_oi"] else "put-heavy"
        out.append(f"    ${s['strike']:>8,.0f} ({s['dist_pct']:>+5.1f}%)  "
                   f"strength {s['wall_strength']:>12,.0f}  {side}")
    out.append("")
    out.append(f"  signed GEX (convention-dependent, do NOT trade off this "
               f"unvalidated): {walls['total_signed_gex']:>+,.0f}")
    return "\n".join(out)


# ─────────────────────────────────────────────
# SELF-TEST
# ─────────────────────────────────────────────
def selftest() -> int:
    fails = []

    def check(name, cond, detail=""):
        print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
        if not cond:
            fails.append(name)

    print("deribit_walls selftest")

    # 1. instrument parsing
    try:
        e, k, cp = parse_instrument("BTC-7AUG26-65500-C")
        check("parse_instrument", (e, k, cp) == ("7AUG26", 65500.0, "C"), f"got {(e,k,cp)}")
    except Exception as ex:
        check("parse_instrument", False, str(ex))

    # 2. expiry parsing -> 08:00 UTC settlement
    dt = expiry_to_dt("7AUG26")
    check("expiry_to_dt 08:00 UTC",
          (dt.year, dt.month, dt.day, dt.hour) == (2026, 8, 7, 8), str(dt))

    # 3. gamma peaks at the money — the core reason we gamma-weight at all
    t = 1.0 / 365.0
    atm = bs_gamma(65000, 65000, t, 0.5)
    otm = bs_gamma(65000, 72000, t, 0.5)
    check("gamma peaks ATM, ~0 far OTM near expiry",
          atm > 0 and otm < atm * 1e-3, f"atm={atm:.3e} otm={otm:.3e}")

    # 4. wall strength scales linearly with OI (so a 2x wall reads 2x strong)
    g = bs_gamma(65000, 65000, t, 0.5)
    check("dollar_gamma linear in OI",
          abs(dollar_gamma(65000, g, 200.0) - 2 * dollar_gamma(65000, g, 100.0)) < 1e-6)

    # 5. gamma falls as expiry lengthens (ATM)
    near = bs_gamma(65000, 65000, 1.0 / 365.0, 0.5)
    far = bs_gamma(65000, 65000, 30.0 / 365.0, 0.5)
    check("ATM gamma decreases with time to expiry", near > far, f"{near:.3e} vs {far:.3e}")

    # 6. degenerate inputs return 0 rather than raising
    check("degenerate inputs -> 0.0",
          bs_gamma(0, 65000, t, 0.5) == 0.0
          and bs_gamma(65000, 65000, 0, 0.5) == 0.0
          and bs_gamma(65000, 65000, t, 0) == 0.0)

    # 7. build_walls on a synthetic payload: the high-gamma near strike must
    #    outrank a far strike carrying 6x the OI (the exact 2026-08-06 case)
    now = datetime(2026, 8, 6, 8, 0, tzinfo=timezone.utc)
    synth = [
        {"instrument_name": "BTC-7AUG26-64000-C", "open_interest": 1000.0,
         "underlying_price": 64371.0, "mark_iv": 30.0, "volume": 0.0},
        {"instrument_name": "BTC-7AUG26-70000-C", "open_interest": 6000.0,
         "underlying_price": 64371.0, "mark_iv": 60.0, "volume": 0.0},
    ]
    w = build_walls(synth, now=now)
    by_strike = {s["strike"]: s for s in w["strikes"]}
    check("near-ATM strike outranks 6x-OI far strike",
          by_strike[64000.0]["wall_strength"] > by_strike[70000.0]["wall_strength"],
          f"64k={by_strike[64000.0]['wall_strength']:.0f} "
          f"70k={by_strike[70000.0]['wall_strength']:.0f}")

    # 8. front expiry auto-selected, and expired ones ignored
    synth2 = synth + [
        {"instrument_name": "BTC-1AUG26-64000-C", "open_interest": 9999.0,
         "underlying_price": 64371.0, "mark_iv": 30.0, "volume": 0.0},
        {"instrument_name": "BTC-28AUG26-64000-C", "open_interest": 5.0,
         "underlying_price": 64371.0, "mark_iv": 30.0, "volume": 0.0},
    ]
    w2 = build_walls(synth2, now=now)
    check("auto-selects front UNEXPIRED expiry", w2["expiry"] == "7AUG26", w2["expiry"])

    # 9. call/put OI land in the right buckets
    synth3 = [
        {"instrument_name": "BTC-7AUG26-64000-C", "open_interest": 10.0,
         "underlying_price": 64371.0, "mark_iv": 30.0, "volume": 1.0},
        {"instrument_name": "BTC-7AUG26-64000-P", "open_interest": 40.0,
         "underlying_price": 64371.0, "mark_iv": 30.0, "volume": 2.0},
    ]
    w3 = build_walls(synth3, now=now)
    s0 = w3["strikes"][0]
    check("call/put OI split correctly",
          s0["call_oi"] == 10.0 and s0["put_oi"] == 40.0 and s0["total_oi"] == 50.0)

    # 10. signed GEX flips sign with put dominance; unsigned strength stays > 0
    check("signed GEX negative when put-heavy, unsigned positive",
          s0["signed_gex"] < 0 < s0["wall_strength"],
          f"signed={s0['signed_gex']:.0f} unsigned={s0['wall_strength']:.0f}")

    print(f"\n{len(fails)} failure(s)" if fails else "\nall checks passed")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--expiry", default=None, help="e.g. 14AUG26 (default: front unexpired)")
    ap.add_argument("--json", action="store_true", help="emit raw JSON")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--record", action="store_true",
                    help="poll and record snapshots to recordings/walls_*.jsonl.gz "
                         "(runs standalone — NOT in the trading loop)")
    ap.add_argument("--interval", type=int, default=300,
                    help="seconds between snapshots when --record (default 300)")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(selftest())

    if args.record:
        import time
        from kalshi_btc_bot import recorder
        recorder.ENABLED = True          # standalone: independent of KALSHI_RECORD
        print(f"recording Deribit walls every {args.interval}s -> recordings/walls_*.jsonl.gz")
        print("(no historical OI exists, so this is the only path to validating the idea)")
        print("Ctrl-C to stop.")
        n = 0
        try:
            while True:
                try:
                    w = build_walls(fetch_raw(), expiry=args.expiry)
                    recorder.record_walls(w)
                    n += 1
                    near = top_walls(w, n=1, max_dist_pct=3.0)
                    top = (f"top wall ${near[0]['strike']:,.0f} "
                           f"({near[0]['dist_pct']:+.1f}%)") if near else "no near wall"
                    print(f"  [{datetime.now(timezone.utc):%H:%M:%S}] #{n} "
                          f"{w['expiry']} spot=${w['spot']:,.0f} {top}")
                except Exception as ex:      # network blips shouldn't kill the run
                    print(f"  [{datetime.now(timezone.utc):%H:%M:%S}] fetch failed: {ex}")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            recorder.close()
            print(f"\nstopped — {n} snapshots recorded. {recorder.stats()}")
        return

    walls = build_walls(fetch_raw(), expiry=args.expiry)
    if args.json:
        print(json.dumps(walls, indent=2))
    else:
        print(summarize(walls))
        print("\n  NOT VALIDATED — no historical OI available from Deribit's public API,")
        print("  so this cannot be backtested. Record forward before trusting it.")


if __name__ == "__main__":
    main()
