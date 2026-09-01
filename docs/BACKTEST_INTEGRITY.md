# Backtest Integrity — what breaks a number, and how to check

> **Reading order:** figures in this document are dated. Anything predating
> 2026-08-07 was computed on a 250-wide RANGE band that does not trade (§4), and
> anything predating 2026-08-11 used a regime momentum window that meant 2.5
> hours in the backtest but 60 seconds live (`QUANT_STANDARDS_AUDIT.md` §1e).
> Anything predating 2026-08-17 also predates the synthetic-posterior fix:
> synthetic bid/ask quotes were being treated as independent Bayesian market
> evidence even though `build_ladder()` generated them from the model family
> under test.
> Anything predating 2026-08-22 predates fee accounting (§9): the taker fee was
> not charged at all, in either the backtest or the live path.
> Current headline results live in [`../README.md`](../README.md).

This document exists because the backtest's headline return has been wrong, by
large factors, more than once — and each time it looked entirely plausible until
someone measured it. It catalogues every class of defect found so far, the
evidence for each, and a checklist to run before quoting any figure.

**Current status (2026-09-01): the backtest now runs the live strategy, and
still must not be believed.** It produces 101 NO trades over 60 days at
+15.15%. The "zero trades" status that stood here since 2026-08-24 was a
harness bug, not a gate that never cleared — the NO entry block sat under an
argument about a *different* strategy that defaults to `None` (§10).

The reason not to believe it is §3. `no_edge_gone` fires **0 times in those 101
trades** and 89 of 101 exits are hold-to-expiry, while live `edge_gone` is the
dominant exit. The synthetic NO exit ask is built from the same DistModel that
supplies `true_prob_curr`, so the overpricing ratio is structurally >= 1 and the
tier is near-unreachable. The backtest is measuring "enter on BOUNDARY_NO and
hold to expiry", which is not the strategy — the exits are where the design
lives, and the synthetic path still cannot test them.

**The two tools that can.** Entries: `boundary_no_quote_replay.py`, real quotes,
held to settlement. Exits: `no_exit_replay.py`, which drives the bot's real
`PositionManager` and `Portfolio` over recorded quotes and recorded book depth,
filling by walking real resting size. Same 20 days, same strategy, three
measurements:

| | trades | return | edge_gone fires |
|---|---|---|---|
| synthetic backtest (60d) | 101 | +15.15% | 0 |
| `no_exit_replay.py` (20d) | 50 | -0.68% | 38 |
| live book (since 08-07) | 156 | -$14.18 | dominant |

The replay lands on the live book and nowhere near the synthetic. Read a
backtest number as a regression test on the simulator, nothing more.

---

## The short version

| Class | Status | Effect when present |
|---|---|---|
| 1. Lookahead bias | **fixed** (Jul 24) | inflated ~14x |
| 2. Tautological exit tiers | **structural** (replay bypasses) | makes win rates meaningless |
| 3. Model-derived exit pricing | **fixed for NO, Sep 1** (`no_exit_replay.py`); unfixed in the synthetic path | unbounded |
| 4. Instrument mismatch | **fixed** (Jul 28) | 2.5x wrong contract width |
| 5. Live/backtest parity | **fixed** (Jul 28) | attribution didn't transfer |
| 6. Rolling window | inherent | ±5% run-to-run |
| 7. Capacity constraint (size vs. real depth) | **re-measured Sep 1 on real depth** | flat to ~$10K; breaks at $20K |
| 8. Synthetic posterior circularity | **fixed Aug 17** | synthetic prices became model evidence |
| 9. Execution costs not charged | **fixed Aug 22** | ~2.5% per round trip, unmodelled |
| 10. Wrong strategy entirely | **harness bug fixed Sep 1**; synthetic path still holds-to-expiry | measured a disabled lane |

---

## 10. The backtest measures a lane that does not trade — partially fixed Sep 1

