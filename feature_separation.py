"""What at entry predicts a band getting HIT, beyond what the model already knows?

THE QUESTION. misprice_failed is not a separate kind of trade — it is what
happens when the band gets hit. Across 176 live NO round trips neither the model
(0.140 winners vs 0.158 stop-outs) nor the market price (0.221 vs 0.242)
separated the two, against a realized gap of 0.502. Kelly cannot help: it is a
monotonic function of the model's own edge estimate, and on this book it
saturates KELLY_CAP on every single trade, making it identical to flat sizing.

So a filter, if one exists, has to come from a feature the model is NOT already
using. This tests every entry-time feature we record.

METHOD. For each band-observation at a BOUNDARY_NO-qualifying moment, compute
the RESIDUAL:

    residual = (band was hit) - (model's true_prob)

Bucketing the realized hit rate alone would just rediscover things the model
already prices — a nearer band is hit more often, and the model knows that.
The residual is what the model MISSES. A feature that separates the residual
carries information the pricer does not have; a feature that does not is already
in the price, however strongly it correlates with the outcome.

Reported per quintile with an expiry-clustered interval on the spread between
the top and bottom bucket, because 17k observations across ~300 expiries is far
fewer independent draws than it looks.

Settlement resolves as-of close from the quotes stream. Runs with the CURRENT
config, so with DRIFT_REVERTING_COEF = 0.0 the residual is against the corrected
model, not the biased one.

Usage:
    python3 feature_separation.py --start 2026-08-12 --end 2026-09-01
"""
from __future__ import annotations
import argparse, datetime as dt, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from kalshi_btc_bot import config as C
from kalshi_btc_bot.model import DistModel
from boundary_no_quote_replay import join_regimes, normalize_universe, tolerant_jsonl_gz
from wing_calibration import (MIN_CLUSTERS, percentile_bootstrap_interval,
                              daterange, spot_at, spot_series)

FEATURES = [
    ("otm_dist",  lambda c, rg, sp: abs(c["otm_dist"])),
    ("hours",     lambda c, rg, sp: c["hours"]),
    ("yes_ask",   lambda c, rg, sp: c["ask"]),
    ("spread",    lambda c, rg, sp: c["ask"] - c["bid"]),
    ("volume",    lambda c, rg, sp: c["vol"]),
    ("abs_z",     lambda c, rg, sp: abs(rg.get("z") or 0.0)),
    ("abs_mom",   lambda c, rg, sp: abs(rg.get("m") or 0.0)),
    ("vol_ewma",  lambda c, rg, sp: rg.get("v") or 0.0),
    ("vol_ratio", lambda c, rg, sp: rg.get("vr") or 1.0),
    ("abs_accel", lambda c, rg, sp: abs(rg.get("ac") or 0.0)),
    ("band_w",    lambda c, rg, sp: c["high"] - c["low"]),
]


def quintile_report(name, rows):
    """rows: [(feature_value, residual, hit, pred, expiry)]"""
    rows = sorted(rows, key=lambda r: r[0])
    n = len(rows)
    if n < 500:
        return None
    q = n // 5
    out = []
    for i in range(5):
        chunk = rows[i * q:(i + 1) * q] if i < 4 else rows[4 * q:]
        m = len(chunk)
        out.append({
            "lo": chunk[0][0], "hi": chunk[-1][0], "n": m,
            "resid": sum(r[1] for r in chunk) / m,
            "hit": sum(r[2] for r in chunk) / m,
            "pred": sum(r[3] for r in chunk) / m,
            "rows": chunk,
        })
    return out


def clustered_spread(top, bot, keyidx=4):
    """Paired interval on (top residual - bottom residual), clustered.

    keyidx selects the cluster key: 4 = expiry, 5 = day.

    EXPIRY CLUSTERING CANNOT TEST REGIME-LEVEL FEATURES. z-score, EWMA vol,
    vol_ratio, momentum and accel are properties of the MOMENT, so every band
    within one expiry shares them. A top and bottom quintile of such a feature
    therefore almost never appear in the same expiry — measured 2026-09-01,
    vol_ewma shared 19 expiries and vol_ratio 7, against a MIN_CLUSTERS of 30 —
    and the paired test silently returns nothing.

    Day clustering fixes the pairing (a day contains many regimes) at the cost of
    power: ~18 days against ~300 expiries. It is the honest test for these
    features, not a better one, and MIN_CLUSTERS is deliberately NOT waived for
    it — if 18 days cannot support an interval, the answer is "not measurable
    here", not a smaller bar.
    """
    def by_key(chunk):
        d = defaultdict(list)
        for row in chunk:
            d[row[keyidx]].append(row[1])
        return {k: sum(v) / len(v) for k, v in d.items()}
    a, b = by_key(top["rows"]), by_key(bot["rows"])
    shared = set(a) & set(b)
    if len(shared) < MIN_CLUSTERS:
        return None, None, len(shared)
    diffs = [a[k] - b[k] for k in shared]
    lo, hi = percentile_bootstrap_interval(diffs)
    return lo, hi, len(shared)


