# Configuration Rationale

Why each parameter holds the value it does: the measurement behind it, what was
tried and rejected, and what breaks if it moves.

Extracted from `kalshi_btc_bot/config.py`, which had grown to 1,040 lines to hold
123 settings — an 86% comment ratio. **Nothing was deleted.** The evidence moved
here; the settings stayed there, each carrying a link to its section.

> Comments never cost anything at runtime. Python strips them at compile time and
> the cached bytecode is byte-identical with or without them — verified at 714
> bytes either way, a 4.7 KB `.pyc`, and a 0.21 ms module load. This split is
> purely so the settings can be read at a glance.

---

## PAPER_CAPITAL

simulated capital for paper mode — matches the
backtest's capacity-curve reference point where
the strategy is shown to work (docs/BACKTEST_INTEGRITY.md
§7); $10K sizes positions past what real Kalshi
depth can absorb without severe exit slippage.

## MAX_EXPOSURE_PCT

Risk controls
2026-07-06: tightened across the board after a live session lost ~7% in under
40 minutes via repeated max-Kelly-sized re-entries on the same boundary
(see boundary_risk cooldown fix same week). Smaller per-trade size + smaller
exposure cap + faster stop + faster session breaker compound together so no
single bad regime can reproduce that drawdown rate.

## STRIKE_CLUSTER_DIST

skip a new entry if its strike is within this many
dollars of an existing open position's strike in the
same expiry window — MAX_POSITIONS caps capital
concentration but not directional correlation. 2026-07-07:
observed live — 4 RANGE positions opened within 2 min on
adjacent strikes (62550-62850), then one BTC breakout
busted all 4 simultaneously and filled every slot with
dead positions, locking out a genuinely better ATM entry.

## UNTRACKED_EXPOSURE_LIMIT

block new trades if live exposure exceeds tracked exposure by this much

## EXIT_COOLDOWN_SECS

2026-07-28: added at 120s after snipe_lock exited B63550 at
$0.27 and the bot re-bought the same ticker 47s later at
$0.34 (+26%), losing $64.02 — a real, single-incident cost.
2026-08-04: cooldown_sweep.py swept this against a genuine
tuning/validation split (40d tune, 19d held out, never seen
during selection) at the $500 paper scale. 0s won clearly on
BOTH windows independently (Sharpe 6.82 vs 6.20 tuning, 7.44
vs 6.05 validation — 15s through 300s were all identical to
each other, meaning any cooldown above 0 was blocking the
exact same handful of re-entries with no added benefit).
The Jul 28 incident is real and still happens sometimes at
0s — the aggregate evidence across ~1,200 trades is that
pressing a signal that's still working outweighs the
occasional worse-price re-chase. STOP_COOLDOWN_SECS (loss
cooldown) is untouched — this only removed the brake on
re-entries the exit itself already proved were profitable.
2026-08-11: REVERTED to 300. Two independent reasons.
(1) That sweep could not test what it appeared to. Backtest
bars are BAR_MINUTES=5 (300s) and fills queue to the NEXT
bar's open, so "0s" means a ~5-MINUTE effective gap, not
zero; 15s-300s all push the signal one further bar (~10 min),
which is exactly why they were byte-identical. The sweep's
real finding is "~5 min beats ~10 min". Live polls every
PRICE_FETCH=2s, so live 0s permits re-entry 150x faster than
the fastest case ever simulated. That regime was untested.
(Re-ran on the corrected RANGE_WIDTH=100 instrument: 0s still
"wins" both windows, and still means ~5 min.)
(2) Real fills say the fast re-chase is the toxic one. FIFO
round trips over all paper history, split by how the PRIOR
trade on that same contract ended:
after a WINNING exit  n=27  WR 44.4%  -24.1% per $ risked
median gap 132s
after a LOSING  exit  n=17  WR 41.2%  +11.8% per $ risked
median gap 419s
Re-entry after a win LOSES; after a loss it gains — and the
only structural difference is that losses already carry
STOP_COOLDOWN_SECS=300 while wins carried none. A winning
exit fires precisely BECAUSE the move is over (scalp_lock,
peak_giveback), so re-entering two minutes later buys the
fade. Observed again live 2026-08-10 B63850: +$1.60 win,
re-entered 51s later, -$6.72.
300 chosen to match the ~5-minute gap the backtest actually
endorses. It is the floor of what has been tested, not a
tuned optimum — the backtest cannot resolve anything shorter,
so revisit as more live re-entry data accrues.

## MIN_EDGE

Entry filters (YES signals)

## DIST_TAIL_DF

None  = lognormal / Gaussian (the historical behaviour)
float = Student-t with this many degrees of freedom, rescaled so the variance
still equals the vol input (t variance is df/(df-2), so the scale is
divided by sqrt(df/(df-2))). Must be > 2 or the variance is infinite.
2026-08-20. The Gaussian was measured MISCALIBRATED on 9,110 settlement-
resolved contract-observations across 87 expiries: it understated P(YES) in
EVERY bucket, and at both ends at once —
model says P(YES)   n      model   market   ACTUAL   error
0-5%              303       3.9%     9.1%    14.5%  -10.6%
10-20%          3,534      14.7%    18.1%    19.5%   -4.8%
35-55%            447      41.3%    58.1%    59.5%  -18.2%
55%+               72      66.6%    86.0%    90.3%  -23.6%
Under-predicting the PEAK and the TAIL simultaneously is not a sigma error —
no sigma fixes both. Raising vol degraded Brier monotonically (0.1726 ->
0.1921 at x2.5); lowering it bottomed at 0.1712. That signature is
leptokurtosis: BTC drifts in a tight range over an hour and occasionally
jumps hard, which a Gaussian denies in both directions.
Tune/validate, split BY EXPIRY so none straddles the boundary (TUNE 13,329
obs / 43 expiries, VALID 23,030 / 44):
df        TUNE Brier   VALID Brier   VALID log-loss
normal        0.2031        0.1525           0.4868
2.5           0.1810        0.1503           0.4763
3.0           0.1850        0.1493           0.4735
8             0.1978        0.1513           0.4815
20            0.2011        0.1521           0.4848
EVERY df beat the Gaussian on BOTH windows. Market mid scores 0.1506 on
VALID, so this takes the model from clearly-worse to level with the price.
WHY 3.0 AND NOT TUNE'S ARGMIN OF 2.5: 2.5 was the bottom edge of the grid,
and t-variance df/(df-2) diverges as df->2, so the search was walking toward
a singularity. Since every value in the range wins both windows, the choice
within it is not critical, and 3.0 sits clear of the boundary. This is a
deliberate deviation from strict tune-selection, made for numerical
stability, NOT because 3.0 scored better on VALID.
WHAT THIS DOES NOT DO: it does not create edge. Level-with-the-market means
zero alpha, and the bot still pays ~2.70% round-trip spread. Its value is
that signals fire on `yes_bid / true_prob`, which was largest exactly where
true_prob was most understated — the signal was detecting its own bias. A
calibrated prior stops manufacturing those trades.

## KALSHI_TAKER_FEE_RATE

Reinstated 2026-08-22. Fee accounting was deleted along with the mistaken
60/40 entry-price cap on 08-18 because the two had been written together.
The cap was wrong; the fee arithmetic was not, and removing it meant every
backtest, replay and paper P&L since has overstated results.
fee = ceil(rate * multiplier * count * price * (1 - price)) per order,
rounded UP to the cent. Measured on the 39 settlement-resolved NO entries at
a realistic 14-lot size: median 1.83% of capital deployed, p90 2.76%. At 1
lot the ceil dominates and it is 2.70% median — small orders are
disproportionately expensive.
Settlement is FREE, so a held contract pays once on entry; anything exited
early pays again on the way out. Blended drag at ~35% early exits is ~2.45%.
For scale: the best stop setting measured -4.1% ROC before fees, -6.6% after.

## BAYES_MARKET_WEIGHT_BASE

Market-price probability update. DistModel.true_prob remains the raw GBM
prior; live entry/position code may blend it with the current top-of-book
quote as evidence. Keep this conservative until real_price_edge_test.py proves
the posterior improves out-of-sample EV ranking.

## MIN_HOURS