> **The "zero trades" diagnosis below was wrong, and the cause was a harness
> bug, not a gate that never clears.** The entire NO entry block —
> `find_boundary_no` included — sat under `if no_threshold is not None:`, and
> `run_backtest` defaults that parameter to `None`. Any caller that did not pass
> a *generic MISPRICE_NO* overpricing bar silently simulated **zero NO trades**.
> One argument about one strategy switched off a different one. Worse,
> `enable_yes`/`enable_snipe` defaulted to `True` while live runs both `False`,
> so the default run measured a YES book the bot does not trade.
>
> Fixed 2026-09-01: the NO block is gated on `C.ENABLE_MISPRICE_NO` /
> `C.ENABLE_BOUNDARY_NO` and the YES/SNIPE flags default to config, so the
> backtest follows the bot unless a caller explicitly overrides. A 60-day run now
> returns **101 NO trades, +15.15%, 84.2% win rate**.
>
> **Do not believe that +15.15%.** It is §3 in its purest form: `no_edge_gone`
> fires **0 times in 101 trades** and 89 of 101 exits are hold-to-expiry, while
> live `edge_gone` is the dominant exit. The synthetic NO exit ask is built from
> the same DistModel that supplies `true_prob_curr`, making the overpricing ratio
> structurally >= 1 and the tier close to unreachable. So the backtest still is
> not measuring the strategy — it now measures "enter on BOUNDARY_NO and hold to
> expiry". The same 20 days through `no_exit_replay.py` (real quotes, real depth)
> return **-0.68%**, next to the live book's -$14.18 over 156 round trips.
>
> The lane is no longer silently empty. It is still not the live strategy until
> §3 is closed for the synthetic path too — which is why `no_exit_replay.py`, not
> the backtest, is now the reference for exits, as
> `boundary_no_quote_replay.py` is for entries.

### The original diagnosis

The live configuration is `ENABLE_YES = False`, `ENABLE_SNIPE = False`, and the
only strategy running is `BOUNDARY_NO`. A 60-day run under that exact config
returns **zero trades**: `build_ladder()`'s synthetic quotes never produce a
contract that clears the BOUNDARY_NO gate set.

So every backtest figure in this repo describes the YES and snipe lanes. The
2026-08-24 run is -43.2% over 193 trades — all 193 YES, none NO. And those
lanes are separately known to disagree with reality: the real trade log has
them at -$413 over 155 trades and -$670 over 76 trades, while the synthetic
version of the same lanes once reported +108%. `build_ladder()` prices quotes
from a lagged-vol member of the model family being tested, so the simulator is
scoring the model against prices the model produced.

This is not fixable by tuning a threshold. It would require replaying a
recorded order book instead of synthesising one — which is what
`boundary_no_quote_replay.py` does, and why that tool, not the backtest, is
the reference for entry quality.

**Consequence for the checklist:** before quoting any backtest number, check
`yes_trades` / `no_trades` in the result JSON. If `no_trades == 0`, the number
says nothing about live behaviour.

---

## 9. Execution costs were not charged — fixed Aug 22

Neither the backtest nor the live P&L accounting charged Kalshi's taker fee.
It is `ceil(0.07 × count × price × (1 - price))` cents; settlement is free.
On an 80¢ NO entry that is ~1.1¢ per contract each way, and the fee peaks at
mid-price, exactly where most entries sit.

`CHARGE_FEES = True` now applies the same `fees.taker_fee()` in both paths,
pinned by `test_fee_parity.py` — backtest/live divergence being the recurring
defect class in this repo (§5).

One implementation trap worth recording: `0.70 × (1 - 0.70)` evaluates to
147.00000000000003 cents where `0.30 × (1 - 0.30)` gives exactly 147.0, so a
naive `ceil` charged an extra whole cent on one side of the book but not the
other. `taker_fee()` rounds to 9 decimals before the `ceil`.

---

## 1. Lookahead bias — fixed Jul 24

Five separate defects, each letting the simulation use information it could not
have had at decision time.

1. **Fills at the signal bar's own close.** A signal generated at bar close was
   also filled at that close. Now queued and filled at the *next* bar's open.
2. **Expiry settled on bar close.** Expiry happens *during* a bar, so
   end-of-bar spot is post-expiry information. Now uses bar open as the proxy.
3. **Exit bids with no adverse-selection haircut.** `DistModel.true_prob`
   converges to 0/1 as `vol_t = vol_h·√T → 0`. That is a math artifact, not a
   market: no maker posts $0.97 on a contract that a late spot flip can still
   send to $0. A joint haircut on time-to-expiry and extremeness now applies.
4. **Stops filling exactly at the threshold.** Now slip 2¢ past it, matching
   live's `FORCE_EXIT_SLIPPAGE_CENTS`.
