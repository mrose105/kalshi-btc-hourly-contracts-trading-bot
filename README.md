# Kalshi BTC Hourly Contracts Trading Bot

**A premium-selling strategy adapted to prediction markets.**

This is short-tail options selling expressed in binary event contracts. On
Kalshi's hourly BTC markets there is no option chain to short — you cannot sell
what you do not own — so the position is taken by *buying the NO side* of an
out-of-the-money band, which is economically the same trade: you are paid now
for the claim that BTC will not be in that band at expiry.

Read it as selling a far-OTM strangle leg and holding it into expiry:

| options | this bot |
|---|---|
| sell an OTM option, collect premium | buy NO on an OTM band at ~$0.81 |
| premium collected | $1.00 − $0.81 = **$0.19** |
| max loss | the $0.81 at risk |
| strike | the band's `[lo, hi)` edge |
| assignment | spot settles inside the band |
| theta | the 15-minute window — time passing *is* the return |
| sell into elevated fear | enter at \|z\| ≥ 1.40, where continuation is overpriced |

Everything that follows in this README is a consequence of that payoff shape,
so it is worth stating the defining number up front:

> **Break-even win rate equals the entry cost.** At $0.81 you must be right
> **81%** of the time to break even. Measured across 41 settlement-resolved
> signals, the win rate is **80%**.

That is the whole strategy in one line. It is the classic short-premium
profile — a high hit rate, a small win, and a rare loss that erases several
wins — and it sits *directly on* its own break-even. There is no comfortable
margin, which is why this repo is obsessive about fees, spread, and entry
price: a 2¢ worse fill moves break-even more than most edges are worth.

The corollary is a rule this codebase enforces everywhere: **cheaper entry is
the only lever that widens the margin, and it is not free.** Waiting for a
discount lowers break-even and lowers the win rate at the same time — see
"Risk Profile" and `docs/CONFIG_RATIONALE.md#watchlist_entry_dip` for what
happened when that was measured.

## Poking holes in this

Start with [`docs/AUDIT_GUIDE.md`](docs/AUDIT_GUIDE.md) — how the pieces fit
together, where each decision actually gets made, where I think it's weakest,
and the mistakes already made so you don't have to find them the slow way.

## What actually runs

One strategy: `BOUNDARY_NO`. When BTC sits at a range extreme (|z| ≥ 1.40) in a
RANGING or REVERTING regime, the market overprices continuation, so the bot
buys NO on the out-of-the-money band in the breakout direction inside the final
15 minutes, and exits on `edge_gone`, a 40% stop, or settlement.

`ENABLE_YES` and `ENABLE_SNIPE` are **off**. The vol-compression YES thesis
described further down is documented history, not live behaviour, and lost
-$413 and -$670 in the real trade log. Note the direction: those lanes *bought*
premium. This one sells it.

**Status: paper.** `PAPER_TRADING = True`, and paper fills walk the real Kalshi
order book rather than filling at a quoted price, so depth and partial fills
are live even though the money is not. Several shipped entry features are
measured on samples of n=11 to n=41 and are gated by tests that refuse to let
them run against the real account. See "Risk Profile" for what that sample size
means, and read it before quoting any figure here.

### Currently collecting: signal data alongside the paper trade log

The bot is running to **accumulate data, not to prove a return.** Two distinct
records are being written in parallel, and they answer different questions:

| record | stream | what only this can answer |
|---|---|---|
| **signal data** | `universe` — the *uncensored* ladder, written **before** any filter | which contracts existed, at what price, in what regime — so any gate can be re-evaluated counterfactually against what actually settled |
| **paper trade log** | `orders`, `marks` | what a real order would have met: book depth at size, partial fills, the drift between the decision price and the executable one |

As of 2026-08-25: **268,000 ladder polls / 47.3M market-observations** across 11
days of `universe`, 22 days of `quotes` and `books`, 17 days of `orders` and
`marks` — 90 files, 74 MB, from 2026-07-28.

The signal record is the more valuable of the two and the reason every figure
on this page is settlement-resolved rather than simulated. Because it is
recorded pre-filter, a gate can be moved and the entire history re-scored
against real outcomes — which is how the watchlist policy was measured, how the
regime gate was shown to block only 15% of signals, and how the band-width
change was caught.

The paper log covers the gap the signal record cannot: **top-of-book replay
assumes a fill.** Live paper trading walks the real book, so it is the only
source for whether size 11 is actually available, how far an order walks, and
how much the quote moves between decision and execution. Every *rejected* order
attempt is recorded with the full book, so failures are as informative as fills.

**Do not read P&L from the current run as evidence.** At roughly 20-40 signals
per week the returns are noise; the fill distribution is the point. `recordings/`
is gitignored and can never be reconstructed — Kalshi publishes no historical
order book — so run `./backup_recordings.sh` before the machine sleeps.

---

## Backtest Results (60-day walk-forward, re-run 2026-08-24)

> **The backtest does not exercise the strategy that currently runs.** With
> `ENABLE_YES` and `ENABLE_SNIPE` set to `False` — the live configuration — a
> 60-day run produces **zero trades**, because it generates no BOUNDARY_NO
> entries at all. Every figure in the table below therefore comes from the YES
> and snipe lanes, both of which are disabled live and which lost **-$413** and
> **-$670** respectively across the real trade log. Treat this section as a
> regression check on the simulator, not as evidence about live performance.
> See `docs/BACKTEST_INTEGRITY.md` before quoting any of it.
>
> **Known parity gaps, open as of 2026-08-25:**
> `BacktestPortfolio` is a separate class from the live `Portfolio`. Fees are
> charged in both (pinned by `test_fee_parity.py`), but these live-only
> features have **no backtest implementation**: delayed entry, watchlist entry,
> minimum hold, depth-confirmed exits, and the lag filter. They were measured
> against recorded ticks resolved by settlement instead — which is the better
> measurement anyway, but it means the two paths are not the same code and
> nothing automatically catches them drifting apart.
> Also open: the SPX distance constants are still BTC values scaled by the
> 0.0857 σ ratio rather than calibrated, and the SPX vol cone is derived from
> yfinance bars rather than the bot's own feed. Both say so in
> `instrument.py`.
>
> **And the YES lanes disagree with reality.** The +108% row below is the YES
> and snipe lanes; the real trade log has them at **-$413.00 over 155 trades
> (42% WR)** and **-$670.66 over 76 trades**. `build_ladder()` manufactures
> synthetic quotes from a lagged-vol version of the same model family being
> tested, so the simulator scores the model against prices the model produced.
> Both lanes are disabled live.

| Run | Return | Sharpe | Profit factor | Win rate | Max DD | Trades |
|---|---:|---:|---:|---:|---:|---:|
| all lanes, Gaussian prior | +108.4% | 7.23 | 1.86 | 47.2% | -7.8% | 246 |
| all lanes, Student-t prior (previous) | -33.2% | -10.60 | 0.58 | 34.3% | -33.8% | 210 |
| **all lanes, current config (2026-08-24)** | **-43.2%** | **-8.41** | **0.62** | **35.8%** | **-26.3%** | **193** |
| **NO-only (matches live config)** | — | — | — | — | — | **0** |

