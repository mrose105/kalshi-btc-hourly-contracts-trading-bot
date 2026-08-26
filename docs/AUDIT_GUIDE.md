# How to check this work

Where to look if you're checking my work. What holds up, what doesn't, and the
mistakes this codebase has already made — so you don't have to find them the
slow way.

    branch   model-calibration-and-exit-fixes @ 3fb56d5 (29 commits ahead of main)
    tests    153 across 16 files
    data     93 recording files, 82 MB, from 2026-07-28
    mode     paper

---

## 1. What the strategy is

It sells premium, the same way selling an out-of-the-money option does. You
can't short a contract on Kalshi, so instead you buy the NO side of a band BTC
probably won't reach. Same bet, different wrapper: you get paid now for saying
it won't happen.

**Here's the part that matters, and it's the reason nothing else on this page
is very impressive.**

You pay about 81 cents for a contract that pays out a dollar. So you win 19
cents when you're right, and lose 81 when you're wrong. Do the arithmetic and
you need to be right **81% of the time just to break even**.

We're right 80% of the time.

That sounds like being one point short, which sounds like nothing. It isn't:

    being 1 point short of break-even     -1.7%
    Kalshi's taker fee                    -1.3%
    ------------------------------------------
    expected                              -3.0%
    actually measured (n=41)              -3.4%

**Fees are nearly half the problem**, and that's the thing people miss when
they look at an 80% win rate and assume it must be working. There is no cushion
here at all — a fill 2 cents worse than expected moves break-even further than
most of the edges anyone is chasing are even worth.

It also means the win rate on its own tells you almost nothing. You could push
it to 85% by buying more expensive contracts and still lose money, because the
bar moves up with the price. See trap 7 — the same thing bites with the median.

What actually runs is one strategy, `BOUNDARY_NO`. When BTC gets stretched to
an extreme (|z| >= 1.40) and the market is ranging rather than trending, it
buys NO on the band in the direction of the supposed breakout, in the last 15
minutes before expiry. The bet is that the move doesn't finish.

`ENABLE_YES` and `ENABLE_SNIPE` are switched off. Those two went the other way
— they *bought* premium instead of selling it — and between them they lost
$413 and $670 in the real trade log.

---

## 2. Claims and their evidence

Sorted by how much you can lean on them. The label is how much I actually
trust it, which is not always how solid the number looks.

### WELL SUPPORTED — the model does not beat the Kalshi price

Five independent attempts, 9,110 contract-observations. Market mid Brier
**0.1611**; Student-t df=3 **0.1632**; Gaussian **0.1726**. A learned
recalibration of the market itself, fit on 43 expiries, *lost* out of sample on
the other 44 (0.1525 vs 0.1506).

This is the one to read before believing any new idea about predicting prices.
The old Gaussian model guessed too low in every single bucket — and because the
trading signal fires when the market price looks high relative to the model's
estimate, **the signal was partly just detecting its own error.** It found the
biggest "edge" exactly where the model was most wrong.

    code   kalshi_btc_bot/model.py, calibration_check.py
    docs   README.md, "Measured on recorded data"

### STRUCTURAL — the backtest measures a lane that does not trade

Under the live config a 60-day run produces **zero** trades. Every published
backtest figure comes from the YES/snipe lanes, which are off. The 2026-08-24
run is -43.2% over 193 trades — all 193 YES, none NO.

**Check `no_trades` in any result JSON before quoting it.** If it is 0, the
number says nothing about live behaviour. `build_ladder()` synthesises quotes
from a lagged-vol member of the same model family being tested, so the
simulator scores the model against prices the model produced.

    docs   docs/BACKTEST_INTEGRITY.md section 10, and checklist item 0

### WELL SUPPORTED — Kalshi's prices lag spot by ~20 seconds

1.2M observations, correlating a past Coinbase move against Kalshi's subsequent
repricing: +0.026 at 2s, +0.133 at 10s, **+0.180 at 20s**, +0.083 at 60s,
+0.049 at 120s. A clean inverted-U, positive on all 10 days and all 8
hour-buckets, stronger on larger moves (+0.255 for >= $30).

