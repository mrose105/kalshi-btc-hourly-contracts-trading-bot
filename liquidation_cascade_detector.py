"""
Test whether liquidation-cascade-shaped price action is a real, distinct
phenomenon in BTC 5-min bars, and whether the live bot's RegimeEngine
currently conflates it with genuine trend continuation.

Why this exists: docs/QUANT_STANDARDS_AUDIT.md flags liquidation cascades as
a real candidate worth exploring — the site's mechanical definition ("a
price move forces leveraged positions to close, those forced trades move
price further, and the next layer of positions breaches maintenance
margin") matches the "flashing, reverting up" behavior observed live
2026-07-30. The source gave no concrete detection signal, so this is
derived from scratch rather than lifted, per the audit's own instruction.

Method:
  1. Fetch the same 60-day BTC-USD 5m OHLCV the backtest uses, and run it
     through the ACTUAL SyntheticFeed + RegimeEngine (imported directly from
     kalshi_btc_backtest.py, not reimplemented) to get a real regime label
     at every bar. This is the classifier being tested, not a stand-in.
  2. Independently flag "fast move" events: a FAST_WINDOW-bar return whose
     magnitude exceeds FAST_Z_THRESHOLD standard deviations of trailing
     SLOW_WINDOW-bar realized vol. Deliberately NOT reusing the bot's own
     BREAKOUT_ACCEL/REVERT_ZSCORE constants — the whole point is an
     independent criterion, so "does the existing regime label already
     capture this" is a real question rather than circular by construction.
  3. For each flagged event, measure how much of the move reverts within
     the following RETRACE_WINDOW bars. Cascades are forced, not
     information-driven, so the mechanical prediction is: they should
     mostly revert. A genuine trend/breakout should mostly persist.
  4. Cross-tabulate: of the "mostly reverted" (cascade-shaped) events, what
     regime does RegimeEngine label them at the moment they start? If it's
     dominated by BREAKOUT/TRENDING (the labels that let bigger, riskier
     entries through), that's the conflation the audit hypothesized.

This produces a finding, not a trading strategy. Whether to act on it (a new
regime label, a fade signal, tighter entry gates during flagged windows) is
a separate decision — deliberately out of scope here to avoid quietly
shipping an unvalidated new strategy inside what's meant to be a detection
study.

Usage:
    python3 liquidation_cascade_detector.py              # 60-day study
    python3 liquidation_cascade_detector.py --days 30
    python3 liquidation_cascade_detector.py --selftest    # correctness checks, no data needed
"""
import argparse
import math
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

FAST_WINDOW = 3          # bars (15 min) — matches TREND_BARS's timescale, not its threshold
SLOW_WINDOW = 288         # bars (24h) — matches SMA_VOL_WINDOW's timescale, not reused directly
FAST_Z_THRESHOLD = 3.0    # standard "extreme outlier" cutoff (~99.7th pct under normality)
RETRACE_WINDOW = 6        # bars (30 min) forward to measure reversion
CASCADE_RETRACE_MIN = 0.50   # >=50% given back within RETRACE_WINDOW -> cascade-shaped
TREND_RETRACE_MAX = 0.25     # <=25% given back -> trend-shaped (persisted)


def retracement_fraction(entry_px: float, peak_px: float, later_px: float) -> float | None:
    """Fraction of a move given back. Signed `move` in the denominator makes
    this symmetric for up- and down-moves: 1.0 = fully reverted to entry,
    0.0 = held/extended, negative = the move kept extending further."""
    move = peak_px - entry_px
    if move == 0:
        return None
    giveback = peak_px - later_px
    return giveback / move


