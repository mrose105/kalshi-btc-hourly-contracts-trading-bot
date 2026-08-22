# Kalshi BTC Hourly Contracts Trading Bot

A live quantitative trading bot for Kalshi's KXBTC binary event markets, built around a **volatility mispricing signal**: Kalshi prices RANGE contracts using a 24-hour lagged SMA vol estimate, while this bot uses a fast EWMA. When current vol compresses below Kalshi's lagged estimate, RANGE contracts are structurally underpriced — the bot detects these windows and buys YES at 2–40¢, targeting 75¢–$1.00 settlement.

---

## Backtest Results (60-day walk-forward, run 2026-08-17)

Current $500 sanity run after the market-posterior/backtest-parity fix:

| Starting capital | Return | Sharpe | Profit factor | Win rate | Max DD | Trades |
|---|---:|---:|---:|---:|---:|---:|
| $500 | **+78.6%** | 6.28 | 1.67 | 46.7% | -7.8% | 225 |

This is a more conservative number than the pre-fix posterior run because the
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

> **Earlier figures in this README were void and have been replaced.** Runs before 2026-08-07 simulated a 250-wide RANGE band; the real KXBTC hourly band is **100 wide**, confirmed against ~20,000 `floor_strike`/`cap_strike` observations from the exchange. The wider band made every simulated contract 2.3–4.7× likelier to pay, inflating returns roughly 4×. Details in [`docs/QUANT_STANDARDS_AUDIT.md`](docs/QUANT_STANDARDS_AUDIT.md) §1d.

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

## Risk Profile — 10,000-path Monte Carlo (at $100)

Return alone says nothing about what running this feels like. Bootstrapping the 250 trades from the $100 backtest into 10,000 resampled equity paths (`python3 montecarlo.py results/backtest_20260811_1820.json --n 10000 --capital 100`):

| Metric | Value |
|---|---|
| Actual final equity | $213 (+113.0%) |
| **Median simulated** | **$213 (+112.8%)** |
| 5th percentile | $164 |
| 95th percentile | $265 |

The actual backtest lands on the median, so the historical trade *ordering* wasn't unusually lucky.

### Drawdown

```
Actual max DD:    -13.9%
Median sim DD:     -6.7%
P(DD > 20%):        0.3%
P(DD > 30%):        0.0%
```

The realised -13.9% sits well above the median path (-6.7%), so this particular ordering was on the rougher side. Tails are much tighter than earlier versions of this README reported, because the compression gate cut trade count roughly 5× — fewer, more selective trades.

### The Monte Carlo still understates risk in three ways

1. **Fixed dollar P&L, no compounding.** The bootstrap resamples raw dollar amounts, but the live bot Kelly-sizes off *current* equity. Real paths compound, widening both tails.
2. **IID resampling destroys loss clustering.** Losses correlate in reality — same regime, adjacent strikes, one BTC move busting several positions at once.
3. **It inherits the model-pricing error.** Every path resamples trades whose exit price came from the bot's own model, not a recorded book.

`SESSION_STOP_PCT` (3%) gates *new entries* only — it never closes open positions, so it does not floor these drawdowns.

---

## The Edge — Kalshi's Vol Lag

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
├── model.py        — lognormal binary option pricer (scipy.stats.norm CDF)
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

## Tech Stack

- **Python 3.11+** — async-ready, type-annotated
- **scipy.stats.norm** — CDF-based binary option pricing (replaces hand-rolled erf)
- **scipy.optimize.brentq** — implied vol extraction from binary option prices
- **numpy / pandas** — vol computation, OHLCV processing
- **yfinance** — BTC-USD 5-min OHLCV for backtesting
- **Kalshi REST API** — RSA-PSS signed requests, IOC order entry
- **Alpaca / alpaca-py** — retained for the separate Kalshi-to-SPY/SPX options lead-signal research lane (`options_signals.py`, `strategy_engine.py`, `unified_analysis.py`, `arb_scanner.py`). The current BTC bot does not execute Alpaca orders.

---

## Disclaimer

This is experimental research software. Binary event markets are high-risk instruments. Past backtest performance does not guarantee future results. Run in paper mode before deploying real capital.