The current-config run is `$500 → $283.98`, avg win +$2.84 against avg loss
-$2.54, 6.3 bars held. Its composition is the thing to read, not its return:
**`yes_trades=193`, `no_trades=0`** — 184 of the 193 fired inside the
compression gate. Adding fee accounting (`CHARGE_FEES`) and raising
`BOUNDARY_NO_OVERPRICING_MIN` to 1.60 moved the number from -33.2% to -43.2%,
but every one of those trades is in a lane that is switched off live. The run
is a regression check on the simulator. It is not a measurement of the
strategy.

The Student-t "collapse" is not a regression, and reading it as one would be
backwards. For YES, `edge = true_prob - ask`, so a prior that under-predicted
`true_prob` was suppressing YES entries. Correcting the calibration removes
that accidental brake, and YES is the lane that loses money. Live this changes
nothing, because YES is off — but it is a landmine if `ENABLE_YES` is ever
flipped back on without re-validating the YES entry gates against the corrected
prior.

What the strategy that *does* run was measured on instead: recorded market
data, resolved by settlement rather than by simulated quotes. See
"Measured on recorded data" below.

The older +78.6% figure was a more conservative number than the pre-fix
posterior run because the
synthetic backtest no longer treats synthetic bid/ask quotes as independent
market evidence. `build_ladder()` manufactures quotes from a lagged-vol version
of the same model family being tested; feeding those prices into the Bayesian
market update was circular. Synthetic backtests now use the raw GBM prior for
entry selection, while live/paper and real quote replay use the market posterior.

Queued entries are also re-priced at the next bar's open: ask, hours-to-expiry,
true probability, and edge are recomputed at fill time before the simulated
position is opened. That mirrors the live path, where every entry re-fetches a
fresh quote and recomputes posterior immediately before sizing/execution.

To inspect BOUNDARY_NO behavior under the synthetic pricing assumptions, run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 kalshi_btc_backtest.py --days 60 --capital 500 --z-sweep
```

The z-sweep leaves YES/SNIPE active, enables the boundary-NO scan, and reports
YES P&L, NO P&L, win rates, drawdown, Sharpe, and average NO P&L per trade side
by side. By default it sets generic MISPRICE_NO's threshold to `999`, which
effectively isolates the z-score-driven BOUNDARY_NO path.

This sweep **does not tune the live z-score**. Live `zscore(300)` measures five
minutes of roughly 2-second ticks; the synthetic feed stretches the same call
to roughly 150 five-minute bars (12.5 hours) to match sample count. Matching the
marginal z distribution does not preserve the predictive horizon. Synthetic
quotes and exits are also generated from the same model family used to rank
them, so synthetic P&L cannot establish real Kalshi inefficiency.

Use the raw recorded universe joined to contemporaneous quote/regime snapshots
for the live entry-edge question:

```bash
python3 boundary_no_quote_replay.py --bootstrap 10000
```

That replay runs the production BOUNDARY_NO selector on contemporaneous live
z-scores and real Kalshi bids, respects `MAX_POSITIONS` and same-expiry strike
clustering, and holds selected contracts to recorded settlement. It reports a
deterministic expiry-clustered confidence interval. It is an entry-quality test:
top-of-book only with the conservative one-contract KXBTC taker fee, excluding
latency, depth, stops, and capacity.

Audited 2026-08-18, the uncensored raw-universe replay contradicts the synthetic
result. At `z=1.40` it resolves 37 selections across 30 expiry clusters: 62.2%
wins at a mean all-in NO cost of 0.751, for **-17.3% EV per dollar**
(expiry-clustered 95% CI [-37.0%, +2.2%], with 95.8% of bootstrap samples
non-positive). A superficially high win rate still loses when each loss risks
roughly 75c to win 25c.

### Measured on recorded data (2026-08-20/21)

The synthetic backtest cannot evaluate the live NO strategy, so it was measured
directly against `recordings/universe_*.jsonl.gz` — the **uncensored** stream,
recorded before the ladder filters — with each contract resolved by where spot
actually settled versus its `[lo, hi)` band. 775–810 contracts, ~685k book
ticks, 87 expiries, 2026-08-12 to 08-21.

**The model was miscalibrated, and the market was not.** 9,110
contract-observations, lower is better:

| predictor | Brier | log-loss | mean pred | bias |
|---|---:|---:|---:|---:|
| market mid | **0.1611** | **0.4973** | 23.5% | -0.9% |
| Student-t df=3 (shipped) | 0.1632 | 0.5043 | 21.4% | -2.9% |
| Gaussian prior (previous) | 0.1726 | 0.5312 | 17.6% | -6.7% |

The Gaussian understated P(YES) in **every** probability bucket, worst at the
extremes (far-OTM 3.9% predicted vs 14.5% realised; near-money 66.6% vs 90.3%).
Since signals fire on `yes_bid / true_prob`, which is largest exactly where
`true_prob` is most understated, **the signal was partly detecting its own
bias.** That is the single most important thing on this page.

**No configuration beat the market price.** Five independent attempts:

1. Gaussian as-is — Brier 0.1726 vs market 0.1611
2. Gaussian with vol rescaled x0.4 to x2.5 — best 0.1712, still loses
3. Student-t df=3 — 0.1632, closes 82% of the gap, still loses
4. Learned recalibration of the market itself — fit on 43 expiries, scored on
   44: 0.1525 vs the market's 0.1506. **Loses out of sample**; adding features
   made it worse, the signature of fitting noise
5. Resting orders to earn the spread instead of paying it — the 2.4% saving is
   real, but P(win | filled) is 76.0% against 91.7% when not filled, a -15.7pp
   adverse-selection gap ~4x the spread saved

No NO-cost region shows edge either: every bucket from 0.55 to 1.00 is negative
or indistinguishable from zero across 8,866 observations / 88 expiries.

**Read this as the operating assumption:** on this instrument, at this horizon,
using public price history, the Kalshi price is the best available forecast.
The strategies differ in how much they lose, not in edge. An edge would require
an information source the price does not already contain — cross-venue
lead-lag, a faster feed than the exchange consensus, or order-flow
microstructure.

### What "60/40" means here

**A probability-quality reference, not a price cap.** It describes the
confidence profile worth trading — a selected outcome the model puts near 60%
against a market implying nearer 40% — and says nothing about what a contract
may cost.

A trade requires exactly one thing: **positive net edge between the
selected-side model probability and the executable market price.** For NO that
is `(1 - true_prob) - (1 - yes_bid)`, measured at the price that actually
fills, and it is enforced at selection (`signals.py`) and re-checked against a
fresh quote before execution (`Portfolio.buy_no`).

There is **no 40c break-even cap and no 38c entry cap.** An earlier revision
read the "60/40" framing as a payoff contract and added `MAX_ENTRY_PRICE`,
`TARGET_MAX_BREAK_EVEN`, `MAX_LADDER_YES_ASK`, fee-aware sizing, and
fee-adjusted stops/P&L. That was a strategy change nobody requested and it has
been reverted. `MAX_ASK` (0.45) is the YES entry ceiling, as before.

Do not introduce a 60% probability gate or an entry-price cap without an
explicit request and a held-out validation. The replay figures above are
research diagnostics on one recorded sample, not forward performance.

For a clean NO-only paper test, set `ENABLE_YES = False` and
`ENABLE_SNIPE = False` in `kalshi_btc_bot/config.py`, then restart the bot. The
corresponding synthetic scenario command is:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 kalshi_btc_backtest.py --days 60 --capital 500 --no-only
```