6 min — keeps entries clear of the TIME_EXIT_MINS kill zone
2026-07-06: was a flat ENTRY_PRICE_IMPROVE_CENTS=4 cross on every entry regardless
of price, sourced from the ladder's up-to-LADDER_CACHE_SECONDS-old snapshot. On a
cheap contract (e.g. ask=$0.13) that flat 4c cross alone produced an instant ~-35%
mark-to-bid loss on fill, tripping STOP_LOSS_PCT with zero real BTC movement.
Replaced with a fresh single-ticker quote fetch immediately before order
submission (Portfolio._fresh_quote) and a limit set to that live best ask
directly — no artificial cross needed since the quote is no longer stale.

## MIN_RANGE_BOUNDARY_BUFFER

skip RANGE entries within $40 of either boundary (ITM or
OTM side), all regimes. Old logic only guarded the OTM side
(dist < -20) — near-money ITM entries like dist +1..+38 with
no directional confirmation were let through and flipped OTM
by expiry on ordinary spot drift (observed: B61650 losers,
2026-07-01/02 overnight session). Widened to 40 and applied
both-sides/all-regimes on 2026-07-06, which fixed the whipsaw
but cut entry frequency ~4x vs the Sharpe-5.66 baseline
(601 trades/wk -> 143/wk). Narrowed back to 20 same day,
but 2026-07-09 head-to-head backtest (identical code,
buffer-only diff) showed 20 nearly quintuples max drawdown
(-3.5% -> -16.0%) and drags Sharpe/profit-factor/win-rate
down vs 40 — reverted to 40, trading entry frequency for
materially better risk-adjusted return. Matches the old gate's
magnitude while keeping the
both-sides/all-regimes fix that closed the whipsaw hole.

## GAMMA_LOCK_MIN_PROFIT

Exit thresholds — unified tiered ladder
TIER 0.5: Gamma-aware convexity lock — closes the asymmetry where YES positions
had no "sell when overpriced" check (the NO side already has one via NO_EDGE_GONE_RATIO).
Fires when profitable + true_prob is reversing (2-tick fade) + gamma is high, i.e. we're
in the near-strike/near-expiry zone where the model's edge can flip faster than the fixed
P&L tiers below would catch. GAMMA_HIGH_THRESHOLD is an initial estimate, not backtested —
tune it from the "gam=" values printed in the live position ticker once you've watched a
session or two.

## GAMMA_HIGH_THRESHOLD

Calibrated from live overnight gam= prints (2026-07-01):
deep-OTM/quiet positions showed |gam| ~1,000-30,000,
near-strike/high-true_prob positions ~60,000-150,000+.
50.0 was non-selective (fired on nearly every tick).

## GAMMA_LOCK_MIN_BID

price — observed live fires at bid $0.17-$0.37 on cheap
entries cut real winners short before they reached meaningful
value (2026-07-01/02 overnight session).

## PEAK_GIVEBACK_MIN_PEAK

TIER 0.75: Peak giveback — `peak` was tracked per-position but never used to
gate an exit. A trade that ran to +140% and fully round-tripped back to
breakeven/loss had zero protection unless it happened to cross gamma_lock or
one of the fixed pnl tiers below. This generalizes the snipe-reversal-lock
idea (TIER 3.75) to ordinary trades: once a real gain has formed, give back
only so much of it before locking. Independent of gamma/convexity, so it
catches reversals gamma_lock's high-convexity gate would miss.

## PEAK_GIVEBACK_FRACTION

(i.e. give back only 25% of the peak). Was 0.50 — a live
trade with peak +85% pnl round-tripped to +41% before this
tier fired, giving back ~52% of the peak. 60-day $5K
backtest showed 0.75 improves Sharpe 6.47 -> 7.57, return
+2621% -> +3262%, and max DD -9.2% -> -8.0% vs 0.50 — the
tighter setting also lets more winners survive to reach
momentum_locked (+32% more trades in that tier), so both
profit tiers work together better.

## PEAK_GIVEBACK_MIN_BID

same rationale as GAMMA_LOCK_MIN_BID — don't lock trivial cents

## PEAK_GIVEBACK_MIN_BID_MULT

A FIXED 20c floor demands a different amount of profit
depending on entry price — +150% for an 8c entry, 0% for
a 20c one — so the cheapest positions, which need the
giveback protection most, effectively had none.
Worse, it can be STRUCTURALLY unreachable: peak_giveback
triggers at entry*(1+peak_pnl*PEAK_GIVEBACK_FRACTION), and
when that price is below the floor the tier can never fire
at ANY price path. Live 2026-08-10 B64150: entry $0.13,
peak $0.21 (+61.5%), trigger $0.19 < $0.20 floor -> exit
window [0.20, 0.19] was empty, and it rode +61.5% to a
total loss (-$7.28). Four such cases observed live; the
corrected-instrument backtest shows 36 of 193 positions
(19%) structurally blocked this way in a 40d window.
min() form, not a pure multiple: a bare 1.3x floor would
be STRICTER than $0.20 for entries above ~15c and would
remove protection those positions already have.
2026-08-10 counterfactual (snipe_giveback_floor_
counterfactual.py, replays real tick paths so nothing
compounds): every candidate beat the status quo on BOTH
windows — unlike earlier sweeps where the sign flipped.
min($0.20,1.20x)  tuning +68.52  validation +14.30
min($0.20,1.30x)  tuning +39.80  validation +17.01
1.30 chosen: it wins the HELD-OUT window, and the two
leaders are within noise on the tuning one.

## SNIPE_PEAK_GIVEBACK_MIN_BID

snipe-specific floor for the same tier — kept equal to
PEAK_GIVEBACK_MIN_BID for now (no-op default). 2026-08-04:
a real snipe (entry $0.13) ran to peak +42% then +46% (bid
$0.17-$0.185) and lost it all — peak_giveback never engaged
because it never crossed $0.20. Snipes enter at 10-25c
(SNIPE_MIN/MAX_ENTRY_PRICE) — a shared $0.20 floor sits
INSIDE that range, so a real percentage-sized run can still
never clear the absolute-cents gate meant to protect it.
Split out so a lower snipe-specific value can be tested
(peak_giveback_bid_sweep.py) without touching the general
entries this floor was calibrated for.
2026-08-05: swept [0.02,0.05,0.08,0.10,0.15,0.20] with a
40d-tune/19d-validate split. Tuning picked $0.10 (Sharpe
6.78); validation picked $0.15 (5.90) and ranked $0.10 WORST
of the four re-checked candidates (5.24) — did not
generalize. Different winner per window = fails the same bar
that validated EXIT_COOLDOWN_SECS -> 0. Left at $0.20 (no-op)
pending more data; re-run the sweep periodically.

## PEAK_GIVEBACK_HARD_LOSS_PCT

TIER 0.75b: once a position has cleared MIN_PEAK, exit
even if bid is below the min-bid floor above, once pnl_pct
has cratered past this threshold. 2026-08-05: exit_coverage_
analysis.py on a 59d/$500 backtest found time_exit_OTM trades
averaged peak +105.5% -> exit -94.9% (200pp giveback, $2,848
total) — fast single-bar crashes fell straight through the
bid floor, skipping the whole window peak_giveback needs to
act, and no other tier is peak-aware. 1.50 is a true no-op
(pnl_pct floors at -100% at bid=0).
2026-08-05: swept [0.30,0.50,0.65,0.80] with the standard
40d-tune/19d-validate split. No-op (1.50) won tuning outright
(Sharpe 6.60 vs 5.89-6.31 for every real threshold — adding
this exit made the tuning window worse, not better); 0.50 won
validation (5.68 vs 5.46) — different winner per window, fails
the bar. Left at 1.50 (no-op): the anecdotal giveback was real,
but exiting early into it cuts more recoveries than it saves,
consistent with STOP_UNCOVERED_PCT's own non-monotonic-price
rationale below.

## SCALP_LOCK_MIN_BID

TIER 1 gate: same rationale — pnl% alone let tiny-entry
positions lock at trivial absolute prices.

## TIME_EXIT_MINS

TIER 5: OTM with < 3 min left — let late-window mispricing play out

## TIME_EXIT_NEAR_DIST

TIER 5 override: skip the force-exit above if still within this
many points of the strike boundary — a near-boundary OTM position
can flip ITM by the buzzer, so only force-exit while still far OTM.
2026-07-07: added after a snipe was force-closed for a modest gain
at TIME_EXIT_MINS while sitting close to the boundary.

## STOP_LOSS_PCT

