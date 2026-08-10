"""
Counterfactual test: should the snipe peak_giveback floor be RELATIVE to
entry price instead of a fixed $0.20?

THE PROBLEM (two real live cases, 2026-08-04 and 2026-08-07):
A snipe entered at $0.08 ran to peak +112.5% (bid $0.17) and gave every cent
back, stopping out at $0.03. peak_giveback never fired because bid never
crossed SNIPE_PEAK_GIVEBACK_MIN_BID ($0.20). Neither did snipe_lock
(needs peak >= SNIPE_PROFIT_LOCK_PEAK = 150%, peak was 112.5%) nor
near_settlement (needs bid >= $0.75).

The floor is absolute, so what it demands depends on entry price:
    entry $0.08 -> needs +150% to reach a $0.20 bid
    entry $0.12 -> needs  +67%
    entry $0.20 -> needs    0%
Since SNIPE_MIN_ENTRY_PRICE is $0.10, the cheapest and most lottery-like
snipes -- the ones most in need of a giveback floor -- get the least
protection. That is backwards from the tier's intent.

HYPOTHESIS: replace the absolute floor with a multiple of entry price
(bid >= mult * entry), so protection scales with the position instead of
penalising cheap entries.

WHY A COUNTERFACTUAL AND NOT ANOTHER SWEEP:
docs/QUANT_STANDARDS_AUDIT.md sec 1b — chopping a compounding, Kelly-sized
backtest into tuning/validation windows mismeasures structural changes,
because a few diverted early trades change capital at every later trade and
the difference amplifies. Here the price path is EXOGENOUS: market data does
not respond to when we exit. So we capture each snipe's real per-tick bid
path once, then replay candidate exit rules against those same paths
offline. No compounding, no path divergence, one backtest run per window.

LIMITATION, stated plainly: a captured path ends at the position's ACTUAL
exit, so this can only evaluate rules that fire at or BEFORE that point. A
candidate floor is always <= the current $0.20, which makes peak_giveback
fire earlier or not at all -- never later -- so every rule tested here is
inside that envelope. A rule that would hold a position LONGER is not
measurable this way and none is tested.

Usage:
    python3 snipe_giveback_floor_counterfactual.py --start 2026-06-08 --end 2026-07-18
    python3 snipe_giveback_floor_counterfactual.py --selftest
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from kalshi_btc_bot import config as C
import kalshi_btc_backtest as B


def replay(path: list, entry: float, count: int, actual_pnl: float,
           floor_abs: float = None, floor_mult: float = None,
           floor_min_of: tuple = None) -> tuple:
    """Replay one snipe's captured tick path under a candidate giveback floor.

    Mirrors kalshi_btc_backtest.manage_exits TIER 0.75 exactly:
        (not itm) and peak_pnl_pct >= PEAK_GIVEBACK_MIN_PEAK
                  and bid >= floor
                  and pnl_pct <= peak_pnl_pct * PEAK_GIVEBACK_FRACTION
    For snipes every earlier tier is gated `not is_snipe`, so peak_giveback is
    the first tier that can fire -- if it triggers, it wins the decision.

    Returns (pnl, fired) — fired False means the rule never triggered, in
    which case the real outcome stands.
    """
    if entry <= 0:
        return actual_pnl, False
    if floor_min_of is not None:
        # min(absolute, mult*entry): never STRICTER than today's absolute floor,
        # but relaxes it for cheap entries where $0.20 is unreachably far above
        # the entry. A pure relative floor is not strictly better — on an
        # expensive entry (say $0.30) a 1.2x floor is $0.36, which blocks fades
        # the current $0.20 floor would catch.
        a, m = floor_min_of
        floor = min(a, m * entry)
    elif floor_mult is not None:
        floor = floor_mult * entry
    else:
        floor = floor_abs
    peak = entry
    for bid, itm in path:
        if bid > peak:
            peak = bid
        peak_pnl_pct = (peak - entry) / entry
        pnl_pct = (bid - entry) / entry
        if (not itm
                and peak_pnl_pct >= C.PEAK_GIVEBACK_MIN_PEAK
                and bid >= floor
                and pnl_pct <= peak_pnl_pct * C.PEAK_GIVEBACK_FRACTION):
            # same realized-fill haircut _close() applies to a market sale
            fill = bid * (1.0 - B._size_impact_penalty(count))
            return (fill - entry) * count, True
    return actual_pnl, False


def capture(start: str, end: str, capital: float) -> list:
    """Run the backtest once, capturing every snipe's per-tick (bid, itm) path."""
    paths = {}          # ticker -> list[(bid, itm)]
    done = []           # completed snipe records

    orig_manage = B.BacktestPortfolio.manage_exits
    orig_close = B.BacktestPortfolio._close

    def patched_manage(self, spot, bar_ts, settle_spot=None):
        for tk, pos in self.positions.items():
            if pos.get("is_no"):
                continue
            c = pos["contract"]
            itm = c["low"] <= spot < c["high"]
            paths.setdefault(tk, []).append((pos.get("bid_now", pos["entry"]), itm))
        return orig_manage(self, spot, bar_ts, settle_spot)

    def patched_close(self, ticker, bid, reason, bar_ts):
        pos = self.positions.get(ticker)
        snap = None
        if pos is not None and not pos.get("is_no"):
            snap = {"ticker": ticker, "entry": pos["entry"], "count": pos["count"],
                    "is_snipe": pos.get("is_snipe", False),
                    "path": list(paths.get(ticker, []))}
        n_before = len(self.trades)
        r = orig_close(self, ticker, bid, reason, bar_ts)
        if snap is not None and len(self.trades) > n_before:
            t = self.trades[-1]
            snap["actual_pnl"] = t["pnl"]
            snap["actual_reason"] = t["reason"]
            snap["peak_pnl_pct"] = t["peak_pnl_pct"]
            done.append(snap)
        paths.pop(ticker, None)
        return r

    B.BacktestPortfolio.manage_exits = patched_manage
    B.BacktestPortfolio._close = patched_close
    try:
        B.run_backtest(capital=capital, start_date=start, end_date=end,
                       verbose=False, use_kelly=True, use_vol_surface=False)
    finally:
        B.BacktestPortfolio.manage_exits = orig_manage
        B.BacktestPortfolio._close = orig_close
    return done