There is still no single headline return — the strategy has a measured
**capacity limit**, and which side of it you're on determines the sign of the
result. Past roughly a few thousand dollars, Kelly sizing wants positions larger
than Kalshi's real KXBTC book depth can absorb without severe exit slippage.
Use capital sweeps before quoting scale-sensitive performance.

> **Earlier figures in this README were void and have been replaced.** Runs before 2026-08-07 simulated a 250-wide RANGE band on *every* contract, while **97% of real KXBTC hourly bands are 100 wide** — measured across ~700,000 `floor_strike`/`cap_strike` observations.
>
> **But the width is not constant, and the bot must not assume it is.** The remaining 3% are 250 wide, and it varies by *window*, not by time to expiry: on 2026-08-25 the 14:00, 15:00, 16:00 and 18:00 windows were each 186 markets at 100 wide, while 17:00 was 78 markets at **250 wide for its entire life**, from four hours out to expiry. Kalshi holds coverage roughly constant (186x100 ≈ 78x250 ≈ $19k) and picks a grid per window. The ticker number is the band **midpoint**, not its floor. `STRIKE_CLUSTER_DIST` now scales with the contract's own width; the σ-based distance gates deliberately stay in dollars. See `test_band_width.py`. The wider band made every simulated contract 2.3–4.7× likelier to pay, inflating returns roughly 4×. Details in [`docs/QUANT_STANDARDS_AUDIT.md`](docs/QUANT_STANDARDS_AUDIT.md) §1d.

**A backtest return is not a live forecast.** Read [`docs/BACKTEST_INTEGRITY.md`](docs/BACKTEST_INTEGRITY.md) before quoting any number here — it documents what the simulation still cannot represent, including that `build_ladder` synthesises its own quotes rather than replaying a recorded book.

### Bias-elimination fixes applied

1. **Fills execute at NEXT bar's open, not current bar's close** — removes the lookahead where a signal generated at bar close was also filled at that close.
2. **Expiry settlement uses bar OPEN, not close** — expiry happens during the bar, so end-of-bar spot is a lookahead.
3. **Adverse-selection haircut on model-derived exit bids** — near expiry `true_prob` collapses toward 0/1; the exit bid is discounted (up to ~15%) since market-makers won't quote fair value on "certain" contracts that can still whipsaw. Without this the backtest inflated ~4×.
4. **Intrabar stop slippage** — stops fill 2¢ worse than the theoretical threshold, matching live `FORCE_EXIT_SLIPPAGE_CENTS`.
5. **Depth-aware size penalty** — exit fills walk a √-law impact curve rather than clearing at the mark.
6. **Correct 100-wide RANGE band** — see the note above.
7. **Real-time regime windows** — momentum is measured over the same wall-clock window in backtest and live. Previously one constant meant 60 seconds live but 2.5 hours in the backtest, so the two reported near-opposite regime mixes and nothing regime-dependent was validly tested (§1e).
8. **Synthetic posterior disabled** — synthetic Kalshi quotes are no longer treated as Bayesian market evidence.
9. **Fill-time revalidation** — next-bar fills recompute probability and edge at the open before entering.

## Risk Profile — 10,000-path Monte Carlo (at $500)

Bootstrapping the *backtest* would resample the YES lanes, which are off. So
this runs on the live strategy's own distribution: every `BOUNDARY_NO`
selection passing the **current** gate set on recorded universe data, each
resolved by where spot actually settled against its `[lo, hi)` band, with the
Kalshi taker fee charged. 88 armings across 66 expiries, 2026-08-18 to 08-26.

Capital is `PAPER_CAPITAL = $500` and sizing follows the live path — 2.5% of
equity per trade (`MAX_TRADE_PCT`), which is **$12.50, or 15 contracts** at the
$0.805 mean entry.

| per trade | |
|---|---:|
| n / expiries | **88 / 66** |
| win rate | 85% |
| mean entry | $0.805 |
| **break-even WR** (= entry cost) | **81%** |
| mean ROC | **+4.5%** |
| median ROC | +20.0% |
| std dev | 44.9% |

Expiry-clustered bootstrap over the 66 expiry means:

| method | 95% CI |
|---|---|
| percentile | [−6.3%, +13.1%] |
| **BCa** (bias-corrected, accelerated) | **[−7.8%, +12.3%]** |

**Note BCa widens the downside** rather than narrowing it — −7.8% against
−6.3%. That is the correction doing its job: the distribution is heavily
left-skewed (median +20.0% against a mean of +4.5%), and a percentile interval
assumes a symmetry that is not there. The honest interval is the wider one, and
**it still contains zero.**

10,000 compounding paths, resampled by expiry cluster:

| horizon | p5 | p25 | median | p75 | p95 | median DD | p95 DD | P(profit) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 day (12 trades) | $473 | $497 | **$506** | $520 | $537 | −2.5% | −7.1% | 70% |
| 1 week (60) | $462 | $504 | **$535** | $565 | $608 | −6.8% | −13.8% | 77% |
| 1 month (250) | $490 | $582 | **$654** | $733 | $855 | −11.7% | −21.3% | 94% |

### How much of this to believe

**The point estimate is positive for the first time.** Every earlier version of
this table was negative; the gates fixed on 2026-08-26/27 (overpricing ratio
1.60 → 1.25, ask ceiling 0.65 → 0.30, `edge_gone` gated, stop 0.40 → 0.30) are
what moved it. That is real, and it is also the problem: **five sweeps were run
the day these numbers were produced.** The best cell of a sweep is a biased
estimate of what the next month does.

**The CI includes zero.** At n=88 with a 44.9% standard deviation, the data
cannot distinguish +4.5% from flat.

**P(profit) = 94% at one month is not a forecast.** A bootstrap cannot resample
a regime its 88 source trades never contained. Those trades span nine days of
one instrument in one vol environment.

**And it lost to buying BTC, badly.** Over the same window BTC went
$64,423 → $78,527, **+21.9%**. $500 held in spot is $609; the median simulated
path here is $535 over a *month*. This is a market-neutral strategy being
measured during a strong rally, which is the least flattering comparison
available — but the answer is still that holding the underlying beat it.

**The median/mean gap is the thing to watch in live trading.** +20.0% median
against +4.5% mean means most trades look good while rare losses do the damage.
Watching the tape will feel better than the account does. Judge it on
expectancy and profit factor, never on hit rate or the typical trade.

`SESSION_STOP_PCT` (3%) gates *new entries* only — it never closes open
positions, so it does not floor these drawdowns.

---

## Kalshi's Price Lag (measured 2026-08-23)

Kalshi's contract prices trail the underlying. Correlating a *past* Coinbase
move against Kalshi's *subsequent* repricing of the same contract, over 1.2M
recorded observations:

| lookahead | 2s | 10s | **20s** | 60s | 120s |
|---|---:|---:|---:|---:|---:|
| corr | +0.026 | +0.133 | **+0.180** | +0.083 | +0.049 |

