#!/usr/bin/env python3
"""Which config gates can be changed without a single test noticing?

    python3 tools/mutate_config.py            # the live strategy's gates (~2 min)
    python3 tools/mutate_config.py --all      # every numeric constant (~20 min)

For each constant: rewrite config.py with a perturbed value, run the whole test
suite, restore. A constant nothing catches is one of two things, and both have
bitten this repo:

    a FIXTURE that stopped binding — three sets of them went vacuous on
    2026-08-27 alone, when a threshold moved past a hardcoded literal

    a GATE with no coverage at all — which is how BOUNDARY_NO_OVERPRICING_MIN
    at 1.15, BOUNDARY_NO_ZSCORE_MIN at 1.40 and BOUNDARY_NO_YES_ASK_MAX at 0.65
    each sat provably inert for weeks with nothing flagging it

It would also have caught the frozen imports: a value bound at import cannot
respond to being changed, so nothing fails.

First real run found 11 of 29 live gates unguarded — including
BOUNDARY_NO_YES_ASK_MAX, which had shipped about an hour earlier in the same
session, with a rationale doc and three repaired fixtures and no test on the
gate itself.

NOT SAFE TO RUN CONCURRENTLY WITH A BOT RESTART. config.py is rewritten in
place and restored in a finally, so a restart during the window could come up
on a perturbed value. The running process is unaffected — it read its config
at import.
"""
import argparse
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from kalshi_btc_bot import config as C          # noqa: E402

CFG = pathlib.Path("kalshi_btc_bot/config.py")

# The gates the live BOUNDARY_NO strategy actually depends on. Deliberately not
# every constant: the YES/snipe lanes are off, and guarding a dead path is
# noise that trains you to ignore the report.
LIVE = [
    "BOUNDARY_NO_OVERPRICING_MIN", "BOUNDARY_NO_YES_ASK_MAX",
    "BOUNDARY_NO_YES_ASK_MIN", "BOUNDARY_NO_ZSCORE_MIN",
    "BOUNDARY_NO_HOURS_MAX", "BOUNDARY_NO_HOURS_MIN",
    "BOUNDARY_NO_MIN_NET_EDGE", "BOUNDARY_NO_OTM_MIN", "BOUNDARY_NO_OTM_MAX",
    "NO_STOP", "NO_EDGE_GONE_MIN_GAIN", "NO_EDGE_GONE_RATIO",
    "NO_PROFIT_CAPTURE", "NO_TIME_PROFIT", "NO_TRUE_PROB_MAX",
    "MIN_HOLD_SECS", "MIN_HOLD_CATASTROPHE",
    "LAG_FILTER_MAX_ADVERSE", "LAG_FILTER_SECS",
    "WATCHLIST_ENTRY_DIP", "WATCHLIST_ENTRY_NET_EDGE",
    "DIST_TAIL_DF", "MAX_SPREAD_PCT", "MAX_SPREAD", "MAX_ASK", "MIN_VOLUME",
    "STRIKE_CLUSTER_DIST",
]

# WHAT THIS TOOL CAN AND CANNOT SEE.
#
# A test whose fixture is DERIVED from config follows the constant wherever it
# goes, so it never fails on a perturbation. That is the correct shape — it is
# the fix for fixtures going vacuous when a threshold moves past a hardcoded
# literal, which happened to three separate files on 2026-08-27. But it means
# such a test validates that the MECHANISM reads config, not that the VALUE is
# sane, and a mutation scan sees straight through it.
#
# So "unguarded" here means "no test asserts a bound on this value". That is a
# defect only where a real invariant exists. Where one does, write it as an
# explicit bound (see test_live_gates.py); where it does not, list it below
# with the reason.
EXPECTED_UNGUARDED = {
    # off by design — the dip is 0.0, so raising it only trips the paper guard
    "WATCHLIST_ENTRY_DIP", "WATCHLIST_ENTRY_NET_EDGE",
    # ladder pre-filters with no principled bound. A spread cap or volume floor
    # is a liquidity preference, not an invariant: any positive value is
    # defensible, and pinning one would be inventing a rule to satisfy a tool.
    # Their WIRING is tested (test_live_gates.py::test_ladder_filters_are_
    # read_from_config); only their magnitude is unpinned, on purpose.
    "MAX_SPREAD", "MIN_VOLUME", "MAX_ASK",
}


def run_suite(tests) -> list:
    return [t for t in tests
            if subprocess.run([sys.executable, t], capture_output=True).returncode]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="every numeric constant")
    a = ap.parse_args()

    tests = sorted(p.name for p in pathlib.Path(".").glob("test_*.py"))
    if a.all:
        names = sorted(k for k in dir(C) if k.isupper() and not k.startswith("_")
                       and isinstance(getattr(C, k), (int, float))
                       and not isinstance(getattr(C, k), bool))
    else:
        names = [k for k in LIVE if hasattr(C, k)]

    orig = CFG.read_text()
    unguarded = []
    print(f"  {len(names)} constants x {len(tests)} test files\n")
    try:
        for k in names:
            v = getattr(C, k)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            new = 0.37 if v == 0 else round(v * 1.7 + 0.13, 6)
            pat = re.compile(rf'^({k}\s*=\s*)([^\s#]+)', re.M)
            if not pat.search(orig):
                print(f"    {k:<30} (derived from instrument.py, skipped)")
                continue
            CFG.write_text(pat.sub(lambda m: m.group(1) + repr(new), orig, count=1))
            bad = run_suite(tests)
            if bad:
                who = ",".join(b.replace("test_", "").replace(".py", "") for b in bad[:3])
                print(f"    {k:<30} {v!s:>9} -> {new!s:<10} caught by {who}", flush=True)
            else:
                unguarded.append(k)
                print(f"    {k:<30} {v!s:>9} -> {new!s:<10} ** NOTHING FAILED **", flush=True)
    finally:
        CFG.write_text(orig)

    real = [k for k in unguarded if k not in EXPECTED_UNGUARDED]
    print(f"\n  unguarded: {len(unguarded)}   unexpected: {len(real)}")
    for k in real:
        print(f"    {k} = {getattr(C, k)}")
    if real:
        print("\n  Each of these can be set to any value and the suite still passes.")
        print("  Either the gate has no test, or a fixture stopped binding.")
    return 1 if real else 0


if __name__ == "__main__":
    sys.exit(main())
