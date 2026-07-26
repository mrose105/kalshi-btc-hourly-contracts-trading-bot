# Strategy, Math & Audit — kalshi_btc_bot

This document explains the bot end-to-end: the edge it exploits, the math behind every calculation, and an audit of whether the implementation is mathematically sound. Every claim is cross-referenced to the code (`file:line`).

---

## 1. What the bot is doing

Kalshi lists **hourly binary contracts** on BTC/USD. Each contract is either:

- **RANGE** — pays $1 if BTC settles between two strikes at the end of the hour; else $0.
- **ABOVE** — pays $1 if BTC settles above a strike.
- **BELOW** — pays $1 if BTC settles below a strike.

Kalshi's market maker prices these using a lagged historical vol estimate. Our thesis: the estimate lags observed BTC realized vol by hours, creating structural mispricing during vol regime changes. When realized vol compresses fast (calm after a spike), Kalshi still prices as if vol were high → RANGE contracts have higher true probability of staying in range than the ask reflects → **buy YES**.

We run **two orthogonal strategies** on top of that thesis:

1. `find_best` (`signals.py:38`) — probability-edge scan. Ranks candidates by `true_prob − ask` and buys the largest positive edge that clears all filters.
2. `find_snipe` (`signals.py:138`) — ROI-ranked scan for cheap deep-OTM lottery tickets (10–25¢). Ranks by `true_prob / ask`, not by absolute prob-point edge, so tail plays with tiny prob-point edges but big ROI aren't crowded out.

---

## 2. The pricing model

### 2.1 Distribution assumption

Assume log-normal geometric Brownian motion for BTC:

```
log(S_T) ~ Normal(μ, σ²·T)
```

where:

- `S_0` = current spot
- `T` = hours to expiry
- `σ` = hourly vol (see §2.3)
- `μ = log(S_0) + drift` — drift is regime-conditional (see §2.2)

For any strike `K`, the probability of being above `K` at expiry is:

```
P(S_T > K) = Φ((μ − log K)/σ_t) = 1 − Φ((log K − μ)/σ_t)
```

where `σ_t = σ·√T` and Φ is the standard normal CDF.

**Implementation** (`model.py:34-84`) — with Itô convexity correction `−σ²·T/2`:

```python
mu = math.log(spot) + drift - 0.5 * vol_t * vol_t
```

| Contract | Code | Formula |
|---|---|---|
| RANGE  [lo, hi] | `norm.cdf(z_hi) − norm.cdf(z_lo)` | `Φ((log hi − μ)/σ_t) − Φ((log lo − μ)/σ_t)` |
| ABOVE  low | `norm.sf(z)` | `1 − Φ((log low − μ)/σ_t)` |
| BELOW  high | `norm.cdf(z)` | `Φ((log high − μ)/σ_t)` |

**Audit:** ✅ Standard log-normal boundary math with the correct real-measure GBM mean. All three formulas are unbiased estimators of the true probability under the assumed distribution. Impact of the Itô term at typical BTC vols (≈0.005 hourly = ~50% annualized) and T ≤ 4h is ~0.005% log-space shift — genuinely negligible in practice, but principled.

### 2.2 Regime-conditional drift

Under real-world (not risk-neutral) probability, we forecast the actual location of `S_T`. The model adds an expected drift based on the current regime:

```
drift = 0                              if RANGING
drift = 0.3 × momentum(60s)            if TRENDING
drift = −0.15 × zscore(300s) × σ_t     if REVERTING
drift = 0.5 × momentum(60s)            if BREAKOUT
```

(`model.py:54-62`)

**Audit:** These are heuristic weightings, not derived. They serve to:
- Shift `P(S_T > K)` in the direction of confirmed momentum.
- Shrink `RANGE` probability in TRENDING/BREAKOUT (drift pushes distribution away from center).

The heuristic multipliers (0.3, 0.15, 0.5) are the kind of thing you'd tune empirically — they're plausible, and the backtest validates that the regime read + drift adds value. Not a math bug.

### 2.3 Volatility inputs

The model takes a **per-tick EWMA vol** from the feed and annualizes:

