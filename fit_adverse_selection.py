"""
Compare the backtest's hand-tuned adverse-selection haircut (_exit_bid() in
kalshi_btc_backtest.py) against real recorded market bids, to eventually
replace a guessed formula with an empirically-grounded one.

Why this exists: docs/BACKTEST_INTEGRITY.md section 3 identifies the
backtest's exit pricing as the unfixed root cause behind its inflated
returns — exits price off _exit_bid(), the bot's own model plus a hand-tuned
discount, not a recorded book. recorder.py (shipped 2026-07-28) exists to
close that gap by recording the real bid at every position-management tick
(the `marks` stream). This script is the consumer: once there is enough
real data, it replaces "we guessed a 15%-max discount curve" with "here is
what the market actually paid."

Deliberately NOT a curve fit. A parametric fit (even a simple polynomial)
over what will realistically remain a small sample for a solo bot risks
being exactly the kind of overfitting this whole audit exists to catch —
a handful of position lifecycles dressed up as a smooth, confident-looking
function. Instead this bins observations into a small (hours-to-expiry x
true_prob-extremity) grid and reports the empirical mean/std/count per cell,
refusing to report any cell that doesn't have enough INDEPENDENT positions
behind it (not just enough ticks — adjacent ticks of the same held position
are highly autocorrelated, not independent draws).

Gated by design: run() refuses to produce comparison output at all until
MIN_INDEPENDENT_POSITIONS distinct positions have been recorded AND they
span a wide enough hours-to-expiry range to say anything about the regime
the haircut actually targets (near expiry). Run it any time — it will
either show a real comparison or explain exactly what's still missing.

Usage:
    python3 fit_adverse_selection.py              # status + comparison if ready
    python3 fit_adverse_selection.py --selftest    # correctness checks, no data needed
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from inspect_recording import load, available_dates
from kalshi_btc_backtest import _exit_bid

# Minimum distinct positions before ANY binned comparison is reported. Chosen
# so that even a coarse HOURS_BUCKETS x EXTREMITY_BUCKETS grid (12 cells
# below) can plausibly get a handful of independent positions per cell rather
# than being dominated by 2-3 position lifecycles' worth of autocorrelated
# ticks pretending to be a real sample.
MIN_INDEPENDENT_POSITIONS = 50
# Require real coverage out to at least half of MAX_HOURS (4h) — a sample
# concentrated entirely in the last 30 minutes (as of 2026-08-03: yes) cannot
# say anything about the haircut's behavior across the regime it's meant to
# cover, no matter how many ticks it contains.
MIN_HOURS_COVERAGE = 2.0
# Minimum independent positions within a single grid cell before that cell's
# empirical numbers are reported at all, vs marked insufficient.
MIN_POSITIONS_PER_CELL = 5

HOURS_BUCKETS = [(0.0, 0.05), (0.05, 0.15), (0.15, 0.5), (0.5, 4.0)]
EXTREMITY_BUCKETS = [(0.0, 0.15), (0.15, 0.35), (0.35, 0.5)]


def _bucket(val: float, buckets: list[tuple]) -> int | None:
    for i, (lo, hi) in enumerate(buckets):
        if lo <= val < hi or (i == len(buckets) - 1 and val == hi):
            return i
    return None


def load_all_marks() -> list[dict]:
    rows = []
    for d in available_dates():
        rows.extend(load("marks", d))
    return rows


def independent_positions(marks: list[dict]) -> dict[str, list[dict]]:
    """Group marks by ticker. Each group is ONE position lifecycle — the
    independent unit for sample-size purposes, not the tick count."""
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for m in marks:
        by_ticker[m["tk"]].append(m)
    return by_ticker


def coverage_report(positions: dict[str, list[dict]]) -> dict:
    all_hours = [m["h"] * 60 for group in positions.values() for m in group]
    return {
        "n_positions": len(positions),
        "n_ticks": sum(len(g) for g in positions.values()),
        "min_minutes": min(all_hours) if all_hours else None,
        "max_minutes": max(all_hours) if all_hours else None,
        "max_hours_covered": (max(all_hours) / 60) if all_hours else 0.0,
    }


def gate(positions: dict[str, list[dict]]) -> tuple[bool, str]:
    cov = coverage_report(positions)
    if cov["n_positions"] < MIN_INDEPENDENT_POSITIONS:
        return False, (
            f"only {cov['n_positions']} independent position(s) recorded "
            f"(need >= {MIN_INDEPENDENT_POSITIONS}). {cov['n_ticks']} raw "
            f"ticks exist, but adjacent ticks of the same held position are "
            f"autocorrelated, not independent — they do not substitute for "
            f"more positions."
        )
    if cov["max_hours_covered"] < MIN_HOURS_COVERAGE:
        return False, (
            f"recorded positions only cover up to {cov['max_hours_covered']:.2f}h "
            f"to expiry (need >= {MIN_HOURS_COVERAGE}h). The haircut's behavior "
            f"in the regime it's meant for (longer-dated, less certain) is "
            f"unobserved."
        )
    return True, "ok"


def compare(positions: dict[str, list[dict]]) -> None:
    """Bin every mark into (hours-bucket, extremity-bucket) and report the
    empirical residual (real bid - what _exit_bid() would have predicted)
    per cell, using each POSITION's mean residual as one observation (so a
    long-held position doesn't get many votes just for having many ticks)."""
    cell_obs: dict[tuple, list[float]] = defaultdict(list)
    cell_positions: dict[tuple, set] = defaultdict(set)

    for tk, group in positions.items():
        # Per-position, per-cell mean residual, so one position contributes
        # at most one data point to a given cell regardless of tick count.
        per_cell: dict[tuple, list[float]] = defaultdict(list)
        for m in group:
            tp, hours, bid = m.get("tp"), m.get("h"), m.get("b")
            if tp is None or hours is None or bid is None or bid <= 0 or tp <= 0:
                continue
            hb = _bucket(hours, HOURS_BUCKETS)
            eb = _bucket(abs(tp - 0.5), EXTREMITY_BUCKETS)
            if hb is None or eb is None:
                continue
            predicted = _exit_bid(tp, hours)
            residual_frac = (bid - predicted) / tp
            per_cell[(hb, eb)].append(residual_frac)
        for cell, vals in per_cell.items():
            cell_obs[cell].append(sum(vals) / len(vals))
            cell_positions[cell].add(tk)

    print(f"\n{'hrs-to-expiry':<16}{'|true_p-0.5|':<16}{'n_pos':>7}{'mean resid%':>13}"
          f"{'std':>8}")
    for hb, (hlo, hhi) in enumerate(HOURS_BUCKETS):
        for eb, (elo, ehi) in enumerate(EXTREMITY_BUCKETS):
            cell = (hb, eb)
            n = len(cell_positions.get(cell, ()))
            label_h = f"{hlo*60:.0f}-{hhi*60:.0f}m" if hhi < 4.0 else f">{hlo*60:.0f}m"
            label_e = f"{elo:.2f}-{ehi:.2f}"
            if n < MIN_POSITIONS_PER_CELL:
                print(f"{label_h:<16}{label_e:<16}{n:>7}  insufficient (need >= {MIN_POSITIONS_PER_CELL})")
                continue
            vals = cell_obs[cell]
            mean = sum(vals) / len(vals)
            std = (sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5 if len(vals) > 1 else 0.0
            print(f"{label_h:<16}{label_e:<16}{n:>7}{mean*100:>12.1f}%{std*100:>8.1f}%")
    print(
        "\n  mean resid% > 0  => real bids pay BETTER than _exit_bid() assumes "
        "(current haircut too aggressive in that cell)\n"
        "  mean resid% < 0  => real bids pay WORSE (current haircut too lax)"
    )


def _selftest() -> bool:
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        ok = ok and cond

    # _exit_bid import sanity: at true_p=0.5 (no extremeness), the discount
    # term is zero regardless of hours_left, so bid should equal
    # true_p - spread/2 exactly.
    from kalshi_btc_backtest import _exit_spread
    b = _exit_bid(0.5, 1.0)
    expected = 0.5 - _exit_spread(0.5, 1.0) / 2
    check("_exit_bid(0.5, *) has zero adverse-selection discount",
          abs(b - expected) < 1e-9)

    # Near-expiry + extreme true_p should discount MORE than mid-hours +
    # extreme true_p (tau_penalty grows toward expiry).
    near = _exit_bid(0.95, 0.01)
    mid = _exit_bid(0.95, 0.4)
    check("discount grows closer to expiry", near < mid)

    # Bucket assignment: boundaries and out-of-range.
    check("bucket() finds the right hours bucket",
          _bucket(0.03, HOURS_BUCKETS) == 0 and _bucket(1.0, HOURS_BUCKETS) == 3)
    check("bucket() returns None outside range", _bucket(-1, HOURS_BUCKETS) is None)

    # Gate refuses below threshold, allows above it — synthetic positions.
    few = {f"T{i}": [{"h": 0.05, "tp": 0.6, "b": 0.5}] for i in range(5)}
    ok_few, _ = gate(few)
    check("gate refuses with too few positions", ok_few is False)

    many = {}
    for i in range(MIN_INDEPENDENT_POSITIONS + 5):
        h = 0.01 + (i % 10) * 0.3  # spread across a wide hours range
        many[f"T{i}"] = [{"h": h, "tp": 0.6, "b": 0.5}]
    ok_many, msg = gate(many)
    check(f"gate allows with enough positions + coverage ({msg})", ok_many is True)

    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        print("Self-check: correctness properties\n")
        sys.exit(0 if _selftest() else 1)

    marks = load_all_marks()
    if not marks:
        sys.exit("No recorded marks found. Run the bot with KALSHI_RECORD=1 first.")

    positions = independent_positions(marks)
    cov = coverage_report(positions)
    print(f"Recorded so far: {cov['n_positions']} independent position(s), "
          f"{cov['n_ticks']} ticks, covering up to {cov['max_hours_covered']:.2f}h "
          f"to expiry.")

    ready, msg = gate(positions)
    if not ready:
        print(f"\nNot enough data yet: {msg}")
        print("\nNo comparison produced — this is intentional. Fitting anything on "
              "too little data would just be a smaller-scale repeat of the "
              "overfitting problem this whole audit exists to catch.")
        return

    print("\nEnough data to compare. Empirical residual = real bid minus what "
          "_exit_bid() would have predicted, as a fraction of true_prob:")
    compare(positions)


if __name__ == "__main__":
    main()
