# Quant Standards Audit — methodology vs. academic/practitioner norms

Audits the bot's methodology against standard quantitative finance practice, and
screens the strategy catalog at quantrading.space for anything actually
transferable to a single-instrument, short-dated binary event-contract bot.

**On the source:** quantrading.space is an unbranded, 600+ article index with no
visible author or citations — treat individual articles with the same
skepticism as any blog. But the content that loaded cross-checks cleanly
against real literature I already know (Bailey & López de Prado on the
deflated Sharpe ratio and probability of backtest overfitting, López de
Prado's *Advances in Financial Machine Learning* ch. 7 on purged/embargo CV,
Glosten-Milgrom on adverse selection, the standard Kelly derivation). Findings
below are graded by confidence: **confirmed** (verified against our own code
plus literature I know independently of the site) vs. **worth exploring**
(the site named a real, correctly-described phenomenon, but didn't yield
enough detail to act on directly).

---

## Methodology — verdict summary

| Area | Verdict | Severity |
|---|---|---|
| Overfitting / parameter selection | **GAP — confirmed** | High |
| Deflated Sharpe ratio / PBO | **GAP — confirmed** | High |
| Purged / embargo cross-validation | **PASS** | — |
| Kelly criterion | **PASS** | — |
| Adverse selection / exit pricing | **Partial — in progress** | Medium |
| Order book depth walking | **PASS** (one minor asymmetry) | Low |
| `gamma_lock` naming | **Docs gap** | Low |

---

## 1. Overfitting / in-sample parameter selection — confirmed gap, high severity

**Standard:** the dominant failure mode in strategy research is fitting noise
in one sample and mistaking it for edge. The fix is a genuine held-out set —
tune on one slice, validate on a slice never touched during tuning — not
re-running the same window until a number looks good.

**What we actually do:** `peak_giveback_sweep.py` grid-searches
`PEAK_GIVEBACK_FRACTION` against the full 60-day backtest and reports
`best_ret = max(results, key=lambda r: r["return_pct"])` — the in-sample
maximum, no holdout. That search is why `config.py` reads:

```python
PEAK_GIVEBACK_FRACTION = 0.75  # backtest showed 0.75 improves Sharpe 6.47 -> 7.57
```

It is not the only one. Two more config constants carry the identical
signature — chosen from a same-window sweep, never re-checked out-of-sample:

```python
NO_STOP = 0.40                  # sweep Jul 22: z2.5/stop0.40 best overall return 1407%
BOUNDARY_NO_ZSCORE_MIN = 2.5    # sweep Jul 22 showed 2.5 best
```

This compounds with the already-documented problem in
`docs/BACKTEST_INTEGRITY.md` §3: exits price off the bot's own model, not a
recorded book. So these three parameters were tuned to maximize a number that
was itself partly self-referential — selection bias stacked on pricing bias.

**Fix:**
1. Stop selecting a parameter from a single full-window sweep. Split into a
   tuning window and a genuinely untouched validation window (e.g. tune on
   the first 40 of 60 days, validate on the last 20, and don't look at the
   last 20 while choosing).
2. Any sweep script should report both windows side by side, not just the
   in-sample winner.
3. Re-validate `PEAK_GIVEBACK_FRACTION`, `NO_STOP`, and `BOUNDARY_NO_ZSCORE_MIN`
   against a window that did not inform their selection before trusting them
   further.

## 2. Deflated Sharpe ratio / probability of backtest overfitting — confirmed gap

**Standard** (Bailey & López de Prado 2014): when N parameter sets or
strategies are tried, the best-looking one is inflated by chance in
proportion to N. The deflated Sharpe ratio discounts the observed Sharpe by
the expected maximum Sharpe achievable under pure noise, given N trials and
the variance across trials; PBO, via combinatorial purged CV, directly
estimates the probability that the in-sample winner underperforms
out-of-sample.

**What we actually do:** nothing. No deflation, no PBO, anywhere in the
repo. `docs/BACKTEST_INTEGRITY.md` already flags "Sharpe above ~6 on a retail
event market is a defect signal" as a qualitative rule of thumb — this is the
formal version of that same instinct, and it would let us quantify it instead
of eyeballing it.

