# Claude Code Audit Handoff - 2026-08-18

## Paste-ready prompt

```text
Act as three independent roles in sequence:

1. Software Architect: trace the full live and backtest data flow and identify
   conceptual, statistical, and architecture defects.
2. Code Reviewer: review the current working tree and the two latest commits for
   correctness, live/backtest parity, execution realism, and missing tests.
3. Quant Reviewer: challenge every performance claim for leakage, circular
   pricing, selection bias, dependence, calibration, and unrealistic fills.

Repository:
/Users/michael/Downloads/Finance/Quant/kalshiArb

Start by reading AGENTS.md (including any parent-level instructions), README.md,
docs/BACKTEST_INTEGRITY.md, docs/QUANT_STANDARDS_AUDIT.md, this handoff, and the
entry paths listed below. Inspect the existing dirty working tree without
reverting it. Do not commit, push, or change trading parameters until the audit
findings are presented and approved.

Primary question:
Does the market-conditioned probability update and the NO-only BOUNDARY_NO
configuration express the intended idea correctly, and is any claimed edge
supported by independent market evidence rather than the model validating its
own prices?

Required review order:
- Map the live path: feed -> regime -> ladder -> DistModel prior/posterior ->
  SignalEngine -> Portfolio fresh-quote validation/book walk -> PositionManager
  -> recorder.
- Map the synthetic path: historical OHLCV -> synthetic ladder -> signal ->
  next-bar fill -> synthetic exit/settlement.
- Compare every live gate, price, fill, size, and exit with its backtest
  counterpart. List mismatches with file/line references.
- Review the posterior mathematics. It is currently a logit-space weighted pool,
  not a prior-likelihood conjugate Bayesian update. Decide whether its naming,
  weighting, spread/time treatment, and 10-point movement cap are defensible.
- Reproduce the tests and results below. Separate synthetic evidence from real
  recorded-quote evidence.
- Audit BOUNDARY_NO at z=1.40 using held-out time windows and clustered inference
  by expiry/event. Do not treat repeated ticks or adjacent mutually exclusive
  contracts as independent observations.
- Review the current paper execution semantics for BUY_NO and forced exits,
  including top-of-book direction, complement pricing, depth walking, IOC limits,
  stale quotes, slippage, and partial fills.
- Separately audit the intended Kalshi-to-Alpaca options lane. The intended
  product is to turn a validated Kalshi lead signal into SPX and NDX options
  execution through Alpaca. Determine exactly what exists versus what is only
  described, and do not conflate it with the BTC/Kalshi execution bot.
- End with findings ordered by severity, then assumptions, then a minimal fix
  plan. Clearly label which claims are proven, encouraging, unproven, or invalid.

Known item that must be verified:
Portfolio.buy_no() rechecks the fresh-quote overpricing ratio, but appears not to
recheck BOUNDARY_NO_MIN_NET_EDGE after recomputing the posterior. The synthetic
fill path does recheck that absolute edge. Confirm whether this is a live/backtest
parity defect and provide a focused test before changing it.

Do not use the +80.94% return or 12.60 Sharpe as a live forecast. The synthetic
backtest deliberately disables the market posterior and still uses synthetic
quotes/model-derived pricing. Treat it as relative strategy evidence only.
```

## Current objective and status

The project is testing whether Kalshi market price should be treated as evidence
in addition to a GBM prior, and whether a separate NO strategy can exploit
overpriced continuation contracts at BTC range extremes.

Current paper configuration is intentionally NO-only:

- `ENABLE_YES = False`
- `ENABLE_SNIPE = False`
- `ENABLE_MISPRICE_NO = False`
- `ENABLE_BOUNDARY_NO = True`
- `BOUNDARY_NO_ZSCORE_MIN = 1.40`

The latest committed posterior implementation is on `main` at:

- `589601d Add market-conditioned probability update`
- `4b250ed Fix synthetic backtest market posterior`

`HEAD` and `origin/main` were both `4b250ed` when this handoff was written.

## Dirty working tree