TIER 6: base stop. 2026-07-06: tightened from 0.60 (which had
itself been widened from 0.40 on 2026-07-01 "to allow late
recoveries") — cut losers quickly, let winners ride via the
profit-lock tiers above instead of hoping for a comeback.

## STOP_UNCOVERED_PCT

2026-07-03 the user judged that a tight stop near expiry is
wrong by design: binary prices don't move monotonically into
settlement, so a 35% stop there cuts winners on ordinary
wobble as often as it saves losers. That reasoning holds for
a position riding INTO expiry — but MIN_HOURS (6 min) lets
positions be OPENED inside the gate, and those never had any
floor at all. 2026-07-28: the 4 entries with stop coverage
netted +$53.53, the 2 without netted -$143.53, riding to -77%
and -92%. This is a catastrophe floor, not a stop — at -65% it
cannot cut a winner, only a position already most of the way
to a total loss. Same number the user chose for
BOUNDARY_RISK_HARD_STOP, for the same reason.

## CUT_NEVER_GREEN_MINS

The premise looked strong: 58 of 313 backtest
trades never traded above entry and lost
-$324.47, only 7 recovering. But "never green
YET" at 5 minutes is a completely different
population from "never green EVER", and the
difference is the whole trade.
Measured by shadow instrumentation (green_by_N,
which records status at each age without acting
on it), of positions still red at 5 minutes:
67% went green later
39% ended winners
net P&L +$251.59, not negative
Their winners average +$16.20 against losers at
-$4.32 — a 3.75:1 payoff living entirely inside
the cohort this rule would have killed. That is
structural, not a fluke: a cheap OTM binary is
SUPPOSED to sit underwater while spot works
toward the strike. Being red early is the normal
state of the eventual winner.
never_green_sweep.py confirms: every value on
0/5/10/15/20/30 loses to OFF on BOTH windows,
monotonically worse the tighter the cut
(tuning +88.9% -> +69.7% at 5 min).

## REENTRY_SIZE_DECAY

0 = disabled. If > 0, any entry on a ticker
already traded this expiry is capped at
DECAY x the dollars deployed on the previous
entry in that same ticker. 1.0 = "never bigger
than last time", 0.5 = each attempt half the
last.
WHY: Kelly sizes UP as a contract collapses.
f* = (true_prob - ask)/(1 - ask), so with the
MODEL UNCHANGED at true_prob=0.20, an ask
falling 0.21 -> 0.09 takes f* from 0 to 0.121
while each dollar also buys 2.3x more
contracts. The model does not have to be wrong
for size to explode — it only has to stay the
same while the market disagrees harder.
Observed live, B63625 on 2026-08-13:
16:10  19 @ $0.21 ($3.99)  -> stop  -$1.90
16:16  33 @ $0.15 ($4.95)  -> stop  -$2.31
16:29 138 @ $0.09 ($12.42) -> stop  -$6.90
7x the contracts on the third attempt, and the
single worst loss of the session. STOP_COOLDOWN
did not stop it: the re-entries landed at 5:01
and 5:02 after each stop, just past the 300s
timer. The cooldown is a timer with no memory —
it never learns that this contract already beat
us. This is the memory.
TESTED, AND NOT ENABLED. The mechanism above
is real, but neither test supports acting:
* Backtest VALIDATION window: 28 trades, 28
distinct tickers — ZERO re-entries. The cap
cannot bind, so all values return
byte-identical results. The window carries
no information about this parameter.
* Backtest TUNING window: only 14 re-entries
in 213 trades. DECAY=0.75 gives return
+88.9% -> +76.4%, maxDD -12.2% -> -11.1%,
Sharpe 6.39 -> 6.48. Underpowered either
way.
* Live-book counterfactual (same fills, fewer
contracts) is the decisive one and it says
NO: return on deployed capital goes -12.3%
(off) -> -17.4% (0.75/0.5). The re-entries
were better per dollar than the first
entries, matching the by-attempt split
where attempt 3+ averages +$21.11 (n=17).
The backtest structurally UNDER-REPRESENTS
re-entry — 5-min bars leave far less room to
re-enter inside a contract's life than 2s live
ticks, which is why live shows 53 re-entries
against the backtest's 14. Treat any future
re-entry rule as untestable on bars.

## STOP_MIN_HOURS

Below this, TIME_EXIT_MINS handles OTM exits and
expiry_settle captures ITM wins — don't stop binary
options in their last bars when the binary payoff
hasn't resolved yet.

## SNIPE_STOP_PCT

above entirely (gated `not is_snipe`) — a fixed % stop
defeats their whole 1000%+-payout thesis. But that leaves
a snipe that never builds a peak with NO floor at all.
2026-08-05: exit_coverage_analysis.py on a 59d/$500
backtest found 179 of 196 losing snipe exits averaged
-94.7% pnl_pct (vs -45.7% for non-snipe stopped/
boundary_risk losses) — 99% of all snipe-loss dollars,
spread across entry times (not just the near-expiry
window).
Swept [0.50,0.65,0.80,0.95] vs the no-op (1.50) with the
standard 40d-tune/19d-validate split: no-op won tuning
outright (Sharpe 6.60 vs 5.84-6.14) but 0.50 won validation
(5.66 vs 5.46) — different winner per window, so initially
left at 1.50 pending more evidence. 2026-08-06: real
paper+live history (FIFO-matched across 61 closed snipe
lots, 5 weeks) settled the disagreement — snipes are net
-$653.41 despite a 57.4% win rate, because expired_settled +
time_exit_OTM (both unprotected) total -$1,712.53, more than
snipe_lock's +$938.08 in wins recovers. That's real fills,
not a model; turned the floor on at 0.50, the validated
out-of-sample winner.

## BOUNDARY_RISK_DIST

TIER 5.25: Boundary risk — ITM but marginal + underwater + near expiry.
TIME_EXIT_MINS (TIER 5) only protects positions once already OTM; a marginal ITM
position carries the same flip risk right up until it crosses. Momentum-gated
(2-tick true_prob fade, same signal as gamma_lock) so ordinary chop doesn't
trigger it — gives the position room to be volatile — but exits once the move
is actually working against it. Hard floor below fires unconditionally as a
backstop even without momentum confirmation.

## BOUNDARY_RISK_MINS

window before expiry this tier is active — wider
than TIME_EXIT_MINS so it can act before the flip

## SNIPE_MIN_ENTRY_PRICE

2026-07-07: floor added — trade log showed 1-9c snipes were
── SNIPE MODE — deep-OTM cheap lottery tickets aimed at asymmetric 1000%+ payouts ──
find_best()'s ranking picks the largest raw probability-point edge, which structurally
favors near-money contracts (both true_prob and ask are larger there). A 3¢ contract
with true_prob=8% has only 5pts of raw edge and never wins that ranking even though its
ROI (true_prob/ask) is 167%. find_snipe() is a separate ROI-ranked scan so these aren't
starved out by the main signal.
a coin flip (2 of 3 resolved outcomes settled for a total
loss of stake), and none had ever reached the 75c
near_settlement tier. Raising the floor screens out the
deepest-OTM tickets where the ask is cheap because Kalshi's
own model already prices them near-zero, not because of lag.

## SNIPE_TRADE_PCT

sized down vs MAX_TRADE_PCT — tail-probability estimates
are noisier, so size the bet down rather than Kelly-size
off an uncertain edge. 2026-07-16: cut 0.02 → 0.01 after
a single 516-contract paper snipe lost $117 (~1.2% of
$10K account) — one bad snipe was wiping out weeks of
small wins. 1% caps single-snipe max loss to ~$100
while backtest still shows the tier remains net-positive.

## SNIPE_PROFIT_LOCK_PEAK

TIER 3.75 — snipe reversal lock. Fires when a snipe that has already run gives
back into a 2-tick true_prob fade (same signal as gamma_lock), NOT a fixed price
cap: a snipe still climbing without a reversal is untouched.
PEAK raised 0.50 -> 1.50 on 2026-08-04, resolving the design question the
previous comment here deliberately left open. Evidence: a 2026-08-04 paper
session saw this tier fire twice at peak 50.0%/54.6%, exiting at +50%/+37%
pnl — essentially at first wobble past the old 50% gate. Snipes enter at
10-13c targeting settlement near $1.00 (a 700-900% gain); locking a third
to half of that is capturing a sliver of the position's designed upside, and
reacting to any 2-tick fade the moment peak crosses 50% behaves like
gamma_lock (fast, convexity-driven) rather than a patient lock that only
protects against a genuine reversal after a genuine run. 1.50 restores the
ORIGINAL documented intent (the dead SNIPE_PROFIT_LOCK_PCT this tier's
thresholds were hardcoded around before being wired to config on Jul 28) —
a snipe must have actually run before this tier can even become eligible.

## SNIPE_PROFIT_LOCK_MIN_BID

absolute price floor — same rationale as GAMMA_LOCK_MIN_BID

## NO_OVERPRICING_MIN

MISPRICE_NO entry filters
Threshold sweep (Jul 22): synthetic backtest can't discriminate thresholds — SMA/EWMA spread
always exceeds 1.40, so all values fire identical trades. Starting at 1.18 for paper to let
real Kalshi pricing tell us where the edge actually lives.

## NO_PROFIT_CAPTURE

MISPRICE_NO exit thresholds
Backtest (Jul 22): no_stop at -30% was the dominant drag (-$90k on 188 stops vs +$60k wins).
Tightened to -20% to cut reversals sooner — BTC spiking into the range rarely recovers.

## NO_STOP

**0.30 as of 2026-08-26** (was 0.40).

A stop must be read against the payoff it is protecting. This strategy risks
~$0.81 to win ~$0.19, so a stop at X% of cost realises a loss of 0.81X against
a maximum gain of 0.19:

    stop 40%  ->  $0.32 lost  =  1.7x the entire premium  ->  erases 1.7 winners
    stop 30%  ->  $0.24 lost  =  1.3x the premium         ->  erases 1.3 winners

A stop that costs several times the max gain is structurally wrong for premium
selling, and 40% was doing exactly that.

Swept over 41 settlement-resolved armings across 33 expiries, 2026-08-18 to
08-25, edge_gone disabled, fees charged, expiry-clustered bootstrap:

    stop |  WR      ROC     PF    total          95% CI   P>0 |   TUNE    VALID
     15% | 59%    +0.8%   1.13    +4.55   [-5.2%, +6.4%]  60% |  +2.4%    -0.4%
     20% | 66%    -0.1%   1.09    +3.87   [-8.4%, +7.0%]  51% |  +4.6%    -3.4%
     25% | 73%    +2.1%   1.34   +12.64   [-6.6%, +9.5%]  71% |  +5.9%    -0.5%
     30% | 76%    +3.7%   1.44   +16.28  [-5.9%, +12.4%]  79% |  +5.9%    +2.1%
     35% | 76%    -0.7%   1.03    +1.51  [-12.6%, +10.1%] 47% |  +0.9%    -1.8%
     40% | 76%    -2.2%   0.94    -3.36   [-15.4%, +9.2%] 37% |  -2.6%    -2.0%
     50% | 78%    -4.6%   0.81   -12.78   [-18.4%, +8.4%] 26% |  -5.6%    -3.9%
    none | 78%    -5.4%   0.78   -15.35   [-19.4%, +7.9%] 23% |  -6.5%    -4.6%

30% is the only level positive on BOTH halves of an expiry-clustered split.

WHAT TO TRUST HERE, AND WHAT NOT TO. The direction is well supported: decay
from 30% upward is monotonic (+3.7 -> -0.7 -> -2.2 -> -4.6 -> -5.4) and the
economics above predict it independently. The exact value is not: n=41, the
95% CI on +3.7% is [-5.9%, +12.4%] and includes zero, P(ROC>0) is 79% rather
than 95%, and roughly 24 configurations were swept the same day across dip
levels, exit variants and stop levels — enough tests that one surviving a
split is unsurprising by chance. Treat 0.30 as "40% was too loose", not as an
optimum. Re-measure before tightening further.

Supersedes the Jul 22 sweep (z2.5 / stop 0.40, "best overall return 1407%"),
which was run on the 250-wide RANGE band that does not trade — see
BACKTEST_INTEGRITY.md section 4 and QUANT_STANDARDS_AUDIT.md section 1d.

## NO_EDGE_GONE_MIN_GAIN

0.0 restores the historical behaviour (fire on any positive pnl).

`edge_gone` sells when the overpricing ratio collapses AND the position is up.
The second condition was near-vacuous: a NO position decays toward $1.00, so
almost every eventual winner crosses "up" early. It fired on 27 of 41 armings.

Measured 2026-08-25, settlement-resolved, fees both sides:

    those 27 exits    ->  +$7.64
    holding the same  ->  +$19.21
    tier cost         ->  -$11.57  (about $0.43/trade)

Only ~$1 of that is the exit fee. Settlement is FREE and an early exit is not,
but the bulk is forfeited convergence: selling at 0.95 gives back the last five
cents of a premium whose risk had already been carried.

Whole-ladder effect, same 41 armings:

    edge_gone ON,  40% stop  ->  -3.8% ROC, PF 0.59, -$14.22
    edge_gone OFF, 30% stop  ->  +3.7% ROC, PF 1.44, +$16.28

0.15 keeps the thesis-exit available for cases where the edge genuinely
collapsed while leaving ordinary convergence alone. Note that 0.15 measured
better than 0.0 (-3.8%) or 0.30 (-2.4%) in the same sweep, and a non-monotonic
interior optimum on n=41 is a noise signature — the defensible claim is "> 0",
not "0.15 exactly".

The 40% stop was measured as GOOD in the same run (+$6.19 vs holding). Only
edge_gone leaks. Do not disable both.

## MIN_HOLD_SECS

0 = off (the historical behaviour: a position could be opened and closed on
consecutive 2s scans).
2026-08-21. A BOUNDARY_NO position was bought at $0.650 and sold 2 SECONDS
later at $0.520 for -$1.82, reason `edge_gone` — a rule that can only fire
when the position is UP. Nothing about the world changed in two seconds; only
the quote did. The bot paid the ~2.70% round-trip spread twice for nothing.
Live book, 286 round trips, by hold time:
< 10s     n=  7   total -$179.63   win rate   0%
10-60s    n= 20   total  -$27.49   win rate  45%
1-5m      n=117   total +$366.70   win rate  54%   <- only + band
5-30m     n=124   total -$952.68   win rate  52%
30m+      n= 18   total -$304.08   win rate  28%
Sub-10-second round trips are 0 for 7. (Era-contaminated — spans the
RANGE_WIDTH bug and the YES/snipe lanes — so read the direction, not the $.)
60s is where an adverse move STOPS being noise. Settlement-resolved, 39 NO
episodes, P(NO settles our way) conditioned on having dipped >2% by time t:
at  10s   dipped 76%  vs  not-dipped 59%   gap +17pp  (WRONG way)
at  30s          65%              68%           -3pp
at  60s          52%              83%          -31pp
at 300s          50%              84%          -34pp
Before ~60s a dip mildly predicts WINNING; after it, losing. Every exit
inside the first minute is therefore triggered by noise. Corroborating: all
26 winners went green at some point, median time-to-first-green 59s (p25 0s,
p75 353s) — exiting sub-minute cuts winners before they turn.
Exempt from the hold, deliberately: expiry-forced exits (never hold into
settlement) and a catastrophe floor, matching the existing design note that
only a -65% floor covers positions opened inside the expiry gate. Of the 7
episodes that reached -40%, only 1 settled our way, so the STOP is doing real
work — it is the FAST exits that are not.
Scope: wired into the NO exit ladder only, which is the lane that trades.
The YES ladder has the same defect and the same symptom on record
(positions.py: "live 2026-07-23 four stops in 15 min, all peak_pnl=0, one at
12s hold") but is disabled, so it is left alone rather than changed blind.

## CONFIRM_EXIT_DEPTH

False = the historical behaviour (decide on top-of-book, fill on depth).
The exit ladder decides using `1 - yes_ask`, a top-of-book quote carrying NO
quantity, while Portfolio.sell() fills by walking the resting ladder for the
whole position. Those are different prices, and on a thin book they are very
different. 2026-08-21: `edge_gone` — a rule that can only fire when the
position is UP — booked -$1.82, because the decision saw a winner and 14 lots
actually cleared 13c lower.
109 recorded exit attempts, fill vs the price the decision used:
median  +0.00%      p10 -13.68%      p90 +2.60%      worst -75.47%
edge_gone        n=43   median +0.00%   worst -24.64%
stop_35%         n=20   median +0.00%   worst -29.84%
snipe_lock       n= 7   median +0.00%   worst -75.47%
Usually free, occasionally ruinous — a fat left tail, not a constant drag.
Applied to PROFIT exits only (misprice_captured, misprice_time, edge_gone):
they claim a gain that may not exist at size, so they are re-checked against
the executable price and skipped if it is not actually a gain. STOPS
deliberately skip the check — if the real price is worse, a stop is MORE
valid, not less, and deferring it would be the opposite of the intent.
Cost: one extra orderbook fetch, and only when a profit rule has already
fired — not once per position per 2s scan.

## ENABLE_BOUNDARY_NO

BOUNDARY_NO — sell OTM premium at range extremes
When z-score shows BTC at the top or bottom of its recent range, fade the
OTM contracts in the continuation direction. Market overprices a breakout
that mean reversion says won't happen. Same `is_no` position + exit logic
as MISPRICE_NO, different entry gate (z-score extremes instead of raw overpricing).

## BOUNDARY_NO_ZSCORE_MIN

|z| must exceed this to count as a range extreme (Aug 17 sweep: 1.40-1.50 best NO band)

## BOUNDARY_NO_OVERPRICING_MIN

**1.25 as of 2026-08-26.** Was 1.15, raised to 1.60 on 08-22 to "raise the
quality bar", now lowered again because that reasoning was backwards.

The gate requires `yes_bid / true_prob >= X` — the market's price must exceed
the model's probability by X. Raising it sounds like demanding a bigger
mispricing. It is not, because the model is the thing being divided by, and the
model is known to be miscalibrated:

    the ratio is largest exactly where true_prob is most UNDERSTATED

That is already documented as the foundational result of this repo (the model
does not beat the market price; five independent tests). So a high bar does not
select for better mispricings, it selects for **larger model errors**. Demanding
1.60 was asking for the contracts the model gets most wrong.

Swept over 91 armings across 61 expiries, 15-min window, z>=1.40,
settlement-resolved, fees charged, expiry-clustered bootstrap:

     ratio     n   /day     WR      ROC     PF               95% CI   P>0     TUNE    VALID
      1.00    91   13.0    82%    +3.3%   1.22   [-6.1%, +12.4%]   76%    +1.0%    +5.1%
      1.15    91   13.0    82%    +3.3%   1.22   [-6.1%, +12.4%]   76%    +1.0%    +5.1%
      1.25    85   12.1    84%    +4.2%   1.29   [-5.3%, +13.8%]   81%    +1.7%    +6.0%
      1.40    67     9.6    82%    +2.9%   1.17   [-8.9%, +14.1%]   70%    +0.1%    +5.4%
      1.60    32     4.6    81%    -2.2%   0.93  [-20.0%, +13.8%]   40%   -13.3%    +3.6%
      1.80    11     1.6    73%   -14.0%   0.55  [-43.3%, +15.9%]   16%   -29.1%    -1.5%
      2.00     7     1.0    57%   -30.4%   0.32   [-68.1%, +9.4%]   10%   -40.8%   -16.6%

Monotonic above 1.25 and positive on BOTH halves of the split from 1.00 to
1.40. It also triples the trade rate, 4.6/day to 12.1.

Note 1.00 / 1.10 / 1.15 are identical — 91 armings each. Below about 1.20 the
ratio admits everything the net-edge gate already admits, so it stops binding
at all. That is the same "provably inert" property the ORIGINAL 1.15 had, and
why the 08-22 note called it inert. The correct reading was that the gate does
little useful work, not that it needed to do more.

1.25 rather than 1.15 keeps a token amount of separation between the two gates
while sitting at the measured peak. Do not read 1.25 as an optimum — n=85, the
CI includes zero, and it is the best cell of a sweep.

Superseded: the 08-22 note claiming 1.15 was inert and 1.60 raised quality.
Half right. It was inert; raising it made things worse, not better.

## NO_EXEMPT_FROM_COOLDOWN

The re-entry cooldown exists to stop MOMENTUM
chasing: a YES position exits on scalp_lock /
peak_giveback, which are PRICE-based, so the move
is over and re-entering buys the fade — measured
at -24.1% per $ risked over 27 round trips.
NO exits are EDGE-based (edge_gone: the
overpricing corrected), and a NO re-entry cannot
happen unless |z| >= BOUNDARY_NO_ZSCORE_MIN, the
overpricing ratio clears, AND net edge clears
BOUNDARY_NO_MIN_NET_EDGE — all on current prices.
The cooldown was guarding against something those
gates already prevent, while blocking re-entry
into a mispricing that had genuinely returned.
Evidence is thin (1 observed NO re-entry: bought
0.78 at +14.5% edge, exited +$1.44, re-entered
0.87 at +4.8% edge, exited +$0.22 — smaller edge,
smaller profit, still positive). Note that
re-entry would now be blocked anyway by the 5%
net-edge floor, so this mainly frees genuinely
strong repeat signals. Set False to restore.

## BOUNDARY_NO_MIN_NET_EDGE

**0.05 -> 0.04, measured 2026-08-30.** The old bar was rejecting trades by
fractions of a cent on the fresh quote — observed live skipping net edge
$0.043 and $0.042 against a $0.050 bar, twice in three seconds on the same
contract.

Swept against 327 candidates over 129 expiries, settlement resolved from the
QUOTES stream at true close_time (never `universe` — see the blind spot in
3b8459a / b2052b3), expiry-split into tune and validate halves:

    net-edge  ratio    n    WR      ROC     PF    total     TUNE    VALID
       0.050   1.25   19   89%    +9.4%   1.83   +15.18    -0.3%   +22.8%  <- was
       0.040   1.25   35   91%    +8.7%   1.96   +26.85    +5.4%   +11.8%  <- now
       0.030   1.25   55   87%    +1.6%   1.08    +5.36    -3.2%    +7.3%
       0.020   1.25   81   89%    +1.5%   1.10    +8.81    +0.4%    +2.6%

0.04 nearly doubles the trade count while holding ROC (+8.7% vs +9.4%),
RAISES profit factor (1.96 vs 1.83), and is positive on both halves — which
0.05 is not (tune -0.3%). Below 0.04 it collapses: +8.7% -> +1.6% -> +1.5%,
with 0.03 negative on tune. A clean gradient either side, not an isolated
spike, which is what makes 0.04 credible rather than cherry-picked.

The RATIO bar was checked in the same sweep and stays at 1.25: at 0.04 net
edge, loosening it to 1.15 buys 6 more trades and costs 3 points of ROC
(+8.7% -> +5.8%). It is doing real work. The net-edge bar was the tight one.

Caveat: n=35, and 12 combinations were swept. The gradient and the
both-halves result are what carry this, not the point estimate.

minimum ABSOLUTE edge on the NO side:
HELD AT 0.05 alongside the raised BOUNDARY_NO_OVERPRICING_MIN (1.60).
2026-08-22, measured at ratio >= 1.60, net of fees, split by expiry:
net_edge    TUNE     VALID      ALL    n    WR
>= 0.00    -0.9%     +3.7%    +1.7%   98   88%
>= 0.05    -1.4%     +3.2%    +0.8%   64   84%   <- kept
>= 0.08       (fewer than 8 candidates — not measurable)
0.00 scores better on this sample, and there is a real argument for it: this
gate keys off (1 - true_prob) - cost, which selects contracts where the MODEL
most disagrees with the price, and the 2026-08-20 calibration work showed
that disagreement is largely the model's own bias. By that logic the gate
partly selects for model error, while the ratio test is relative and less
bias-sensitive.
Kept at 0.05 anyway, deliberately. Requiring a positive absolute edge is the
economic floor of the trade — paying 80c for something the model values at
82c is a coherent bet, and a gate at 0.00 would accept a 0.1c edge that
cannot survive a 2.5% fee-and-spread load. The +0.9pp that 0.00 buys on 118
expiries is not worth removing the one gate that enforces the trade's own
arithmetic. Raise it above 0.05 only when there is enough data to measure it.
(1 - true_prob) - no_cost  >=  this
The overpricing gate above is a RATIO, which is
scale-free and therefore blind to what the position
costs. At true_prob=0.066 / yes_ask=0.10 the ratio is
1.52 — sails through — while the absolute edge is
3.4pp on a 92c position.
2026-08-13, the eight BOUNDARY_NO entries taken so far,
by NO cost: risk/reward and edge degrade together.
cost 0.65  R:R 1.9:1  edge +18.8%
cost 0.78  R:R 3.5:1  edge +14.5%
cost 0.82  R:R 4.6:1  edge  +5.6%
cost 0.86  R:R 6.1:1  edge  +3.2%
cost 0.92  R:R 11.5:1 edge  +1.4%   <- +$0.10 realised
A 92c NO risks 92c to win 8c: one loss erases eleven
wins. Worse, 1.4pp sits INSIDE our own model error —
calibration_check.py measured true_prob predicting
13.6% where 18.3% occurred, a ~4.7pp miss. An edge
smaller than the model's known error is noise with a
sign, not edge. 0.05 is set at roughly that error
floor; it would have kept the +18.8% and +14.5%
entries and rejected the four thinnest.

## BOUNDARY_NO_HOURS_MAX

**0.25 (15 min) — re-confirmed 2026-08-27 under the corrected gates.**

Entries run from BOUNDARY_NO_HOURS_MIN (0.08h = 4.8 min) to here, a 10.2-minute
slice — 17% of an hourly contract's life, and the single biggest cut in the
funnel at -84% of rows. The floor exists so an entry cannot land inside its own
exit window: time_forced_no fires under 2 minutes and NO_TIME_PROFIT under 5.

An earlier sweep suggested opening the window to 60 minutes (-1.6% against
-2.2% at 15 min), and that result is VOID. It ran at ratio 1.60 and ask ceiling
0.65 — the first selecting for model error, the second admitting the cheap-NO
population that measured -36% ROC. Both interact with time to expiry, because a
band further from expiry has more time to be reached and prices closer to a
coin flip. Correcting them flips the ordering completely.

Re-swept at ratio 1.25 / ask 0.30, settlement-resolved, expiry-clustered:

     max    n   /day    WR    cost     ROC     PF    total          95% CI   P>0    TUNE   VALID
     15m   88   11.0   85%   0.805   +4.5%   1.31  +$35.07  [-4.6%,+13.5%]  83%   +1.9%   +6.6%
     21m  136   17.0   82%   0.808   +0.5%   1.03   +$6.67   [-7.4%,+8.0%]  56%   -7.2%   +6.1%
     30m  178   22.2   80%   0.812   -2.4%   0.88  -$37.88   [-8.7%,+3.8%]  23%   -7.8%   +2.2%
     45m  225   28.1   82%   0.818   -1.3%   0.93  -$27.02   [-6.3%,+3.7%]  32%   -5.3%   +2.6%
     60m  277   34.6   82%   0.822   -1.0%   0.94  -$28.03   [-5.3%,+3.0%]  30%   -3.1%   +1.0%

15m is the only row positive on both halves of the split, the only one with
P(ROC>0) above 56%, and the only one that makes money. 60 minutes buys 3x the
trades and turns +$35 into -$28.

Sliced rather than accumulated, so a good early bucket cannot carry the average:

    entry at        n     WR    cost      ROC     PF     total
    4.8-15 min     52    85%   0.814    +2.6%   1.17   +$12.15
    15-24 min      57    81%   0.817    -2.7%   0.87   -$12.89
    24-36 min      45    82%   0.827    -1.1%   0.90    -$7.40
    36-63 min     123    82%   0.825    -1.8%   0.90   -$19.89

Only the final 15 minutes makes money. Every other slice of the hour is
negative, consistently, at -1% to -3%.

WHY, most likely: this sells premium, and premium decays fastest into expiry.
Inside the last 15 minutes an OTM band that has not been reached is running out
of time to be reached, and the position converges to $1.00. Earlier in the hour
the same band still has real probability of being touched, and is priced for
it — you collect less and carry the risk longer.

DO NOT re-open this window without re-running the sweep. It has now been tested
twice and reversed once, purely because two other gates were wrong at the time.

This is also the gate that explains the live exit mix: entering inside the last
15 minutes means most positions run into time_forced_no rather than any
thesis exit. Both trades on 2026-08-26 exited that way, at 0.9518 and 0.98
against a mean of 0.8857 for the edge_gone exits they replaced.

## DELAYED_ENTRY_DIP

When a NO signal first fires, record its cost and DON'T buy. Buy only once the
same signal re-fires at DELAYED_ENTRY_DIP below that first-sighting cost.
Entry price is the binding constraint on this strategy's break-even WR: a NO
bought at X has max gain (1-X)/X, so 0.74 caps you at +35% and needs 74% to
break even, while 0.60 pays +67% and needs 60%.
0.0 = OFF = current behaviour, buy on first sighting. THE DEFAULT.
2026-08-20 settlement-resolved study, 39 NO episodes / 28 expiry clusters,
recorded marks + uncensored universe spot. The dip is real and common —
72% of positions dip >=5% below the signal price, 56% dip 10%, 46% dip 15% —
but on this sample it did NOT pay, because break-even and win rate fall
together:
policy       avg cost   BE needed   P(win)    edge
at signal      0.74        74%        67%     -7.0%
wait -5%       0.67        66%        61%     -5.8%
wait -10%      0.64        64%        55%     -9.6%
wait -15%      0.60        60%        56%     -4.1%
wait -20%      0.56        56%        47%     -9.7%
ROC -6.9% to -18.6% vs -9.5% holding the undelayed entry; -10% being worse
than both neighbours is noise at n=18-22, not a sweet spot. Second cost:
waiting FILTERS OUT WINNERS. At -15% you skip 21 of 39 trades and 16 of the
skipped ones settled in our favour — positions that never dip are the good
ones.
WHY IT IS SHIPPED ANYWAY, and what is genuinely untested: that study bought
every dip unconditionally. This implementation cannot — a ticker only fills
if find_boundary_no/find_no_scalp RE-FIRES at the dipped price, with fresh
spot, fresh true_prob, fresh z-score and fresh net edge. Dips caused by spot
walking into the band stop re-firing and expire unfilled; dips that are quote
noise still qualify. The recording cannot answer whether that separation
works, because it has no counterfactual for "would the signal still fire
here". That is the open question this flag exists to measure. Treat any
result as unvalidated until it holds on data recorded AFTER it was switched
on. See also MIN_RANGE_BOUNDARY_BUFFER below: 72% of these entries carried
less than $40 of spot cushion against a $100-wide band, and the $0-20 cushion
bucket alone ran -35.7% ROC. That may be the bigger lever.
2026-08-20: switched ON at 0.10 in PAPER mode, by explicit request, to record
the data the recording could not provide. It is a measurement run, not a
validated parameter. test_delayed_entry.py refuses to let it be > 0 while
PAPER_TRADING is False.
2026-08-20 (second pass): the trigger is a BAND, not a floor.
`DELAYED_ENTRY_DIP` is the minimum dip; `DELAYED_ENTRY_DIP_MAX` caps it. A dip
that goes straight past the cap is ABANDONED, not bought.
Why: a floor buys the cheaper contract by accepting a worse one, which is why
the edge was flat in entry price. Settlement-resolved, 39 NO episodes:
policy              P(win)  avg cost   BE     edge
enter at signal       67%     0.74    74%    -7.0%
dip >= 5%, no cap     61%     0.67    66%    -5.8%
dip >= 10%, no cap    55%     0.64    64%    -9.6%
dip in [5%, 10%]      67%     0.71    71%    -4.4%
dip in [5%, 15%]      65%     0.69    69%    -4.2%
The band is the only shape that holds win rate at the 67% base while paying
less. Dips that blow THROUGH the band settle at 50% ([5,10]) / 40% ([5,15]).
Shallow dips are price moving without information; deep dips are information.
Tune (first 19 episodes) / validate (last 20): every capped band beat the
undelayed baseline on BOTH windows; every uncapped floor lost the tuning
window. NOT VALIDATED — n is 9-11 per window, the baseline itself swings
-21.5% to +1.9% between them, 5 of 11 policies clear the bar, and every
variant is still negative (best -3.2%). Least-bad, not proven.
Cap widened 0.10 -> 0.12 on 2026-08-20. Reason is the offline grid, which
preferred [5%,12%] over [5%,10%] on BOTH windows before any live data existed
(TUNE -19.4% vs -20.0%, VALID +11.6% vs +6.9%, ALL -4.2% vs -6.2%); 0.10 was
picked to match a spoken number, not because it scored best.
Live evidence is consistent but is NOT the reason and must not become it:
on 2026-08-20 both delayed fills overshot 10% (-12.0% and -10.3%), so
[5%,10%] would have taken zero trades and abandoned the day's only winner by
$0.002. That is n=2 — treat it as a sanity check on band WIDTH, never as
threshold selection.
Why live dips overshoot a narrow band: the scan runs every ~2s and no_cost
moves in whole cents, so a 5-point band on a 68c contract is ~3.4c and one
tick of movement can clear it entirely.
2026-08-21: TURNED OFF. Not because the band was disproven — it was never
measured — but because it starves the funnel. One live day produced
11 queued -> 1 filled -> 9 expired, a ~9% conversion rate. (Four of those
nine were the all_matches bug, since fixed; the rest genuinely never dipped
into the band.) At ~1 fill/day the paper run cannot accumulate the 30+ fills
across 25+ expiries needed to tell a -2.9% edge from a -7.0% one, so the
measurement this flag exists to perform is unreachable while it is on.
Re-enable once the undelayed book has enough volume to be a baseline worth
comparing against.

## DELAYED_ENTRY_DIP_MAX

NOTE: 0.0 no longer disables ARMING — WATCHLIST_ENTRY_DIP arms on its own now
(pending.PendingEntries._arming_on). Until 2026-08-24 this flag gated both,
so shipping 0.0 alongside WATCHLIST_ENTRY_DIP = 0.05 left the watchlist
permanently inert. Setting BOTH to 0.0 is the real off switch.

## WATCHLIST_ENTRY_DIP

**0.0 — OFF as of 2026-08-26.**

The idea was that a cheaper entry lowers break-even, so waiting for a dip
should widen a margin that has none. It does lower break-even. It lowers the
win rate faster.

Swept over 41 settlement-resolved armings, 33 expiries, fees charged,
expiry-clustered split:

     dip |  n   WR    cost      ROC    PF |    TUNE    VALID
    0.0% | 41  80%  $0.814    -3.4%  0.87 |   -8.4%    +0.0%
    2.5% | 25  68%  $0.725   -10.6%  0.72 |  -21.3%    -2.2%
    5.0% | 21  62%  $0.690   -15.4%  0.65 |  -36.0%    -2.7%
    7.5% | 18  56%  $0.668   -21.2%  0.55 |  -43.7%    -6.9%
   10.0% | 18  56%  $0.660   -20.1%  0.58 |  -40.7%    -7.0%
   12.5% | 15  47%  $0.613   -26.0%  0.51 |  -40.9%   -13.0%
   15.0% | 13  38%  $0.586   -35.6%  0.41 |  -54.2%   -19.8%
   20.0% | 11  36%  $0.545   -33.9%  0.45 |  -72.9%    -1.3%
   25.0% |  9  33%  $0.513   -35.5%  0.45 | -103.6%   +19.0%
   30.0% |  6  33%  $0.495   -38.3%  0.47 | -103.8%   +27.2%

Monotonic. No level is positive on both halves. The apparent winners at
25-30% are n=3-5 against a TUNE of -103%.

WHY IT FAILS, which is the part worth keeping: the discount is not free
information. It arrives on the contracts that were going to lose anyway. Cost
falls 11 points and the win rate falls 18, straight through the 69% break-even
a $0.69 entry needs.

This is the THIRD independent confirmation of the same effect. Scale-in was
rejected for it (P(win) decaying monotonically with dip depth, 67% -> 36% at
-30%). Dip-adding was rejected for it. Now the watchlist. Treat "buy it
cheaper" as a known-dead direction on this instrument unless something
genuinely new is measured.

An earlier run measured +12.0% at n=14 over 6 days and is superseded. It armed
on a different pricer and covered a shorter window; 7 days and n=21 give
-15.4%.

Note it also spent its entire life inert before this — arming was gated behind
DELAYED_ENTRY_DIP, so nothing was ever queued. See `pending.py::_arming_on`.

## LAG_FILTER_SECS

0 or None = OFF.
Kalshi's contract prices LAG the underlying. Measured 2026-08-2x over 1.2M
observations, correlating a PAST Coinbase move against Kalshi's SUBSEQUENT
repricing of the same contract:
lag     corr     Kalshi repricing per $100 of spot move
2s    +0.026            +0.06c
10s    +0.133            +0.63c
20s    +0.180            +0.93c   <- peak
60s    +0.083            +0.43c
120s    +0.049            +0.25c
A clean inverted-U peaking at 20s: Kalshi is still absorbing the move 20
seconds later and has finished by ~2 minutes. Confirmed independently by
backing Kalshi's implied spot out of the band ladder — it moves only 30-40%
of a Coinbase move over the following 10-60s. There is NO persistent basis:
the band Kalshi prices highest is the band containing Coinbase spot 75% of
the time and the median offset is exactly $0, so this is timing, not a
different index.
WHY A FILTER AND NOT A TRADE. Trading the lag directly needs the repricing to
beat the ~2c spread, i.e. a ~$200 spot move inside 20s. That happens 0.19% of
the time. As a FILTER it costs nothing — nothing is crossed.
WHAT IT FIXES. 256 of 257 signalled contracts (100%) drew down after entry,
median MAE -13.6%. Buying right after an unpriced adverse move means paying a
quote that is about to fall. That is what the drawdown is.
Measured, rejecting an arm when spot moved more than X toward the band over
the prior ~20s (arming only, the larger sample):
reject if moved >   n    WR      ROC   P(>0)
(no filter)        38   79%    -1.0%    45%
$100               37   78%    -1.5%    44%
$50                32   81%    +1.8%    62%
$25                27   85%    +7.4%    81%   <- shipped
$10                16   88%    +6.9%    77%
$0                 11   91%    +8.3%    73%
Win rate climbs MONOTONICALLY as the filter tightens, 79 -> 91%. A
dose-response, not one lucky cell, and it retains 71% of the sample. With the
watchlist on, $25 gives +18.0% at P(>0) 87% (n=11).
NOT VALIDATED. n=27 armed / 11 with the watchlist, and $25 came from a
five-value grid. What makes it worth shipping over the other candidates is
that it has a measured MECHANISM rather than a grid search behind it.

## ZSCORE_WINDOW_SECS

Lookback for `feed.zscore()`, the |z| that gates every BOUNDARY_NO entry.

**Was hardcoded as `feed.zscore(300)` at regime.py:30.** Moved to config
2026-08-30 purely so it can be swept — the default is the same 300s, so
behaviour is byte-identical. A sweep against a literal silently does nothing,
which is the frozen-import failure this repo has already paid for: seven values
of `NO_OVERPRICING_MIN` once produced byte-identical trades because signals.py
had bound a name-local snapshot at import time.

It is a DIFFERENT window from `MOMENTUM_WINDOW_SECS` (600s), so `mom` and `z`
on the dashboard measure 10 and 5 minutes respectively and routinely disagree
without contradicting each other.

**What the number means, and why it is not what it looks like.** This is the
z-score of the price LEVEL — how far the last tick sits from the window mean,
in sample stdevs of that same window. Not a z-score of returns.

Measured over 138,150 live ticks:

    p50  1.12      p75  1.67      p90  2.19      p95  2.53      p99  3.30

    |z| >= 1.40 (the entry bar):     36% of ticks
    if z were normally distributed:  16%

The typical tick already sits 1.1 sigma from its own mean, and the entry bar
admits a THIRD of all ticks. That is structural rather than a miscalibration:
this is the endpoint of a random walk measured against that walk's own running
mean, and the mean LAGS — dragged by where price has been, while the last tick
is wherever price got to. The distribution is far fatter than normal theory.

So `BOUNDARY_NO_ZSCORE_MIN = 1.40` is a coarse filter, not a rare-event
detector, and raising it measured as inert for exactly this reason: the density
right there is enormous. The real rationing comes from the 15-minute window
(-84% of rows) and the yes_ask band (-97% of what survives).

## MOMENTUM_WINDOW_SCALED

2026-08-11 sweep (momentum_window_sweep.py, 40d tune / 19d held out), real-time
windows vs the 60s-scaled baseline:
TUNING                     VALIDATION
baseline 60s   +24.9%  Sharpe 3.71  WR 33.1%   +21.6%  Sharpe  9.99  WR 51.4%
600s           +72.7%  Sharpe 5.61  WR 44.4%   +22.9%  Sharpe 15.30  WR 56.8%
900s           +14.0%  Sharpe 2.18            +25.8%  Sharpe 15.37
1800s          +59.3%  Sharpe 4.70            +17.1%  Sharpe  7.85
3600s          +16.5%  Sharpe 2.29            +12.4%  Sharpe  6.18
900s edges 600s on validation but is the WORST on tuning — erratic, so noise.
600s is the only window strong on both, and beats the baseline on both for
Sharpe, return and win rate. Note it does NOT trade more: validation goes
70 -> 37 trades. The gain is selection (WR 51.4% -> 56.8%, PF 1.91 -> 2.75),
not volume.
Whether the BACKTEST stretches that window by TIME_SCALE (150x at 5-min bars
/ 2s polling). True reproduces the historical behaviour, in which the same
constant meant 60s live but 2.5 HOURS in the backtest — which is why the two
reported near-opposite regime mixes (backtest 43% TRENDING / 18% BREAKOUT vs
live 0.37% / 0.00%) and why nothing regime-dependent has ever been validly
backtested. False makes both sides measure the same real-time window.

## BREAKOUT_MOM_MULT

Was hardcoded 2 in regime.py, which coupled it to
TRENDING's threshold — you could not calibrate one
without moving the other. Split out so BREAKOUT can
be tuned while TRENDING (now firing 20.8% of ticks
after the 2026-08-11 window fixes) stays put.
2026-08-13, measured on 38,257 paired live
observations at MOMENTUM_WINDOW_SECS=600, the JOINT
probability of both BREAKOUT gates clearing:
accel 0.0040 / mom 0.0030 -> 0.144%  (current)
accel 0.0025 / mom 0.0020 -> 1.012%
accel 0.0017 / mom 0.0015 -> 3.550%
accel 0.0013 / mom 0.0015 -> 5.617%
accel 0.0013 / mom 0.0010 -> 7.227%
|accel| p99 is 0.00267, so the 0.004 gate sits near
the 99.8th percentile — BREAKOUT has never once
fired in 267,868 recorded live ticks.

## KELLY_FRACTION

Kelly position sizing

## VOL_RATIO_COMPRESSION

Vol compression (Kalshi pricing-lag) signal
When fast EWMA << slow EWMA, Kalshi's lagged model overestimates vol →
RANGE contracts are mispriced cheap → buy YES, target 80¢+ or full settlement

## MIN_EDGE_COMPRESSION

lower entry bar when compressed (structural edge is larger)

## TRADE_ONLY_COMPRESSION

gate the YES entries (find_best and find_snipe) on the
compression regime, rather than merely lowering the edge
bar inside it. Deliberately does NOT gate find_no_scalp or
find_boundary_no: those sell overpriced OTM premium at
z-score range extremes, a different edge thesis from the
EWMA-vs-SMA vol lag this gate is derived from, and the
validation below was YES-only (the backtest omits NO
signals unless --no-threshold is passed). Gating them
would extend an unvalidated constraint to a strategy it
was never tested against. See [NO strategy status] —
BOUNDARY_NO is still unproven on its own tiny sample.
2026-08-08: once RANGE_WIDTH was corrected
to the real 100-wide band (see kalshi_btc_backtest.py),
segmenting the 59d/$500 run showed the entire loss comes
from trading OUTSIDE compression:
compression  n=132  WR 43.2%  +$88.44  (+18.4%/$ risked)
normal vol   n=338  WR 17.5%  -$443.86 (-47.3%/$ risked)
Normal-vol trades also carry the LARGEST model edge
(median 0.0645 vs 0.0163) while losing — outside
compression the EWMA/SMA gap is noise, not mispricing, so
the model manufactures fake edge exactly where it is least
reliable. That is the mechanism behind the
"edge is anti-predictive" result in calibration_check.py.
Gating on compression flips the sign on BOTH windows of a
40d-tune/19d-validate split, which no other change this
session managed:
tuning      -53.6% Sharpe -6.91  ->  +41.2% Sharpe 4.03
validation  -32.1% Sharpe -9.92  ->  +23.8% Sharpe 9.49
with max drawdown cut ~4-5x (-66%/-32% -> -14%/-6.9%).
Set False to restore the old all-regimes behaviour.

## RECORD_BOOK_INTERVAL

Intervals
Market-data recording (KALSHI_RECORD=1). Book capture runs on its own thread
and its own cadence so it never sits on the trading path, and so its API rate
can be tuned independently of SCAN_INTERVAL. Each cycle costs one orderbook
request per visible ladder contract plus one per open position — typically
2-8 calls.

## BARS_PER_HOUR

Ticks per hour, derived from the actual polling interval. model.py and
regime.py annualize per-tick vol with sqrt(BARS_PER_HOUR); this was
hardcoded 900 (4s ticks) after PRICE_FETCH dropped to 2s, silently
understating hourly vol by sqrt(2) (~29%) and inflating every RANGE edge.

## BOUNDARY_NO_YES_ASK_MAX

**0.30 as of 2026-08-27.** Was 0.65, which never bound.

This is a tail-selling strategy and it works in proportion to how far out the
tail actually is. A cheap NO means yes_bid is HIGH, which means the band sits
near spot and genuinely gets hit; an expensive NO is a far-OTM band that mostly
does not. Since no_cost = 1 - yes_bid, the ask ceiling is what decides how
close to a coin flip the bot is willing to go.

Found by asking the opposite question — whether entries ABOVE $0.85 were the
losers. They are the best group. Two independent datasets agree:

  REAL trade log, 58 NO round trips across three config eras:
    bucket        n    WR   b/e   margin     total    on risk     ret
    $0.50-0.70    8   50%   65%   -14.7%    -$9.14    $75.84   -12.1%
    $0.70-0.78   12   42%   75%   -33.4%   -$15.52   $114.02   -13.6%
    $0.78-0.82   12   67%   79%   -12.4%    -$1.16   $113.88    -1.0%
    $0.82-0.85   14   57%   83%   -25.4%    -$3.65   $133.77    -2.7%
    $0.85-1.01   12   92%   87%    +4.8%    -$0.02   $113.73    -0.0%

  REPLAY, 100 armings under the current gates, settlement-resolved:
    $0.00-0.70    7   43%   66%   -22.7%   -$18.74   ROC -36.0%   PF 0.37
    $0.70-0.75   17   76%   72%    +4.4%    +$5.42   ROC  +4.7%   PF 1.16
    $0.75-0.80   25   84%   77%    +6.7%   +$14.96   ROC  +7.1%   PF 1.43
    $0.80-0.85   31   84%   82%    +1.7%    +$2.30   ROC  +0.7%   PF 1.05
    $0.85-0.90   19   95%   86%    +8.3%   +$15.49   ROC  +8.6%   PF 2.60

Break-even rises with entry cost, so expensive entries SHOULD be harder. They
are not, because the win rate rises faster than the bar does. That is the whole
finding.

Sweep of the ceiling, 100 armings / 70 expiries, expiry-clustered:

    ask_max    n   /day    WR   cost     ROC     PF           95% CI   P>0    TUNE   VALID
       0.65  100   12.5   82%  0.790   +2.0%   1.13   [-7.1%,+11.3%]  68%   +2.9%   +1.3%
       0.40  100   12.5   82%  0.790   +2.0%   1.13   (identical)
       0.35   97   12.1   82%  0.795   +2.0%   1.13   [-7.2%,+10.6%]  66%   +1.7%   +2.1%
       0.30   88   11.0   85%  0.805   +4.5%   1.31   [-4.9%,+13.1%]  84%   +1.3%   +6.9%
       0.25   67    8.4   87%  0.824   +3.6%   1.28   [-5.9%,+12.6%]  78%   -0.8%   +6.8%
       0.20   36    4.5   92%  0.852   +6.5%   1.78   [-5.5%,+16.1%]  87%   +1.2%   +9.9%
       0.15    8    1.0  100%  0.877  +13.1%   9.99  [+12.2%,+13.8%] 100%     nan  +13.1%

0.40 / 0.50 / 0.65 are byte-identical at n=100: nothing clearing the other
gates ever had an ask above 0.40, so the old ceiling never bound. Third inert
gate found in this codebase after the 1.15 overpricing bar and the 1.40
z-score, and the same signature every time — identical results across a range.

0.30 doubles ROC, lifts PF 1.13 -> 1.31, and is positive on BOTH halves of the
split at a cost of 12 of 100 armings. 0.25 fails the split (TUNE -0.8%). 0.20
is better still but cuts volume to 4.5/day. IGNORE 0.15 — 100% win rate on n=8
with a CI excluding zero is eight coin flips landing the same way, not an edge.

RELATED, and the reason to believe the direction: waiting for a dip moves you
DOWN this cost axis, from ~$0.81 toward ~$0.69 — exactly the direction this
table says is worse. The dip-buying failure and this finding are the same
gradient observed twice. See #watchlist_entry_dip.

n=88 and the CI includes zero. 0.30 means "0.65 never bound and the cheap end
loses", not that 0.30 is optimal.