```
σ_h = σ_bar × √BARS_PER_HOUR
σ_t = σ_h × √T
```

`BARS_PER_HOUR = 3600 // PRICE_FETCH`, so **1800** at the current 2 s poll. This was hardcoded to `900` (correct only for the retired 4 s poll) in both `model.py` and `regime.py`, silently understating hourly vol by √2 (~29%) and inflating every RANGE edge. Fixed Jul 23 by deriving it from `PRICE_FETCH`.

**Vol estimator (`feed.py:47-66`)** — RiskMetrics EWMA:

```
σ²_t = λ·σ²_{t-1} + (1−λ)·r²_t     with r_t = log(P_t / P_{t-1})
```

- `λ = 0.99` → half-life = `ln(0.5)/ln(0.99) ≈ 69 ticks`, which is **2.3 min at the 2 s poll** (not the 4.6 min quoted while `PRICE_FETCH` was 4 s).

The companion slow EWMA (`λ = 0.999`, ~46 min) no longer exists — §2.4's `vol_ratio` was rewritten Jul 16 to an SMA on 5-min bars, so this estimator now feeds only `regime["vol"]` and hence `DistModel.true_prob`.

**Audit:** ✅ Standard RiskMetrics form. Returns spanning a feed outage are now excluded (Jul 25): `fetch()` records nothing when the HTTP call fails, so the next return covered the whole gap while being weighted as one `PRICE_FETCH` interval — a 5-min hole inflated this vol 5.6×, and a 15-min hole 9.5×, repricing the entire ladder.

**Vol regime scaling** (`model.py:44-49`):

```
σ_h ×= 1.15  if HIGH
σ_h ×= 0.92  if LOW
```

**Audit:** These are defensive heuristics ("when realized vol is high, expect fatter tails than the EWMA captures"). They biased RANGE probability lower when vol is high, which is directionally sensible (widening the distribution reduces P(range)). Multipliers are ad-hoc; a stricter version would use a vol-of-vol estimate. Not incorrect, just heuristic.

**Vol floor/cap** (`model.py:11-19`):

```
σ_h ∈ [0.003, 0.030]     # ~30%–280% annualized
```

Prevents data-glitch spikes from corrupting `true_prob`. The upper cap `0.030` sits just above HIGH-regime-scaled worst-case vol (`0.015 × 1.15 = 0.0172`) so normal high-vol pricing is unaffected. **Audit:** ✅ Correctly bounded — was `0.080` before Jul 6 (would have never actually clamped), now tight enough to be a real safety net.

### 2.4 Vol compression signal — the core edge

The bot's central claim is that Kalshi's vol estimator lags realized vol. We proxy the size of the lag with:

```
vol_ratio = σ_fast / σ_slow
```

If `vol_ratio < 0.55`, we assume Kalshi's pricing still reflects the pre-compression regime → RANGE contracts are systematically cheap → structural edge.

**Implementation — live and backtest use the same formula** (`feed.py:vol_ratio`, `kalshi_btc_backtest.py:130-135`):

```
σ_fast = std of last 12 5-min bar log-returns    = 1h realized vol
σ_slow = std of last 288 5-min bar log-returns   = 24h SMA vol (Kalshi's window)
vol_ratio = σ_fast / σ_slow
```

At startup the live bot calls `feed.bootstrap_history(hours=24)` to pull 24 h of BTC 5-min bars from yfinance so the 24h SMA is meaningful from the first scan tick (before this fix, the live bot would have needed 24h of runtime to populate the window). Each tick then aggregates the incoming Coinbase price into the currently-forming 5-min bar and closes the bar at boundary crossings.

**Audit:** ✅ Live and backtest now compute the identical statistic. Rewritten 2026-07-16 — previously the live bot used a `EWMA(46min)/EWMA(4.6min)` ratio on 4-second ticks, which measured a very different (much shorter) lag than Kalshi's stated 24h window. That mismatch meant the live `vol_compression = True` signal fired much more often than the backtest suggested, degrading signal quality. The fix restores backtest parity in *formula*, but two feed defects broke it in *data* until Jul 25:

1. `bootstrap_history()` passed naive local datetimes to yfinance, which reads them as UTC — under EDT the 24 h window closed four hours early (measured 243 min of lag vs 2 min once timezone-aware).
2. That hole then fed `_maybe_close_5min_bar()`, which advanced only one bar per tick, replaying the gap as ~48 synthetic bars stamped 5 minutes apart but each holding ~2 seconds of price movement. Their near-zero returns collapsed the fast window to `vol_ratio ≈ 0.079` against a 0.55 threshold.

Net effect: **the compression regime fired continuously for roughly the first hour after every restart**, dropping `MIN_EDGE` to 0.010 and widening the OTM gate to 150 on fabricated data. Both are fixed; gaps now re-anchor to the live slot and `vol_ratio` returns a neutral 1.0 until the fast window holds enough contiguous bars. Paper results recorded before Jul 25 were collected under this defect.

### 2.5 Vol surface (implied vol term structure)

`vol_surface.py` extracts Kalshi's **implied** hourly vol per expiry window via Brent's method on the RANGE pricing function:

```
Solve for σ:  RangePrice(σ, S, lo, hi, T) − ask = 0
```

(`vol_surface.py:51-74`)

The pricing function is monotone decreasing in σ (higher vol → wider distribution → less range probability), so Brent's method is guaranteed to bracket the root as long as `ask ∈ [0.02, 0.92]` (`vol_surface.py:61`).

**Vega** — sensitivity of range price to vol — via central FD (`vol_surface.py:77-90`):

```
vega = [P(σ + dv) − P(σ − dv)] / (2·dv)
```

**Audit:** ✅ Standard implied-vol inversion. Both the root-finder bracketing and the vega FD are numerically sound. `xtol=1e-7` and `maxiter=50` are reasonable convergence parameters.

The term structure `KalshiVolTerm.fit` (`vol_surface.py:122+`) fits one implied vol per expiry window and identifies the expiry with the largest `kalshi_iv − our_vol_h` — the "most-lagged" expiry gets a 0.2¢ ranking tiebreaker in `find_best` (`signals.py:122-126`). This is intentionally conservative: it prefers the theoretically-best expiry without expanding the trade universe.

### 2.6 Gamma

Convexity risk measure — how fast `true_prob` moves per $ move in spot:

```
Γ_$ = ∂²P/∂S² × S²
```

Dollar-scaled so magnitude is comparable across price levels (a 0.01 gamma at BTC=$60K is very different from the same at BTC=$100K without scaling).

**Implementation** (`model.py:82-98`) — central finite difference with bump `h = 0.001 × S` (0.1% of spot):

```
Γ_$ = (P(S+h) − 2·P(S) + P(S−h)) / h² × S²
```

**Audit:** ✅ Correct. Bump size (0.1%) is small enough for accurate second-derivative approximation of a smooth CDF-based function. Used by TIER 0.5 (`gamma_lock`) to detect the near-strike/near-expiry zone where edge can flip faster than fixed P&L tiers would react.

---

## 3. Expected value and Kelly sizing

### 3.1 Expected value of a YES trade

Buy YES at ask `a`; contract pays $1 with true probability `p`, $0 otherwise.

```
EV = p·(1 − a) + (1 − p)·(−a) = p − a
```

Positive EV requires `p > a`. This is the raw edge. Filter threshold: `raw_edge ≥ 0.015` (1.5%) normally, `≥ 0.010` (1.0%) during vol compression (`config.py:39, 199`).

### 3.2 Binary Kelly

For a bet where you risk $1, win `b` net dollars with probability `p`, lose $1 with probability `1−p`, the Kelly-optimal fraction is:

```
f* = (p·(b+1) − 1) / b
```

For a Kalshi YES at ask `a`:
- Net gain per $ risked if win: `b = (1 − a)/a`
- Substitute: `f* = (p − a) / (1 − a)`

**Implementation** (`portfolio.py:135-146`):

```python
edge   = true_prob - ask
f_star = edge / (1.0 - ask)
return min(KELLY_CAP, max(0.005, f_star * KELLY_FRACTION))
```