Positive on all 10 days and all 8 hour-buckets, and **stronger on larger
moves** (+0.255 for moves ≥ $30 vs +0.179 below). It is structural. There is no
persistent basis to go with it: the band Kalshi prices highest contains
Coinbase spot 75% of the time at a median offset of exactly $0 — so an observed
gap is timing, and it closes.

**It is not tradeable directly.** A round trip pays two spreads, ~20% of a 20¢
contract, against ~9.5% the lag delivers. Every directional-lag configuration
tested came back negative. What *is* free is using it as a **filter**:
`LAG_FILTER_MAX_ADVERSE = 25.0` refuses any entry where spot has moved more
than $25 toward the band over the trailing `LAG_FILTER_SECS = 20`, because that
quote is stale and about to fall. The direction that counts flips with the side
being faded — z<0 fades bands below spot, so a *falling* spot is the adverse
one; z>0 is the mirror. Getting that backwards would reject exactly the good
entries, which is what most of `test_lag_filter.py` guards.

**The bot's own feed was half the problem.** It read
`api.coinbase.com/v2/prices/BTC-USD/spot`, a cached retail endpoint that itself
lags Coinbase's exchange ticker by ~10s (corr +0.739). Half the head start was
gone before the strategy saw a price. `feed.py` now reads
`api.exchange.coinbase.com/products/BTC-USD/ticker`, with the old endpoint kept
as a fallback. Webull tracks the exchange ticker at a fixed ~$19 offset and
leads the retail endpoint by the same 10s — it confirmed the finding but adds
nothing beyond the exchange feed.

`python3 feed_compare.py` shows all sources side by side with a rolling lag
correlation. Kalshi has no public spot endpoint, so its view is backed out of
the band ladder — peak band ±3 neighbours, price-weighted. Do **not** weight the
full ladder: with ~118 bands including deep tails that put the estimate $893 off
in a live check. The local version reproduced Kalshi's own displayed price to
the dollar ($77,749.99 against the app's $77,750.80).

---

## Historical thesis — Kalshi's Vol Lag (YES lanes, disabled)

> This is what the bot was originally built around. `ENABLE_YES` and
> `ENABLE_SNIPE` are `False`; these lanes lost -$413 and -$670 in the real
> trade log. Kept because the compression machinery still gates `find_best`
> and `find_snipe`, and because flipping either flag back on without
> re-validating against the corrected Student-t prior is a live landmine.

Kalshi prices RANGE contracts using a rolling average of historical vol. This bot uses a fast EWMA that responds in minutes. The gap between them creates structural mispricing:

```
When BTC consolidates after a spike:

  Fast EWMA:  ████░░░░░░  ← sees current calm
  SMA (24h):  ████████░░  ← still reflects the spike

  vol_ratio = EWMA / SMA = 0.45  (< 0.55 threshold → COMPRESSED)

Kalshi prices RANGE contracts as if vol is still elevated → assigns them 30¢
Our model prices the same contract at 45¢ true probability

  Edge = 45¢ − 30¢ = 15¢  →  BUY YES
```

During compression windows, 2–4¢ ATM RANGE contracts can settle at $1.00 — a 25–50× payoff.

---

## Architecture

```
BTCFeed          RegimeEngine       SignalEngine              PositionManager
─────────        ────────────       ────────────              ───────────────
EWMA vol    →   RANGING /     →   find_best (YES RANGE)  →  Multi-tier exit ladder
SMA vol         TRENDING /        find_snipe (OTM snipe)     on its own thread
vol_ratio       REVERTING /       find_boundary_no (NO)      (exits never blocked)
momentum        BREAKOUT          find_no_scalp (disabled)
zscore

4 independent background threads: price · sync · position · signal-scan
```

### Package structure

```
kalshi_btc_bot/
├── config.py       — all thresholds and risk params in one place
├── feed.py         — BTC price feed, EWMA/SMA vol, vol_ratio
├── regime.py       — market regime classifier
├── model.py        — binary option pricer; Student-t tails (DIST_TAIL_DF=3)
├── contracts.py    — ladder parsing, ITM/OTM helpers
├── ladder.py       — live Kalshi ladder fetcher
├── signals.py      — SignalEngine: entry filters, edge ranking, vol-term boost
├── positions.py    — PositionManager: 6-tier exit ladder
├── portfolio.py    — Kelly sizing, exposure limits, session stop
├── vol_surface.py  — Kalshi implied vol term structure (Brent's method)
├── app.py          — main loop (independent threaded price/sync/position/scan loops)
└── __main__.py     — entry point (`python3 -m kalshi_btc_bot`)
kalshi_btc_backtest.py  — walk-forward backtest with intrabar stop simulation
```

---

## Signal Engine — Entry Logic

Three parallel scans on each tick.

> **`TRADE_ONLY_COMPRESSION` (default on):** `find_best` and `find_snipe` return `None` unless the market is in a vol-compression regime. Segmenting a corrected-instrument backtest showed the *entire* loss came from trading outside compression (normal vol: -47.3% per $ risked; compressed: +18.4%), and gating on it flipped the sign on both windows of a tune/validate split. `find_boundary_no` is deliberately **not** gated — it collects mean-reversion premium at z-score extremes, a different edge thesis that works in normal/high vol, which is also when the order book is actually populated.


- **`find_best`** — probability-edge scan for the highest-edge contract. **RANGE-only in the RANGING regime**; TRENDING / REVERTING / BREAKOUT regimes also consider ABOVE / BELOW contracts, gated by the regime's direction (an ABOVE won't be bought during a confirmed downtrend, and vice-versa).
- **`find_snipe`** — separate ROI-ranked scan for cheap deep-OTM lottery tickets that `find_best` would never surface (small raw-edge points but 30%+ ROI on a 10–25¢ ask).
- **`find_boundary_no`** — mean-reversion premium-collection scan. When BTC is at a range extreme (|z-score| ≥ 1.40, RANGING or REVERTING regime), the market overprices the probability of continuation — OTM contracts in the breakout direction are too expensive relative to true probability. The bot buys NO on those contracts (betting BTC mean-reverts rather than breaks out), analogous to selling an OTM option at the extreme to collect overpriced premium. NO pays $1 if BTC fails to reach the OTM range by expiry. Exits via 40% stop-loss or expiry settlement.

  **This is the only lane that runs live.** Its own gates, all read
  module-qualified from `config.py` at call time:

  | gate | value | note |
  |---|---:|---|
  | `BOUNDARY_NO_HOURS_MAX` | **0.25** | 15 min. Was 0.50; ROC -4.5% → -1.5% |
  | `BOUNDARY_NO_OVERPRICING_MIN` | **1.60** | was 1.15, which was provably inert — sweeping below it changed nothing |
  | `BOUNDARY_NO_ZSCORE_MIN` | 1.40 | also inert below its current value |
  | `BOUNDARY_NO_MIN_NET_EDGE` | 0.05 | 0.00 still requires non-negative edge, but a 0.1¢ edge cannot survive a ~2.5% fee-and-spread load |
  | `LAG_FILTER_MAX_ADVERSE` | 25.0 | reject stale quotes — see "Kalshi's Price Lag" |
  | `WATCHLIST_ENTRY_DIP` | 0.05 | arm strict, fill on the model's valuation |

  `find_boundary_no(..., all_matches=True)` returns every qualifying contract,
  not just the best. (`find_no_scalp` still has the old single-best shape and
  would need the same treatment if `ENABLE_MISPRICE_NO` is re-enabled.)

  `DELAYED_ENTRY_DIP` is **0.0 (off)**, superseded by watchlist entry: a fixed
  dip floor waits for a price the gates may never re-approve, whereas the
  watchlist arms on the gates and then fills on the model's valuation.
  `DELAYED_ENTRY_DIP_MAX = 0.12` still caps it if it is ever switched back on.

  > **The watchlist was inert until 2026-08-24 and this README said otherwise.**
  > `_pending` is written in one place, inside `gate()`, which returned early
  > whenever `DELAYED_ENTRY_DIP <= 0`. Shipped config was `DELAYED_ENTRY_DIP =
  > 0.0` with `WATCHLIST_ENTRY_DIP = 0.05`, so nothing ever armed and
  > `watchlist_fills()` returned `[]` for the life of every process. The bot was
  > buying at arming — the baseline measured at **-1.7% ROC** — while the
  > config, the tests and this file all described the dip policy measured at
  > **+12.0%**. Found by asking why a 3h41m session placed zero orders when 122
  > observations had cleared every model gate. Arming is now enabled by
  > `_arming_on()`, true if *either* flag is set; `test_watchlist_entry.py`
  > fails if a switched-on feature is unreachable under the live config.
  > Any figure attributed to watchlist entry before this date describes
  > buy-at-arming, not the watchlist.

  `ENABLE_MISPRICE_NO` is off for an **operational** reason, not a performance
  one — its measured edge was real. Re-enabling it requires fixing
  `find_no_scalp`'s single-best shape first.

