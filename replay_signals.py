"""
Replay find_best() vs find_snipe() against the real recorded quotes stream —
answers "which signal actually had more opportunities tonight" from real
ladder/regime data, not a single live snapshot or speculation.

Uses the real SignalEngine class (not reimplemented) and reconstructs the
regime dict from recorder.py's stored `rg` sub-record. `use_t` isn't stored
directly (recorder.py only keeps regime/direction/vol/vol_h/vol_ratio/
vol_compression/zscore/mom), but regime.py's RegimeEngine sets
use_t = (regime type != "RANGING") for every branch, so it's exactly
reconstructible from the stored regime string.

Usage:
    python3 replay_signals.py 2026-08-05
    python3 replay_signals.py 2026-08-04 2026-08-05   # multiple days
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from inspect_recording import load
from kalshi_btc_bot.model import DistModel
from kalshi_btc_bot.signals import SignalEngine


def regime_from_rg(rg: dict) -> dict:
    r = rg.get("r", "RANGING")
    return {
        "regime": r,
        "direction": rg.get("d", "NEUTRAL"),
        "use_t": r != "RANGING",
        "vol": rg.get("v", 0.0),
        "vol_h": rg.get("vh", 0.0),
        "vol_ratio": rg.get("vr", 1.0),
        "vol_compression": bool(rg.get("vc", False)),
        "zscore": rg.get("z", 0.0),
        "mom": rg.get("m", 0.0),
    }


def ladder_from_quote(q: dict) -> list[dict]:
    out = []
    for c in q.get("l", []):
        out.append({
            "ticker": c["tk"], "bid": c["b"], "ask": c["a"],
            "vol": c["v"], "hours": c["h"],
            "low": c["lo"], "high": c["hi"],
            "otm_dist": c["d"], "itm": c["itm"],
            "strike": (c["lo"] + c["hi"]) / 2 if c.get("lo") is not None else 0,
            "type": "RANGE",
        })
    return out


def main():
    days = sys.argv[1:] or ["2026-08-05"]
    dist = DistModel()
    sig_e = SignalEngine(dist)

    ticks = 0
    empty_ladder = 0
    best_fires = 0
    snipe_fires = 0
    both_fire = 0
    neither = 0

    for day in days:
        quotes = load("quotes", day)
        for q in quotes:
            ticks += 1
            ladder = ladder_from_quote(q)
            if not ladder:
                empty_ladder += 1
                continue
            regime = regime_from_rg(q.get("rg", {}))
            spot = q["spot"]
            vol = regime["vol"]
            best = sig_e.find_best(spot, vol, regime, ladder, {})
            snipe = sig_e.find_snipe(spot, vol, regime, ladder, {})
            b, s = best is not None, snipe is not None
            best_fires += b
            snipe_fires += s
            both_fire += (b and s)
            neither += (not b and not s)

    print(f"days: {days}")
    print(f"total quote ticks: {ticks}")
    print(f"  empty ladder (no contracts visible at all): {empty_ladder} "
          f"({empty_ladder/ticks*100:.0f}%)")
    non_empty = ticks - empty_ladder
    print(f"  non-empty ladder ticks: {non_empty}")
    if non_empty:
        print(f"\n  of non-empty ticks:")
        print(f"    find_best qualifies:  {best_fires:>6} ({best_fires/non_empty*100:.1f}%)")
        print(f"    find_snipe qualifies: {snipe_fires:>6} ({snipe_fires/non_empty*100:.1f}%)")
        print(f"    both qualify:         {both_fire:>6} ({both_fire/non_empty*100:.1f}%)")
        print(f"    neither qualifies:    {neither:>6} ({neither/non_empty*100:.1f}%)")


if __name__ == "__main__":
    main()