Tracked changes that belong to the current experiment:

- `README.md`: documents z-sweep and NO-only testing.
- `kalshi_btc_backtest.py`: adds z-score sweep, YES/SNIPE toggles, and `--no-only`.
- `kalshi_btc_bot/app.py`: honors live YES/SNIPE enable flags.
- `kalshi_btc_bot/config.py`: switches to NO-only and lowers the boundary z gate
  from 2.50 to 1.40.

Untracked items that must not be blindly committed:

- `.codex/`
- `bot_session.lo` (empty at last inspection)
- Generated `results/`, `recordings/`, and session logs unless intentionally
  selected and checked for sensitive/local data.

Never use `git add -A` for this handoff.

## Relevant code paths

Live path:

```text
kalshi_btc_bot/__main__.py
  -> app.py
  -> feed.py + regime.py + ladder.py
  -> model.py: DistModel.true_prob() / posterior_prob()
  -> signals.py: SignalEngine.find_boundary_no()
  -> portfolio.py: Portfolio.buy_no()
  -> positions.py: PositionManager.manage()
  -> recorder.py + live_view.py
```

Synthetic path:

```text
kalshi_btc_backtest.py
  -> SyntheticFeed / RegimeEngine / DistModel
  -> build_ladder() synthetic bid/ask
  -> SignalEngine(dist, use_market_posterior=False)
  -> next-bar-open revalidation
  -> BacktestPortfolio.buy_no() / manage_exits()
```

Supporting checks:

- `test_market_posterior.py`
- `real_price_edge_test.py`
- `docs/BACKTEST_INTEGRITY.md`
- `docs/QUANT_STANDARDS_AUDIT.md`
- `results/backtest_20260818_1500.json`
- `results/z_sweep_20260817_2350.json`
- `recordings/quotes_*.jsonl.gz`
- `recordings/books_*.jsonl.gz`
- `recordings/marks_*.jsonl.gz`
- `recordings/orders_*.jsonl.gz`

Separate Kalshi-to-Alpaca research lane:

- `options_signals.py`
- `strategy_engine.py`
- `unified_analysis.py`
- `arb_scanner.py`
- `kalshi_es_analysis.py`

An existing architecture graph is available in `graphify-out/`, but it predates
the current uncommitted YES/SNIPE toggles. Verify changed source directly.

## Kalshi-to-Alpaca options lane

The intended architecture is a cross-platform lead-signal system: a validated
Kalshi signal should lead an SPX or NDX directional options decision, with Alpaca
providing options data and eventually paper/live execution.

The current source does not yet implement that full intent:

- `options_signals.py` reads Alpaca SPY option-chain data and produces flow,
  skew, unusual-activity, and GEX features.
- `strategy_engine.py` combines an older Kalshi lead-lag signal with SPY options
  features, but `enter_position()` only mutates an in-memory SPY position and
  prints it. It does not submit an Alpaca order.
- The parser exposes only `monitor` and `backtest`, despite the module docstring
  describing a `paper` mode.
- No NDX or QQQ implementation was found in the Python/Markdown corpus.
- The active BTC bot does not call this lane and does not execute Alpaca orders.

Claude should treat this as a separate, incomplete research subsystem. Before
implementation, it should verify whether Alpaca supports the exact SPX/NDX
contracts and order flow intended, define the signal contract between the
Kalshi model and options executor, and preserve the separation between signal
generation, instrument translation, risk, and broker execution.

## Intended probability logic

`DistModel.true_prob()` remains the raw GBM prior based on spot, EWMA volatility,
hours to expiry, contract bounds, and regime drift.

`DistModel.posterior_prob()`:

1. Converts the prior and current Kalshi bid/ask midpoint to logits.
2. Pools them with a market weight based on base weight, time remaining, and
   spread tightness.
3. Caps market weight at `0.35`.
4. Caps posterior movement from the prior at 10 probability points.

This solves the operational defect where a large market repricing did not move
the model estimate, but it is not a formal likelihood-based Bayesian posterior.
The audit should decide whether to rename it, calibrate it empirically, or replace
it with a better-specified update.