Filters applied before every entry:

| Filter | Description |
|--------|-------------|
| Expiry gate | 6 min – 4 hours to expiry (`MIN_HOURS` = 0.10, `MAX_HOURS` = 4.0) |
| Max ask | YES entries skipped above 45¢ (`MAX_ASK`); the shared ladder filters on the same value |
| Min volume | Ladder rows below 50 contracts of volume are skipped |
| OTM gate | RANGE: ≤ $50 OTM (normal vol), ≤ $150 OTM (vol compressed). ABOVE/BELOW: ≤ $100 OTM (`MAX_OTM_T`). All tighten dynamically as expiry approaches (≤ $60 OTM inside 30 min; ≤ $30 OTM inside 20 min) |
| RANGE boundary buffer | Skip RANGE entries within $40 of *either* boundary (`MIN_RANGE_BOUNDARY_BUFFER`), all regimes, unless vol-compressed (structural mispricing exception) |
| Spread filter | Skip if bid/ask spread > 5¢ or > 25% of YES ask, re-validated against a fresh single-ticker quote at order time (retried 3× — a single dropped request used to discard a valid signal silently). |
| Min edge | `raw_edge = true_prob − kalshi_ask ≥ 1.5%` (drops to **1.0%** during vol compression) |
| Strike clustering | Skip if the strike is within $150 of an existing open position's strike in the same expiry window |
| Time-exit collision | Skip if the entry would immediately land inside the `TIME_EXIT_MINS` OTM force-close window |

**Snipe entry filters** (separate ROI scan):

| Filter | Value |
|--------|-------|
| Ask band | 10¢ ≤ ask ≤ 25¢ (`SNIPE_MIN_ENTRY_PRICE` / `SNIPE_MAX_ENTRY_PRICE`) |
| Min ROI | `true_prob / ask − 1 ≥ 30%` (`SNIPE_MIN_EDGE_RATIO`) |
| Trade size | 1% of account (`SNIPE_TRADE_PCT`) — sized down vs. `MAX_TRADE_PCT` since tail-probability estimates are noisier |

Edge calculation uses a lognormal GBM pricer with regime-conditional drift. Vol regime (HIGH/NORMAL/LOW) scales the vol input. During vol compression, the effective edge bar drops to 1.0%, the OTM allowance widens to $150, and near-money RANGE contracts get a +1.5¢ structural-underpricing bonus in the ranking.

### Market-Conditioned Probability

`DistModel.true_prob()` is the raw GBM prior. Live and paper entry scans wrap it
with `posterior_prob()`, a conservative logit-space blend with the current
Kalshi bid/ask midpoint. This fixes the defect where a contract could reprice
from 21¢ to 9¢ and the model would stay essentially unmoved. Market price is now
evidence, not only something to trade against.

The posterior is intentionally disabled in the synthetic backtest because those
quotes are generated by `build_ladder()` itself. Real quote replay should use
posterior; synthetic research should not.

**It has not been shown to add value, and the naming oversells it.** Audited
2026-08-18:

- **It is not a Bayesian posterior.** It is a logarithmic opinion pool —
  `logit(post) = (1−w)·logit(prior) + w·logit(market)` — with `w` set by hand
  (`0.15 + 0.10·time + 0.10·spread`, capped at 0.35). That functional form *is*
  the posterior mean under a Gaussian log-odds prior and likelihood, but only if
  `w = τ_market/(τ_prior+τ_market)`. Here it is judgmental and uncalibrated.
- **The headline real-quote result is a threshold artifact.** Because the mid is
  always below the ask, `posterior_edge < prior_edge` identically, so clearing a
  fixed `MIN_EDGE` just demands a larger prior edge — it selects *fewer*
  contracts, not *different* ones. Matched on sample size the advantage
  disappears:

  | Selection | n | EV/$1 |
  |---|---:|---:|
  | posterior ≥ `MIN_EDGE` | 38 | +34.1% |
  | prior ≥ `MIN_EDGE` | 58 | +5.0% |
  | **prior top-38 (matched n)** | **38** | **+31.6%** |

  The two 38-contract sets share **36 of 38** contracts. Raising `MIN_EDGE` from
  0.015 to 0.028 on the raw prior reproduces the result with no posterior at all.
- **It is not statistically significant either way.** Clustering by expiry
  (mutually exclusive RANGE bands resolved by one BTC path): 29 clusters,
  95% CI **[−36.3%, +100.4%]**, P(EV ≤ 0) = 18.2%.
- **`BAYES_MAX_MOVE` binds backwards.** The ±0.10 cap engages only when prior and
  market disagree most — exactly when market evidence is most informative.

The posterior stays enabled on the live entry path because it is *conservative*
(it can only shrink apparent edge toward the market), not because it is proven.
Do not cite the +34.1% figure as evidence for it.

---

## Exit Ladder (checked every position-check interval; exits are never blocked by other gates)

