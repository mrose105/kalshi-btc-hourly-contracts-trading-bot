"""`accel` is the sole input to the BREAKOUT branch and was never recorded.

regime.py has always published it (regime.py:75), but the quotes stream kept
only ['r','d','v','vh','vr','vc','z','m'] — so BREAKOUT could not be studied
beyond the n=6 signals that happened to reach the orders log. It is 0.8% of
ticks and measured -23.7% ROC on that tiny sample; blocking it looks right and
is essentially unverified.

Also note the labelling trap this guards against caring about: `direction` in
BREAKOUT is derived from accel, so "DN" means DECELERATING, not falling. A live
reading of `BREAKOUT DN | mom=+0.336%` is not a contradiction — price is up
over 10 min while the last 5 min gave some of it back.
"""
import sys
sys.path.insert(0, ".")

REC = open("kalshi_btc_bot/recorder.py").read()
APP = open("kalshi_btc_bot/app.py").read()
REG = open("kalshi_btc_bot/regime.py").read()


def _quotes_payload():
    body = REC.split("def record_quotes")[1].split("\n\ndef ")[0]
    return body.split('"rg": {')[1].split("},")[0]


def test_accel_is_recorded_in_the_quotes_stream():
    """THE GAP. Published by regime.py, dropped by the recorder."""
    assert '"accel"' in REG, "regime.py must still publish accel"
    assert 'regime.get("accel")' in _quotes_payload(), (
        "accel is computed every tick and thrown away — BREAKOUT cannot be "
        "analysed without it")


def test_the_existing_regime_fields_are_untouched():
    """PARITY: adding a key must not drop one. Old readers index by name."""
    body = _quotes_payload()
    for k in ('"r"', '"d"', '"v"', '"vh"', '"vr"', '"vc"', '"z"', '"m"'):
        assert k in body, f"regime field {k} disappeared from the payload"


def test_accel_is_on_the_status_line():
    """It gates 0.8% of ticks invisibly; if it is going to block, show it."""
    assert "accel={regime['accel']" in APP


def test_breakout_direction_still_comes_from_accel():
    """Documents the trap rather than 'fixing' it — the semantics are correct,
    only the shared field name is confusing. Changing it would silently alter
    signals.py's direction handling, which is not in scope here."""
    branch = REG.split('regime    = "BREAKOUT"')[1][:200]
    assert 'direction = "UP" if accel > 0 else "DN"' in branch


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