def evaluate(snipes: list, label: str) -> dict:
    cur_abs = C.SNIPE_PEAK_GIVEBACK_MIN_BID
    rules = ([("current  $%.2f abs" % cur_abs, {"floor_abs": cur_abs})]
             + [("abs      $%.2f" % v, {"floor_abs": v}) for v in (0.15, 0.12, 0.10, 0.05)]
             + [("rel      %.2fx entry" % m, {"floor_mult": m}) for m in (1.2, 1.3, 1.5, 1.8, 2.0)]
             + [("min($%.2f, %.2fx)" % (cur_abs, m), {"floor_min_of": (cur_abs, m)})
                for m in (1.2, 1.3, 1.5, 1.8, 2.0)])

    actual_total = sum(s["actual_pnl"] for s in snipes)
    print(f"\n=== {label} ===")
    n_snipe = sum(1 for s in snipes if s.get("is_snipe"))
    print(f"  positions captured: {len(snipes)} ({n_snipe} snipe / "
          f"{len(snipes)-n_snipe} non-snipe)   actual total P&L: {actual_total:+.2f}")
    import kalshi_btc_bot.config as _cc
    blocked = 0
    for s in snipes:
        e = s["entry"]
        if e <= 0 or not s["path"]:
            continue
        pk = max([b for b, _ in s["path"]] + [e])
        ppct = (pk - e) / e
        if ppct < _cc.PEAK_GIVEBACK_MIN_PEAK:
            continue
        trigger = e * (1 + ppct * _cc.PEAK_GIVEBACK_FRACTION)
        if trigger < cur_abs:
            blocked += 1
    print(f"  peaked >={_cc.PEAK_GIVEBACK_MIN_PEAK:.0%} but giveback trigger price sits BELOW "
          f"the ${cur_abs:.2f} floor -> peak_giveback could NEVER fire: {blocked}")
    print(f"\n  {'rule':>22} {'fired':>6} {'total P&L':>11} {'vs actual':>11}")
    out = {}
    for name, kw in rules:
        tot = 0.0
        fired = 0
        for s in snipes:
            pnl, f = replay(s["path"], s["entry"], s["count"], s["actual_pnl"], **kw)
            tot += pnl
            fired += f
        out[name] = tot
        print(f"  {name:>22} {fired:>6} {tot:>+11.2f} {tot-actual_total:>+11.2f}")
    return out