| Tier | Trigger | Reason |
|------|---------|--------|
| 0.5 | Up ≥15% + true\_prob fading 2 consecutive ticks + high dollar-gamma (≥40,000) + bid ≥ 35¢ | Gamma-aware convexity lock |
| 0.75 | Peak unrealized gain ≥25% and current gain has faded to ≤75% of that peak + bid ≥ `min($0.20, 1.30 × entry)` | Peak giveback — the floor is relative because a flat 20¢ gate could sit *above* the tier's own trigger price on cheap entries, making it mathematically unable to fire (19% of positions in a 40-day window) |
| 1 | Up 40% + < 15 min left + bid ≥ 30¢ | Scalp lock |
| 2 | Up 100% + < 9 min left | Momentum lock |
| 3 | Up 150% + < 15 min left | Strong profit |
| 3.75 | Snipe-only: up ≥150% + true\_prob fading 2 ticks + bid ≥ 12¢ | Snipe reversal lock |
| **3.5** | **Bid ≥ 75¢** | **Near settlement** — captures vol-compression plays entered at 2–4¢ without exiting early at Tier 4 (applies to snipes too) |
| 4 | Up 300% (non-snipe only) | Mega profit |
| 5 | < 3 min left + OTM + still > 15 points from the strike boundary | Time exit (near-boundary positions ride to settlement instead) |
| 5.25 | ITM but marginal (within 15 points of boundary), down ≥10%, < 10 min left, and true\_prob still fading (or down ≥65% unconditional hard stop) | Boundary risk |
| 6 | Down 35%/time\_urgency + > 18 min left (gated off in the final `TIME_EXIT_MINS` if already ITM) | Stop loss |
| — | Mid price ≤ 0.5¢ | Safety near-zero exit |

### NO exit ladder (the one that actually runs)

BOUNDARY_NO / MISPRICE_NO positions use a separate, much shorter ladder in
`positions.py`. `no_pnl_pct` is measured against the NO bid (`1 - yes_ask`).

| Trigger | Reason | Notes |
|---|---|---|
| `no_pnl_pct ≥ NO_PROFIT_CAPTURE` (80%) | `misprice_captured` | never reached in 41 recorded episodes — max MFE was +39.1% |
| `no_pnl_pct ≥ NO_TIME_PROFIT` (40%) + < 5 min left | `misprice_time` | also never reached |
| `overprice_r < NO_EDGE_GONE_RATIO` (1.05) **and up** | `edge_gone` | the only thesis-based exit |
| `no_pnl_pct ≤ -NO_STOP` (40%) | `misprice_failed` | **a pure percentage stop** — nothing checks whether the mispricing thesis actually failed |
| `no_pnl_pct ≤ -MIN_HOLD_CATASTROPHE` (65%) | `no_catastrophe` | bypasses the minimum hold |
| < 2 min left | `time_forced_no` | bypasses the minimum hold |

Two things worth knowing about this ladder:

**`misprice_failed` is a misnomer.** It is a price stop, not a thesis check.
The only thesis-aware exit (`edge_gone`) is gated on being *up*, so a position
whose thesis is intact but losing has no thesis-aware exit — it rides to the
40% stop. Empirically that is still the right call: of 7 episodes that reached
-40%, only 1 settled our way, and stopping averaged -40% against -82.8% for
holding. At -25% the thesis survives far more often (5/12), but holding is
still worse (-43.5% vs -25.0%), because entries around 80¢ cap the recovery at
+20% while each failure loses the whole premium.

### Entry timing — the measured defect

**256 of 257 signalled contracts (100%) drew down after entry.** Median MAE
-13.6% against a median MFE of +4.6%: the typical adverse move is three times
the typical favourable one. That follows from the gate itself — it fires on
`yes_bid / true_prob` being high, which means NO is *cheap*, and it keeps
getting cheaper. The bot catches the start of a repricing, not the end.

The **exits are not the problem**. Across 92 resolvable live round trips the
actual exits beat hold-to-settlement by **+$50.84** (`edge_gone` +$16.65, the
40% stop +$34.19) — and the book was still negative. `edge_gone` banking 1-6c
per contract looks like leakage and is not: settlement win rate on those
positions is 72%, so roughly one in four would have gone on to lose the whole
premium.

| fix tried | result |
|---|---|
| `BOUNDARY_NO_HOURS_MAX` 0.50 → **0.25** | **adopted** — ROC -4.5% → -1.5%. Buys less exposure, not a better price: cost moves $0.820 → $0.803, win rate flat |
| wait for the repricing to stop (no new low for N ticks) | **rejected** — filters 2 of 256 signals; the drawdown oscillates rather than sliding |
| require N consecutive qualifying ticks | **rejected** — removes 11%, moves ROC 0.1pp; the transient behind a -$4.07 loss lasted ~20 ticks |
| `LAG_FILTER_MAX_ADVERSE = 25` (reject stale quotes) | **adopted, paper-gated** — attacks the cause rather than the symptom: the drawdown *is* Kalshi not having repriced yet. n=27 |
| `WATCHLIST_ENTRY_DIP = 0.05` (arm strict, fill on value) | **adopted, paper-gated** — cost $0.803 → $0.689 for 1pp of win rate. n=14. *Inert in live until 2026-08-24 — see the warning below* |

### Watchlist entry (`WATCHLIST_ENTRY_DIP`)

The reachable discount is capped by *when you are allowed to look*. Insisting
every entry gate re-passes at the dipped price caps it at **2.2%** (median),
because the gates going stale **is** the discount: as spot drifts toward the
band `true_prob` rises, the overpricing ratio collapses, and the signal stops
firing precisely while the contract gets cheap.

So a contract is armed on the full gate set, then filled off the raw ladder
when (a) price has dipped `WATCHLIST_ENTRY_DIP` below the arming cost and
(b) the model still values NO above that price by `WATCHLIST_ENTRY_NET_EDGE`.
Stale gates are the discount; the model's valuation is the filter.

| policy (15-min window) | n | WR | cost | ROC | VALID | P(>0) |
|---|---:|---:|---:|---:|---:|---:|
| buy at arming | 110 | 80% | $0.803 | -1.7% | -0.2% | — |
| dip 5%, net_edge ≥ 0.05 | 14 | 79% | $0.689 | **+12.0%** | +26.2% | 79% |

Win rate falls 1pp while cost falls 11pp. **n=14 across 6 days, P(>0)=79% —
a live measurement, not an edge.** An earlier run measured +4.9% at n=39 but
armed on the raw prior, which live does not do; re-armed correctly the sample
collapsed to 14. The fill pricer barely matters (+12.9% prior vs +12.0%
posterior); the *arming* pricer changes everything.

### A closed market is not a settled market

Kalshi's `closed` status means trading has stopped; the outcome is determined
later. Treating it as settled made the bot book an outcome from
`is_in_money(spot)` at the arbitrary moment it noticed the close, using *our*
spot rather than Kalshi's settlement value.

Observed 2026-08-23 on `KXBTC-26AUG2323-B77250`: close_time 23:00, still
quoting 0.17/0.20 at 22:50:14, booked as settled six seconds later and credited
the full $1.00. Spot was $77,359 against a band of [77,200, 77,300) — **$59
outside, with ten minutes left.** A coin flip recorded as a certainty. (It
happened to be right; spot passed back through the band before the real close.)
`expired_settled` is 29 exits and -$1,187.51 across the paper history, so how
that branch decides is not a detail.

`_SETTLED` is now `{"finalized", "settled", "determined"}` — `closed` removed —
with a fallback for a position whose status never updates: `close_time` already
passed by 0.05h **and** both sides stopped quoting. Pinned by
`test_settlement_detection.py`.

### Execution costs

