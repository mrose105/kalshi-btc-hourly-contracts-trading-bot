"""
Two-angle analysis prompted by a real 2026-08-05 paper loss: a snipe entered
at 10.2 min to expiry (B64750, 21 @ $0.23) rode to a full $0 settlement with
no exit tier ever touching it.

Angle A — stop coverage: kalshi_btc_bot/positions.py gates TIER 5.25
(boundary_risk) and TIER 6 (stop_loss / STOP_UNCOVERED_PCT catastrophe
floor) behind `if not is_snipe:` — snipes get NONE of that tier, regardless
of entry timing. The narrower "6-18 min entry timing gap" hypothesis raised
in conversation turned out to be a special case of this: snipes have zero
stop coverage at any entry time, not just inside a timing window.

Angle B — giveback: for every trade that had ANY positive peak (peak_pnl_pct
> 0), how much of that peak got given back before exit? Measures whether
peak_giveback's own tolerance (1 - PEAK_GIVEBACK_FRACTION) is the dominant
source of giveback, or whether tiers that aren't giveback-aware (stop_loss,
time_exit_OTM, expiry_settle) are bleeding real peaks with no floor at all.

Uses whatever backtest run is passed in — trades must include the
entry_hours/peak_pnl_pct/is_snipe fields added to _close()'s trade record
on 2026-08-05 (kalshi_btc_backtest.py). Re-run the backtest first if the
JSON predates that.

Usage:
    python3 exit_coverage_analysis.py results/backtest_20260805_1824.json
"""
import json
import sys
from collections import defaultdict


def load(path: str) -> list[dict]:
    d = json.load(open(path))
    trades = d["trades"]
    missing = [k for k in ("entry_hours", "peak_pnl_pct", "is_snipe") if k not in trades[0]]
    if missing:
        raise SystemExit(f"trades missing {missing} -- re-run the backtest to regenerate this file")
    return trades


def fmt_money(x: float) -> str:
    return f"${x:+,.2f}"


def angle_a_stop_coverage(trades: list[dict]) -> None:
    print("\n" + "=" * 70)
    print("ANGLE A -- stop/catastrophe-floor coverage by is_snipe")
    print("=" * 70)

    snipe = [t for t in trades if t["is_snipe"]]
    non_snipe = [t for t in trades if not t["is_snipe"]]

    protected_reasons = {"stop_loss", "boundary_risk"}
    snipe_protected = [t for t in snipe if t["reason"] in protected_reasons]
    print(f"\nSnipe trades: {len(snipe)}  |  entries protected by TIER 5.25/6: "
          f"{len(snipe_protected)}  <- should be 0 (tier is gated `not is_snipe`)")

    unprotected_loss_reasons = {"time_exit_OTM", "expiry_settle", "near_zero"}
    snipe_losses = [t for t in snipe if t["pnl"] < 0]
    snipe_unprot = [t for t in snipe_losses if t["reason"] in unprotected_loss_reasons]
    print(f"\nSnipe losing trades: {len(snipe_losses)}  "
          f"({sum(t['pnl'] for t in snipe_losses):+,.2f} total)")
    print(f"  of which exited via an unprotected tier {unprotected_loss_reasons}: "
          f"{len(snipe_unprot)}  ({sum(t['pnl'] for t in snipe_unprot):+,.2f} total)")
    if snipe_unprot:
        avg_pct = sum(t["pnl_pct"] for t in snipe_unprot) / len(snipe_unprot)
        worst = min(snipe_unprot, key=lambda t: t["pnl_pct"])
        print(f"  avg pnl_pct on those: {avg_pct:.1f}%   worst single trade: "
              f"{worst['ticker']} {worst['pnl_pct']:.1f}% ({fmt_money(worst['pnl'])})")

    non_snipe_losses = [t for t in non_snipe if t["pnl"] < 0]
    non_snipe_stopped = [t for t in non_snipe_losses if t["reason"] in protected_reasons]
    if non_snipe_stopped:
        avg_pct_ns = sum(t["pnl_pct"] for t in non_snipe_stopped) / len(non_snipe_stopped)
        print(f"\nFor contrast -- non-snipe losses caught by stop_loss/boundary_risk: "
              f"{len(non_snipe_stopped)}, avg pnl_pct {avg_pct_ns:.1f}%")
        print("  (this is the floor a snipe of the same size never gets)")

    # Timing-gap sub-angle: among snipes, does entry_hours matter at all if
    # the tier is unconditionally off? Report it anyway for completeness --
    # if unprotected losses cluster at short entry_hours, that's the riskiest
    # subset to prioritize if/when coverage is added.
    dated = [t for t in snipe_unprot if t["entry_hours"] is not None]
    if dated:
        dated.sort(key=lambda t: t["entry_hours"])
        print(f"\n  entry_hours distribution of unprotected snipe losses (n={len(dated)}):")
        buckets = defaultdict(lambda: [0, 0.0])
        for t in dated:
            mins = t["entry_hours"] * 60
            if mins < 6:
                b = "<6min"
            elif mins < 18:
                b = "6-18min"
            elif mins < 60:
                b = "18-60min"
            else:
                b = ">60min"
            buckets[b][0] += 1
            buckets[b][1] += t["pnl"]
        for b in ("<6min", "6-18min", "18-60min", ">60min"):
            if b in buckets:
                n, pnl = buckets[b]
                print(f"    {b:>10}: {n:>3} trades, {pnl:+,.2f}")