Live entry signals use the posterior. Position risk exits intentionally continue
to use the raw prior while recording prior, market, posterior, and market weight.
Synthetic backtests intentionally disable the posterior because their quotes are
manufactured by the same model family; treating those quotes as evidence would
be circular.

## Reproduced evidence

Posterior unit checks:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -c "import test_market_posterior as t; tests=[getattr(t,n) for n in dir(t) if n.startswith('test_')]; [f() for f in tests]; print(f'{len(tests)} posterior tests passed')"
```

Result: `5 posterior tests passed`. `pytest` was not installed in the active
Python environment.

Real recorded-quote calibration:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 real_price_edge_test.py
```

Results from 313,544 quote ticks and 276 first-sighting contracts:

| Selection | n | Mean ask | Settlement rate | EV per $1 |
|---|---:|---:|---:|---:|
| Raw prior passes edge gate | 58 | 0.1314 | 0.1379 | +5.0% |
| Posterior passes edge gate | 38 | 0.1374 | 0.1842 | +34.1% |
| Posterior rejects | 238 | 0.2314 | 0.2143 | -7.4% |

Interpretation: encouraging contract-level selection evidence, but only 38
qualifying contracts. Contracts sharing an expiry are dependent, and the
all-tick repeated-observation view was negative. This is not yet an out-of-sample
performance forecast.

