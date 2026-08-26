# How to check this work

A reviewer's orientation. Which claims to trust, which to doubt, and which have
already fooled us.

    branch   model-calibration-and-exit-fixes @ 3fb56d5 (29 commits ahead of main)
    tests    153 across 16 files
    data     93 recording files, 82 MB, from 2026-07-28
    mode     paper

---

## 1. What the strategy is

Short-tail options selling, adapted to prediction markets. You cannot short a
Kalshi contract, so the position is taken by **buying the NO side** of an
out-of-the-money band — economically identical: you are paid now for the claim
that BTC will not be there at expiry.

**One number governs everything below. Break-even win rate equals the entry
cost.** At $0.81 you must be right 81% of the time. The measured rate across 41
settlement-resolved signals is 80%.

The strategy sits *on* its own break-even. That is why this repo is obsessive
about fees, spread and fill price — a 2c worse fill moves the bar further than
most of the edges being chased are worth. It is also why the win rate alone
tells you almost nothing (see trap 7).

What actually runs is one lane, `BOUNDARY_NO`: at a range extreme (|z| >= 1.40)
in a RANGING or REVERTING regime, buy NO on the OTM band in the breakout
direction inside the final 15 minutes. `ENABLE_YES` and `ENABLE_SNIPE` are off
— those lanes *bought* premium and lost -$413 and -$670 in the real trade log.

---

## 2. Claims and their evidence

Ordered by how much weight each can bear. The confidence label is the honest
one, not the one the number looks like.

### WELL SUPPORTED — the model does not beat the Kalshi price

Five independent attempts, 9,110 contract-observations. Market mid Brier
**0.1611**; Student-t df=3 **0.1632**; Gaussian **0.1726**. A learned
recalibration of the market itself, fit on 43 expiries, *lost* out of sample on
the other 44 (0.1525 vs 0.1506).

This is the foundational result — read it before believing any new forecasting
idea. The original Gaussian understated P(YES) in every bucket, and since
signals fire on `yes_bid / true_prob`, which is largest exactly where
`true_prob` is most understated, **the signal was partly detecting its own
bias.**

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

Note the trap in the last row: +27.2% on VALID, at **n=3**, with TUNE at
-103.8%. Cherry-pick it and you ship a disaster. **No dip level is positive on
both halves.** Best is not dipping.

    config  WATCHLIST_ENTRY_DIP — still 0.05, i.e. still on
    docs    docs/CONFIG_RATIONALE.md#watchlist_entry_dip

### PROVISIONAL — check this hardest — the exit fix, +6.3% ROC, PF 1.84

The most consequential recent change and the one most likely to be wrong.
Gating `edge_gone` on a 15% minimum gain, and tightening `NO_STOP` 0.40 -> 0.30:

    configuration                  WR     ROC    PF    total          95% CI  P>0    TUNE   VALID
    old: edge_gone>0, stop 40%    66%   -3.8%  0.59  -$14.22  [-12.2%, +3.3%] 16%   -7.2%   -1.4%
    shipped: >15%, stop 30%       80%   +6.3%  1.84  +$23.02  [-0.7%, +13.3%] 96%   +3.4%   +8.4%

**Why to doubt it:** n=41 over 33 expiries, the CI still touches zero, and
roughly **24 configurations** were swept the same day across dip levels, exit
variants and stop levels. One surviving a tune/validate split among 24 is not
surprising by chance.

**Why to believe the direction anyway:** the payoff arithmetic predicts it
independently. Risking $0.81 to win $0.19, a 40% stop realises $0.32 — **1.7x
the entire premium** — so one stop-out erases 1.7 winners. And decay above 30%
is monotonic (+3.7 -> -0.7 -> -2.2 -> -4.6 -> -5.4), not a spike.

Treat 0.30 as "40% was too loose" and 0.15 as "> 0", never as optima.

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

The pattern is worth internalising: **every one was overturned by a larger
sample, and every one was too optimistic.** That is a selection effect, not bad
luck — small samples that look good are the ones that get written down.

---

## 3. Re-running it

Everything below reads the recorded tape. No API keys needed except where noted.

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

