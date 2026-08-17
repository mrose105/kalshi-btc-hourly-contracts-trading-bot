"""spx_dry_run.py — run the existing strategies against live SPX contracts, trade nothing.

Answers two questions that cannot be answered from the BTC backtest:

  1. Do the strategies fire at all on KXINXU/KXINX, and on what?
     The ladder, regime engine, pricing model and all four signal functions are
     the production ones, unmodified. Only the instrument changes.

  2. What does ewma_volatility() actually return for SPX?
     This is the number the vol cone in instrument.py must be set against, and
     it CANNOT be derived from 5-minute bars. The model consumes an EWMA over
     2-second ticks, and tick estimators carry microstructure bias whose sign
     differs by instrument: BTC's bid-ask bounce inflates it, while a repeated
     index quote between polls deflates it. Setting SPX's cone from bar vol
     would repeat the RANGE_WIDTH mistake in a new place.

Run during the US equity session (09:30-16:00 ET); outside it the feed
correctly refuses to record ticks and there is nothing to measure.

    KALSHI_INSTRUMENT=SPX python3 spx_dry_run.py --minutes 30

Writes a JSON record to spx_dry_run_<timestamp>.json for later analysis.
"""

import argparse
import collections
import datetime
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path

os.environ.setdefault("KALSHI_INSTRUMENT", "SPX")
sys.path.insert(0, str(Path(__file__).parent))

from kalshi_es_analysis import KalshiClient
from kalshi_btc_bot import instrument
from kalshi_btc_bot.config import BARS_PER_HOUR, PRICE_FETCH, SCAN_INTERVAL
from kalshi_btc_bot.ladder import Ladder
from kalshi_btc_bot.model import DistModel, _VOL_H_CAP, _VOL_H_FLOOR
from kalshi_btc_bot.regime import RegimeEngine
from kalshi_btc_bot.signals import SignalEngine

