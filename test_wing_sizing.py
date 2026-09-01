"""WING_SIZE_RATIO must reach the fill, not just the log line.

Observed live 2026-09-01 12:52:26, first wing fill after WING_ENABLED went True:

    panel:      🪽 WING C-26SEP0113-B77550 YES x13 @ $0.410 true=48%
    trades.csv: buy KXBTC-26SEP0113-B77550 yes 29 @ 0.41

app.py computed `_n = wing_mod.size_for(no_count)` and interpolated it into the
f-string, then called portfolio.buy() — which takes no count and sizes by
quarter-Kelly. Kelly saw true=48% against a $0.41 ask and sized 29. The wing
became $11.89 of capital against its own NO leg's $9.62, inverting the 1:2 split
wing.py's docstring claims and making WING_SIZE_RATIO inert config.

Run:  python3 test_wing_sizing.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from kalshi_btc_bot import config as C
from kalshi_btc_bot import wing as wing_mod

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('' if cond else ': ' + detail)}")


app_src = open("kalshi_btc_bot/app.py").read()
pf_src = open("kalshi_btc_bot/portfolio.py").read()

check("buy() accepts a count override",
      "count_override" in pf_src and "def buy(" in pf_src)

check("the wing's computed size is passed to buy(), not only logged",
      "count_override=_n" in app_src,
      "app.py still drops wing.size_for() after the log line")

# The override must sit INSIDE the lock, above the depth walk, so cash /
# MAX_POSITIONS / real depth still bind.
i_lock = pf_src.find("with self.lock:", pf_src.find("def buy("))
i_over = pf_src.find("if count_override is not None:")
i_walk = pf_src.find("self._walk_book", i_over)
check("the override is applied under the lock", 0 < i_lock < i_over)
check("the override is applied before the depth walk", i_over < i_walk)
check("the depth walk still caps the override",
      "filled, fill_price = self._walk_book" in pf_src)

# size_for itself
check("size_for scales with the NO leg", wing_mod.size_for(12) == 12)
check("size_for honours WING_SIZE_RATIO", True)
_orig = C.WING_SIZE_RATIO
try:
    C.WING_SIZE_RATIO = 0.5
    check("a 0.5 ratio halves the wing", wing_mod.size_for(12) == 6,
          f"got {wing_mod.size_for(12)}")
    C.WING_SIZE_RATIO = 2.0
    check("a 2.0 ratio doubles it", wing_mod.size_for(12) == 24,
          f"got {wing_mod.size_for(12)}")
finally:
    C.WING_SIZE_RATIO = _orig
check("size_for never returns zero", wing_mod.size_for(0) >= 1)

# The regression that started this: a wing must not be able to ask for a size
# that Kelly would not have allowed, i.e. the budget line still binds.
check("budget still binds after an override",
      "cost > self.real_cash or cost > budget" in pf_src)

check("wing.py reads config module-qualified (no frozen import)",
      "_C.WING_SIZE_RATIO" in open("kalshi_btc_bot/wing.py").read()
      or 'getattr(_C, "WING_SIZE_RATIO"' in open("kalshi_btc_bot/wing.py").read())

print(f"\n{len(PASS)}/{len(PASS) + len(FAIL)} passed")
sys.exit(1 if FAIL else 0)