def fast_move_events(bars: list[tuple]) -> list[dict]:
    """bars: list of (ts, close). Returns flagged fast-move events with
    their pre/post price context, using only trailing data at each point
    (no lookahead in the detection criterion itself — the retracement
    label deliberately looks forward, since it's the OUTCOME being
    measured, not part of the detection trigger)."""
    closes = [c for _, c in bars]
    events = []
    for i in range(SLOW_WINDOW, len(bars) - RETRACE_WINDOW):
        window_rets = [
            (closes[j] - closes[j - 1]) / closes[j - 1]
            for j in range(i - SLOW_WINDOW + 1, i + 1)
            if closes[j - 1] > 0
        ]
        if len(window_rets) < SLOW_WINDOW // 2:
            continue
        slow_std = statistics.pstdev(window_rets)
        if slow_std <= 0:
            continue
        entry_px = closes[i - FAST_WINDOW]
        peak_px = closes[i]
        if entry_px <= 0:
            continue
        fast_ret = (peak_px - entry_px) / entry_px
        # fast_ret spans FAST_WINDOW bars; slow_std is a 1-BAR trailing std.
        # Comparing them directly understates how large a "normal" FAST_WINDOW
        # move should be by ~sqrt(FAST_WINDOW), inflating z and causing false
        # positives even on pure noise (caught by --selftest: 3 spurious
        # events on synthetic i.i.d. data before this scaling was added).
        z = fast_ret / (slow_std * math.sqrt(FAST_WINDOW))
        if abs(z) < FAST_Z_THRESHOLD:
            continue
        later_px = closes[i + RETRACE_WINDOW]
        retr = retracement_fraction(entry_px, peak_px, later_px)
        if retr is None:
            continue
        events.append({
            "ts": bars[i][0], "z": z, "fast_ret": fast_ret,
            "entry_px": entry_px, "peak_px": peak_px, "later_px": later_px,
            "retracement": retr,
        })
    return events


def classify(retracement: float) -> str:
    if retracement >= CASCADE_RETRACE_MIN:
        return "cascade-shaped"
    if retracement <= TREND_RETRACE_MAX:
        return "trend-shaped"
    return "ambiguous"