NO-only synthetic backtest:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 kalshi_btc_backtest.py --days 60 --capital 500 --no-only
```

Result file: `results/backtest_20260818_1500.json`

| Metric | Result |
|---|---:|
| Trades | 217 |
| Return | +80.94% |
| Win rate | 83.9% |
| Profit factor | 2.12 |
| Sharpe | 12.60 |
| Max drawdown | -6.0% |
| Average win | +$4.21 |
| Average loss | -$10.35 |

This is a short-premium payoff: losses average about 2.5 times wins, with a
roughly 71% break-even win rate. The observed 83.9% win rate produces the profit
factor, but tail losses and clustered BTC breakouts remain the central risk.

Time split of the same synthetic sample:

| Window | Trades | Win rate | P&L | Profit factor |
|---|---:|---:|---:|---:|
| First half | 100 | 89.0% | +$188.61 | 2.49 |
| Second half | 117 | 79.5% | +$216.07 | 1.92 |

All ten calendar weeks were profitable in the synthetic run, but this does not
remove synthetic-book and model-pricing bias.

Z-score sweep result: `results/z_sweep_20260817_2350.json`. The practical best
band was approximately 1.40-1.50. The current 1.40 choice favored NO P&L and
trade quality, but it was selected on the same 60-day sample and still needs a
frozen held-out validation window.

## Known risks and audit hypotheses

1. `posterior_prob()` is a heuristic logarithmic opinion pool, not a generative
   prior/likelihood/posterior model. Its parameters are judgmental until
   calibrated on held-out real quotes.
2. The synthetic NO-only backtest does not test the posterior. It tests the GBM
   prior, BOUNDARY_NO gates, and synthetic execution assumptions.
3. Synthetic exits still depend partly on model-derived prices. The high Sharpe
   and low drawdown must not be quoted as expected live performance.
4. Resolved 2026-08-18: `Portfolio.buy_no()` now revalidates
   `BOUNDARY_NO_MIN_NET_EDGE` against the fresh executable YES bid.
5. The z=1.40 threshold was chosen after inspecting the same sample. Parameter
   selection bias remains until tested on a frozen later period.
6. First-sighting contracts are less correlated than repeated ticks but remain
   clustered by expiry and mutually exclusive ranges. Inference must cluster by
   event/expiry.
7. NO has negative-skew payoff risk: many small wins and occasional losses that
   erase several winners. Average loss is materially larger than average win.
8. Resolved 2026-08-18: README and config both state `|z| >= 1.40`.
9. Keep signal generation separate from execution and keep hard risk limits in
   configuration. Do not hide new execution assumptions inside model code.
10. The Alpaca options lane currently overstates its implementation status:
    documentation mentions paper forwarding, but the strategy engine only
    simulates SPY shares in memory. SPX/NDX option selection and broker order
    submission remain unimplemented and require independent signal validation.

## Verification already run

- Posterior test functions: passed, 5/5.
- `real_price_edge_test.py`: completed successfully.
- 60-day `$500` NO-only backtest: completed successfully.
- `git diff --check`: passed.
- Earlier syntax check for `app.py`, `config.py`, and `kalshi_btc_backtest.py`:
  passed with bytecode redirected outside the repo.

## Next steps

1. Perform the independent audit and confirm or reject the fresh net-edge parity
   finding before implementing anything.
2. Build a BOUNDARY_NO real-quote replay that uses recorded bids, asks, book
   depth, marks, and settlement, with one decision per event and clustered
   statistics.
3. Freeze parameters and evaluate a later held-out period before changing z,
   posterior weights, stop size, or sizing.
4. After approved fixes, update README/docs, rerun checks, stage only intentional
   source/docs files, and commit without recordings, logs, credentials, or local
   tool directories.

## Codex continuation — REVERTED payoff-policy changes

A previous continuation read the user's "60/40 coin" remark as a payoff
contract and implemented an entry-price policy from it. That was a
misinterpretation. **"60/40" is a probability-quality reference** — a selected
outcome estimated near 60% against a market implying nearer 40% — **not a rule
that contracts must cost 40c or less.** No strategy switch or live threshold
change was requested.

Reverted 2026-08-19:

- `TARGET_MAX_BREAK_EVEN` and `MAX_ENTRY_PRICE` removed.
- `MAX_ASK` restored to `0.45` (it had been aliased to `MAX_ENTRY_PRICE`).
- `MAX_LADDER_YES_ASK` removed; `BOUNDARY_NO_YES_ASK_MAX` restored to `0.65`.
- Bilateral 38c filters removed from `signals.py`, `portfolio.py`, and the
  backtest (selection, live YES/NO, and next-open synthetic fill).
- `kalshi_btc_bot/fees.py` deleted; fee-aware sizing and fee-adjusted
  stops/P&L removed from live and synthetic paths; the `fee` column is out of
  `trades.csv` again.
- `max_ask_sweep.py` controls `MAX_ASK` again.
- Tests written solely for the cap removed; `boundary_no_quote_replay.py`
  decoupled from the policy (its conservative one-contract fee is now a local
  research accounting term, not a config threshold).

**A trade requires positive net edge between the selected-side model
probability and the executable market price** — for NO,
`(1 - true_prob) - (1 - yes_bid)` — enforced at selection and re-checked
against a fresh quote at execution. Do not activate a 60% probability gate or
any entry-price cap without an explicit request and held-out validation.

Preserved from that work because it is independent correctness:

- **V2 order semantics** — `POST/DELETE /portfolio/events/orders`, YES-book
  direction mapping for all four buy/sell x YES/NO combinations (buying NO at
  N is a YES-book ask at 1-N), and NO `average_fill_price` converted from the
  YES leg.
- **Uncensored replay** — `boundary_no_quote_replay.py` decides on raw
  `universe` rows joined to contemporaneous quote/regime data, never the
  historically filtered `l` ladder.
- **Entry correctness** — NO cost and overpricing/edge measured at the
  executable YES bid rather than the ask, plus fresh-quote
  `BOUNDARY_NO_MIN_NET_EDGE` revalidation in `Portfolio.buy_no`.
- **Paper execution** — BUY_NO order-attempt recording, depth-walk fill
  accounting, and urgent exits crediting real executable depth instead of a
  size-less top quote.

Diagnostics that survive the revert (research only, one recorded sample, not
forward performance): legacy BOUNDARY_NO n=37 resolved, 62.2% WR, mean all-in
cost 0.751, EV/$1 -17.3%, expiry-clustered CI [-37.0%, +2.2%].