ANN = math.sqrt(8760)          # annualization convention used in model.py
NOMINAL_CASH = 500.0           # sizing inputs for the NO scans; nothing is traded


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=30.0)
    args = ap.parse_args()

    inst = instrument.ACTIVE
    print("=" * 68)
    print(f"  SPX DRY RUN — {inst.name} | series {', '.join(inst.series)}")
    print(f"  vol cone [{inst.vol_h_floor}, {inst.vol_h_cap}] hourly "
          f"({inst.vol_h_floor*ANN:.0%}–{inst.vol_h_cap*ANN:.0%} annualized)")
    print(f"  NO ORDERS ARE PLACED")
    print("=" * 68)

    client = KalshiClient(
        api_key_id=os.environ["KALSHI_API_KEY_ID"],
        private_key_path=os.environ["KALSHI_PRIVATE_KEY_PATH"],
        base_url=os.environ.get("KALSHI_BASE_URL",
                                "https://api.elections.kalshi.com/trade-api/v2"),
    )
    client.login()

    feed = instrument.make_feed()
    regime_e = RegimeEngine()
    dist = DistModel()
    ladder_e = Ladder(client)
    signal_e = SignalEngine(dist)

    print("\n  Bootstrapping 5-min bars...")
    n = feed.bootstrap_history(hours=24)
    print(f"  ✓ {n} bars")

    vols, ratios, spots = [], [], []
    ladder_sizes, type_counts = [], collections.Counter()
    signals: list = []
    regimes = collections.Counter()
    scan_next = 0.0
    stale_ticks = 0
    deadline = time.time() + args.minutes * 60

    print(f"  Running {args.minutes:.0f} min...\n")
    while time.time() < deadline:
        spot = feed.fetch()
        if not getattr(feed, "session_open", True):
            stale_ticks += 1
            if stale_ticks % 30 == 1:
                age = getattr(feed, "last_quote_age", None)
                print(f"  ⏸  session closed / quote stale "
                      f"({age:.0f}s old) — not recording ticks")
            time.sleep(PRICE_FETCH)
            continue

        vol = feed.ewma_volatility()
        vols.append(vol)
        ratios.append(feed.vol_ratio())
        spots.append(spot)

        now = time.time()
        if now >= scan_next:
            scan_next = now + SCAN_INTERVAL
            regime = regime_e.detect(feed)
            regimes[regime.get("regime", "?")] += 1
            ladder = ladder_e.get(spot) or []
            ladder_sizes.append(len(ladder))
            for c in ladder:
                type_counts[c["type"]] += 1

            found = []
            for name, fn in (
                ("YES_BEST",    lambda: signal_e.find_best(spot, vol, regime, ladder, {})),
                ("SNIPE",       lambda: signal_e.find_snipe(spot, vol, regime, ladder, {})),
                ("MISPRICE_NO", lambda: signal_e.find_no_scalp(
                    spot, vol, regime, ladder, {}, NOMINAL_CASH, NOMINAL_CASH)),
                ("BOUNDARY_NO", lambda: signal_e.find_boundary_no(
                    spot, vol, regime, ladder, {}, NOMINAL_CASH, NOMINAL_CASH)),
            ):
                try:
                    s = fn()
                except Exception as e:
                    print(f"  ⚠️  {name} raised: {type(e).__name__}: {e}")
                    continue
                if s:
                    rec = {
                        "t": datetime.datetime.now().isoformat(timespec="seconds"),
                        "strategy": name, "ticker": s.get("ticker"),
                        "type": s.get("type"), "ask": s.get("ask"),
                        "true_prob": s.get("true_prob"), "edge": s.get("edge"),
                        "otm_dist": s.get("otm_dist"), "hours": s.get("hours"),
                        "spot": round(spot, 2),
                    }
                    signals.append(rec)
                    found.append(f"{name}:{s.get('ticker','')[-13:]}")

            vh = vol * math.sqrt(BARS_PER_HOUR)
            clamp = "FLOOR" if vh < _VOL_H_FLOOR else "CAP" if vh > _VOL_H_CAP else "ok"
            print(f"  [{datetime.datetime.now():%H:%M:%S}] spot={spot:8.2f} "
                  f"vol_h={vh:.5f}({vh*ANN:5.1%}ann,{clamp:5}) "
                  f"ratio={feed.vol_ratio():.2f} {regime.get('regime','?'):9} "
                  f"ladder={len(ladder):3} "
                  f"{'| ' + ' '.join(found) if found else ''}")

        time.sleep(PRICE_FETCH)

    # ── Report ────────────────────────────────────────────────────
    print("\n" + "=" * 68)
    if not vols:
        print("  No live ticks recorded — was the market open?")
        return 1

    vh = sorted(v * math.sqrt(BARS_PER_HOUR) for v in vols)
    def q(p): return vh[min(int(len(vh) * p), len(vh) - 1)]
    below = sum(1 for v in vh if v < _VOL_H_FLOOR) / len(vh)
    above = sum(1 for v in vh if v > _VOL_H_CAP) / len(vh)

    print(f"  TICK-EWMA HOURLY VOL  (n={len(vh)}, the cone calibration input)")
    for p in (0.01, 0.05, 0.25, 0.50, 0.75, 0.95, 0.99):
        print(f"    p{int(p*100):<3} {q(p):.6f}   ({q(p)*ANN:6.1%} annualized)")
    print(f"    clamped at FLOOR ({_VOL_H_FLOOR}): {below:.1%}"
          f"   at CAP ({_VOL_H_CAP}): {above:.1%}")
    if below > 0.05 or above > 0.05:
        print(f"    ⚠️  cone is binding on live data — widen it in instrument.py, "
              f"a clamped vol silently rewrites every true_prob")

    print(f"\n  LADDER   scans={len(ladder_sizes)} "
          f"mean_size={statistics.mean(ladder_sizes) if ladder_sizes else 0:.1f} "
          f"max={max(ladder_sizes) if ladder_sizes else 0}")
    print(f"    contract types seen: {dict(type_counts) or 'NONE'}")
    print(f"  REGIME   {dict(regimes)}")
    print(f"  VOL_RATIO median={statistics.median(ratios):.3f}" if ratios else "")

    print(f"\n  SIGNALS  {len(signals)} total")
    by = collections.Counter(s["strategy"] for s in signals)
    for k in ("YES_BEST", "SNIPE", "MISPRICE_NO", "BOUNDARY_NO"):
        print(f"    {k:14} {by.get(k,0)}")
    for s in signals[:15]:
        print(f"      {s['t'][11:]} {s['strategy']:12} {str(s['ticker'])[-14:]:15} "
              f"ask={s['ask']} true={s['true_prob']} edge={s['edge']}")
    if len(signals) > 15:
        print(f"      ... and {len(signals)-15} more")

    out = Path(f"spx_dry_run_{datetime.datetime.now():%Y%m%d_%H%M%S}.json")
    out.write_text(json.dumps({
        "instrument": inst.name, "series": list(inst.series),
        "cone": [inst.vol_h_floor, inst.vol_h_cap],
        "minutes": args.minutes, "ticks": len(vh),
        "vol_h_percentiles": {str(p): q(p) for p in
                              (0.01, 0.05, 0.25, 0.50, 0.75, 0.95, 0.99)},
        "clamped_floor_frac": below, "clamped_cap_frac": above,
        "ladder_types": dict(type_counts),
        "ladder_mean_size": statistics.mean(ladder_sizes) if ladder_sizes else 0,
        "regimes": dict(regimes), "signals": signals,
    }, indent=2))
    print(f"\n  → {out}")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
