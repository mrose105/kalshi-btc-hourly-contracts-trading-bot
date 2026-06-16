# Kalshi BTC Binary Options Bot

A live quantitative trading bot for Kalshi's KXBTC binary event markets, built around a **volatility mispricing signal**: Kalshi prices RANGE contracts using a 24-hour lagged SMA vol estimate, while this bot uses a fast EWMA. When current vol compresses below Kalshi's lagged estimate, RANGE contracts are structurally underpriced — the bot detects these windows and buys YES at 2–40¢, targeting 75¢–$1.00 settlement.

---

## Backtest Results (7-day walk-forward, Jun 9–15 2026)

| Metric | Value |
|--------|-------|
| Starting capital | $50 |
| Final capital | $926 |
| Return | **+1,753%** |
| Sharpe ratio | **5.66** |
| Profit factor | 2.54 |
| Max drawdown | -6.1% |
| Trades | 601 |
| Win rate | 48.8% |
| Avg hold | 8 min |
| Vol compression WR | **52.8%** vs 46.8% normal |

Backtest uses real BTC-USD 5-minute OHLCV from yfinance. Fills modeled at Kalshi's spread. Intrabar stop simulation uses bar High/Low to replicate live 8-second polling.

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
BTCFeed          RegimeEngine       SignalEngine        PositionManager
─────────        ────────────       ────────────        ───────────────
EWMA vol    →   RANGING /     →   Scan Kalshi    →   6-tier exit ladder
SMA vol         TRENDING /        ladder for           every 8 seconds
vol_ratio       REVERTING /       mispriced            (exits never blocked)
momentum        BREAKOUT          RANGE contracts
zscore
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
└── app.py          — main loop
kalshi_btc_backtest.py  — walk-forward backtest with intrabar stop simulation
```

---

## Signal Engine — Entry Logic

Only enters on **RANGE contracts** (BTC stays between two strikes). Filters applied before every entry:

| Filter | Description |
|--------|-------------|
| Expiry gate | 5 min – 4 hours to expiry |
| OTM gate | ≤ $50 OTM (normal vol), ≤ $150 OTM (vol compressed) |
| Regime gate | OTM entries blocked in RANGING regime with conf < 60% |
| Spread filter | Skip if bid/ask spread > 5¢ or 25% of ask |
| Min edge | raw_edge = true\_prob − kalshi\_ask ≥ 1.5% |

Edge calculation uses a lognormal GBM pricer with regime-conditional drift. Vol regime (HIGH/NORMAL/LOW) scales the vol input. During vol compression, the effective edge bar drops to 1.0% and OTM allowance widens.

---

## Exit Ladder (6 tiers, checked every 8 seconds)

| Tier | Trigger | Reason |
|------|---------|--------|
| 1 | Up 60% + true prob fading 2 consecutive ticks | Scalp reversal |
| 2 | Up 100% + < 9 min left | Momentum lock |
| 3 | Up 150% + < 15 min left | Strong profit |
| **3.5** | **Bid ≥ 75¢** | **Near settlement** — captures vol-compression plays entered at 2–4¢ without exiting early at Tier 4 |
| 4 | Up 300% | Mega profit |
| 5 | < 10 min left + OTM | Time exit |
| 6 | Down 40% + > 12 min left | Stop loss (gated: doesn't fire in final 12 min where binary payoff is binary) |

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
| Max portfolio exposure | 40% of account |
| Max position size | 5% of account (Kelly-sized, capped at 2× initial) |
| Max concurrent positions | 4 |
| Cash reserve | 5% minimum |
| Session stop | New entries halt if account down 80% |
| Stop loss | 40% per position (gated: won't fire in final 12 min) |
| Entry type | Immediate-or-cancel only (no resting orders) |

Position sizing uses **quarter-Kelly** with an 8% cap:
```
f* = edge / (1 − ask)
size = min(f* × 0.25, 0.08) × account_value
```

---

## Quickstart

### Backtest (no API keys needed)

```bash
pip install -r requirements.txt
python3 kalshi_btc_backtest.py --days 7 --capital 50
python3 kalshi_btc_backtest.py --days 7 --capital 50 --vol-surface   # with implied vol term structure
python3 kalshi_btc_backtest.py --days 7 --capital 50 --no-stop       # compare without stop loss
python3 kalshi_btc_backtest.py --days 7 --capital 50 --verbose       # print every trade entry
```

### Live / Paper trading

```bash
cp .env.example .env
# fill in KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH

# Paper mode (no real orders): set PAPER_TRADING = True in kalshi_btc_bot/config.py
python3 -m kalshi_btc_bot.app

# Live mode: set PAPER_TRADING = False
caffeinate -dimsu python3 -m kalshi_btc_bot.app   # caffeinate keeps Mac awake
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