def selftest() -> int:
    fails = []

    def check(name, cond, detail=""):
        print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
        if not cond:
            fails.append(name)

    print("snipe_giveback_floor_counterfactual selftest")

    # the real 2026-08-07 case: entry $0.08, peak bid $0.17, faded to $0.03.
    # PEAK_GIVEBACK_FRACTION 0.75 -> trigger once pnl <= 0.75*peak_pnl.
    path = [(0.08, False), (0.11, False), (0.17, False), (0.14, False),
            (0.10, False), (0.06, False), (0.03, False)]
    actual = (0.03 - 0.08) * 62

    pnl_cur, fired_cur = replay(path, 0.08, 62, actual, floor_abs=0.20)
    check("current $0.20 floor never fires on a $0.17 peak",
          not fired_cur and pnl_cur == actual)

    pnl_15, fired_15 = replay(path, 0.08, 62, actual, floor_abs=0.15)
    check("$0.15 floor also misses (trigger price is below it)", not fired_15,
          f"fired={fired_15}")

    pnl_10, fired_10 = replay(path, 0.08, 62, actual, floor_abs=0.10)
    check("$0.10 floor fires and beats the real outcome",
          fired_10 and pnl_10 > actual, f"pnl={pnl_10:.2f} vs {actual:.2f}")

    pnl_rel, fired_rel = replay(path, 0.08, 62, actual, floor_mult=1.3)
    check("1.3x-entry floor ($0.104) fires and beats the real outcome",
          fired_rel and pnl_rel > actual, f"pnl={pnl_rel:.2f}")

    # a rule must never fire while ITM (snipes on the settlement path are exempt)
    itm_path = [(0.08, True), (0.30, True), (0.10, True)]
    _, fired_itm = replay(itm_path, 0.08, 10, 0.0, floor_mult=1.2)
    check("never fires while ITM", not fired_itm)

    # a peak below PEAK_GIVEBACK_MIN_PEAK must not trigger anything
    flat = [(0.10, False), (0.11, False), (0.10, False)]
    _, fired_flat = replay(flat, 0.10, 10, 0.0, floor_mult=1.0)
    check("no fire when peak < PEAK_GIVEBACK_MIN_PEAK", not fired_flat)

    # A PURE relative floor is NOT strictly better: on an expensive entry it is
    # STRICTER than the absolute floor and blocks a fade the current rule
    # catches. entry $0.30, peak $0.50, fade to $0.32 -> trigger needs
    # pnl <= 0.75*peak_pnl, satisfied at $0.32; abs floor $0.20 passes, but a
    # 1.2x floor is $0.36 and blocks it. This is why min(abs, mult) is tested.
    hi = [(0.30, False), (0.50, False), (0.32, False)]
    _, f_abs = replay(hi, 0.30, 10, 0.0, floor_abs=0.20)
    _, f_rel = replay(hi, 0.30, 10, 0.0, floor_mult=1.2)
    _, f_min = replay(hi, 0.30, 10, 0.0, floor_min_of=(0.20, 1.2))
    check("pure relative floor is STRICTER on expensive entries",
          f_abs and not f_rel, f"abs={f_abs} rel={f_rel}")
    check("min(abs, mult) keeps the expensive-entry exit the absolute floor got",
          f_min, f"min={f_min}")

    # and min(abs, mult) still rescues the cheap-entry case
    _, f_min_cheap = replay(path, 0.08, 62, actual, floor_min_of=(0.20, 1.3))
    check("min(abs, mult) also fires on the cheap-entry case", f_min_cheap)

    # degenerate entry
    _, f_zero = replay(path, 0.0, 10, -1.0, floor_mult=1.5)
    check("zero entry -> no fire, returns actual", not f_zero)

    print(f"\n{len(fails)} failure(s)" if fails else "\nall checks passed")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2026-06-12")
    ap.add_argument("--end", default="2026-07-22")
    ap.add_argument("--valid-start", default="2026-07-22")
    ap.add_argument("--valid-end", default="2026-08-10")
    ap.add_argument("--capital", type=float, default=500.0)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(selftest())

    print(f"Snipe peak_giveback floor counterfactual  (capital ${args.capital:,.0f})")
    print(f"  current SNIPE_PEAK_GIVEBACK_MIN_BID = ${C.SNIPE_PEAK_GIVEBACK_MIN_BID:.2f}")

    tune = capture(args.start, args.end, args.capital)
    r_tune = evaluate(tune, f"TUNING  {args.start} -> {args.end}")

    valid = capture(args.valid_start, args.valid_end, args.capital)
    r_valid = evaluate(valid, f"VALIDATION  {args.valid_start} -> {args.valid_end} (held out)")

    best_t = max(r_tune, key=r_tune.get)
    best_v = max(r_valid, key=r_valid.get)
    print(f"\n{'='*64}\nREAD\n{'='*64}")
    print(f"  best on tuning:     {best_t}")
    print(f"  best on validation: {best_v}")
    if best_t == best_v:
        print("  -> SAME rule wins both windows. Real signal, not an in-sample fit.")
    else:
        print("  -> Different rule wins out-of-sample; the tuning winner did NOT")
        print("     generalize. Treat the tuning ranking as noise.")
    print("\n  Per-position replay against exogenous price paths: no compounding,")
    print("  so these deltas are the rule's own effect (see module docstring).")


if __name__ == "__main__":
    main()