5. **NO-side exits with no mirror haircut.** Same fabrication on the
   counterparty side.
6. **Next-bar fills carried prior-bar probability.** The simulated order was
   correctly delayed to the next bar's open, but it still used the signal bar's
   `true_prob`. Now hours-to-expiry, ask, probability, and edge are recomputed
   at the open before the fill is accepted.

Progression as each landed:

| Stage | Return | Sharpe |
|---|---|---|
| Pre-audit | +2,927% | 7.09 |
| + exit haircut | +701% | 4.64 |
| + remaining four | +185–194% | 5.15–5.31 |

**Lesson:** every one of these made the number *better*. Bias is not random —
it is directional, because a simulation that peeks always peeks in its own
favour.

---

## 2. Tautological exit tiers — structural

`momentum_locked`, `scalp_lock`, `gamma_lock`, `near_settlement` and
`snipe_lock` all report **100% win rate**, always, in every run. This is not
skill. Those tiers only *fire* when the position is already profitable —
`momentum_locked` requires `pnl_pct >= 1.00`, so it cannot close a loser.

Consequences:

- A per-tier win rate of 100% is information-free. Do not cite it.
- The overall win rate is a function of **tier mix**, not forecasting accuracy.
  Shift trades between tiers and the win rate moves without the strategy
  changing at all.
- Because these tiers exit at a model-derived price (§3), each additional
  firing compounds the model's optimism once more.

**Measured sensitivity.** Changing the tier-1 condition alone — from the
backtest's old `+40% AND faded 10% from peak` to live's `+40% AND bid ≥ 30¢ AND
T < 15 min` — moved the 60-day run from **+185% to +2,111%** (Sharpe 7.50),
with `scalp_reversal` firing 778 times at 100% WR. One condition, 11x swing.

**Lesson:** if a single exit condition can move the result by an order of
magnitude, the result is measuring the condition, not the edge.

---

## 3. Model-derived exit pricing — the root cause, unfixed

Entries use a simulated Kalshi ask. Exits use `_exit_bid(true_prob, hours)` —
the bot's *own model*, discounted. There is no recorded order book anywhere in
the pipeline.

This means the backtest cannot distinguish "the model was right" from "the model
was confident." Whenever a tier exits on a model price, the simulation books a
profit that depends on the model being correct — the exact thing the backtest is
supposed to be testing.

Sharpest illustration: after full logic parity was restored (§5), the 60-day run
printed **+16,314%**, with `near_settlement` alone contributing **689 trades at
100% WR for +$1.23M**. That tier fires when the model's bid reaches 75¢, and
near expiry `true_prob` converges to 1 mechanically. The haircut in §1.3 reduces
this but does not remove it.

**The only real fix** is to record live quotes and book depth and replay against
them. Until then, treat backtest *return* as untrustworthy and use it only for
relative comparisons where the pricing error is held constant.

**Important Aug 17 boundary:** market-conditioned posterior probabilities are
for live/paper and recorded real quote replay only. The synthetic backtest now
constructs `SignalEngine(..., use_market_posterior=False)` so `build_ladder()`'s
model-manufactured bid/ask cannot become Bayesian evidence. This fixes entry
selection circularity; it does not fix model-derived exit prices.

**Known live evidence for contrast:** the Jul 1–3 live record — 63 trades,
**profit factor 0.78** — is small and stale, but market-verified rather than
model-generated.

---

## 4. Instrument mismatch — fixed Jul 28

The simulated contract was not the contract that trades.

- **Live** (`kalshi_btc_bot/contracts.py`): `parse_contract` read the ticker's
  trailing number as the range **floor** and assumed a **100-wide** band. Both
  wrong. That number is the **midpoint**, and real KXBTC bands are **250 wide**
  on the hourlies, **500** on the weekly. `B74625` is $74,500–$74,749.99.
  Verified against `floor_strike`/`cap_strike` on all 200 open markets.

  Error in `true_prob` at BTC $63,885, 0.5h out: **+0.066 to +0.367**. `MIN_EDGE`
  is **0.015**, so the parameterization error ran **4x to 24x the decision
  threshold** and varied in sign. `gamma` was ~50% off, and `otm_distance` /
  `is_in_money` ~$125 off, which silently broke every distance gate
  (`MIN_RANGE_BOUNDARY_BUFFER`, `BOUNDARY_RISK_DIST`, `TIME_EXIT_NEAR_DIST`).

