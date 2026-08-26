# How this thing is wired

This is for anyone poking at the codebase. It explains how the pieces fit
together, where each decision actually gets made, and where I think it's
weakest — so you can find holes without reading 5,000 lines first.

It's not a performance writeup. The returns are roughly break-even-to-negative
and that's said plainly below, but the numbers aren't the point of this
document. How it's built is.

    branch   model-calibration-and-exit-fixes @ f69e7e5
    tests    153 across 16 files
    data     93 recording files, 82 MB, back to 2026-07-28
    mode     paper — no real orders

---

## 1. What it's doing, in one paragraph

It sells premium, the same way selling an out-of-the-money option does. You
can't short a contract on Kalshi, so instead it buys the NO side of a price
band BTC probably won't reach in the next few minutes. Same bet, different
wrapper: you get paid now for saying something won't happen.

The economics of that are unforgiving, and worth understanding before anything
else, because every design decision downstream is a consequence of them.

You pay about **81 cents** for a contract that pays **a dollar**. So you make 19
cents when you're right and lose 81 when you're wrong. That means you need to
be right **81% of the time just to break even.** We're right about 80%.

    1 point short of break-even     -1.7%
    Kalshi's taker fee              -1.3%
    --------------------------------------
    expected                        -3.0%
    measured over 41 trades         -3.4%

Two things follow, and between them they explain most of the code:

- **There's no cushion.** A fill two cents worse than expected moves
  break-even more than most of the edges anyone's chasing are worth. That's why
  there's so much machinery around fees, spread and fill price.
- **The win rate is close to meaningless on its own.** You could push it to 85%
  by buying more expensive contracts and still lose money, because the bar you
  have to clear goes up with the price.

---

## 2. How it's wired

Six threads, all started in `app.py`, each on its own timer. They share state
through a `Portfolio` object and a `PendingEntries` queue. No message bus, no
async — just threads and a lock.

    thread      every   what it does
    price         ~1s   pull BTC spot from Coinbase's exchange ticker
    scan           2s   look for something to buy
    position       —    check what we're holding, decide whether to exit
    book           5s   snapshot order books to disk
    sync          20s   reconcile our idea of the account against Kalshi's
    summary      180s   print a status line

### The buy path

All of this happens inside `scan_step()` in `app.py`, every 2 seconds.

**1. Read the market.** `feed.py` gives spot. `regime.py` turns recent price
history into a label (RANGING / TRENDING / REVERTING / BREAKOUT), a z-score, a
volatility estimate and a momentum number.

**2. Build a candidate list.** `ladder.py` fetches Kalshi's contracts for the
nearest expiry and throws out anything untradeable — no quote, too little
volume, spread too wide. *This filter runs before anything is recorded as a
signal, and it had the spread test backwards for a while. See mistake 7.*

**3. Look for a setup.** `signals.py::find_boundary_no()` wants all of:

    regime is RANGING or REVERTING          not trending
    |z-score| >= 1.40                       BTC is stretched
    4.8 to 15 minutes until expiry          the window
    contract is $10-250 out of the money    far enough to be cheap
    YES price between 0.10 and 0.65
    yes_bid / model_probability >= 1.60     market looks too confident
    net edge >= $0.05                       worth doing after costs
    spot hasn't moved >$25 toward the band  the quote isn't stale

That last one exists because Kalshi's prices trail spot by about 20 seconds
(measured over 1.2M observations, peak correlation at 20s). You can't trade the
lag directly — a round trip costs two spreads against the ~9.5% the lag pays —
but refusing to buy a quote that hasn't caught up yet is free.

**4. Price it.** `model.py` gives a probability: a Student-t distribution (fat
tails, `df=3`), then blended with the market's own implied probability. That
blend matters more than it looks — the model on its own is *worse* than the
market price, so the market is doing real work inside that number.

**5. Buy it now.** `pending.py` can hold a contract back and wait for a
cheaper price, but that's switched off — every dip level measured worse than
not waiting. Section 4.

**6. Actually buy.** `portfolio.py::buy_no()` re-fetches a fresh quote,
re-checks the edge, walks the real order book for depth, and places an
immediate-or-cancel order. In paper mode it still walks the real book, so
depth and partial fills are genuine even though the money isn't.

### The sell path