Where `KELLY_FRACTION = 0.25` (quarter-Kelly) and `KELLY_CAP = 0.025` (2.5% of account hard cap).

**Audit:** ✅ Formula is the correct closed-form binary Kelly. Quarter-Kelly + 2.5% cap is a well-established safety pattern:

- Full Kelly maximizes long-run log-utility but has ~50% drawdown risk during unlucky streaks.
- Quarter-Kelly approximately quarters the drawdown risk while keeping ~44% of the log-utility growth rate.
- The 2.5% cap is another guardrail against high-edge / low-price entries where Kelly would size aggressively (a $0.05 ask with 30% true prob has Kelly f* = 26% — the cap prevents that).

Edge case: when `true_prob ≤ ask`, `kelly_fraction` returns **`0.0`** — no edge, no size. It previously returned `MAX_TRADE_PCT`, i.e. maximum size on a zero-or-negative-edge input, guarded only by the caller's `MIN_EDGE` recheck. Note the caller still falls back to `MAX_TRADE_PCT` if Kelly rounds the contract count to zero, so that recheck remains load-bearing.

### 3.3 Snipe sizing

Deep-OTM lottery entries skip Kelly entirely and use a fixed `SNIPE_TRADE_PCT = 0.01` (1%). Rationale: tail-probability estimates from a log-normal model are inherently noisy — Kelly would over-size a noisy estimate. Cut from 2% to 1% on Jul 16 after a single 516-contract paper snipe lost $117 (~1.2% of a $10K account), so one bad snipe was erasing weeks of small wins.

**Audit:** ✅ Sound principle. In practice, when your edge estimate has meaningful variance, fractional Kelly reduces to fixed sizing that's a fraction of "Kelly-if-you-were-certain".

---

## 4. Regime detection

`regime.py` classifies market state into four regimes on each tick:

| Regime | Trigger | `use_t` |
|---|---|---|
| BREAKOUT  | `|accel| > 0.004` **AND** `|mom(60s)| > 2·0.0015` | True |
| TRENDING  | `consecutive_bars ≥ 3` **AND** `|mom(60s)| > 0.0015` | True |
| REVERTING | `|zscore(300s)| > 1.5` **AND** `|accel| < 0.001` | True |
| RANGING   | (fallback) | **False** |

