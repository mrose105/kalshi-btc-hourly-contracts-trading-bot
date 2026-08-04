# Kalshi BTC Hourly Contracts Trading Bot

A live quantitative trading bot for Kalshi's KXBTC binary event markets, built around a **volatility mispricing signal**: Kalshi prices RANGE contracts using a 24-hour lagged SMA vol estimate, while this bot uses a fast EWMA. When current vol compresses below Kalshi's lagged estimate, RANGE contracts are structurally underpriced — the bot detects these windows and buys YES at 2–40¢, targeting 75¢–$1.00 settlement.

---

## Backtest Results (60-day walk-forward, run 2026-08-04, post depth-realism fixes)

There is no single headline return — the strategy has a real, measured **capacity limit**, and which side of it you're on determines the sign of the result:

| Starting capital | Return | Sharpe | Profit factor | Trades |
|---|---|---|---|---|
| $100 | **+412%** | 7.16 | 1.37 | 1,321 |
| $200 | +442% | 7.29 | 1.38 | 1,319 |
| $500 | +307% | 6.52 | 1.30 | 1,287 |
| $1,000 | +129% | 4.70 | 1.19 | 1,257 |
| $2,000 | +29% | 2.10 | 1.07 | 1,164 |
| $5,000 | -39% | -3.28 | 0.83 | 986 |
| $10,000 | -60% | -7.43 | 0.66 | 858 |

At small size the edge is real and strong. Past roughly $2,000, Kelly sizing wants positions larger than Kalshi's real KXBTC order-book depth can absorb without severe exit slippage — the strategy doesn't scale, and profitability inverts smoothly and monotonically, not because of a bug but because the backtest now models that constraint instead of assuming infinite liquidity (`_size_impact_penalty()` / `_MAX_ENTRY_SIZE` in `kalshi_btc_backtest.py`, anchored to real recorded Kalshi book depth in `recordings/*.jsonl.gz`).

These figures still move run to run — `--days 60` is a rolling window anchored to *now*, not fixed dates. Full methodology, known limitations, and the capacity-constraint finding in detail are in [`docs/BACKTEST_INTEGRITY.md`](docs/BACKTEST_INTEGRITY.md).

Backtest uses real BTC-USD 5-minute OHLCV from yfinance and applies five bias-elimination fixes to prevent inflated returns:

1. **Fills execute at NEXT bar's open, not current bar's close** — removes the lookahead where a signal generated at bar close was also filled at that same close.
2. **Expiry settlement uses bar OPEN (not close)** — expiry happens during the bar, so end-of-bar spot is a lookahead. Bar open is a defensible proxy for spot at the actual expiry moment.
3. **Adverse-selection haircut on all model-derived exit bids** — near expiry, when `DistModel.true_prob` mechanically collapses toward 0/1, the exit bid is discounted (up to ~15%) to reflect the reality that Kalshi market-makers won't quote at fair value on "certain" contracts that could still whipsaw. Without this the backtest inflated ~4×.
4. **Intrabar stop slippage** — stops fill 2¢ worse than the theoretical threshold (matches live `FORCE_EXIT_SLIPPAGE_CENTS`), not at exactly the stop price.
5. **NO position exit markup** — mirror haircut on the counterparty side so NO exits don't fabricate settlement-certainty gains either.

Intrabar stop simulation uses bar High/Low to replicate live polling. `SESSION_STOP_PCT` peak-drawdown breaker resets each day to model the live workflow (bot restarted per session). As of 2026-08-04, both entries and exits are also **depth-aware**: exits carry a size-based impact penalty and entries are capped to what a real book could plausibly fill, calibrated conservatively against real recorded Kalshi order-book depth (`recordings/*.jsonl.gz`) rather than assuming any position size fills at the model price — this is what produces the capacity curve above.

**Known limitation:** exit prices still come from the bot's own probability model plus a hand-tuned discount (now also size-adjusted), not a recorded Kalshi order book directly — see [`docs/BACKTEST_INTEGRITY.md`](docs/BACKTEST_INTEGRITY.md) §3 for the full breakdown and current status. The only market-verified (non-simulated) result to date is the 2026-07-01–03 live run: 63 trades, profit factor 0.78. Paper trade before deploying real capital, and size to the capacity the table above shows, not the account balance you wish you had.

---

## Risk Profile — 10,000-path Monte Carlo (at $100)

Return alone says nothing about what running this actually feels like. Bootstrapping the 1,321 trades from the **$100-capital** backtest run into 10,000 resampled equity paths (`python3 montecarlo.py results/backtest_20260804_0802.json --n 10000 --capital 100`):

| Percentile | Final equity | Return |
|------------|--------------|--------|
| worst path | $45 | -54.8% |
| p5 | $329 | +228.8% |
| p25 | $436 | +336.4% |
| **p50 (median)** | **$511** | **+411.3%** |
| p75 | $587 | +486.9% |
| p95 | $699 | +599.3% |
| best path | $954 | +853.6% |