- **Backtest**: `RANGE_WIDTH` was changed 100 → 250 on 2026-07-28, which moved
  the 60-day run **+185% → +402%** and win rate **36.6% → 54.0%**.

> **⚠️ That change was WRONG and has been reverted (2026-08-07).** The real
> KXBTC hourly band is **100 wide**, confirmed against ~20,000
> `floor_strike`/`cap_strike` observations recorded from the exchange itself
> (`recordings/quotes_*`); the only 250-wide observation in the entire set is
> from the single day the change was made. A 250-wide band is 2.3–4.7× likelier
> to contain spot at settlement, so every simulated contract was systematically
> overpriced — backtest snipe entries ran a median $0.218 against $0.120 in
> live fills. Reverting moved the same window from +229% to -71%, i.e. the
> profitability that this section originally credited to the "fix" was the bug.
> Full detail in [`QUANT_STANDARDS_AUDIT.md`](QUANT_STANDARDS_AUDIT.md) §1d.

**Lesson (updated):** the exchange publishes the authoritative geometry — never
infer it from a ticker string, and never accept a change that *improves* the
backtest without the same scrutiny you'd give one that hurts it. This one
survived 10 days and 35 commits precisely because it made the number go up. The
check that would have caught it in one line: compare the backtest's entry-price
distribution against real fills in `trades.csv`. They now match (p25/median/p75
0.137/0.168/0.222 vs live 0.130/0.170/0.230); before the revert the backtest was
~10¢ rich.

---

## 5. Live / backtest parity — partially fixed Jul 28

Exit-tier P&L attribution only transfers between backtest and live if the
ladders are identical. They were not, and the gaps were invisible because both
sides used the same tier *names*.

Divergences found:

| | Live | Backtest (before) |
|---|---|---|
| Entry signals | find_best, boundary_no, **find_snipe** | find_best, boundary_no — **no snipes** |
| `is_snipe` gating | 8 tiers | **none** |
| `snipe_lock` tier | yes | **missing** |
| scalp tier | +40% ∧ bid≥30¢ ∧ T<15m | +40% ∧ faded 10% |
| stop | mid-based, time-urgency, never-covered | bid-based, flat |
| intrabar stop | n/a | fired first, **ungated** |
| re-entry cooldown | 120s / 300s | **none** |
| sizing | no cash reserve, compounds | `MIN_CASH_PCT` reserve, capped at 2x initial |
| ticker identity | stable per contract | **regenerated every bar** |

The headline symptom: **`momentum_locked` was the backtest's largest winner
(+$51,601) and has never fired live or in paper.** Live's `scalp_lock` closes
those positions at +40% before they can reach +100%, and snipes are excluded
from tier 2 entirely.

**The largest single defect was not in the ladder at all.** The synthetic
ticker embedded the *bar* timestamp (`KXBTC-SIM-{bar_ts:%H%M}-B{low}-...`), so
the same strike and expiry got a different ticker string every 5 minutes. Three
live gates all key on ticker identity — the `c["ticker"] in existing` skip,
`_clustered()`, and re-entry cooldowns — and all three silently failed. The
simulation could hold and re-buy the same contract indefinitely.

Fixed by snapping expiries to the hourly grid Kalshi actually lists on, so a
contract persists across bars and its `hours_left` decays as it does live.
Effect on the 7-day run: **124 trades / +191% / Sharpe 20.13 → 70 trades /
+3.12% / PF 1.10 / Sharpe 3.23.**

Sizing was also brought to parity — the backtest held back a `MIN_CASH_PCT`
reserve (removed live) and clamped every trade to 2x the *initial* max trade,
which stopped it compounding at all while live sizes off current equity.

60-day $10K after full parity: **1,219 trades, +276%, PF 1.29, Sharpe 6.36,
max DD −20.4%** — against +16,314% before. Still elevated, and
`near_settlement` still books 38 trades at 100% WR for +$30k, which is §3
showing through.

**Lesson:** matching tier *names* is not parity. Diff the conditions — and check
that entity identity is stable, because gates that key on it fail silently.

---

## 6. Rolling window — inherent

