# Claude Handoff: Wing Calibration Audit and Required Fixes

Date: 2026-09-01  
Repository: `/Users/michael/Downloads/Finance/Quant/kalshiArb`  
Branch: `model-calibration-and-exit-fixes`  
Current uncommitted change: untracked `wing_calibration.py`

## Paste-ready prompt

```text
Act as a quantitative code reviewer and implementer for the Kalshi BTC bot.

Repository:
/Users/michael/Downloads/Finance/Quant/kalshiArb

Start by reading:
- AGENTS.md and parent instructions
- README.md
- docs/AUDIT_GUIDE.md
- docs/BACKTEST_INTEGRITY.md
- docs/QUANT_STANDARDS_AUDIT.md
- this handoff
- wing_calibration.py
- boundary_no_quote_replay.py
- kalshi_btc_bot/wing.py
- kalshi_btc_bot/signals.py
- kalshi_btc_bot/config.py

The current working tree contains an untracked wing_calibration.py. Treat it as
the proposed research change. Do not revert unrelated work. Do not enable the
wing in production, change trading thresholds, commit, or push until the
findings and test results are reported.

Objective:
Make the wing calibration study statistically and temporally honest before it
is used to decide whether the disabled YES wing should ever be enabled.

Required fixes:

1. Remove settlement look-ahead.
   wing_calibration.py::spot_at currently selects the nearest quote spot on
   either side of close_time within 120 seconds. A post-close spot can therefore
   determine the historical outcome. Replace this with an as-of resolver that
   uses the latest valid spot at or before close_time, with an explicit maximum
   staleness bound. Return unresolved when no valid prior sample exists. Add a
   regression test whose post-close price would change the outcome and prove it
   is ignored.

2. Remove regime look-ahead.
   boundary_no_quote_replay.py::join_regimes currently chooses the nearest regime
   snapshot, including a future snapshot up to tolerance_secs away. For each
   universe tick, attach only the latest quote/regime snapshot at or before the
   tick. Enforce a maximum age and count/report rows discarded for stale or
   missing regimes. Add a test where the future regime would make BOUNDARY_NO
   fire and the prior regime would not; the test must prove no signal is selected.

3. Correct the confidence-interval implementation.
   bca_interval is not BCa; it is a basic percentile bootstrap. Either implement
   genuine BCa correctly or rename it to percentile_bootstrap_interval and state
   the limitation. The interval must require a meaningful minimum number of
   independent expiry clusters, not merely eight observations. Never label a
   one-expiry or very-small-cluster result as a 95% confidence interval. Report
   observation count, expiry count, and the cluster weighting explicitly.

4. Make the fee calculation observation-level.
   Calculate taker_fee(1, ask) for each row before averaging. Do not calculate a
   single fee from the mean ask. Add a test with differing asks and verify the
   summary equals the mean of per-row net edges.

5. Make the study's scope explicit and honest.
   normalize_universe filters by bid, volume, MAX_ASK, spread, and spread percent.
   The study therefore measures liquid, production-ladder-visible bands, not
   every market band. Preserve the production filters, but print/report the
   censored counts and use precise wording in the module docstring and output.

6. Separate conditional calibration from executable replay.
   The study intentionally passes existing={} and samples/deduplicates ticks.
   Do not silently call this a full live replay. Either document it as a
   conditional quote calibration, or add a separate mode that models portfolio
   state, MAX_POSITIONS, strike clustering, cooldowns, and one-entry-per-scan.
   Preserve the distinction in output and README language.

7. Harden CLI and timestamp handling.
   Reject --every values below 1 with argparse validation. Require or normalize
   timestamps to an explicit timezone (UTC is preferred for recordings), and
   reject ambiguous naïve timestamps rather than depending on machine local time.

Required tests:
- as-of settlement ignores future spot
- as-of regime join ignores future regime
- stale/missing as-of data is excluded and counted
- minimum expiry-cluster guard prevents false-positive intervals
- renamed/non-BCa method is not mislabeled
- per-observation fee aggregation
- --every 0 is rejected
- existing wing.py behavior remains disabled when WING_ENABLED=False
- no config import freezing is introduced

Verification:
- Run syntax checks with PYTHONDONTWRITEBYTECODE=1.
- Run the focused tests. If pytest is unavailable, do not run unrestricted
  unittest discovery because this repository contains live-auth tests; use an
  explicit test list or install/use the project environment if already present.
- Run the calibration on at least one small recorded date and then the requested
  date range only after the focused tests pass.
- Compare pre-fix and post-fix counts, expiry counts, realized rates, net edges,
  and intervals. Treat any positive edge that disappears after as-of correction
  as invalidated, not as a regression to work around.

Acceptance criteria:
- No research result uses a future spot or future regime value.
- The output clearly distinguishes observations from independent expiries.
- No positive/negative verdict is printed when the independent-cluster sample is
  below the declared minimum.
- Fees are charged per observation.
- wing.py remains disabled and no production config is changed.
- All changes have focused regression tests and the working tree diff is
  reviewed file by file before any commit.

End with findings ordered by severity, the exact files/lines changed, test
commands/results, before-vs-after calibration results, and remaining risks.
```