`positions.py::manage()`, on the position thread. Checked in order:

    up 80%                          take it
    up 40% and under 5 min left     take it
    down 65%                        catastrophe floor, ignores the min-hold
    down 30%                        stop out
    under 2 min left                get out
    up 15% and the edge is gone     thesis exit

That last one used to fire on *any* profit, which sounds harmless and wasn't —
it sold winners early and cost more than the stop was saving. Section 4.

### The recording path

Completely separate. `recorder.py` runs a background thread reading a bounded
queue. If the queue fills it drops writes rather than blocking the trading
loop. Six streams, gzipped JSON, one file per stream per day.

---

## 3. Where each decision is actually made

If you want to challenge a specific behaviour, go here.

    decision                          file           function / setting
    what price is BTC                 feed.py        fetch()
    what regime are we in             regime.py      detect()
    which contracts are tradeable     ladder.py      get()
    is this a setup                   signals.py     find_boundary_no()
    what's it really worth            model.py       posterior_prob()
    should we wait for a better price pending.py     gate(), watchlist_fills()
    how many contracts                portfolio.py   quarter-Kelly, 2.5% cap
    can we actually fill it           portfolio.py   buy_no(), _walk_book()
    should we get out                 positions.py   manage()
    what gets written to disk         recorder.py    record_*()

Every threshold lives in `config.py` — 195 lines, settings only. The reasoning
behind each one is in `docs/CONFIG_RATIONALE.md`, keyed by setting name.

---

## 4. Where I think it's weakest

The honest list. This is where help would be most useful.

**The entry logic has no edge and I can't find one.** Five separate attempts to
predict outcomes better than the Kalshi price all failed. Market Brier score
0.1611; the best model manages 0.1632. A learned correction to the market
itself was fit on half the data and lost on the other half. If there's an edge
here it needs information the price doesn't already contain — cross-venue
timing, order flow, something structural. Not a better distribution.

**"Buy it cheaper" is a dead end, and I'd like someone to tell me why.** Every
dip level measured worse than not waiting, monotonically — win rate falls from
80% to 33% as the discount deepens. Three separate attempts at variations of
this (scale-in, dip-adding, the watchlist) reached the same result on separate
samples: the discount arrives on the contracts that were going to lose anyway.
Switched off now. But cheaper entry is the *only* lever that widens a margin
this thin, so if there's a version of it that works I haven't found it.

**The exit changes are new and thinly evidenced.** I gated the thesis-exit and
tightened the stop two days ago. 41 trades, confidence interval still touching
zero, and I tried about 24 configurations that same day — try 24 things and one
passing a split test is expected, not impressive. I believe the *direction*
because the arithmetic supports it independently: a 40% stop takes a 32-cent
loss against a 19-cent upside, so each stop-out wipes out nearly two winners.
I don't believe the specific numbers. **This is what I'd most want a second
opinion on.**

**The backtest measures something that isn't running.** Under the live config
it produces zero trades. Every backtest number in this repo comes from two
strategies that are switched off. It's a regression test on the simulator, not
evidence about the strategy.

**Almost every measurement assumes we hold to expiry, and we don't.** The bot
exits early. That gap hid a real problem for weeks — when finally simulated,
the exit logic was performing *worse* than just holding.

**Band width isn't constant and the code mostly assumes it is.** Most hourly
bands are $100 wide, ~3% are $250, and it varies hour to hour. One gate now
scales with it. The others are deliberately still fixed dollar amounts — the
reasoning is that "will spot move $250" is a volatility question and shouldn't
change just because Kalshi drew the grid differently. That's a judgement call
and worth arguing with.

**The config-freezing bug was worse than I thought when I wrote this.** Eleven
settings across five modules were bound at import, so any sweep against them
silently did nothing. Fixed, and `test_frozen_config.py` now scans for it
automatically — it found one more within a minute of being written. Checked
rather than assumed: no past sweep went through those modules, so no earlier
conclusion is void. But that's the kind of thing where I'd rather have a second
pair of eyes than my own reassurance.

---

## 5. Mistakes already made

Worth reading not as confession but because each one teaches something about
how the system is put together. Every one produced a confident number that was
wrong and got past review at the time.

**1. Config changes that don't change anything.** `from .config import X` copies
the value once, at import. A sweep that sets `C.X` later never reaches it — the
code keeps working, but the tooling silently lies, and a sweep that never varied
anything looks like a finished experiment saying "this parameter doesn't
matter". A seven-value threshold sweep once produced seven identical results.
Found and fixed three separate times before someone finally counted: eleven
settings across five modules. `test_frozen_config.py` now checks for it.