def angle_b_giveback(trades: list[dict]) -> None:
    print("\n" + "=" * 70)
    print("ANGLE B -- giveback: peak_pnl_pct reached vs. realized pnl_pct")
    print("=" * 70)

    had_peak = [t for t in trades if t["peak_pnl_pct"] > 0]
    print(f"\nTrades with a positive peak at some point: {len(had_peak)} / {len(trades)}")

    by_reason = defaultdict(list)
    for t in had_peak:
        by_reason[t["reason"]].append(t)

    print(f"\n{'reason':>16}  {'n':>4}  {'avg peak%':>9}  {'avg exit%':>9}  "
          f"{'avg giveback pp':>16}  {'capture ratio':>13}  {'total giveback $':>17}")
    total_giveback_dollar = 0.0
    for reason, ts in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        avg_peak = sum(t["peak_pnl_pct"] for t in ts) / len(ts)
        avg_exit = sum(t["pnl_pct"] for t in ts) / len(ts)
        avg_giveback_pp = avg_peak - avg_exit
        capture = avg_exit / avg_peak if avg_peak else 0.0
        giveback_dollar = sum(
            (t["peak_pnl_pct"] - t["pnl_pct"]) / 100 * t["entry"] * t["count"] for t in ts
        )
        total_giveback_dollar += giveback_dollar
        print(f"{reason:>16}  {len(ts):>4}  {avg_peak:>8.1f}%  {avg_exit:>8.1f}%  "
              f"{avg_giveback_pp:>15.1f}pp  {capture:>12.1%}  {giveback_dollar:>+16,.2f}")

    print(f"\nTotal giveback across all trades that ever had a peak: "
          f"{fmt_money(total_giveback_dollar)}")

    # Isolate the tiers that are NOT giveback-aware -- anything that isn't
    # peak_giveback/snipe_lock itself but still had a real peak run first.
    giveback_aware = {"peak_giveback", "snipe_lock", "gamma_lock"}
    blind = [t for t in had_peak if t["reason"] not in giveback_aware and t["peak_pnl_pct"] >= 20]
    if blind:
        blind_dollar = sum(
            (t["peak_pnl_pct"] - t["pnl_pct"]) / 100 * t["entry"] * t["count"] for t in blind
        )
        print(f"\nTrades that ran up >=20pp peak but exited via a tier with NO giveback "
              f"awareness ({giveback_aware} excluded): {len(blind)}")
        print(f"  total giveback on those: {fmt_money(blind_dollar)}")
        by_r = defaultdict(int)
        for t in blind:
            by_r[t["reason"]] += 1
        print(f"  by exit reason: {dict(by_r)}")


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: python3 exit_coverage_analysis.py <results/backtest_*.json>")
    trades = load(sys.argv[1])
    print(f"Loaded {len(trades)} trades from {sys.argv[1]}")
    angle_a_stop_coverage(trades)
    angle_b_giveback(trades)


if __name__ == "__main__":
    main()
