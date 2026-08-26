"""No tunable setting may be frozen at import time.

`from .config import X` binds a name-local snapshot when the module loads. Any
later mutation of `config.X` — which is what every sweep and every test fixture
does — never reaches that module. The code keeps working; the TOOLING silently
lies, which is worse, because a sweep that never varied anything looks like a
finished experiment reporting "this parameter does not matter".

Found three times in this codebase:

    2026-08-03  signals.py    a 7-value threshold grid produced byte-identical
                              trades on all seven
    2026-08-2x  regime.py     MOMENTUM_WINDOW_SECS
    2026-08-26  ladder.py     MAX_ASK / MAX_SPREAD_PCT / MIN_VOLUME, plus the
                              exit tiers in positions.py and MAX_TRADE_PCT in
                              app.py — ten names in all, every one of them
                              something a sweep sets

This test exists so there is no fourth time. It is deliberately mechanical:
find every config name that anything in the repo assigns to at runtime, then
assert no module imports that name directly.
"""
import pathlib
import re
import sys
sys.path.insert(0, ".")

PKG = pathlib.Path("kalshi_btc_bot")
ROOT = pathlib.Path(".")

# Names assigned at runtime anywhere: sweeps, the backtest, test fixtures.
_ASSIGN = re.compile(r'(?:^|[^.\w])(?:_?C|config)\.([A-Z_]{3,})\s*=(?!=)')


def _mutated_names() -> set:
    """Config names something assigns to at runtime.

    Strips comments first. Without that, prose like "At PRICE_FETCH=2s" inside
    a docstring reads as an assignment — which it did, on the first run of this
    test, and sent me chasing a bug that was a sentence.
    """
    found = set()
    for f in list(ROOT.glob("*.py")) + list(PKG.glob("*.py")):
        try:
            code = "\n".join(l.split("#", 1)[0] for l in f.read_text().splitlines())
            found |= set(_ASSIGN.findall(code))
        except Exception:
            pass
    return found


def _direct_imports(path: pathlib.Path) -> set:
    """Names this module binds via `from .config import ...`."""
    src = path.read_text()
    names = set()
    for m in re.finditer(r'from \.config import \(([^)]*)\)', src):
        names |= {x.strip() for x in m.group(1).replace("\n", " ").split(",") if x.strip()}
    for m in re.finditer(r'from \.config import ([^(\n]+)$', src, re.M):
        names |= {x.strip() for x in m.group(1).split(",") if x.strip()}
    return {n for n in names if n and n[0].isupper()}


def test_no_mutated_setting_is_frozen_by_a_direct_import():
    """THE BUG. A name that something assigns to must never be import-bound."""
    mutated = _mutated_names()
    assert mutated, "found no runtime assignments at all — the scan is broken"
    offenders = []
    for f in sorted(PKG.glob("*.py")):
        for n in sorted(_direct_imports(f) & mutated):
            offenders.append(f"{f.name}: {n}")
    assert not offenders, (
        "these settings are assigned at runtime somewhere but frozen at import "
        "here, so a sweep against them silently does nothing:\n  "
        + "\n  ".join(offenders))


def test_the_known_offenders_are_actually_fixed():
    """Names it has already bitten. Explicit so a regression is unambiguous."""
    for mod, names in (
        ("ladder.py",    ["MAX_ASK", "MAX_SPREAD_PCT", "MIN_VOLUME",
                          "LADDER_CACHE_SECONDS", "MIN_HOURS", "MAX_HOURS"]),
        ("positions.py", ["NO_STOP", "NO_PROFIT_CAPTURE", "NO_TIME_PROFIT",
                          "PAPER_TRADING", "STOP_LOSS_PCT", "STOP_MIN_HOURS"]),
        ("app.py",       ["MAX_TRADE_PCT", "PAPER_TRADING"]),
        ("portfolio.py", ["PAPER_TRADING"]),
    ):
        got = _direct_imports(PKG / mod)
        for n in names:
            assert n not in got, f"{mod} still freezes {n} at import"


def test_a_mutation_actually_propagates_end_to_end():
    """The real check: set it on config, read it through the module."""
    from kalshi_btc_bot import config as C
    from kalshi_btc_bot import positions as P
    from kalshi_btc_bot import ladder as L

    for mod, name in ((P, "NO_STOP"), (L, "MAX_ASK")):
        old = getattr(C, name)
        try:
            setattr(C, name, 0.999)
            assert getattr(mod._C, name) == 0.999, (
                f"{mod.__name__} did not see config.{name} change — still frozen")
        finally:
            setattr(C, name, old)


def test_every_module_that_reads_config_has_the_qualified_handle():
    """If a module touches config at all it should hold `_C`, not loose names."""
    for f in sorted(PKG.glob("*.py")):
        src = f.read_text()
        if "config" not in src:
            continue
        if _direct_imports(f) and "from . import config as _C" not in src:
            raise AssertionError(
                f"{f.name} imports config names directly but has no `_C` handle "
                f"— any tunable among them is frozen")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t(); print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1; print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests)-failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