**Not tradeable directly.** A round trip costs two spreads (~20% of a 20c
contract) against ~9.5% the lag delivers; every directional configuration
tested came back negative. Only the *filter* form is free.

    code   feed_compare.py, signals.py lag filter, config LAG_FILTER_*

### WELL SUPPORTED (three independent confirmations) — buying the dip selects losers

Scale-in, dip-adding, and the watchlist all reached the same result on separate
samples. P(win) decays monotonically with dip depth. The watchlist sweep is the
cleanest version — cost falls exactly as designed, and the win rate falls
through break-even with it:

     dip |  n   WR    cost      ROC    PF |    TUNE    VALID
    0.0% | 41  80%  $0.814    -3.4%  0.87 |   -8.4%    +0.0%
    5.0% | 21  62%  $0.690   -15.4%  0.65 |  -36.0%    -2.7%
   10.0% | 18  56%  $0.660   -20.1%  0.58 |  -40.7%    -7.0%
   15.0% | 13  38%  $0.586   -35.6%  0.41 |  -54.2%   -19.8%
   30.0% |  6  33%  $0.495   -38.3%  0.47 | -103.8%   +27.2%

Look at the bottom row before you get excited about it. That +27.2% is three
trades, and the other half of the split is -103.8%. Pick that row and you'd
ship a disaster. **Nothing works on both halves.** Not waiting for a dip at all
is the best of them.

    config  WATCHLIST_ENTRY_DIP — still 0.05, i.e. still on
    docs    docs/CONFIG_RATIONALE.md#watchlist_entry_dip

### PROVISIONAL — check this hardest — the exit fix, +6.3% ROC, PF 1.84

The most consequential recent change and the one most likely to be wrong.
Gating `edge_gone` on a 15% minimum gain, and tightening `NO_STOP` 0.40 -> 0.30:

    configuration                  WR     ROC    PF    total          95% CI  P>0    TUNE   VALID
    old: edge_gone>0, stop 40%    66%   -3.8%  0.59  -$14.22  [-12.2%, +3.3%] 16%   -7.2%   -1.4%
    shipped: >15%, stop 30%       80%   +6.3%  1.84  +$23.02  [-0.7%, +13.3%] 96%   +3.4%   +8.4%

**Why you should doubt it:** 41 trades. The confidence interval still touches
zero. And I tried about **24 different configurations that same day** across
dips, exits and stops — if you try 24 things, one of them passing a split test
isn't surprising, it's expected.

**Why I still think the direction is right:** the arithmetic says so on its
own, without any backtest. You're risking 81 cents to make 19. A 40% stop means
taking a 32-cent loss — **nearly twice the entire amount you were playing for.**
One of those wipes out almost two winners. And the results get steadily worse
as the stop gets looser (+3.7 -> -0.7 -> -2.2 -> -4.6 -> -5.4), which is a real
pattern rather than one lucky number.

So read 0.30 as "40% was clearly too loose" and 0.15 as "it needs to be above
zero". Don't read either as the right answer.

    code    positions.py edge_gone block; config NO_STOP, NO_EDGE_GONE_MIN_GAIN
    tests   test_no_exits.py
    docs    docs/CONFIG_RATIONALE.md#no_stop

### PROVISIONAL — the regime gate blocks only ~15% of signals

Of 48 signals passing every gate except regime, 41 are allowed and 7 blocked.
The blocked ones are worse (-18.1% vs -3.4%), BREAKOUT worst at -23.7% over 6.
So the gate earns its keep — but on seven observations, CI [-62.5%, +15.1%].

The gate applies *per tick*, so it delays far more than it blocks. The real
restriction is the 15-minute window (-84% of rows) and the ask band (-97% of
what survives).

### DO NOT QUOTE — superseded figures still in circulation

Each looked solid when published.

    figure                        why it is void
    +1,752% validation            pre-2026-08-07, 250-wide band on every contract
    +108.4% backtest              YES/snipe lanes, disabled live, synthetic quotes
    watchlist +12.0% (n=14)       6 days; 7 days / n=21 gives -15.4%
    Monte Carlo +17.8% (n=11)     n=41 gives -3.4%
    exits beat holding +$50.84    older config; now -$5.38 on 41 armings