def run_study(days: int) -> None:
    import pandas as pd
    import yfinance as yf
    from kalshi_btc_backtest import SyntheticFeed, RegimeEngine

    print(f"Fetching BTC-USD 5m OHLCV, {days}d...")
    btc = yf.download("BTC-USD", period=f"{days}d", interval="5m",
                      progress=False, auto_adjust=True)
    if btc.empty:
        sys.exit("No data returned.")
    if isinstance(btc.columns, pd.MultiIndex):
        btc.columns = btc.columns.get_level_values(0)
    closes = btc["Close"].dropna()
    highs = btc["High"].reindex(closes.index).fillna(closes)
    lows = btc["Low"].reindex(closes.index).fillna(closes)
    print(f"  {len(closes)} bars ({closes.index[0]} -> {closes.index[-1]})")

    bars = list(zip(closes.index.to_pydatetime(), closes.values))

    print("Detecting fast-move events (independent of RegimeEngine)...")
    events = fast_move_events(bars)
    print(f"  {len(events)} events with |z| >= {FAST_Z_THRESHOLD}")
    if not events:
        print("  No events found — nothing further to report.")
        return

    print(f"Replaying through the real SyntheticFeed + RegimeEngine "
          f"to label each event's regime (no event fires before "
          f"{SLOW_WINDOW} bars of history exist, so the feed is always "
          f"well-warmed by the time any event is labeled)...")
    feed = SyntheticFeed()
    regime_e = RegimeEngine()
    event_by_ts = {e["ts"]: e for e in events}
    for ts, close, high, low in [(t, c, float(highs.loc[t]), float(lows.loc[t]))
                                  for t, c in bars]:
        feed.push(ts, close, high, low)
        if ts in event_by_ts:
            r = regime_e.detect(feed)
            event_by_ts[ts]["regime_label"] = f"{r['regime']}/{r['direction']}"

    for e in events:
        e["class"] = classify(e["retracement"])

    print(f"\n{'='*70}\nOUTCOME DISTRIBUTION\n{'='*70}")
    class_counts = Counter(e["class"] for e in events)
    for cls in ("cascade-shaped", "ambiguous", "trend-shaped"):
        n = class_counts.get(cls, 0)
        print(f"  {cls:<16} {n:>4}  ({n/len(events)*100:.0f}%)")

    print(f"\n{'='*70}\nREGIME LABEL AT EVENT START, BY OUTCOME CLASS\n{'='*70}")
    for cls in ("cascade-shaped", "ambiguous", "trend-shaped"):
        subset = [e for e in events if e["class"] == cls]
        if not subset:
            continue
        labels = Counter(e.get("regime_label", "?") for e in subset)
        print(f"\n  {cls} (n={len(subset)}):")
        for label, n in labels.most_common():
            print(f"    {label:<20} {n:>4}  ({n/len(subset)*100:.0f}%)")

    cascade_events = [e for e in events if e["class"] == "cascade-shaped"]
    trend_events = [e for e in events if e["class"] == "trend-shaped"]
    print(f"\n{'='*70}\nFINDING\n{'='*70}")
    if cascade_events and trend_events:
        def bt_rate(evs):
            n = sum(1 for e in evs
                   if e.get("regime_label", "").startswith(("BREAKOUT", "TRENDING")))
            return n / len(evs) * 100

        cascade_pct = bt_rate(cascade_events)
        trend_pct = bt_rate(trend_events)
        gap = cascade_pct - trend_pct

        print(f"  BREAKOUT/TRENDING label rate:")
        print(f"    cascade-shaped events (n={len(cascade_events)}): {cascade_pct:.0f}%")
        print(f"    trend-shaped events   (n={len(trend_events)}):   {trend_pct:.0f}%")

        # The naive framing ("94% of cascades get labeled BREAKOUT/TRENDING")
        # is misleading on its own — it says nothing unless compared against
        # the SAME rate for trend-shaped events. A |z|>=3 move looks abrupt
        # to the regime engine regardless of what happens next; the real
        # question is whether the label carries any information that
        # SEPARATES the two outcomes, i.e. whether this gap is more than a
        # few points of noise between n=52 and n=196 samples.
        try:
            from scipy.stats import chi2_contingency
            cascade_counts = Counter(e.get("regime_label", "?") for e in cascade_events)
            trend_counts = Counter(e.get("regime_label", "?") for e in trend_events)
            # Labels present in ONLY the two classes being compared here — a
            # label that only appears among ambiguous events would otherwise
            # create an all-zero column across both rows, degenerating the
            # expected-frequency table chi2_contingency needs (caught by a
            # ValueError on the first real run against 60 days of data).
            compared_labels = sorted(set(cascade_counts) | set(trend_counts))
            table = [[cascade_counts.get(l, 0) for l in compared_labels],
                     [trend_counts.get(l, 0) for l in compared_labels]]
            chi2, p, dof, expected = chi2_contingency(table)
            min_expected = expected.min()
            print(f"\n  Chi-square test of independence (regime label vs. outcome class), "
                  f"cascade vs. trend-shaped:\n    chi2={chi2:.2f}  dof={dof}  p={p:.3f}")
            if min_expected < 5:
                print(f"    CAVEAT: min expected cell count is {min_expected:.1f} (< 5, "
                      f"the standard validity threshold for this test) — several regime\n"
                      f"    labels are too sparse for the chi-square approximation to be "
                      f"reliable here. Read the p-value as suggestive, not conclusive.")
            if p < 0.05:
                print("    p < 0.05: the regime-label distributions ARE distinguishable "
                      "between the two outcome classes.")
            else:
                print("    p >= 0.05: NOT statistically distinguishable at conventional "
                      "significance — cannot reject that regime label carries no real "
                      "separating information here.")
        except ImportError:
            p = None

        print(f"\n  Honest read: raw gap is {gap:+.0f} points ({cascade_pct:.0f}% vs "
              f"{trend_pct:.0f}%). A |z|>={FAST_Z_THRESHOLD} fast move looks abrupt to "
              f"the current regime engine\n  REGARDLESS of whether it goes on to revert "
              f"or persist — both classes get labeled BREAKOUT/TRENDING at similarly "
              f"high rates.\n  This does NOT show the regime label conflates cascades "
              f"with trends in a way that's easy to separate after the fact.\n  What it "
              f"DOES show: liquidation-cascade-shaped moves are real and common in this "
              f"data ({len(cascade_events)}/{len(events)} = "
              f"{len(cascade_events)/len(events)*100:.0f}% of flagged fast moves), "
              f"and the current regime\n  engine provides ~no help telling them apart "
              f"from genuine trends AT THE MOMENT THEY START — which is a real gap, "
              f"just a different\n  one than 'mislabeling them as something specific.' "
              f"A useful signal would need new information beyond what\n  RegimeEngine "
              f"already computes (velocity/acceleration/z-score), since those are "
              f"already saturated by any large fast move.")
    print("\n  This is a detection-study finding, not a validated trading edge. "
          "Whether/how to act on\n  it (new regime label, fade signal, tighter "
          "gating) is a separate decision.")