# Features constant within an expiry — must be clustered by DAY, not expiry.
REGIME_LEVEL = {"abs_z", "abs_mom", "vol_ewma", "vol_ratio", "abs_accel"}
# Degenerate in this sample: every band is 100-wide and accel is 0 for most
# observations, so their quintile "spread" is an artefact of tie-breaking.
DEGENERATE = {"band_w", "abs_accel"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-08-12")
    ap.add_argument("--end", default="2026-09-01")
    ap.add_argument("--every", type=int, default=4)
    a = ap.parse_args()
    if a.every < 1:
        ap.error("--every must be >= 1")

    uni = daterange("universe", a.start, a.end)
    qs = daterange("quotes", a.start, a.end)
    if not uni:
        raise SystemExit("no universe recordings")
    s_ts, s_sp = spot_series(qs)
    print(f"  {len(s_ts):,} spot samples / {len(qs)} quote days", flush=True)
    print(f"  DRIFT_REVERTING_COEF = {C.DRIFT_REVERTING_COEF}", flush=True)

    dist = DistModel()
    data = defaultdict(list)
    seen = set()
    total = 0
    byday = {Path(p).stem.split("_")[1][:10]: p for p in qs}
    for up in uni:
        day = Path(up).stem.split("_")[1][:10]
        if day not in byday:
            continue
        u = tolerant_jsonl_gz(up); u.sort(key=lambda r: r.get("t", ""))
        q = tolerant_jsonl_gz(byday[day]); q.sort(key=lambda r: r.get("t", ""))
        ticks = join_regimes(u, q, tolerance_secs=5)
        print(f"    {day} {len(ticks):,}", flush=True)
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
            reg = {"regime": rg.get("r"), "direction": rg.get("d"), "vol": vol,
                   "zscore": rg.get("z") or 0.0, "mom": rg.get("m") or 0.0}
            for c in normalize_universe(row, now):
                try:
                    close = dt.datetime.fromisoformat(
                        str(c["close_time"]).replace("Z", "+00:00")).timestamp()
                except Exception:
                    continue
                ss = spot_at(s_ts, s_sp, close, 120.0)
                if ss is None:
                    continue
                k = (c["ticker"], int(float(c["hours"]) * 30))
                if k in seen:
                    continue
                seen.add(k)
                hit = 1.0 if float(c["low"]) <= ss < float(c["high"]) else 0.0
                pred = dist.true_prob(c, float(spot), float(vol),
                                      float(c["hours"]), reg)
                exp = c["ticker"].rsplit("-", 1)[0]
                total += 1
                for fname, fn in FEATURES:
                    try:
                        data[fname].append((float(fn(c, rg, spot)),
                                            hit - pred, hit, pred, exp, day))
                    except Exception:
                        pass

    print(f"\n  {total:,} band-observations, residual = hit - model\n")
    print(f"  {'feature':11s} {'quintile range':>22s} {'n':>6s} {'model':>7s} "
          f"{'actual':>7s} {'residual':>9s}")
    ranked = []
    for fname, _ in FEATURES:
        qs_ = quintile_report(fname, data[fname])
        if not qs_:
            continue
        print(f"\n  {fname}")
        for i, b in enumerate(qs_):
            print(f"  {'':11s} {f'{b[chr(108)+chr(111)]:.4g} .. {b[chr(104)+chr(105)]:.4g}':>22s} "
                  f"{b['n']:6d} {b['pred']:7.4f} {b['hit']:7.4f} {b['resid']:+9.4f}")
        if fname in DEGENERATE:
            print(f"  {'':11s} DEGENERATE in this sample — no variation, skipped")
            continue
        regime_lvl = fname in REGIME_LEVEL
        keyidx, unit = (5, "days") if regime_lvl else (4, "expiries")
        lo, hi, nsh = clustered_spread(qs_[4], qs_[0], keyidx)
        spread = qs_[4]["resid"] - qs_[0]["resid"]
        tag = " [regime-level, day-clustered]" if regime_lvl else ""
        if lo is None:
            print(f"  {'':11s} top-bottom spread {spread:+.4f}  "
                  f"(only {nsh} shared {unit} — below MIN_CLUSTERS={MIN_CLUSTERS},"
                  f" not measurable){tag}")
            ranked.append((abs(spread), fname, spread, None, None,
                           "not measurable", unit, nsh))
        else:
            v = "SEPARATES" if (lo > 0 or hi < 0) else "spans 0"
            print(f"  {'':11s} top-bottom spread {spread:+.4f}  "
                  f"CI [{lo:+.4f}, {hi:+.4f}]  {nsh} {unit}  {v}{tag}")
            ranked.append((abs(spread), fname, spread, lo, hi, v, unit, nsh))

    print(f"\n  RANKED by |top-bottom residual spread|")
    for _, fname, sp, lo, hi, v, unit, nsh in sorted(ranked, key=lambda r: -r[0]):
        ci = f"CI [{lo:+.4f}, {hi:+.4f}]" if lo is not None else "no interval"
        print(f"    {fname:11s} {sp:+.4f}  {ci:26s} {nsh:3d} {unit:8s} {v}")
    sep = [r for r in ranked if r[5] == "SEPARATES"]
    unm = [r for r in ranked if r[5] == "not measurable"]
    if not sep:
        print(f"\n  NO feature separates the residual. Nothing recorded at entry")
        print(f"  predicts a band getting hit beyond what the model already prices.")
    if unm:
        print(f"  Not measurable here: {', '.join(r[1] for r in unm)}")


if __name__ == "__main__":
    main()