The pattern is the point here: **every single one got overturned by more data,
and every single one was too optimistic.** That's not bad luck. Small samples
that happen to look good are the ones that get written down and repeated — the
ones that look bad get quietly retested.

---

## 3. Re-running it

All of this runs off the recorded data. You don't need API keys except where
noted.

**Tests first — they encode the bugs.**

    for f in test_*.py; do python3 "$f" >/dev/null || echo "FAIL $f"; done

153 tests / 16 files. Several are *policy* tests rather than behaviour tests:
`test_no_exits.py` and `test_watchlist_entry.py` refuse to let an unvalidated
feature run while `PAPER_TRADING` is False.

**The entry-quality question.**

    python3 boundary_no_quote_replay.py --bootstrap 10000

Production selector on real recorded z-scores and real Kalshi bids, held to
recorded settlement, expiry-clustered CI. This — not the backtest — is the
reference for entry quality.

**The backtest, knowing what it is.**

    python3 kalshi_btc_backtest.py --days 60 --capital 500
    # then check no_trades in the JSON. If 0, it measured a disabled lane.

**Inspect the raw tape.**

    python3 inspect_recording.py
    python3 feed_compare.py            # live, needs keys
    ./backup_recordings.sh --verify    # checksums all 93 files

**If you only do one thing, do this:** pick any claim in the README, find the
days of `universe_*.jsonl.gz` it's based on, and work it out again yourself.
That file is recorded *before* any filtering, so you can change any rule and
re-score the whole history against what actually happened. That's how
everything here was worked out — and how three of the earlier conclusions got
overturned.

---

## 4. Traps

Every one of these produced a confident number that was wrong, and every one
got past review at the time. Roughly ordered by how easy it'd be to fall for
again.