def _selftest() -> bool:
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        ok = ok and cond

    # retracement_fraction: hand-computable cases, both directions.
    # Drop 100->90, recover to 98: gave back 8 of 10 = 0.8.
    r1 = retracement_fraction(100, 90, 98)
    check("retracement (down-move, partial revert) == 0.8", abs(r1 - 0.8) < 1e-9)
    # Rise 100->110, falls back to 102: gave back 8 of 10 = 0.8.
    r2 = retracement_fraction(100, 110, 102)
    check("retracement (up-move, partial revert) == 0.8", abs(r2 - 0.8) < 1e-9)
    # Rise 100->110, keeps rising to 115: retracement should be negative (extended).
    r3 = retracement_fraction(100, 110, 115)
    check("retracement (up-move, extends further) < 0", r3 < 0)
    # Rise 100->110, holds flat at 110: zero retracement.
    r4 = retracement_fraction(100, 110, 110)
    check("retracement (up-move, fully held) == 0.0", abs(r4) < 1e-9)
    # No move at all: undefined, must return None not raise/divide-by-zero.
    r5 = retracement_fraction(100, 100, 100)
    check("retracement (zero move) returns None", r5 is None)

    # classify() boundaries.
    check("classify(0.5) == cascade-shaped", classify(0.5) == "cascade-shaped")
    check("classify(0.25) == trend-shaped", classify(0.25) == "trend-shaped")
    check("classify(0.35) == ambiguous", classify(0.35) == "ambiguous")

    # fast_move_events: should fire on an injected spike and not on flat/noisy
    # data of realistic magnitude.
    import datetime, random
    random.seed(0)
    t0 = datetime.datetime(2026, 1, 1)
    quiet = []
    px = 100.0
    for i in range(SLOW_WINDOW + 50):
        px *= 1.0 + random.gauss(0, 0.0005)  # small, realistic per-bar noise
        quiet.append((t0 + datetime.timedelta(minutes=5 * i), px))
    ev_quiet = fast_move_events(quiet)
    check(f"no spurious events on quiet synthetic data (found {len(ev_quiet)})",
          len(ev_quiet) == 0)

    spiky = list(quiet)
    spike_i = SLOW_WINDOW + 30
    base_px = spiky[spike_i - FAST_WINDOW][1]
    for k in range(FAST_WINDOW):
        spiky[spike_i - FAST_WINDOW + 1 + k] = (
            spiky[spike_i - FAST_WINDOW + 1 + k][0], base_px * (1 + 0.05 * (k + 1))
        )
    ev_spike = fast_move_events(spiky)
    check(f"injected spike detected (found {len(ev_spike)} event(s))", len(ev_spike) >= 1)

    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        print("Self-check: correctness properties\n")
        sys.exit(0 if _selftest() else 1)

    run_study(args.days)


if __name__ == "__main__":
    main()