## Audit findings and evidence

### 1. High: settlement outcome can use future information

`wing_calibration.py::spot_at()` searches the samples immediately before and
after the requested time and chooses whichever is closer. The ±120-second
default means a quote after `close_time` can resolve the contract. This is a
look-ahead/data-leakage defect, not merely interpolation.

Relevant locations:

- `wing_calibration.py:74-99` — spot loading and nearest-sample resolver
- `wing_calibration.py:227-241` — settlement lookup and outcome assignment

Minimal reproduction observed during audit: for samples at t=100 and t=103,
requesting t=101.8 selected the t=103 value. A post-close value that changes the
band membership would therefore change the reported edge.

### 2. High: regime join can use a future snapshot

`boundary_no_quote_replay.py::join_regimes()` uses the nearest regime row within
five seconds. The candidate at `index` is at or after the universe timestamp and
can be selected when it is closer. The joined `zscore`, regime, momentum, and
volatility are then used by `SignalEngine.find_boundary_no()`.

Relevant locations:

- `boundary_no_quote_replay.py:167-184` — nearest regime join
- `wing_calibration.py:195-216` — joined regime controls signal selection

This can make a signal fire based on a regime that was not known at the time of
the quote.

### 3. Medium: confidence intervals are mislabeled and under-gated

`bca_interval()` is a basic percentile bootstrap despite its name. It only
checks `len(values) >= 8`; for the final interval, `values` are expiry cluster
means, so eight observations from one expiry can produce a seemingly decisive
interval. The audit reproduced a degenerate `[1.0, 1.0]` interval for eight
identical values.

Relevant locations:

- `wing_calibration.py:136-147`
- `wing_calibration.py:282-316`

### 4. Medium: this is not a full executable replay

The signal call passes an empty existing-position map, and the study samples
every fifth fired tick before deduplicating by a two-minute remaining-time
bucket. It does not model capacity, strike clustering, cooldowns, or the
portfolio's already-open positions.

Relevant locations:

- `wing_calibration.py:150-156` — sampling option
- `wing_calibration.py:193-245` — state-free selection and deduplication

This can be a valid conditional market calibration, but it must not be reported
as executable strategy P&L or a live sequence replay.

### 5. Medium: population is filtered before calibration

The imported `normalize_universe()` excludes markets with no positive bid,
insufficient volume, excessive ask, or excessive spread. This is consistent with
the production ladder but means the study is conditioned on visible/liquid
markets. Counts of excluded observations should be reported.

Relevant location:

- `boundary_no_quote_replay.py:113-164`

### 6. Low: fee is computed from mean ask

The aggregate rows calculate one fee at the mean ask rather than the fee for each
observation. The absolute difference is usually small, but the method should be
exact and consistent with the row-level net-edge calculations.

Relevant locations:

- `wing_calibration.py:257-265`
- `wing_calibration.py:276-280`

### 7. Low: CLI and timezone robustness

- `fired % args.every` raises for `--every 0`.
- Several timestamp paths accept naïve ISO timestamps and rely on the host's
  local timezone when calling `.timestamp()`.

Relevant locations:

- `wing_calibration.py:65-86`
- `wing_calibration.py:150-156`
- `wing_calibration.py:220`

## Current safety status

- `kalshi_btc_bot/config.py` keeps `WING_ENABLED = False`.
- `kalshi_btc_bot/wing.py` is not changed by this proposal.
- Do not enable the wing based on the historical rationale in its docstring.
- No production trading parameter should change as part of this calibration
  repair.

## Verification already performed

- AST/syntax checks passed for `wing_calibration.py`,
  `boundary_no_quote_replay.py`, and `kalshi_btc_bot/wing.py`.
- `pytest` is not installed in the active Python environment.
- Unrestricted `unittest` discovery is unsafe in this repository because it
  imports a live-auth test and attempted a network request; use explicit tests.
- The existing `graphify-out/` knowledge graph was queried read-only, but it
  predates this untracked calibration file and is not authoritative for its
  implementation.

## Handoff contract

Before handing the work back:

1. Keep the wing disabled.
2. Do not commit recordings, credentials, generated outputs, or local session
   files.
3. Show the exact diff for every changed source/test file.
4. Provide pre-fix and post-fix calibration output, including the number of
   independent expiries.
5. State clearly whether the wing edge is supported, unproven, or invalidated.