`--days 60` is anchored to *now*, not to fixed dates, so every run uses a
different slice. Two runs four hours apart on identical config gave **+193.8% /
Sharpe 5.31** and **+184.7% / Sharpe 5.15** — with an identical −16.01% max
drawdown, confirming the simulation itself was unchanged.

Quote post-audit results as a range, never to three significant figures. Do not
attribute large gaps to the window: it accounts for ~±5%, while the bias audit
accounted for ~14x. Conflating the two produces a claim that collapses on
inspection.

---

## 7. Capacity constraint — modeled Aug 4, superseded Sep 1

> **SUPERSEDED 2026-09-01. The curve below describes the retired YES/Kelly
> configuration, measured against a MODELLED impact penalty.** `no_exit_replay.py`
> now measures capacity against recorded book depth, walking real resting size on
> both legs, and gets a far flatter curve for the strategy the bot actually runs
> (BOUNDARY_NO-only, `NO_TRADE_PCT = 0.02` flat sizing, no Kelly):
>
> | Capital | Entries | Return | | Capital | Entries | Return |
> |---|---|---|---|---|---|---|
> | $500 | 50 | -0.68% | | $5,000 | 47 | -0.93% |
> | $1,000 | 48 | -0.76% | | $10,000 | 44 | -1.25% |
> | $2,000 | 48 | -0.83% | | $20,000 | 13 | -2.84% |
>
> $500 to $10,000 is nearly flat — real depth absorbs 20x. The break is at
> $20,000, and the failure mode is not worse fills but NO fills: `_walk_book`
> cannot fill a ~500-contract order at the limit and rejects it, collapsing
> entries to 13 (a thin survivor sample — win rate 53.8%, median book staleness
> 69.5s — so treat -2.84% as directional only).
>
> The two curves are not comparable and the difference is not a contradiction:
> different strategy (YES vs NO), different sizing (Kelly, 243-contract median,
> vs flat 2%), different trade count (1,321 vs ~50 in 20 days), different exit
> pricing (modelled vs recorded book). **What survives is the principle**: report
> return as a function of capital, never as one number. What does not survive is
> the specific claim that a few thousand dollars is the limit for the current
> configuration. Capacity is not the binding constraint below ~$10k — see the
> README capacity table and `docs/STATE.md` §3.
>
> Both curves share one blind spot: neither models our own order moving the book,
> so both understate the constraint, increasingly so at size.

### The original Aug 4 measurement (YES + Kelly)

Neither `_exit_bid()` nor the entry sizing in `buy()`/`buy_no()` took position
size into account at all. A 1-contract exit and a 1,000+-contract exit priced
identically — the backtest assumed infinite liquidity on both legs of every
trade. At $10K capital, Kelly sizing routinely produced 100–1,000+ contract
positions (median 243, 91% exceeded a conservative 100-contract reference
depth). Real recorded Kalshi KXBTC book depth (`recordings/*.jsonl.gz`) ranges
from a median ~460 top-of-book contracts down to as thin as 1 on a specific
strike (2026-08-04) — and on that day, selling 833 contracts into that thin
book realized a ~4.4¢ blended fill against a ~19¢ quote moments earlier, ~58%
below top-of-book, entirely from consuming real resting depth.

**Fix.** `_size_impact_penalty(count)`: a deliberately coarse, conservative
discount (not a fitted curve — real book-*shape* data is still far too sparse
for that, same gate as §3/`fit_adverse_selection.py`), using the general shape
of market-impact literature (impact ~ √size beyond a reference depth), capped
well below the one severe real anecdote (32% at 833 contracts vs. the 58%
actually realized) so a single event doesn't get treated as a calibrated
constant. Applied **exactly once, at the realized fill inside `_close()`** —
not to the ongoing per-tick mark. First attempt applied it to both and flipped
the 60-day $10K return from +276% to **-74.6%**; that implausibly large swing
was the tell that the decision-making mark itself had been discounted, causing
stops to trigger far more readily on top of realizing worse prices — live
never does this, since positions are marked and exit decisions made off the
raw quoted bid, with size-driven slippage only discovered at the moment of the
actual sell. Corrected to -61.0%.

