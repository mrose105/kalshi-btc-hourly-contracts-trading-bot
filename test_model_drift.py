"""The reverting drift is OFF, config-gated, and not frozen at import.

Measured 2026-09-01 (model_error_decomp.py), 17,613 band-observations at
BOUNDARY_NO-qualifying moments, settlement as-of close from the quotes stream:

    side            n     model   no-drift  realized   ratio
    continuation  6291   0.1196    0.1602    0.1725    1.44x under
    counter       7494   0.1910    0.1485    0.1536    0.80x over

`-zscore * vol_t * 0.15` under-predicted the continuation side by 44% and
over-predicted the counter side by 20%. Those cancel in aggregate (whole
population reads 1.01x) which is why it survived review — but BOUNDARY_NO only
ever buys the continuation side, so it took the full error, and an understated
true_prob inflates ask/true_prob, which IS the entry gate.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from kalshi_btc_bot import config as C
from kalshi_btc_bot.model import DistModel

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('' if cond else ': ' + detail)}")


check("DRIFT_REVERTING_COEF exists", hasattr(C, "DRIFT_REVERTING_COEF"))
check("it is OFF by default", C.DRIFT_REVERTING_COEF == 0.0,
      f"got {C.DRIFT_REVERTING_COEF}")

src = open("kalshi_btc_bot/model.py").read()
check("model.py reads the coefficients module-qualified (not frozen)",
      "_C, \"DRIFT_REVERTING_COEF\"" in src or "_C, 'DRIFT_REVERTING_COEF'" in src)
check("no bare literal 0.15 drift term remains",
      "zscore\"] * vol_t * 0.15" not in src and "zscore'] * vol_t * 0.15" not in src)

d = DistModel()
band = {"type": "RANGE", "low": 78600.0, "high": 78700.0}
spot, vol, hours = 78500.0, 1e-4, 0.2
rev = {"regime": "REVERTING", "direction": "DN", "vol": vol,
       "zscore": 2.5, "mom": 0.001}

off = d.true_prob(band, spot, vol, hours, rev)
_orig = C.DRIFT_REVERTING_COEF
try:
    C.DRIFT_REVERTING_COEF = 0.15
    on = d.true_prob(band, spot, vol, hours, rev)
finally:
    C.DRIFT_REVERTING_COEF = _orig

check("the coefficient actually reaches true_prob at runtime", abs(on - off) > 1e-6,
      "changing config.DRIFT_REVERTING_COEF did nothing — frozen import?")
check("the old drift SUPPRESSED continuation probability", on < off,
      f"drift-on {on:.4f} should be below drift-off {off:.4f}")
# The whole finding in one assertion: at z=2.5 the term roughly halved it.
check("its effect at z=2.5 was ~2x, not a rounding detail", off / on > 1.5,
      f"ratio {off / on:.2f}x")

# A RANGING regime never took the branch, so it must be untouched either way.
rang = {**rev, "regime": "RANGING"}
r_off = d.true_prob(band, spot, vol, hours, rang)
try:
    C.DRIFT_REVERTING_COEF = 0.15
    r_on = d.true_prob(band, spot, vol, hours, rang)
finally:
    C.DRIFT_REVERTING_COEF = _orig
check("RANGING is unaffected by the reverting coefficient", abs(r_on - r_off) < 1e-12)
check("with drift off, REVERTING and RANGING agree", abs(off - r_off) < 1e-12,
      f"{off:.6f} vs {r_off:.6f}")

check("TRENDING / BREAKOUT coefficients are also config-gated",
      hasattr(C, "DRIFT_TRENDING_COEF") and hasattr(C, "DRIFT_BREAKOUT_COEF"))

print(f"\n{len(PASS)}/{len(PASS) + len(FAIL)} passed")
sys.exit(1 if FAIL else 0)
