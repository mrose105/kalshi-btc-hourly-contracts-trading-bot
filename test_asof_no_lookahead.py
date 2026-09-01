"""Nothing in a research result may be decided by data from the future.

Pins the two as-of resolvers against the defects found in the 2026-09-01 audit
of wing_calibration.py:

  join_regimes()  took the NEAREST regime snapshot, so a snapshot from the
                  future was chosen whenever it happened to be closer — 96.5%
                  of 38,583 joins on 2026-08-30. zscore/regime/mom from that
                  join decide whether find_boundary_no() fires.

  spot_at()       took the NEAREST spot to close_time, so a price printed AFTER
                  the close could resolve the outcome — 12 of 24 closes on the
                  same day.

Both leaks were sub-second on clean recordings, which is exactly why they
survived review. Neither is bounded across a recording gap, and this repo has
gaps routinely (18.6 minutes on 2026-08-31). These tests use gaps deliberately.

Run:  python3 test_asof_no_lookahead.py
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from boundary_no_quote_replay import join_regimes
from wing_calibration import (
    MIN_CLUSTERS,
    percentile_bootstrap_interval,
    side_for,
    spot_at,
)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('' if cond else ': ' + detail)}")


def _q(t, z):
    return {"t": t, "rg": {"r": "REVERTING", "d": "UP", "v": 1e-4, "z": z, "m": 0.0}}


# ── 1. regime join ignores the future ────────────────────────────────────────
# Tick at 12:00:02. A regime BEFORE it says z=0.10 (no signal). A regime AFTER
# it says z=3.00 (would fire). "Nearest" picks the future one; as-of must not.
uni = [{"t": "2026-08-30T12:00:02+00:00", "spot": 78000.0, "m": []}]
quotes = [_q("2026-08-30T12:00:00+00:00", 0.10),
          _q("2026-08-30T12:00:03+00:00", 3.00)]
joined = join_regimes(uni, quotes, tolerance_secs=5)
check("regime join takes the PRIOR snapshot, not the nearer future one",
      len(joined) == 1 and joined[0]["rg"]["z"] == 0.10,
      f"got {joined[0]['rg']['z'] if joined else 'nothing'}")

# A snapshot exactly contemporaneous is not the future.
joined = join_regimes([{"t": "2026-08-30T12:00:00+00:00", "spot": 1.0, "m": []}],
                      [_q("2026-08-30T12:00:00+00:00", 1.23)], tolerance_secs=5)
check("an exactly-equal timestamp counts as known",
      len(joined) == 1 and joined[0]["rg"]["z"] == 1.23)

# Across a gap, a stale prior snapshot is dropped rather than back-filled.
joined = join_regimes([{"t": "2026-08-30T12:20:00+00:00", "spot": 1.0, "m": []}],
                      [_q("2026-08-30T12:00:00+00:00", 1.0)], tolerance_secs=5)
check("a regime older than tolerance is dropped, not back-filled",
      joined == [], f"got {len(joined)} rows")

# No prior snapshot at all -> dropped, never reaching forward.
joined = join_regimes([{"t": "2026-08-30T12:00:00+00:00", "spot": 1.0, "m": []}],
                      [_q("2026-08-30T12:00:01+00:00", 9.0)], tolerance_secs=5)
check("with only a future snapshot the row is dropped", joined == [])


# ── 2. settlement ignores the future ─────────────────────────────────────────
# Close at t=100. Prior spot 78,000 (band [78000,78100) -> YES). A post-close
# print at t=100.4 of 77,999 would flip it OUT of the band.
ts = [98.0, 100.4]
sp = [78000.0, 77999.0]
check("settlement uses the last spot AT OR BEFORE close",
      spot_at(ts, sp, 100.0, 120.0) == 78000.0,
      f"got {spot_at(ts, sp, 100.0, 120.0)}")

lo, hi = 78000.0, 78100.0
resolved = spot_at(ts, sp, 100.0, 120.0)
check("the post-close print does not flip band membership",
      lo <= resolved < hi)

check("a spot staler than tolerance is unresolved, not guessed",
      spot_at([0.0], [1.0], 500.0, 120.0) is None)
check("no prior sample at all resolves to None",
      spot_at([200.0], [1.0], 100.0, 120.0) is None)
check("an exactly-at-close sample is usable",
      spot_at([100.0], [55.0], 100.0, 120.0) == 55.0)


# ── 3. intervals cannot be falsely decisive ──────────────────────────────────
lo, hi = percentile_bootstrap_interval([1.0] * 8)
check("8 identical values no longer yield a degenerate interval",
      lo is None and hi is None, f"got [{lo}, {hi}]")
lo, hi = percentile_bootstrap_interval([0.0] * (MIN_CLUSTERS - 1))
check(f"below MIN_CLUSTERS ({MIN_CLUSTERS}) no interval is returned",
      lo is None)
lo, hi = percentile_bootstrap_interval([0.05, -0.02] * MIN_CLUSTERS)
check("at or above MIN_CLUSTERS an interval is produced", lo is not None)
check("the method is not called BCa",
      not any("bca" in n.lower() for n in dir(sys.modules["wing_calibration"])),
      "a name containing 'bca' is still exported")


# ── 4. direction classification ──────────────────────────────────────────────
# z > 0 means spot is extended UP, so it came from BELOW.
below = {"low": 77800.0, "high": 77900.0}
above = {"low": 78100.0, "high": 78200.0}
inside = {"low": 78000.0, "high": 78100.0}
check("z>0: the band under spot is 'behind'", side_for(below, 78050.0, 2.0) == "behind")
check("z>0: the band over spot is 'ahead'", side_for(above, 78050.0, 2.0) == "ahead")
check("z<0 mirrors the classification", side_for(below, 78050.0, -2.0) == "ahead")
check("the band containing spot is neither", side_for(inside, 78050.0, 2.0) == "in")


print(f"\n{len(PASS)}/{len(PASS) + len(FAIL)} passed")
sys.exit(1 if FAIL else 0)
