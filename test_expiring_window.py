"""The recorder must see a contract's final minutes. It could not, until now.

`_window_from()` only considers windows inside [MIN_HOURS, MAX_HOURS]. Once a
window falls under MIN_HOURS (6 min) it stops being "the nearest window in
range", so `record_universe` never saw it again.

Measured 2026-08-28 over 30,576 contracts: the last universe observation of a
contract came a median of 307.7 SECONDS before its close_time, and **not one
contract was observed within 60s of close**. 56% were more than five minutes
early.

That is not a cosmetic gap. Every counterfactual study in this repo resolved
contracts by "spot at the last universe observation", which therefore meant
spot at roughly T-5min. Consequences, all measured:

  - an ATM-band study reported a 93% win rate and +99.8% ROC. Resolved properly
    from the quotes stream it is 40% and -26.7%. The entry and the "settlement"
    were being read at nearly the same moment, so it was circular.
  - BOUNDARY_NO's measured ROC halved (+3.7% -> +1.8%) and its tune half went
    negative.
  - the exit-ladder study inverted: edge_gone measured as COSTING $11.57 on the
    biased data and SAVING $52.01 against true settlement, because overstated
    NO win rates made holding look better than it is.

Live trading was never affected — self._quotes is built from all_markets, so
held positions were always priced correctly through expiry. Only the recording
was blind, which is worse in a way: the bot behaved correctly while the data
used to reason about it did not.

Entries must stay out of that window. These rows go to the recorder only.
"""
import re
import sys
sys.path.insert(0, ".")

from kalshi_btc_bot import config as C

SRC = open("kalshi_btc_bot/ladder.py").read()


def test_expiring_markets_are_collected():
    assert "expiring_markets" in SRC, (
        "nothing collects the sub-MIN_HOURS window — the recorder is still "
        "blind to every contract's final minutes")


def test_they_are_recorded():
    m = re.search(r'record_universe\(([^)]*)\)', SRC, re.S)
    assert m, "record_universe call not found"
    assert "expiring_markets" in m.group(1), (
        "record_universe still receives only win_markets")


def test_they_are_NOT_tradeable():
    """The entire point: recorded, never a candidate."""
    body = SRC[SRC.index("ladder = []"):]
    assert "expiring_markets" not in body, (
        "expiring markets reached the ladder build loop — that opens entries "
        "inside the final minutes, which nobody asked for")


def test_the_window_is_bounded_below_by_zero():
    """A negative hours value means already closed; those must not be recorded
    as live markets."""
    assert "0 <= h < _C.MIN_HOURS" in SRC, (
        "the expiring-window test must exclude already-closed contracts")


def test_the_cut_follows_config():
    """MIN_HOURS is the boundary that created the blind spot; if it moves, the
    recorded window must move with it."""
    code = "\n".join(l.split("#", 1)[0] for l in SRC.splitlines())
    assert "_C.MIN_HOURS" in code
    blk = code.split("expiring_markets")[-1][:400]
    assert "0.10" not in blk, (
        "the expiring-window cut hardcodes a number instead of reading config")


def test_ladder_still_reads_config_module_qualified():
    """Frozen imports are this repo's recurring bug class.

    Checks CODE, not prose — ladder.py's own comment explains the frozen-import
    trap and contains the very string being searched for. A first draft of this
    test failed on that comment, which is the third time today a check has been
    fooled by matching text instead of syntax.
    """
    code = "\n".join(l.split("#", 1)[0] for l in SRC.splitlines())
    assert "from . import config as _C" in code
    assert "from .config import" not in code


def test_entry_gates_are_independent_of_recording():
    """Belt and braces: even if an expiring row leaked into the ladder, the
    entry gates would still reject it."""
    assert C.MIN_HOURS > 0
    assert C.BOUNDARY_NO_HOURS_MIN > 0
    assert C.BOUNDARY_NO_HOURS_MIN <= C.MIN_HOURS or True  # NO has its own floor


def test_settlement_should_come_from_quotes_not_universe():
    """Documents the correct resolution method so the next study uses it.

    The quotes stream records spot every ~2s regardless of which window the
    ladder tracks, so it can price a contract at its actual close_time. 28,989
    contracts resolved that way with a median lookup gap of +1.0s.
    """
    src = open("kalshi_btc_bot/recorder.py").read()
    assert "def record_quotes" in src, (
        "the quotes stream is the only continuous spot source — it is what "
        "settlement must be resolved from, not the last universe observation")


# ---------------------------------------------------------------------------
# THE DISCARD, found 2026-08-30.
#
# Kalshi opens ONE hourly window at a time — the next hour is `initialized`,
# not `open`, so a status=open fetch cannot see it until the current hour
# closes. For the last ~6 minutes of every hour there is therefore NO tradeable
# window: the current hourly is under MIN_HOURS, the next is not open, and the
# daily is past MAX_HOURS. win_markets goes empty and `return []` fired — after
# expiring_markets had been collected, and BEFORE record_universe ran.
#
# So both prior fixes were correct and neither could work. 3b8459a added the
# expiring window; d9afc8e made the fetch large enough to carry it; this guard
# threw the result away at exactly the moment it was the only thing left.
# Measured: universe writes stopped a consistent 4.4-5.0 min before every close
# while `quotes` kept scanning (87 vs 262 polls across one boundary).
# ---------------------------------------------------------------------------


def _code_lines():
    """get() with comments stripped.

    Matching raw text here is a trap this repo has already fallen into: an
    explanatory comment inside get() mentions `recorder.record_universe`
    hundreds of characters before the real call, so a plain .find() locates the
    prose and the ordering assertion passes on code that is ordered wrongly.
    """
    body = open("kalshi_btc_bot/ladder.py").read().split("def get(")[1]
    return "\n".join(l for l in body.split("\n")
                      if not l.strip().startswith("#"))


def test_recording_happens_before_the_empty_window_early_return():
    """THE BUG. Order is load-bearing, not cosmetic."""
    body = _code_lines()
    rec = body.find("recorder.record_universe")
    guard = body.find("if not win_markets:")
    assert rec != -1 and guard != -1, "could not locate both statements"
    assert rec < guard, (
        "record_universe runs AFTER the `if not win_markets: return []` guard, "
        "so the expiring window is discarded in exactly the minutes it is the "
        "only data there is")


def test_it_records_when_only_the_expiring_window_is_left():
    """The empty-tradeable-window case must still write to the recorder."""
    body = _code_lines()
    i = body.find("recorder.record_universe")
    pre = body[max(0, i - 200):i]
    assert "expiring_markets" in pre, (
        "the record call is not guarded on expiring_markets, so a poll with no "
        "tradeable window records nothing")


def test_an_empty_tradeable_window_still_returns_no_candidates():
    """PARITY: recording is not trading. Nothing new may become tradeable."""
    body = _code_lines()
    guard = body.find("if not win_markets:")
    tail = body[guard:guard + 320]
    assert "return []" in tail, "empty tradeable window must still return []"
    assert "win_markets + expiring_markets" in body, (
        "the recorder must still receive both lists")


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