**The single most useful check:** take any claim in the README, find the
`universe_*.jsonl.gz` days it cites, and re-derive it. The universe stream is
recorded *before* the ladder filters, so any gate can be moved and the whole
history re-scored against real settlement. That is how every finding here was
produced, and how three of them were overturned.

---

## 4. Traps

Each of these produced a confident, wrong number that survived review. Ordered
by how easy they are to fall into again.

**1. Frozen imports.** `from .config import X` binds a snapshot at import time.
Mutating `C.X` later — which every sweep does — silently changes nothing. This
actually bit: a 7-value threshold grid produced *byte-identical* trades and
nobody noticed. Read config as `_C.X`, module-qualified.
Still present in `ladder.py` — known, unfixed.

**2. Two flags where one silently gates the other.** `WATCHLIST_ENTRY_DIP =
0.05` shipped alongside `DELAYED_ENTRY_DIP = 0.0`. The only write to `_pending`
sat inside a branch the second flag guarded, so the watchlist was **inert for
its entire life** while config, tests and README all described it running.
*Tell:* a feature that is "on" but whose log lines never appear. Grep for its
distinctive output before trusting that it ran.

**3. Settlement is not what the bot does.** Every analysis here resolves by
settlement. The bot exits early. That gap was invisible for a long time — and
when finally simulated, the live ladder was *worse* than holding (-$14.22 vs
-$8.84). *Ask of any figure:* does this hold to expiry? The bot does not.

**4. The session log is a screen recording.** `bot_session.log` captures the
live dashboard, which redraws its "RECENT EVENTS" panel every frame. Counting
occurrences multiplies every event by how long it stayed on screen — one signal
read as 9,659. Use `recordings/orders_*.jsonl.gz` for events; the log is for
eyeballing only.

**5. `closed` is not `settled`.** Kalshi's `closed` means trading stopped; the
outcome is determined later. Treating it as settled booked an outcome from *our*
spot at an arbitrary moment — once crediting a full $1.00 on a contract $59
outside its band with ten minutes to run. Fixed; pinned by
`test_settlement_detection.py`.

**6. Float comparison on cent-grid prices.** `0.33 - 0.28` is
`0.050000000000000044`; `0.38 - 0.33` is `0.04999999999999999`. Against a 0.05
bar, **41% of exact 5c spreads were wrongly rejected.** The same class of bug
charged an extra cent of fee on one side of the book. Round to cent precision
before any threshold comparison.

**7. The median is always positive here.** At any win rate above 50% the median
trade is a win. Both policies show positive medians (+16.6%, +26.6%) while both
means are negative. Watch this strategy live and you see mostly winners; the
left tail eats it later. Judge on expectancy and profit factor — never hit
rate, never the median trade.

**8. Gates measured against the wrong leg.** The spread gate divided by
`yes_ask` while the bot pays the NO cost. Since `BOUNDARY_NO` targets ask
0.10-0.65 by construction, the denominator was always the small one — **the
gate rejected hardest exactly where trading was cheapest** (5c on an 0.85 NO
reads as 25% when it costs 6%).

**9. The instrument is not constant.** 97% of hourly bands are $100 wide; 3%
are $250, and it varies by *window*, not by time to expiry — one hour can be
250-wide for its whole life while the hours either side are 100. Any
absolute-dollar gate silently changes meaning. Also: the ticker number is the
band **midpoint**, not its floor.

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

Gzipped JSON Lines, one file per stream per UTC day, in `recordings/`. 93 files,
82 MB, from 2026-07-28. **Gitignored — it exists on one laptop and one iCloud
mirror.** Kalshi publishes no historical order book, so a lost day is lost
permanently.

    stream    days  one record per                only this can answer
    universe    11  ladder poll, PRE-FILTER       counterfactuals: move a gate,
                                                  re-score the whole history
    quotes      22  scan tick + regime            what the classifier saw
    books       22  book snapshot, both ladders   depth at size
    marks       17  held-position mark            prior/market/posterior per tick
    orders      17  order attempt + full book     fill quality; rejects are as
                                                  useful as fills
    walls        1  Deribit open interest         irreplaceable, no historical API

`universe` is the valuable one: 268,000 polls, 47.3M market-observations,
written before any filter. `quotes` and `books` are already filtered and cannot
answer "what would a different gate have done?"

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