`CHARGE_FEES = True`. Kalshi's taker fee is
`ceil(0.07 × count × price × (1 - price))` cents, settlement is free. Charged
identically in backtest, paper, and live — pinned by `test_fee_parity.py`,
because backtest/live divergence is the recurring bug class in this repo. The
implementation rounds to 9 decimal places before `ceil`: `0.70 × (1 - 0.70)`
evaluates to 147.00000000000003 cents against `0.30`'s exact 147.0, and without
the round that float asymmetry charges a whole extra cent on one side.

Note the percentage asymmetry this creates: a 2¢ spread on a 20¢ contract is
10% of the position; on an 80¢ NO it is 2.5%. The NO lane is the cheap one to
trade, which is part of why it is the lane still running.

**Two guards sit in front of the profit exits** (`config.py` for the evidence):

- `MIN_HOLD_SECS = 60` — before ~60s an adverse move does not predict the
  settlement (at 10s a dip mildly predicts *winning*, 76% vs 59%), so an exit
  inside that window is reacting to quote noise. Sub-10s round trips are
  0-for-7 in the live book. Stops are deferred, not cancelled; expiry and the
  -65% catastrophe floor are exempt.
- `CONFIRM_EXIT_DEPTH = True` — profit exits are re-priced against the depth
  the whole position needs, because the decision used a size-less top-of-book
  quote while the fill walks the ladder. That gap turned one `edge_gone`
  take-profit into -$1.82. Stops deliberately skip the check: a worse
  executable price makes a stop *more* valid, not less.

Snipe positions (deep-OTM lottery entries, ask 10¢–25¢) skip tiers 0.5–4 and 6 by design — see `config.py` `SNIPE_PROFIT_LOCK_PCT` for the rationale — and only exit via 3.5 (near-settlement), 3.75 (snipe reversal lock), or 5 (OTM time exit). Entry price floor added 2026-07-07 after trade-log review showed sub-10¢ snipes were a coin flip that never reached the 75¢ near-settlement tier — the floor screens out tickets priced cheap because Kalshi's own model already sees them as near-zero, not because of vol lag.

---

## Vol Surface Module

`kalshi_btc_bot/vol_surface.py` fits Kalshi's **implied vol term structure** across expiry windows using Brent's method on the binary option pricing equation:

```python
# For each expiry, solve: DistModel(σ) = kalshi_ask
iv = implied_vol_range(ask=0.40, spot=100_000, lo=99_900, hi=100_100, hours=0.5)
# → 0.00270 hourly vol

vg = binary_range_vega(iv, spot=100_000, lo=99_900, hi=100_100, hours=0.5)
# → -135.25  (negative: higher vol → lower RANGE prob)
```

The fitted term structure reveals that Kalshi's 24h lag hits **short-dated contracts hardest** — 5-min contracts show the largest positive vol edge during compression, 3h contracts are fairly priced. The signal engine uses this to prefer the best-lag expiry window when entering.

```
expiry    Kalshi IV   Our EWMA    Edge (IV−EWMA)
0.083h    0.00512     0.00395     +0.00117  ← most lag
0.250h    0.00503     0.00395     +0.00108
1.000h    0.00428     0.00395     +0.00032
3.000h    0.00374     0.00395     −0.00021  ← no edge
```

---

## Risk Controls