**2. A feature that's on but can't run.** `WATCHLIST_ENTRY_DIP` was set to 0.05
so it looked live, but the code that arms it sat behind a different flag set to
0. It never ran once, while config, tests and README all said it was working.
*Look for:* a feature that's "on" whose log lines never appear.

**3. The session log isn't a log.** `bot_session.log` records the live
dashboard, which redraws its events panel every couple of seconds. Counting
occurrences counts screen time, not events — one signal appeared 9,659 times.
Use `recordings/orders_*.jsonl.gz` for anything you're counting.

**4. "Closed" doesn't mean "settled".** On Kalshi, closed means trading stopped;
the outcome is decided later. Treating it as final once credited a full $1.00
on a contract sitting $59 outside its band with ten minutes still to run.

**5. Prices are cents, floats aren't.** `0.33 - 0.28` comes out
`0.050000000000000044`; `0.38 - 0.33` comes out `0.04999999999999999`. Against
a 0.05 limit one passes and one fails — **41% of exact 5-cent spreads were
rejected for no reason.**

**6. The typical trade always looks good.** Win more than half the time and the
median trade is a winner by definition. Both strategies show a positive median
while both lose money on average. Watch it trade and you'll see mostly winners,
right up until a few bad ones take it all back.

**7. Measuring a cost against the wrong side.** The spread check divided by the
YES price while we pay the NO price — and this strategy only looks at cheap YES
prices, so it always divided by the small number. It rejected hardest exactly
where trading was cheapest.

**8. The contracts aren't consistent.** ~3% of hourly bands are $250 wide
instead of $100, varying hour to hour rather than by time to expiry. Anything
written as a fixed dollar amount quietly means something different on those.
Also: the number in the ticker is the **middle** of the band, not the bottom.

---

## 6. Checking it yourself

**Run the tests first — they encode the bugs above.**

    for f in test_*.py; do python3 "$f" >/dev/null || echo "FAIL $f"; done

153 tests, 16 files. Some are policy rather than behaviour: `test_no_exits.py`
and `test_watchlist_entry.py` refuse to let an unvalidated feature run while
`PAPER_TRADING` is False.

**Re-derive any claim.**

    python3 boundary_no_quote_replay.py --bootstrap 10000

**If you only do one thing:** pick a claim, find the days of
`universe_*.jsonl.gz` behind it, and work it out yourself. That stream is
recorded *before* any filtering, so you can change any rule and re-score the
whole history against what actually happened. That's how everything here was
worked out, and how several earlier conclusions got overturned.

### What's recorded

    stream    days  per record                    what only this can answer
    universe    11  a full ladder poll,           what a different rule would
                    before filtering              have done
    quotes      22  a scan tick + regime          what the classifier saw
    books       22  order book, both sides        was there depth at size
    marks       17  a held position               model vs market, tick by tick
    orders      17  an order attempt + the book   fill quality; rejects included
    walls        1  Deribit open interest         can't be re-fetched, ever

Not in git — one laptop, one iCloud copy. Kalshi publishes no historical order
book, so a lost day is gone for good.

---

## 7. Code map

    file            lines  what's in it
    portfolio.py     1222  order placement, fresh-quote checks, depth walking
    positions.py      563  the exit ladder, settlement detection
    signals.py        459  find_boundary_no, strike clustering, lag filter
    app.py            379  the six threads and how they're wired together
    recorder.py       343  the recording queue and flush policy
    pending.py        257  arming and the wait-for-a-dip logic
    model.py          219  Student-t prior, market blend
    config.py         195  settings only
    ladder.py         189  the pre-filter — frozen imports live here

    README.md                      strategy, current numbers, known gaps
    docs/BACKTEST_INTEGRITY.md     ten ways a backtest number breaks. Start at 0
    docs/CONFIG_RATIONALE.md       why each setting is what it is
    docs/QUANT_STANDARDS_AUDIT.md  methodology vs practitioner norms
    docs/STRATEGY.md               the math

---

Generated 2026-08-26, paper mode. Figures come from recorded data resolved
against actual settlement unless said otherwise. Sample sizes are small and
named wherever they matter.