Entry side got a size **cap** only (`_MAX_ENTRY_SIZE = 500`, 5× the exit
penalty's reference depth) — not a symmetric price penalty. That was tried
first (worse ask paid for large buys, mirroring the exit discount) and pushed
the result to -73.3%, worse than the exit-only fix. Reverted: the only real
evidence behind the impact function is a *sell*; there's no equivalent
evidence entries face the same effect, and stacking two evidence-thin
penalties on the same trade goes further than the evidence supports. With the
cap alone: -59.7% — the cap barely binds against the 243-contract median,
confirming the exit-side penalty does essentially all of the work.

**The capacity curve.** Re-ran the corrected backtest across capital scales,
everything else held constant, to separate "is the strategy broken" from "is
$10K too much size for real depth":

| Capital | Return | Sharpe | Profit factor | Trades |
|---|---|---|---|---|
| $100 | +412% | 7.16 | 1.37 | 1,321 |
| $200 | +442% | 7.29 | 1.38 | 1,319 |
| $500 | +307% | 6.52 | 1.30 | 1,287 |
| $1,000 | +129% | 4.70 | 1.19 | 1,257 |
| $2,000 | +29% | 2.10 | 1.07 | 1,164 |
| $5,000 | -39% | -3.28 | 0.83 | 986 |
| $10,000 | -60% | -7.43 | 0.66 | 858 |

Smooth and monotonic — the signature of a real constraint, not an artifact of
one parameter choice. **The edge is real and strong at small capital scale and
does not scale to $10K.** Treat any future headline figure as a *function of
capital*, not a single number — see `README.md`'s capacity-curve table, which
replaced the old single-figure headline for exactly this reason.

**What this does and doesn't resolve.** This models a real, previously-absent
dimension (size vs. depth) and materially changes which capital scales look
viable. It does **not** resolve §3 — exits still price off the model, now with
a coarse size adjustment on top, not off a recorded book. The two issues are
independent: §3 is about whether the *price* at any given size is trustworthy;
§7 is about whether that price is achievable at the *size actually traded*.
Both need to hold for a number to be believed.

---

## Checklist before quoting any backtest number

0. **Check `no_trades` in the result JSON first.** If it is 0, the run measured
   the disabled YES/snipe lanes and says nothing about the live strategy (§10).
   Nothing further on this list matters until this one passes. Since 2026-09-01
   the lane is gated on the bot's own `ENABLE_*` flags, so this should pass by
   default — if it does not, a caller is overriding `enable_yes`/`enable_snipe`.
0b. **Check `no_edge_gone` in the exit breakdown.** If it is 0 while the run has
   NO trades, the exits were never exercised and the run measures hold-to-expiry,
   not the live ladder (§3). Use `no_exit_replay.py` for any exit claim.
1. **Does any tier report 100% win rate?** If yes, it is a profit-lock tier and
   its win rate is tautological. Check what fraction of total P&L it carries.
2. **What prices the exit?** If it is the model rather than a recorded book, the
   absolute return is not a forecast.
3. **Does the simulated instrument match the traded one?** Width, strike
   semantics, expiry spacing — check against `floor_strike`/`cap_strike`.
4. **Do backtest and live ladders match condition-for-condition?** Not just tier
   names. Diff them.
5. **Are all live entry signals modelled?** A missing signal silently removes a
   whole strategy branch.
6. **Perturb one exit condition and re-run.** If the result moves by more than
   ~2x, it is measuring that condition, not an edge.
7. **Is the window fixed or rolling?** If rolling, quote a range.
8. **Does the number seem too good?** +2,927%, +2,111%, +16,314% were each
   plausible-looking outputs of a broken pipeline. Sharpe above ~6 at large
   capital scale ($5K+, where §7's capacity constraint should already be
   dragging results down) is still a defect signal. At small, account-realistic
   scale it's no longer automatically suspect post-§7 (the $100 run genuinely
   shows ~7) — but verify the capital was actually stated and small before
   accepting it.
9. **What capital was it run at?** A single number without a stated capital
   scale is close to meaningless post-§7 — re-run across a few scales (§7's
   table) before trusting a figure at any one of them, especially $10K or
   above.

---

## Related

- `docs/STRATEGY.md` §8.1 — post-audit figures and pre/post attribution
- `docs/STRATEGY.md` §8.2 — the 100%-win-rate tiers
- `README.md` — bias-elimination fixes and Monte Carlo risk profile
- `live_pnl.py` — live-vs-backtest per-tier win rate comparison