**1. Config changes that don't actually change anything.** `from .config import
X` copies the value once, when the file loads. If a sweep sets `C.X = something`
later, the code that imported it that way never sees it. This really happened —
a sweep tried seven different thresholds and got seven identical results,
because none of them took effect. Nobody noticed for a while. Read settings as
`_C.X` instead.
Still wrong in `ladder.py`, which we know about and haven't fixed.

**2. A feature that's switched on but can't run.** `WATCHLIST_ENTRY_DIP` was set
to 0.05, so it looked live. But the code that arms it sat behind a different
flag that was set to 0, so it never ran once — not a single time — while the
config, the tests and the README all said it was working.
*What to look for:* a feature that's "on" but whose log lines never show up.
Grep for its output before believing it ran.

**3. Almost every number here assumes we hold to expiry. The bot doesn't.**
It exits early. That gap went unnoticed for a long time, and when it was finally
simulated properly, the bot's own exit logic turned out to be *worse* than just
holding (-$14.22 against -$8.84).
*Ask of any figure:* does this assume holding to the end? The bot doesn't.

**4. The session log isn't a log.** `bot_session.log` is a recording of the
live dashboard, which redraws the whole "RECENT EVENTS" box every couple of
seconds. So counting how many times something appears counts how long it sat on
screen, not how often it happened. One signal showed up 9,659 times.
Use `recordings/orders_*.jsonl.gz` for anything you're counting. The log is
only for reading with your eyes.

**5. "Closed" doesn't mean "settled".** On Kalshi, closed means trading stopped
— the result gets decided later. Treating it as final meant booking an outcome
based on *our* price at whatever moment we happened to notice. Once it credited
a full $1.00 on a contract that was $59 outside its band with ten minutes still
to go.
Fixed, and `test_settlement_detection.py` keeps it fixed.

**6. Prices are in cents, but floats aren't.** `0.33 - 0.28` comes out as
`0.050000000000000044`. `0.38 - 0.33` comes out as `0.04999999999999999`. Same
five cents, but against a 0.05 limit one passes and one fails — **41% of exact
5-cent spreads were being rejected for no reason.** The same bug charged an
extra cent of fee on one side of the book. Round to cents before comparing
anything.

**7. The typical trade always looks good here.** If you win more than half the
time, the middle trade is a winner by definition. Both strategies show a
positive median (+16.6% and +26.6%) while both actually lose money on average.
So if you watch this thing trade, you'll see mostly winners and feel good about
it, right up until a few bad ones take it all back.
Judge it on expectancy and profit factor. Not the win rate, not the typical
trade.

**8. Measuring a cost against the wrong side of the trade.** The spread check
divided by the YES price, but we're paying the NO price. And this strategy only
ever looks at cheap YES prices (0.10-0.65), so it was always dividing by the
small number — **which meant it rejected hardest exactly where trading was
cheapest.** A 5-cent spread on an 85-cent NO reads as 25% when it really costs
6%.

**9. The contracts themselves aren't consistent.** Most hourly bands are $100
wide, but about 3% are $250 — and it changes hour to hour, not by how far out
you are. One hour can be 250-wide start to finish while the hours on either
side are 100. Anything written as a fixed dollar amount quietly means something
different on those.
Also worth knowing: the number in the ticker is the **middle** of the band, not
the bottom.

---

## 5. Known open

    ladder.py frozen imports     latent; nothing depends on it today
    spx_vol_calibration.py       cited by instrument.py, does not exist
    SPX distance constants       BTC values x 0.0857 sigma ratio, not calibrated
    backtest / live parity       delayed entry, watchlist, min-hold, depth exits
                                 and the lag filter have no backtest implementation
    find_no_scalp                single-best shape; needs all_matches if
                                 MISPRICE_NO is re-enabled
    WATCHLIST_ENTRY_DIP          still 0.05 though the sweep says 0.0
    250-wide grids               52 of 53 trades were 100-wide; unvalidated

---

## 6. The data

Compressed JSON, one file per stream per day, in `recordings/`. 93 files, 82 MB,
going back to 2026-07-28. **It's not in git — it lives on one laptop and one
iCloud copy.** Kalshi doesn't publish historical order books, so if a day goes
missing it's gone for good.

    stream    days  one record per                only this can answer
    universe    11  ladder poll, PRE-FILTER       counterfactuals: move a gate,
                                                  re-score the whole history
    quotes      22  scan tick + regime            what the classifier saw
    books       22  book snapshot, both ladders   depth at size
    marks       17  held-position mark            prior/market/posterior per tick
    orders      17  order attempt + full book     fill quality; rejects are as
                                                  useful as fills
    walls        1  Deribit open interest         irreplaceable, no historical API

`universe` is the one that matters — 268,000 snapshots covering 47.3 million
individual contract observations, all recorded before anything gets filtered
out. `quotes` and `books` have already been filtered, so they can't tell you
what a different rule would have done.

---

## 7. Code map

    file                 lines  what to look for
    portfolio.py          1222  buy_no fresh-quote revalidation, spread gate,
                                depth walking
    positions.py           563  the NO exit ladder — edge_gone, stop,
                                settlement detection
    signals.py             459  find_boundary_no, strike clustering, lag filter
    app.py                 379  main loop; how gate() and watchlist_fills() run
    recorder.py            343  non-blocking queue, per-stream flush cadence
    pending.py             257  arming and watchlist fills; _arming_on()
    model.py               219  Student-t prior, market posterior blend
    config.py              195  settings only — rationale in CONFIG_RATIONALE.md
    ladder.py              189  the pre-filter; frozen imports live here

Documents, in reading order:

    README.md                       the strategy, current figures, parity gaps
    docs/BACKTEST_INTEGRITY.md      ten classes of defect + checklist. Start at 0
    docs/CONFIG_RATIONALE.md        why every parameter holds its value
    docs/QUANT_STANDARDS_AUDIT.md   methodology vs practitioner norms
    docs/STRATEGY.md                the math

---

Generated 2026-08-26 on branch `model-calibration-and-exit-fixes` @ `3fb56d5`,
paper mode. Figures are settlement-resolved on recorded data unless stated.
Sample sizes are small and named everywhere they matter — where a claim is
labelled provisional, the number is probably not the number.