The actual backtest ($512) lands almost exactly on the median, so the historical trade *ordering* wasn't unusually lucky. The tail is meaningfully worse here than at smaller scale: doubling capital from $44 to $100 roughly doubles typical position size too, pushing more trades past the depth threshold where `_size_impact_penalty()` bites — the worst-path Monte Carlo result at $44 was -4.5%; at $100 it's -54.8%.

### Drawdown is the thing to watch

```
P(equity ever dips below start)    88.0%
P(max DD > 20%)                    61.2%
P(max DD > 30%)                    25.6%

Max DD:  median -23%   p95 -48%   worst -131%
```

The backtest's own -20.4% max drawdown sits close to the median path (-23%). Roughly 3 in 5 paths take a 20%+ drawdown and 1 in 4 take 30%+ — the edge being real at this scale doesn't mean the ride is smooth, and it's a noticeably rougher ride than at $44. A worst-case beyond -100% is not a typo: the bootstrap resamples fixed dollar P&Ls with no bankruptcy floor.

### The Monte Carlo still understates risk in three ways

1. **Fixed dollar P&L, no compounding.** The bootstrap resamples raw dollar amounts, but the live bot Kelly-sizes off *current* equity. Real paths compound, widening both tails.
2. **IID resampling destroys loss clustering.** Losses correlate in reality — same regime, adjacent strikes, one BTC move busting several positions at once. Shuffling breaks that, so real drawdowns can run worse than the median suggests.
3. **It inherits the model-pricing error.** Every path resamples trades whose exit price came from the bot's own model (now with a depth-aware size penalty, but still not a recorded book). The bootstrap treats those P&Ls as given.

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

Three parallel scans on each tick:

- **`find_best`** — probability-edge scan for the highest-edge contract. **RANGE-only in the RANGING regime**; TRENDING / REVERTING / BREAKOUT regimes also consider ABOVE / BELOW contracts, gated by the regime's direction (an ABOVE won't be bought during a confirmed downtrend, and vice-versa).
- **`find_snipe`** — separate ROI-ranked scan for cheap deep-OTM lottery tickets that `find_best` would never surface (small raw-edge points but 30%+ ROI on a 10–25¢ ask).
- **`find_boundary_no`** — mean-reversion premium-collection scan. When BTC is at a range extreme (|z-score| ≥ 2.5, RANGING or REVERTING regime), the market overprices the probability of continuation — OTM contracts in the breakout direction are too expensive relative to true probability. The bot buys NO on those contracts (betting BTC mean-reverts rather than breaks out), analogous to selling an OTM option at the extreme to collect overpriced premium. NO pays $1 if BTC fails to reach the OTM range by expiry. Exits via 40% stop-loss or expiry settlement.

Filters applied before every entry:

| Filter | Description |
|--------|-------------|
| Expiry gate | 6 min – 4 hours to expiry (`MIN_HOURS` = 0.10, `MAX_HOURS` = 4.0) |
| Max ask | Skip anything priced above 45¢ (`MAX_ASK`) — the strategy targets the cheap side of the ladder |
| Min volume | Ladder rows below 50 contracts of volume are skipped |
| OTM gate | RANGE: ≤ $50 OTM (normal vol), ≤ $150 OTM (vol compressed). ABOVE/BELOW: ≤ $100 OTM (`MAX_OTM_T`). All tighten dynamically as expiry approaches (≤ $60 OTM inside 30 min; ≤ $30 OTM inside 20 min) |
| RANGE boundary buffer | Skip RANGE entries within $40 of *either* boundary (`MIN_RANGE_BOUNDARY_BUFFER`), all regimes, unless vol-compressed (structural mispricing exception) |
| Spread filter | Skip if bid/ask spread > 5¢ or > 25% of ask, re-validated against a fresh single-ticker quote at order time |
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

---

## Exit Ladder (checked every position-check interval; exits are never blocked by other gates)

| Tier | Trigger | Reason |
|------|---------|--------|
| 0.5 | Up ≥15% + true\_prob fading 2 consecutive ticks + high dollar-gamma (≥40,000) + bid ≥ 35¢ | Gamma-aware convexity lock |
| 0.75 | Peak unrealized gain ≥25% and current gain has faded to ≤75% of that peak + bid ≥ 20¢ | Peak giveback |
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

# Paper mode (no real orders, simulated $10,000 capital): set PAPER_TRADING = True
# in kalshi_btc_bot/config.py (this is the default)
python3 -m kalshi_btc_bot

# Live mode: set PAPER_TRADING = False
caffeinate -dimsu python3 -m kalshi_btc_bot   # caffeinate keeps Mac awake
```

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

---

## Disclaimer

This is experimental research software. Binary event markets are high-risk instruments. Past backtest performance does not guarantee future results. Run in paper mode before deploying real capital.
