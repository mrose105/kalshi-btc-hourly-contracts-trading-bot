# Kalshi BTC Hourly Contracts Trading Bot

A live quantitative trading bot for Kalshi's KXBTC binary event markets, built around a **volatility mispricing signal**: Kalshi prices RANGE contracts using a 24-hour lagged SMA vol estimate, while this bot uses a fast EWMA. When current vol compresses below Kalshi's lagged estimate, RANGE contracts are structurally underpriced — the bot detects these windows and buys YES at 2–40¢, targeting 75¢–$1.00 settlement.

---

## Backtest Results (60-day walk-forward, May 24 – Jul 22 2026)

| Metric | Value |
|--------|-------|
| Starting capital | $5,000 |
| Final capital | $190,258 |
| Return | **+3,705%** |
| Sharpe ratio | **4.31** |
| Profit factor | 2.41 |
| Max drawdown | -12.9% |
| Trades | 1,931 |
| Win rate | 63.0% |
| Avg hold | 11 min |
| Vol compression WR | **58.2%** vs 65.5% normal (53% of P&L from compression trades) |

Backtest uses real BTC-USD 5-minute OHLCV from yfinance. Fills modeled at Kalshi's spread (widened dynamically near settlement to reflect thin end-of-hour liquidity). Intrabar stop simulation uses bar High/Low to replicate live polling. `SESSION_STOP_PCT` peak-drawdown breaker resets each day to model the live workflow (bot restarted per session). See [`docs/STRATEGY.md`](docs/STRATEGY.md) for the full math and audit.

Dominant exit by P&L: `momentum_locked` (475 trades, 100% WR, +$173,495). Biggest drag: `no_stop` (95 trades, -$48,246).

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

4 independent daemon threads: price · sync · position · signal-scan
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
- **`find_boundary_no`** — premium-collection scan that sells OTM NO contracts when BTC is extended far from the center of the RANGING regime (|z-score| ≥ 2.5, RANGING or REVERTING regime). Targets overpriced YES contracts where the market is pricing in too much probability of BTC staying near the extreme — the bot fades that by buying the NO side (i.e., selling the overpriced YES-equivalent). Position exits via stop-loss at 40% loss or expiry settlement.

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
python3 kalshi_btc_backtest.py --days 60 --capital 5000               # matches the results table above
python3 kalshi_btc_backtest.py --days 7  --capital 5000 --vol-surface # with implied vol term structure
python3 kalshi_btc_backtest.py --days 7  --capital 5000 --no-stop     # compare without stop loss
python3 kalshi_btc_backtest.py --days 7  --capital 5000 --verbose     # print every trade entry
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