**Fix:** given the sweep history above, N (number of parameter combinations
effectively tried across sessions: peak-giveback sweep, NO_STOP sweep,
z-score sweep, plus every manual threshold edit in `config.py`'s change log)
is not small. Before quoting Sharpe 6.36 (or any future backtest Sharpe) as a
headline number again, compute a deflated version against a realistic trial
count. This is the single highest-leverage fix on this list — it directly
answers "is this Sharpe real" instead of asserting it by feel.

## 3. Purged / embargo cross-validation — pass

**Standard:** shuffled k-fold CV on financial time series leaks future
information into training folds; purging removes training observations whose
label window overlaps the test fold, embargo adds a buffer after each test
fold.

**Our status:** this specific failure mode doesn't apply — the backtest
already runs strictly in chronological order with no shuffling and no k-fold
structure, so there's no fold-adjacency leakage to purge. This is the same
underlying principle as the lookahead-bias fixes already shipped
(`docs/BACKTEST_INTEGRITY.md` §1), arrived at independently rather than via
this named technique. Recorded as a pass, not silently skipped.

## 4. Kelly criterion — pass

**Standard:** binary Kelly `f* = (p - ask)/(1 - ask)` for a $1-payout
contract priced at `ask`; fractional Kelly at 25–50% of full Kelly is
standard practice against estimation error in the probability input.

**Our status:** `Portfolio.kelly_fraction()` matches the formula exactly,
runs quarter-Kelly (25%, within the recommended range), floors at zero on
non-positive edge, and caps at `KELLY_CAP`. No changes needed.

## 5. Adverse selection / exit pricing — partial, already in progress

**Standard** (Glosten-Milgrom): quotes near an information event price in
anticipated adverse selection; a naive fair-value price is systematically
better than what's actually executable.

**Our status:** `_exit_bid()`'s haircut (`docs/BACKTEST_INTEGRITY.md` §1.3)
is exactly this idea, implemented as a hand-tuned discount rather than fit to
data. The real fix is already underway and correctly targeted: `recorder.py`
(shipped) captures `marks` (real bid/ask at every position-management tick)
and `books` (full resting depth). Once enough of that data accumulates, the
haircut curve — discount vs. hours-to-expiry, discount vs. distance from
50% — should be fit against real recorded fills instead of guessed. That
turns it into an actual empirical adverse-selection model instead of a
plausible-looking formula.

## 6. Order book depth walking — pass, one minor asymmetry

**Standard:** never assume infinite depth at top-of-book; walk resting
levels for size, and cap an IOC-style order at its limit.

**Our status:** verified directly against the live API (2026-07-28 session):
levels are `[price_cents, "qty_string"]`, ascending, top-of-book last —
`_walk_book` sorts descending and consumes correctly. One residual gap: paper
`sell()` calls `_walk_book` with no `limit_price_c`, so a paper-mode sell can
walk arbitrarily deep with no price floor, while live IOC sells have an
implicit floor via the urgency-slippage logic. This only affects paper
realism, not live money — low priority.

## 7. `gamma_lock` naming — docs gap only

**Standard:** true gamma scalping is continuous delta-hedging of a
long-gamma option position against an underlying — you profit from realized
vol exceeding implied vol via the rehedge cycle itself.

**Our status:** `gamma_lock` is not gamma scalping. It uses
`DistModel.gamma()` (a finite-difference convexity measure) purely as an exit
trigger — "this position's edge can flip fast, lock now" — with no
underlying hedge leg at all. Not a bug; the code's own docstring says
"Simulated gamma" and never claims to hedge. Worth being explicit about this
in `STRATEGY.md` so nobody assumes the bot runs real gamma scalping, which
would require adding a BTC spot/perp hedge leg — a materially different,
out-of-scope undertaking.

---

## Strategy catalog — what's actually transferable

The site's Strategies section is broad (200+ articles); most of it targets
instruments this bot doesn't trade (equity options, pairs baskets, perp
funding). Screened for genuine relevance to a single-instrument, short-dated
BTC binary RANGE bot:

**Volatility risk premium — confirms the existing thesis, not a new
strategy.** The variance/volatility risk premium (implied vol systematically
exceeds subsequent realized vol) is one of the most replicated findings in
options markets. The bot's core edge — buying RANGE when Kalshi's lagged vol
estimate overstates realized vol — is functionally the same trade from the
buy-cheap-convexity side. Useful as independent confirmation the underlying
hypothesis is economically grounded, not just a backtest artifact.

**Liquidation cascades — worth exploring, confirmed relevant to what you
watched live.** Site's definition, confirmed accurate: *"a feedback loop — a
price move forces leveraged positions to close, those forced trades move
price further, and the next layer of positions breaches maintenance
margin."* That's the same shape as the "flashing, reverting up" behavior
observed live on 2026-07-30 — fast one-directional move, then snap-back. The
site didn't yield a concrete detection filter (truncated), so this isn't
something to lift directly; it would need to be derived and backtested from
scratch — e.g. unusually fast one-directional velocity followed by
above-average reversion, as a regime distinct from the current
`RegimeEngine`'s BREAKOUT/TRENDING split, which may currently be conflating a
cascade-and-revert with genuine trend continuation. Flagged as a real
candidate, not yet verified.

**Gamma scalping (as an actual strategy, not the exit-tier name) — not
transferable without scope expansion.** Requires a continuously-hedgeable
underlying position; this bot has no spot/perp leg to hedge with. Would be a
different, larger project.

**Funding rate arbitrage, statistical arbitrage / PCA residuals — not
applicable.** Kalshi's KXBTC contracts carry no funding rate, and this bot
trades one instrument, not a basket. Correctly out of scope.

---

## Priority order

1. **Deflated Sharpe ratio / PBO** — highest leverage, directly answers
   whether the backtest's Sharpe numbers mean anything.
2. **Re-validate the three swept parameters** (`PEAK_GIVEBACK_FRACTION`,
   `NO_STOP`, `BOUNDARY_NO_ZSCORE_MIN`) against a genuinely held-out window.
3. **Fit the adverse-selection haircut from recorded `marks` data** once
   there's enough of it — the recorder is already shipped, this is a
   downstream analysis task.
4. **Liquidation-cascade regime detection** — real candidate, needs to be
   derived and backtested, not lifted.
5. `gamma_lock` docstring/`STRATEGY.md` clarification, paper-sell depth
   floor — both low priority, quick fixes whenever convenient.