| Control | Value |
|---------|-------|
| Max portfolio exposure | 18% of account |
| Max position size | 2.5% of account (quarter-Kelly sized, capped at 2.5%) |
| Max concurrent positions | 4 |
| Strike clustering | New entries blocked within $150 of an existing open position's strike in the same expiry window — caps directional correlation across positions, not just capital |
| Cash reserve | 5% minimum |
| Session stop | New entries halt if account is down 3% from its running peak (high-water mark, not just the session's starting balance). Resets on bot restart |
| Post-stop cooldown | 5-minute re-entry lockout on any ticker that just stopped out (`STOP_COOLDOWN_SECS`) |
| Untracked-exposure guard | Blocks new entries if live Kalshi-reported exposure diverges from the bot's tracked exposure by > 25% (`UNTRACKED_EXPOSURE_LIMIT`) — catches orphaned positions from prior crashed sessions before they compound |
| Stop loss | 35% per position, scaled tighter as expiry nears (gated: won't fire once inside the final OTM time-exit window if already ITM, and only fires with > 18 min left so short-duration binaries resolve via `TIME_EXIT_MINS` / `expiry_settle` instead) |
| Force-exit slippage | On urgent exits the limit crosses the stale bid by 2¢ (`FORCE_EXIT_SLIPPAGE_CENTS`) to guarantee the fill |
| Entry type | Immediate-or-cancel only (no resting orders); every entry re-fetches the live best bid/ask right before order placement and fills at that fresh ask (YES) / NO-implied price — never a cached ladder quote |
| Entry spread filter | Skipped if bid/ask spread > 5¢ or > 25% of ask, re-validated against the fresh quote at order time |
| Paper-mode fills | Depth-capped against the live Kalshi order book (`/markets/{ticker}/orderbook`), not a flat quoted price — a paper order walks resting levels up to its own IOC limit price, partial-filling or rejecting if size exceeds actual resting depth at that price. Live mode was never affected (real Kalshi IOC orders already return actual `fill_count`/`average_fill_price`) |

Position sizing uses **quarter-Kelly** with a 2.5% cap:
```
f* = edge / (1 − ask)
size = min(f* × 0.25, 0.025) × account_value
```

---

## Quickstart

### Backtest (no API keys needed)

```bash
pip install -r requirements.txt
python3 kalshi_btc_backtest.py --days 60 --capital 100                 # small scale (see capacity curve above)
python3 kalshi_btc_backtest.py --days 60 --capital 10000               # the scale where the capacity constraint bites
python3 kalshi_btc_backtest.py --days 60 --capital 100 --vol-surface   # with implied vol term structure
python3 kalshi_btc_backtest.py --days 60 --capital 100 --no-stop       # compare without stop loss
python3 kalshi_btc_backtest.py --days 60 --capital 100 --verbose       # print every trade entry
python3 montecarlo.py --n 10000 --capital 100                          # bootstrap equity fan + drawdown distribution
```

Remember what the backtest can and cannot answer: it produces **zero**
BOUNDARY_NO trades, so it never exercises the live strategy. For that, replay
recorded books against settlement:

```bash
python3 boundary_no_quote_replay.py --bootstrap 10000
```

### Tests

```bash
for f in test_*.py; do python3 "$f" >/dev/null || echo "FAIL $f"; done
```

122 tests across 14 files, all passing. Several are deliberately *policy*
tests rather than behaviour tests: `test_lag_filter.py` and
`test_watchlist_entry.py` each assert that if their feature is switched on then
`PAPER_TRADING` must be `True`, so an unvalidated n=11–27 feature cannot reach
the real account by accident. `test_fee_parity.py` pins backtest and live fee
accounting to the same function.

### Feed comparison

```bash
python3 feed_compare.py                 # Coinbase + Kalshi implied, rolling lag corr
python3 feed_compare.py --csv out.csv   # log every tick
```

### Live / Paper trading

```bash
cp .env.example .env
# fill in KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH

# Paper mode (no real orders, simulated $500 capital — PAPER_CAPITAL): PAPER_TRADING = True
# in kalshi_btc_bot/config.py (this is the default)
python3 -m kalshi_btc_bot

# Live mode: set PAPER_TRADING = False
caffeinate -dimsu python3 -m kalshi_btc_bot   # caffeinate keeps Mac awake

# Live dashboard + recording — THIS IS THE NORMAL WAY TO RUN IT
KALSHI_RECORD=1 KALSHI_LIVE_VIEW=1 caffeinate -dimsu python3 -m kalshi_btc_bot 2>&1 | tee -a bot_session.log
```

Two independent env vars, both gated on the string being **exactly** `1`
(`os.getenv(...) == "1"`), so `KALSHI_RECORD=true` silently does nothing:

| var | effect if unset |
|---|---|
| `KALSHI_RECORD=1` | **no market data is captured at all** — `recordings/` gets no file for the session |
| `KALSHI_LIVE_VIEW=1` | falls back to the scrolling log instead of the `live_view.py` dashboard |

`KALSHI_RECORD` is the one that matters and the easy one to forget, because the
bot trades perfectly normally without it and says nothing. A session run without
it on 2026-08-15 traded for 1h35m and left zero recordings, which silently costs
every downstream tool (`missed_trades.py`, `exit_timing_study.py`,
`real_price_edge_test.py`) its only input. Check after starting:

```bash
ls -lt recordings/ | head -3    # newest files should carry today's UTC date
```

**Keep the Mac on AC power, or run this first:**

```bash
sudo pmset -b sleep 0 disablesleep 1
```

`caffeinate -s` only prevents system sleep **on AC power**. On battery the
machine sleeps anyway, the process stays alive, and it receives nothing — this
cost 21% of all recorded data (68 gaps totalling 43.8h, almost all exactly 2.0h
and clustered 01:00–09:00 UTC). The gaps are non-random: they concentrate
overnight, which biases every recording-based study toward US-session
conditions.

### API key setup

1. Create a Kalshi account at [kalshi.com](https://kalshi.com)
2. Go to **Account Settings → API Keys → Create New Key**
3. Save the `.pem` private key file (shown once only)
4. Set environment variables:

```bash
export KALSHI_API_KEY_ID="your-key-id"
export KALSHI_PRIVATE_KEY_PATH="$HOME/.kalshi-key.pem"
```

Demo environment (paper only):
```bash
export KALSHI_BASE_URL="https://demo-api.kalshi.co/trade-api/v2"
```

---

## Data Recording & Storage

`kalshi_btc_bot/recorder.py`. Everything the bot sees goes to
**gzipped JSON Lines**, one file per stream per UTC day, in `recordings/`:

```
recordings/{stream}_{YYYY-MM-DD}.jsonl.gz     87 files, 67 MB
```

No database. One JSON object per line, short keys to keep the files small, and
a day-rolled handle per stream so a long session never holds one enormous file
open. Every record carries `t`, an ISO-8601 UTC timestamp to milliseconds —
that field is also what selects the file, so records are filed by *event* day,
not by when the process happened to start.

| stream | one record per | key fields | ~size/day |
|---|---|---|---:|
| `universe` | full ladder poll, **pre-filter** | `spot`, `win`, `m[]` of `{tk, a, b, v, lo, hi, ct}` | 3.5 MB |
| `books` | order-book snapshot | `tk`, `b`, `a`, `h`, `spot`, `held`, `yes[]`, `no[]` depth ladders | 1.1 MB |
| `quotes` | scan tick | `spot`, `rg` (regime: `r`, `d`, `v`, `vh`, `vr`, `vc`, `z`, `m`), `l[]` | 1.1 MB |
| `marks` | held-position mark | `tk`, `b`, `a`, `entry`, `cnt`, `peak`, `tp`, `prior`, `mkt`, `post`, `mw` | 7 KB |
| `orders` | order attempt | `ev`, `side`, `lim`, `want`, `fill`, `px`, `why`, plus the full `book` at decision time | small |
| `walls` | Deribit OI snapshot (~5 min) | wall levels | small |

**`universe` is the important one.** It is recorded *before* the ladder
filters, so it is the only uncensored record of what was actually available —
which is what makes counterfactual studies possible. Every "measured on
recorded data" figure in this README comes from replaying it and resolving each
contract by where spot settled against its `[lo, hi)` band. `quotes` and
`books` are already filtered and cannot answer "what would this other gate have
done?"

### Design details that are load-bearing

**Recording never blocks trading.** `_emit()` puts onto a bounded
`queue.Queue(maxsize=10000)` with `put_nowait` and a daemon writer thread
drains it. A full queue increments `dropped` and returns; a write exception is
swallowed. The bot degrades to losing data rather than stalling on disk I/O in
a trading loop.

**Flush cadence is per-stream, and a single shared counter was wrong.**

```python
_FLUSH_EVERY = {"orders": 1, "walls": 1, "marks": 5,
                "books": 50, "quotes": 200, "universe": 20}
```

With ~25,000 `quotes` writes against ~6 `orders` writes in a session, a shared
global threshold means the odds an order write ever lands on it are near zero —
so the `orders` stream sat unflushed in memory all day, invisible to anything
reading the file and one `kill -9` from being lost. `orders` and `marks` are
the two streams the exit-pricing work depends on, so they flush immediately or
near it. `walls` too: those snapshots arrive every ~5 min and can never be
re-fetched, since Deribit publishes no historical open interest.

`close()` is wired to both `KeyboardInterrupt` and `atexit`, so a graceful
shutdown flushes everything regardless of cadence.

**It is off unless you ask for it.** `ENABLED = os.getenv("KALSHI_RECORD") == "1"`
— exactly the string `1`. The bot trades normally and silently without it. See
the Quickstart warning; a session on 2026-08-15 traded 1h35m and left zero
recordings.

### Derived outputs

`results/` (311 files, 88 MB) holds analysis JSON, not market data — 269
`backtest_*.json` runs plus lead-lag and unified-analysis reports. Backtest
runs are named `backtest_{YYYYMMDD}_{HHMM}.json`; the current-config run quoted
above is `backtest_20260824_1621.json`. `montecarlo.py` and the replay tools
read these rather than re-running the simulation.

Nothing prunes either directory automatically.

---

## Tech Stack

- **Python 3.11+** — async-ready, type-annotated
- **scipy.stats.norm** — CDF-based binary option pricing (replaces hand-rolled erf)
- **scipy.optimize.brentq** — implied vol extraction from binary option prices
- **numpy / pandas** — vol computation, OHLCV processing
- **yfinance** — BTC-USD 5-min OHLCV for backtesting
- **Coinbase Exchange ticker** — live spot (`api.exchange.coinbase.com/products/BTC-USD/ticker`), with the retail `v2/prices` endpoint as fallback only; the retail feed lags the exchange by ~10s
- **Kalshi REST API** — RSA-PSS signed requests, IOC order entry. Note `no_cost = 1 - yes_bid`, and the V2 event-order endpoint is a YES-leg book
- **Alpaca / alpaca-py** — retained for the separate Kalshi-to-SPY/SPX options lead-signal research lane (`options_signals.py`, `strategy_engine.py`, `unified_analysis.py`, `arb_scanner.py`). The current BTC bot does not execute Alpaca orders.

---

## Disclaimer

This is experimental research software. Binary event markets are high-risk instruments. Past backtest performance does not guarantee future results. Run in paper mode before deploying real capital.