`use_t = True` unlocks ABOVE/BELOW contracts with direction gating (won't buy ABOVE during a confirmed downtrend). RANGING is the only regime that restricts entries to RANGE contracts.

**Vol regime** — hourly-vol thresholds (`regime.py:44-50`, `config.py:192-193`):

- `HIGH`: `σ_h > 0.015` (~150% annualized)
- `LOW`: `σ_h < 0.005` (~50% annualized)
- else `NORMAL`

Used by (a) `DistModel.true_prob` for the ±15% / ±8% vol scaling, and (b) as a diagnostic display field.

---

## 5. Entry logic — how a trade actually happens

Every 2 seconds, `scan_step` (`app.py:94+`) fires three sequential filter passes:

### 5.1 `find_best` — probability-edge scan

For each contract on the live ladder, apply these filters in order (`signals.py:53-104`):

1. Skip if already held; skip if within $150 of an existing strike (clustering).
2. Expiry gate: 6 min ≤ T ≤ 4 h.
3. Direction gate (trending regimes): skip ABOVE in downtrend, BELOW in uptrend.
4. Contract-type gate: RANGING regime → RANGE-only. Other regimes → also allow ABOVE/BELOW.
5. OTM gate: RANGE ≤ $50 OTM normally / $150 during compression; ABOVE/BELOW ≤ $100 OTM. Tightens dynamically as expiry approaches (≤ $60 inside 30 min, ≤ $30 inside 20 min).
6. RANGE boundary buffer: skip if `|otm_dist| < 40` (near-boundary flip risk), unless vol-compressed.
7. Skip if entry would immediately land in the `TIME_EXIT_MINS` OTM force-close window.
8. Compute `true_prob` (§2.1), form `raw_edge = true_prob − ask`.
9. Rank boosts: ITM contracts get `raw_edge × 1.15`; compression + near-money RANGE gets `+0.015`; best-vol-lag expiry gets `+0.002` tiebreaker.
10. Keep the highest `rank_edge` above `MIN_EDGE` (or `MIN_EDGE_COMPRESSION` during compression).

### 5.2 `find_snipe` — ROI-ranked scan (separate scan for tail plays)

`find_best` structurally favors near-money contracts (`true_prob` and ask are both larger there, so `raw_edge` in pt terms scales with contract size). A 3¢ contract with 8% true prob has only 5 pt of raw edge but 167% ROI. `find_snipe` reranks by `true_prob / ask` so these aren't crowded out (`signals.py:138-180`):

1. Ask ∈ [10¢, 25¢].
2. Direction gate (same as find_best).
3. Ratio: `true_prob / ask ≥ 1.30` (30%+ ROI).
4. Take highest ratio.

Sizes at fixed 2% of account.

### 5.3 `find_no_scalp` — MISPRICE_NO (currently disabled)

Buys NO when YES is ≥ 1.4× overpriced relative to `true_prob`. Currently `ENABLE_MISPRICE_NO = False` (`config.py:21`) pending fill-reconciliation fixes for live orders.

### 5.4 Fresh-quote guarantee

Every entry re-fetches the live best bid/ask right before order placement via `_fresh_quote` (`portfolio.py`) — never uses a cached ladder snapshot. Prevents fills at stale prices when the ladder scan and order submission are separated by hundreds of ms.

---

## 6. Exit ladder — how a trade closes

`positions.py` runs `manage()` every 2 s on a dedicated thread. Exits are **never** gated by the entry filters or the session breaker — once opened, a position always evaluates through this ladder in order (first hit wins):

| Tier | Trigger | Reason | Applies to |
|---|---|---|---|
| — | contract settled or past expiry | `expired_settled` / `SETTLED` | all |
| 0.5 | `bid ≥ 35¢` AND `pnl ≥ 15%` AND `true_prob` fading 2 ticks AND `|gamma| ≥ 40,000` | `gamma_lock` | non-snipe |
| 0.75 | `peak_pnl ≥ 25%` AND `bid ≥ 20¢` AND `pnl ≤ 75% × peak_pnl` | `peak_giveback` | **all** (snipes only while OTM) |
| 1 | `bid ≥ 30¢` AND `pnl ≥ 40%` AND `T < 15 min` | `scalp_lock` | non-snipe |
| 2 | `pnl ≥ 100%` AND `T < 9 min` | `momentum_locked` | non-snipe |
| 3 | `pnl ≥ 150%` AND `T < 15 min` | `profit_extracted` | non-snipe |
| 3.75 | `bid ≥ 12¢` AND `peak_pnl ≥ 50%` AND `pnl ≥ 15%` AND `true_prob` fading 2 ticks | `snipe_lock` | **snipe only** |
| **3.5** | **`bid ≥ 75¢`** | **`near_settlement`** | **all** |
| 4 | `pnl ≥ 300%` | `mega_profit` | non-snipe |
| 5 | `T < 3 min` AND OTM AND `|dist| > 15` | `time_exit_OTM` | all |
| 5.25 | ITM AND `|dist| ≤ 15` AND `pnl ≤ −10%` AND `T < 10 min` AND (`true_prob` fading OR `pnl ≤ −65%`) | `boundary_risk` | non-snipe |
| 6 | `bid > 0` AND `pnl ≤ −35%/time_urgency` AND `T > 18 min` AND NOT (ITM AND `T < 3 min`) | `stop_35%` | non-snipe |
| — | `mid ≤ 0.5¢` | `near_zero` | all |

**Snipe philosophy:** snipes skip most capital-protection tiers by design (`peak_giveback` is the exception — it applies to snipes while OTM, since it is peak-relative and floors giveback without capping upside). A snipe's max loss is already sunk at the cheap entry; there's no capital to "protect" by bailing early. Locking at pnl ≥ 40% defeats the 1000%+ payoff thesis. Snipes ride to either near-settlement, tier-3.75 reversal-lock, OTM time exit, or worthless expiry.

**Backtest parity — partial, verify before trusting attribution.** The backtest mirrors most tiers but **tier 1 differs materially**:

```python
# backtest (kalshi_btc_backtest.py:587)   +40% AND faded 10% from peak, no time gate
elif pnl_pct >= C.SCALP_LOCK_PCT and drop_peak > 0.10:

# live (positions.py tier 1)              +40% AND bid ≥ 30¢ AND T < 15 min, no fade gate
if bid >= SCALP_LOCK_MIN_BID and pnl_pct >= SCALP_LOCK_PCT and hours < 0.25:
```

In the backtest a position at +40% that is still *rising* (`drop_peak = 0`) is left open and can run to `momentum_locked` at +100%. Live, that same position closes at +40% as soon as it is inside 15 minutes. The backtest also has **no `snipe_lock` tier and no `is_snipe` gating**, so snipes there flow through tiers the live bot excludes them from.

Consequence: P&L attribution does **not** transfer between the two. `momentum_locked` is the backtest's largest winner (+$51,601) yet has never fired in live or paper trading — across 31 recent paper trades, 9 peaked above +100% and every one was closed first by `snipe_lock` (4, structurally excluded from tier 2), `expired_settled` (3), `scalp_lock` (1) or `peak_giveback` (1).

---

## 7. Risk controls

| Control | Value | Purpose |
|---|---|---|
| `MAX_EXPOSURE_PCT` | 18% | Cap on total capital-at-risk |
| `MAX_TRADE_PCT` | 2.5% | Single-trade cap, same as `KELLY_CAP` |
| `MAX_POSITIONS` | 4 | Concurrency cap |
| `MIN_CASH_FLOOR` | $0.25 | Absolute cash floor (the old 5% `MIN_CASH_PCT` reserve check was removed as redundant — `MAX_EXPOSURE_PCT` already caps deployment at 18%) |
| `SESSION_STOP_PCT` | 3% from running peak | Halt new entries after a drawdown (resets on restart) |
| `STOP_COOLDOWN_SECS` | 300 s | Re-entry lockout after stop-out (prevents whipsaw) |
| `STRIKE_CLUSTER_DIST` | $150 | Correlated-position cap |
| `UNTRACKED_EXPOSURE_LIMIT` | 25% | Block trading if live/tracked exposure diverges |
| `FORCE_EXIT_SLIPPAGE_CENTS` | 2¢ | Cross stale bids by this much on urgent exits |
| `STOP_LOSS_PCT` | 35%, scaled by time urgency | Base stop, tightens as expiry nears |
| `STOP_MIN_HOURS` | 0.30 (~18 min) | Don't stop in the final bars — let binaries resolve |

---

## 8. Backtest fidelity

The backtest mirrors the live bot's math for pricing, sizing, and exits, but with two known asymmetries:

1. **Vol compression signal** (§2.4) — now SMA-based on 5-min bars in *both* backtest and live (rewritten Jul 16 for parity). This asymmetry no longer applies.
2. **Fill model** — backtest models fills at Kalshi's spread with an intrabar stop simulation using bar high/low (`kalshi_btc_backtest.py:465+`, `_exit_spread` widens dynamically near settlement per commit `49d5882`). Live uses actual Kalshi IOC orders in prod and depth-capped order-book walking in paper mode. Realistic but not identical.

60-day backtest at $10,000 starting capital (Jul 24 2026, **post lookahead-bias audit**):

| Metric | Value |
|---|---|
| Trades | 516 |
| Win rate | 36.6% |
| Return | +185% |
| Sharpe | 5.15 |
| Profit factor | 1.41 |
| Max drawdown | -16.0% |
| Avg hold | 11 min |
| Vol-compression WR | 39.1% vs 35.4% normal-vol |
| Vol-compression P&L | 61% of total |

Dominant winner: `momentum_locked` (122 trades, 100% WR, +$51,601). Largest drag: `stop_loss` (274 trades, 0% WR, -$34,549).

### 8.1 Why this differs from the earlier Jul 16 figures

An earlier revision of this document cited a Jul 16 run at $5,000 capital: 1,366 trades, +2,621%, Sharpe 6.47. **That run predates the lookahead-bias audit** and is not comparable. The audit (README §"bias-elimination fixes") removed five sources of inflation, the largest being fills executing at the same bar's close that generated the signal, and exits pricing off an un-haircut model bid:

| Stage | Return | Sharpe |
|---|---|---|
| Pre-audit | +2,927% | 7.09 |
| + adverse-selection exit haircut | +701% | 4.64 |
| + next-bar fills, expiry proxy, stop slippage, NO haircut | +185–194% | 5.15–5.31 |

Two separate effects are easy to conflate here, so to be explicit:

- **The bias audit** accounts for essentially all of the gap (~14×). These were real defects.
- **The rolling window** accounts for only ~±5%. `--days 60` is anchored to *now*, so two runs the same day four hours apart gave +193.8%/Sharpe 5.31 and +184.7%/Sharpe 5.15 on identical config and an identical −16.01% max drawdown.

Quote the post-audit figure as a range (≈ +185–195%, Sharpe ≈ 5.2). The pre-audit numbers should not be cited at all.

### 8.2 The 100% win rates are structural, not predictive

`momentum_locked` reports 100% WR because it *only fires when `pnl_pct ≥ 100%`* — it cannot close a loser by construction. The same is true of `scalp_reversal`, `gamma_lock` and `near_settlement`. These are not evidence of forecasting skill; they are profit-lock tiers, and their win rate is a tautology.

This matters because the backtest exits at a **model-derived bid** (`true_prob + spread`, haircut applied) rather than a recorded order book. Every additional firing of a profit-lock tier compounds the model's own optimism once more. Measured sensitivity: changing the tier-1 condition alone to match live's `scalp_lock` moves the backtest from **+185% to +2,111%** (Sharpe 7.50, `scalp_reversal` firing 778× at 100% WR). An 11× swing from one condition means the backtest cannot discriminate between these ladders, and its absolute return should not be read as a forecast of live P&L.

---

## 9. Audit summary

**✅ Sound and verified:**
- Log-normal probability integrals for RANGE/ABOVE/BELOW (`model.py:34-84`), now with the Itô convexity correction `−σ²·T/2`.
- Binary Kelly closed-form derivation (`portfolio.py:135-146`).
- RiskMetrics EWMA vol update for the fast-realized signal (`feed.py:47-66`), half-life math matches docstrings.
- Implied vol inversion via Brent's method (`vol_surface.py:51-74`).
- Central finite difference for gamma and vega — appropriate bump sizes.
- Vol floor/cap (`model.py:11-19`) — genuinely bounds runaway readings after the Jul 6 tightening.
- **Vol compression signal now uses 5-min bar SMA(24h) vs SMA(1h) in both live and backtest** (§2.4). Bootstrapped at startup from 24h of yfinance data so the signal is live from tick 1.
- Backtest session-stop reset per day — matches live workflow of restarting the bot each session.
**❌ Known divergences (see §6 and §8.2):**
- **Backtest tier 1 does not match live `scalp_lock`**, and the backtest has no `snipe_lock`/`is_snipe` gating. Exit-tier P&L attribution does not transfer between backtest and live.
- **Profit-lock tiers report 100% win rate by construction** and exit at a model-derived bid, so backtest absolute return is not a forecast of live P&L.

**⚠️ Design choices worth being aware of (not bugs):**
- **Vol regime scaling factors** (×1.15 HIGH, ×0.92 LOW) in `DistModel.true_prob` are heuristic multipliers, not derived from a vol-of-vol model. Directionally sensible (wider distribution when vol is high) but the specific magnitudes are calibration parameters, not first-principles.
- **Regime-conditional drift weights** (0.3, 0.15, 0.5 in `model.py:54-62`) are similarly heuristic. The backtest validates that they add value in aggregate; individual weightings are not derived.

**No math bugs.** Pricing, edge, Kelly, gamma, vega, and vol-compression calculations are all textbook-correct implementations of the standard formulas.
