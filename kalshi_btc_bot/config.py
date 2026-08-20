# ─────────────────────────────────────────────
# MODE — switch here before running
# ─────────────────────────────────────────────
PAPER_TRADING = True    # True = paper mode (no real orders), False = live trading
PAPER_CAPITAL = 500.00     # simulated capital for paper mode — matches the
                            # backtest's capacity-curve reference point where
                            # the strategy is shown to work (docs/BACKTEST_INTEGRITY.md
                            # §7); $10K sizes positions past what real Kalshi
                            # depth can absorb without severe exit slippage.

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

# Risk controls
# 2026-07-06: tightened across the board after a live session lost ~7% in under
# 40 minutes via repeated max-Kelly-sized re-entries on the same boundary
# (see boundary_risk cooldown fix same week). Smaller per-trade size + smaller
# exposure cap + faster stop + faster session breaker compound together so no
# single bad regime can reproduce that drawdown rate.
MAX_EXPOSURE_PCT    = 0.18       # max 18% of real portfolio value in positions (was 0.40)
MIN_CASH_PCT        = 0.05       # keep 5% as cash reserve
MAX_TRADE_PCT       = 0.025      # max 2.5% of real portfolio per trade (was 0.05)
NO_TRADE_PCT        = 0.02       # max 2% of real portfolio per MISPRICE_NO trade (was 0.04)
ENABLE_MISPRICE_NO = False      # disabled — BOUNDARY_NO is the active NO strategy
MAX_POSITIONS       = 4
STRIKE_CLUSTER_DIST = 150        # skip a new entry if its strike is within this many
                                  # dollars of an existing open position's strike in the
                                  # same expiry window — MAX_POSITIONS caps capital
                                  # concentration but not directional correlation. 2026-07-07:
                                  # observed live — 4 RANGE positions opened within 2 min on
                                  # adjacent strikes (62550-62850), then one BTC breakout
                                  # busted all 4 simultaneously and filled every slot with
                                  # dead positions, locking out a genuinely better ATM entry.
                                  # 2026-08-17: note 150 > RANGE_WIDTH (100), so this already
                                  # blocks the ADJACENT same-expiry strike. That makes a
                                  # separate "one snipe per expiry" filter redundant — it was
                                  # measured at 1 blocked decision in 58 days, worth -$1.50,
                                  # and rejected. See snipe_concentration_counterfactual.py.
                                  # If per-expiry concentration ever needs tightening, widen
                                  # THIS number rather than adding a parallel filter.
SESSION_STOP_PCT    = 0.03       # stop NEW entries if down 3% (was 0.05)
MIN_CASH_FLOOR      = 0.25       # never trade with less than $0.25
UNTRACKED_EXPOSURE_LIMIT = 0.25  # block new trades if live exposure exceeds tracked exposure by this much
EXIT_RETRY_COOLDOWN = 10         # seconds to wait before retrying an unfilled live exit
STOP_COOLDOWN_SECS  = 300        # block re-entry on same ticker for 5 min after stop loss
EXIT_COOLDOWN_SECS  = 300        # block re-entry on same ticker after a *profitable* exit.
                                  # 2026-07-28: added at 120s after snipe_lock exited B63550 at
                                  # $0.27 and the bot re-bought the same ticker 47s later at
                                  # $0.34 (+26%), losing $64.02 — a real, single-incident cost.
                                  # 2026-08-04: cooldown_sweep.py swept this against a genuine
                                  # tuning/validation split (40d tune, 19d held out, never seen
                                  # during selection) at the $500 paper scale. 0s won clearly on
                                  # BOTH windows independently (Sharpe 6.82 vs 6.20 tuning, 7.44
                                  # vs 6.05 validation — 15s through 300s were all identical to
                                  # each other, meaning any cooldown above 0 was blocking the
                                  # exact same handful of re-entries with no added benefit).
                                  # The Jul 28 incident is real and still happens sometimes at
                                  # 0s — the aggregate evidence across ~1,200 trades is that
                                  # pressing a signal that's still working outweighs the
                                  # occasional worse-price re-chase. STOP_COOLDOWN_SECS (loss
                                  # cooldown) is untouched — this only removed the brake on
                                  # re-entries the exit itself already proved were profitable.
                                  #
                                  # 2026-08-11: REVERTED to 300. Two independent reasons.
                                  #
                                  # (1) That sweep could not test what it appeared to. Backtest
                                  # bars are BAR_MINUTES=5 (300s) and fills queue to the NEXT
                                  # bar's open, so "0s" means a ~5-MINUTE effective gap, not
                                  # zero; 15s-300s all push the signal one further bar (~10 min),
                                  # which is exactly why they were byte-identical. The sweep's
                                  # real finding is "~5 min beats ~10 min". Live polls every
                                  # PRICE_FETCH=2s, so live 0s permits re-entry 150x faster than
                                  # the fastest case ever simulated. That regime was untested.
                                  # (Re-ran on the corrected RANGE_WIDTH=100 instrument: 0s still
                                  # "wins" both windows, and still means ~5 min.)
                                  #
                                  # (2) Real fills say the fast re-chase is the toxic one. FIFO
                                  # round trips over all paper history, split by how the PRIOR
                                  # trade on that same contract ended:
                                  #     after a WINNING exit  n=27  WR 44.4%  -24.1% per $ risked
                                  #                                 median gap 132s
                                  #     after a LOSING  exit  n=17  WR 41.2%  +11.8% per $ risked
                                  #                                 median gap 419s
                                  # Re-entry after a win LOSES; after a loss it gains — and the
                                  # only structural difference is that losses already carry
                                  # STOP_COOLDOWN_SECS=300 while wins carried none. A winning
                                  # exit fires precisely BECAUSE the move is over (scalp_lock,
                                  # peak_giveback), so re-entering two minutes later buys the
                                  # fade. Observed again live 2026-08-10 B63850: +$1.60 win,
                                  # re-entered 51s later, -$6.72.
                                  #
                                  # 300 chosen to match the ~5-minute gap the backtest actually
                                  # endorses. It is the floor of what has been tested, not a
                                  # tuned optimum — the backtest cannot resolve anything shorter,
                                  # so revisit as more live re-entry data accrues.
FORCE_EXIT_SLIPPAGE_CENTS = 2    # cross stale bids by this many cents on urgent exits

# Entry filters (YES signals)
MIN_EDGE            = 0.015
MIN_VOLUME          = 50
MAX_ASK             = 0.45
MAX_SPREAD          = 0.05       # max 5c bid/ask spread
MAX_SPREAD_PCT      = 0.25       # max spread as 25% of ask
# 2026-07-06: was a flat ENTRY_PRICE_IMPROVE_CENTS=4 cross on every entry regardless
# of price, sourced from the ladder's up-to-LADDER_CACHE_SECONDS-old snapshot. On a
# cheap contract (e.g. ask=$0.13) that flat 4c cross alone produced an instant ~-35%
# mark-to-bid loss on fill, tripping STOP_LOSS_PCT with zero real BTC movement.
# Replaced with a fresh single-ticker quote fetch immediately before order
# submission (Portfolio._fresh_quote) and a limit set to that live best ask
# directly — no artificial cross needed since the quote is no longer stale.
MIN_HOURS           = 0.10       # 6 min — keeps entries clear of the TIME_EXIT_MINS kill zone
MAX_HOURS           = 4.0
MAX_OTM_T           = 100
MAX_OTM_B           = 150
MIN_RANGE_BOUNDARY_BUFFER = 40   # skip RANGE entries within $40 of either boundary (ITM or
                                  # OTM side), all regimes. Old logic only guarded the OTM side
                                  # (dist < -20) — near-money ITM entries like dist +1..+38 with
                                  # no directional confirmation were let through and flipped OTM
                                  # by expiry on ordinary spot drift (observed: B61650 losers,
                                  # 2026-07-01/02 overnight session). Widened to 40 and applied
                                  # both-sides/all-regimes on 2026-07-06, which fixed the whipsaw
                                  # but cut entry frequency ~4x vs the Sharpe-5.66 baseline
                                  # (601 trades/wk -> 143/wk). Narrowed back to 20 same day,
                                  # but 2026-07-09 head-to-head backtest (identical code,
                                  # buffer-only diff) showed 20 nearly quintuples max drawdown
                                  # (-3.5% -> -16.0%) and drags Sharpe/profit-factor/win-rate
                                  # down vs 40 — reverted to 40, trading entry frequency for
                                  # materially better risk-adjusted return. Matches the old gate's
                                  # magnitude while keeping the
                                  # both-sides/all-regimes fix that closed the whipsaw hole.

# Exit thresholds — unified tiered ladder
# TIER 0.5: Gamma-aware convexity lock — closes the asymmetry where YES positions
# had no "sell when overpriced" check (the NO side already has one via NO_EDGE_GONE_RATIO).
# Fires when profitable + true_prob is reversing (2-tick fade) + gamma is high, i.e. we're
# in the near-strike/near-expiry zone where the model's edge can flip faster than the fixed
# P&L tiers below would catch. GAMMA_HIGH_THRESHOLD is an initial estimate, not backtested —
# tune it from the "gam=" values printed in the live position ticker once you've watched a
# session or two.
GAMMA_LOCK_MIN_PROFIT = 0.15     # don't fire on noise — require at least 15% gain first
GAMMA_HIGH_THRESHOLD  = 40000.0  # dollar-gamma magnitude considered high convexity risk.
                                  # Calibrated from live overnight gam= prints (2026-07-01):
                                  # deep-OTM/quiet positions showed |gam| ~1,000-30,000,
                                  # near-strike/high-true_prob positions ~60,000-150,000+.
                                  # 50.0 was non-selective (fired on nearly every tick).
GAMMA_LOCK_MIN_BID  = 0.35       # TIER 0.5 gate: don't lock gamma risk below this absolute
                                  # price — observed live fires at bid $0.17-$0.37 on cheap
                                  # entries cut real winners short before they reached meaningful
                                  # value (2026-07-01/02 overnight session).

# TIER 0.75: Peak giveback — `peak` was tracked per-position but never used to
# gate an exit. A trade that ran to +140% and fully round-tripped back to
# breakeven/loss had zero protection unless it happened to cross gamma_lock or
# one of the fixed pnl tiers below. This generalizes the snipe-reversal-lock
# idea (TIER 3.75) to ordinary trades: once a real gain has formed, give back
# only so much of it before locking. Independent of gamma/convexity, so it
# catches reversals gamma_lock's high-convexity gate would miss.
PEAK_GIVEBACK_MIN_PEAK = 0.25    # only protect peaks of at least 25% unrealized gain
PEAK_GIVEBACK_FRACTION = 0.75    # exit once current pnl has faded to <= 75% of that peak
                                  # (i.e. give back only 25% of the peak). Was 0.50 — a live
                                  # trade with peak +85% pnl round-tripped to +41% before this
                                  # tier fired, giving back ~52% of the peak. 60-day $5K
                                  # backtest showed 0.75 improves Sharpe 6.47 -> 7.57, return
                                  # +2621% -> +3262%, and max DD -9.2% -> -8.0% vs 0.50 — the
                                  # tighter setting also lets more winners survive to reach
                                  # momentum_locked (+32% more trades in that tier), so both
                                  # profit tiers work together better.
PEAK_GIVEBACK_MIN_BID  = 0.20    # same rationale as GAMMA_LOCK_MIN_BID — don't lock trivial cents
PEAK_GIVEBACK_MIN_BID_MULT = 1.30 # the floor above is applied as min(abs, MULT * entry).
                                  # A FIXED 20c floor demands a different amount of profit
                                  # depending on entry price — +150% for an 8c entry, 0% for
                                  # a 20c one — so the cheapest positions, which need the
                                  # giveback protection most, effectively had none.
                                  # Worse, it can be STRUCTURALLY unreachable: peak_giveback
                                  # triggers at entry*(1+peak_pnl*PEAK_GIVEBACK_FRACTION), and
                                  # when that price is below the floor the tier can never fire
                                  # at ANY price path. Live 2026-08-10 B64150: entry $0.13,
                                  # peak $0.21 (+61.5%), trigger $0.19 < $0.20 floor -> exit
                                  # window [0.20, 0.19] was empty, and it rode +61.5% to a
                                  # total loss (-$7.28). Four such cases observed live; the
                                  # corrected-instrument backtest shows 36 of 193 positions
                                  # (19%) structurally blocked this way in a 40d window.
                                  # min() form, not a pure multiple: a bare 1.3x floor would
                                  # be STRICTER than $0.20 for entries above ~15c and would
                                  # remove protection those positions already have.
                                  # 2026-08-10 counterfactual (snipe_giveback_floor_
                                  # counterfactual.py, replays real tick paths so nothing
                                  # compounds): every candidate beat the status quo on BOTH
                                  # windows — unlike earlier sweeps where the sign flipped.
                                  #   min($0.20,1.20x)  tuning +68.52  validation +14.30
                                  #   min($0.20,1.30x)  tuning +39.80  validation +17.01
                                  # 1.30 chosen: it wins the HELD-OUT window, and the two
                                  # leaders are within noise on the tuning one.
SNIPE_PEAK_GIVEBACK_MIN_BID = 0.20  # snipe-specific floor for the same tier — kept equal to
                                  # PEAK_GIVEBACK_MIN_BID for now (no-op default). 2026-08-04:
                                  # a real snipe (entry $0.13) ran to peak +42% then +46% (bid
                                  # $0.17-$0.185) and lost it all — peak_giveback never engaged
                                  # because it never crossed $0.20. Snipes enter at 10-25c
                                  # (SNIPE_MIN/MAX_ENTRY_PRICE) — a shared $0.20 floor sits
                                  # INSIDE that range, so a real percentage-sized run can still
                                  # never clear the absolute-cents gate meant to protect it.
                                  # Split out so a lower snipe-specific value can be tested
                                  # (peak_giveback_bid_sweep.py) without touching the general
                                  # entries this floor was calibrated for.
                                  # 2026-08-05: swept [0.02,0.05,0.08,0.10,0.15,0.20] with a
                                  # 40d-tune/19d-validate split. Tuning picked $0.10 (Sharpe
                                  # 6.78); validation picked $0.15 (5.90) and ranked $0.10 WORST
                                  # of the four re-checked candidates (5.24) — did not
                                  # generalize. Different winner per window = fails the same bar
                                  # that validated EXIT_COOLDOWN_SECS -> 0. Left at $0.20 (no-op)
                                  # pending more data; re-run the sweep periodically.
PEAK_GIVEBACK_HARD_LOSS_PCT = 1.50  # TIER 0.75b: once a position has cleared MIN_PEAK, exit
                                  # even if bid is below the min-bid floor above, once pnl_pct
                                  # has cratered past this threshold. 2026-08-05: exit_coverage_
                                  # analysis.py on a 59d/$500 backtest found time_exit_OTM trades
                                  # averaged peak +105.5% -> exit -94.9% (200pp giveback, $2,848
                                  # total) — fast single-bar crashes fell straight through the
                                  # bid floor, skipping the whole window peak_giveback needs to
                                  # act, and no other tier is peak-aware. 1.50 is a true no-op
                                  # (pnl_pct floors at -100% at bid=0).
                                  # 2026-08-05: swept [0.30,0.50,0.65,0.80] with the standard
                                  # 40d-tune/19d-validate split. No-op (1.50) won tuning outright
                                  # (Sharpe 6.60 vs 5.89-6.31 for every real threshold — adding
                                  # this exit made the tuning window worse, not better); 0.50 won
                                  # validation (5.68 vs 5.46) — different winner per window, fails
                                  # the bar. Left at 1.50 (no-op): the anecdotal giveback was real,
                                  # but exiting early into it cuts more recoveries than it saves,
                                  # consistent with STOP_UNCOVERED_PCT's own non-monotonic-price
                                  # rationale below.

SCALP_LOCK_MIN_BID  = 0.30       # TIER 1 gate: same rationale — pnl% alone let tiny-entry
                                  # positions lock at trivial absolute prices.
SCALP_LOCK_PCT      = 0.40       # TIER 1: up 40% + < 15 min left
MOMENTUM_LOCK_PCT   = 1.00       # TIER 2: up 100% + < 9 min
STRONG_PROFIT_PCT   = 1.50       # TIER 3: up 150% + < 15 min
PROFIT_EXIT_MEGA    = 3.00       # TIER 4: up 300%, no conditions
TIME_EXIT_MINS      = 3          # TIER 5: OTM with < 3 min left — let late-window mispricing play out
TIME_EXIT_NEAR_DIST = 15         # TIER 5 override: skip the force-exit above if still within this
                                  # many points of the strike boundary — a near-boundary OTM position
                                  # can flip ITM by the buzzer, so only force-exit while still far OTM.
                                  # 2026-07-07: added after a snipe was force-closed for a modest gain
                                  # at TIME_EXIT_MINS while sitting close to the boundary.
STOP_LOSS_PCT       = 0.35       # TIER 6: base stop. 2026-07-06: tightened from 0.60 (which had
                                  # itself been widened from 0.40 on 2026-07-01 "to allow late
                                  # recoveries") — cut losers quickly, let winners ride via the
                                  # profit-lock tiers above instead of hoping for a comeback.
STOP_UNCOVERED_PCT  = 0.65       # TIER 6 floor for positions OPENED inside STOP_MIN_HOURS.
                                  # 2026-07-03 the user judged that a tight stop near expiry is
                                  # wrong by design: binary prices don't move monotonically into
                                  # settlement, so a 35% stop there cuts winners on ordinary
                                  # wobble as often as it saves losers. That reasoning holds for
                                  # a position riding INTO expiry — but MIN_HOURS (6 min) lets
                                  # positions be OPENED inside the gate, and those never had any
                                  # floor at all. 2026-07-28: the 4 entries with stop coverage
                                  # netted +$53.53, the 2 without netted -$143.53, riding to -77%
                                  # and -92%. This is a catastrophe floor, not a stop — at -65% it
                                  # cannot cut a winner, only a position already most of the way
                                  # to a total loss. Same number the user chose for
                                  # BOUNDARY_RISK_HARD_STOP, for the same reason.
CUT_NEVER_GREEN_MINS = 0         # REJECTED BY EVIDENCE — kept at 0, do not enable.
                                  # The premise looked strong: 58 of 313 backtest
                                  # trades never traded above entry and lost
                                  # -$324.47, only 7 recovering. But "never green
                                  # YET" at 5 minutes is a completely different
                                  # population from "never green EVER", and the
                                  # difference is the whole trade.
                                  # Measured by shadow instrumentation (green_by_N,
                                  # which records status at each age without acting
                                  # on it), of positions still red at 5 minutes:
                                  #     67% went green later
                                  #     39% ended winners
                                  #     net P&L +$251.59, not negative
                                  # Their winners average +$16.20 against losers at
                                  # -$4.32 — a 3.75:1 payoff living entirely inside
                                  # the cohort this rule would have killed. That is
                                  # structural, not a fluke: a cheap OTM binary is
                                  # SUPPOSED to sit underwater while spot works
                                  # toward the strike. Being red early is the normal
                                  # state of the eventual winner.
                                  # never_green_sweep.py confirms: every value on
                                  # 0/5/10/15/20/30 loses to OFF on BOTH windows,
                                  # monotonically worse the tighter the cut
                                  # (tuning +88.9% -> +69.7% at 5 min).
REENTRY_SIZE_DECAY  = 0.0        # 0 = disabled. If > 0, any entry on a ticker
                                  # already traded this expiry is capped at
                                  # DECAY x the dollars deployed on the previous
                                  # entry in that same ticker. 1.0 = "never bigger
                                  # than last time", 0.5 = each attempt half the
                                  # last.
                                  #
                                  # WHY: Kelly sizes UP as a contract collapses.
                                  # f* = (true_prob - ask)/(1 - ask), so with the
                                  # MODEL UNCHANGED at true_prob=0.20, an ask
                                  # falling 0.21 -> 0.09 takes f* from 0 to 0.121
                                  # while each dollar also buys 2.3x more
                                  # contracts. The model does not have to be wrong
                                  # for size to explode — it only has to stay the
                                  # same while the market disagrees harder.
                                  #
                                  # Observed live, B63625 on 2026-08-13:
                                  #   16:10  19 @ $0.21 ($3.99)  -> stop  -$1.90
                                  #   16:16  33 @ $0.15 ($4.95)  -> stop  -$2.31
                                  #   16:29 138 @ $0.09 ($12.42) -> stop  -$6.90
                                  # 7x the contracts on the third attempt, and the
                                  # single worst loss of the session. STOP_COOLDOWN
                                  # did not stop it: the re-entries landed at 5:01
                                  # and 5:02 after each stop, just past the 300s
                                  # timer. The cooldown is a timer with no memory —
                                  # it never learns that this contract already beat
                                  # us. This is the memory.
                                  #
                                  # TESTED, AND NOT ENABLED. The mechanism above
                                  # is real, but neither test supports acting:
                                  #   * Backtest VALIDATION window: 28 trades, 28
                                  #     distinct tickers — ZERO re-entries. The cap
                                  #     cannot bind, so all values return
                                  #     byte-identical results. The window carries
                                  #     no information about this parameter.
                                  #   * Backtest TUNING window: only 14 re-entries
                                  #     in 213 trades. DECAY=0.75 gives return
                                  #     +88.9% -> +76.4%, maxDD -12.2% -> -11.1%,
                                  #     Sharpe 6.39 -> 6.48. Underpowered either
                                  #     way.
                                  #   * Live-book counterfactual (same fills, fewer
                                  #     contracts) is the decisive one and it says
                                  #     NO: return on deployed capital goes -12.3%
                                  #     (off) -> -17.4% (0.75/0.5). The re-entries
                                  #     were better per dollar than the first
                                  #     entries, matching the by-attempt split
                                  #     where attempt 3+ averages +$21.11 (n=17).
                                  # The backtest structurally UNDER-REPRESENTS
                                  # re-entry — 5-min bars leave far less room to
                                  # re-enter inside a contract's life than 2s live
                                  # ticks, which is why live shows 53 re-entries
                                  # against the backtest's 14. Treat any future
                                  # re-entry rule as untestable on bars.
                                  #
                                  # 2026-08-19, SCALE-IN (adding to an OPEN
                                  # winner, not re-entering a closed one) was
                                  # tested against this rule and REJECTED.
                                  # Bars said the opportunity was large and
                                  # safe — 614 add-chances, 88.6% of them on
                                  # positions that were UP (median +27.6%).
                                  # The recorded live book said the opposite:
                                  # 0 of 30 policies beat the baseline entries,
                                  # every one at a 0% win rate, and raising the
                                  # "only add when up X%" bar made it strictly
                                  # worse (-26% at +10% -> -51% at +20%) — the
                                  # signature of buying near the peak. Exactly
                                  # the bars-vs-ticks gap this note warns about,
                                  # in the direction that flatters the feature.
                                  # See scale_in_policy_sweep.py for the grid
                                  # and scale_in_opportunity.py for the bar
                                  # measurement that would have misled us.
                                  # NOTE also: adding is impossible today by
                                  # construction — buy() returns False when
                                  # `ticker in self.positions` (portfolio.py:490,
                                  # kalshi_btc_backtest.py:542) — so shipping it
                                  # would have been a real change, not a tweak.
STOP_MIN_HOURS      = 0.30       # TIER 6 gate: stop only fires if > 18 min left.
                                  # Below this, TIME_EXIT_MINS handles OTM exits and
                                  # expiry_settle captures ITM wins — don't stop binary
                                  # options in their last bars when the binary payoff
                                  # hasn't resolved yet.
SNIPE_STOP_PCT      = 0.50       # TIER 6-snipe catastrophe floor. Snipes skip TIER 5.25/6
                                  # above entirely (gated `not is_snipe`) — a fixed % stop
                                  # defeats their whole 1000%+-payout thesis. But that leaves
                                  # a snipe that never builds a peak with NO floor at all.
                                  # 2026-08-05: exit_coverage_analysis.py on a 59d/$500
                                  # backtest found 179 of 196 losing snipe exits averaged
                                  # -94.7% pnl_pct (vs -45.7% for non-snipe stopped/
                                  # boundary_risk losses) — 99% of all snipe-loss dollars,
                                  # spread across entry times (not just the near-expiry
                                  # window).
                                  # Swept [0.50,0.65,0.80,0.95] vs the no-op (1.50) with the
                                  # standard 40d-tune/19d-validate split: no-op won tuning
                                  # outright (Sharpe 6.60 vs 5.84-6.14) but 0.50 won validation
                                  # (5.66 vs 5.46) — different winner per window, so initially
                                  # left at 1.50 pending more evidence. 2026-08-06: real
                                  # paper+live history (FIFO-matched across 61 closed snipe
                                  # lots, 5 weeks) settled the disagreement — snipes are net
                                  # -$653.41 despite a 57.4% win rate, because expired_settled +
                                  # time_exit_OTM (both unprotected) total -$1,712.53, more than
                                  # snipe_lock's +$938.08 in wins recovers. That's real fills,
                                  # not a model; turned the floor on at 0.50, the validated
                                  # out-of-sample winner.

# TIER 5.25: Boundary risk — ITM but marginal + underwater + near expiry.
# TIME_EXIT_MINS (TIER 5) only protects positions once already OTM; a marginal ITM
# position carries the same flip risk right up until it crosses. Momentum-gated
# (2-tick true_prob fade, same signal as gamma_lock) so ordinary chop doesn't
# trigger it — gives the position room to be volatile — but exits once the move
# is actually working against it. Hard floor below fires unconditionally as a
# backstop even without momentum confirmation.
BOUNDARY_RISK_DIST      = 15     # points from boundary considered "at risk" while ITM
BOUNDARY_RISK_MINS      = 10     # window before expiry this tier is active — wider
                                  # than TIME_EXIT_MINS so it can act before the flip
BOUNDARY_RISK_MIN_LOSS  = -0.10  # ignore trivial pnl noise, require real drawdown first
BOUNDARY_RISK_HARD_STOP = -0.65  # unconditional cap — fires even without momentum confirm

# ── SNIPE MODE — deep-OTM cheap lottery tickets aimed at asymmetric 1000%+ payouts ──
# find_best()'s ranking picks the largest raw probability-point edge, which structurally
# favors near-money contracts (both true_prob and ask are larger there). A 3¢ contract
# with true_prob=8% has only 5pts of raw edge and never wins that ranking even though its
# ROI (true_prob/ask) is 167%. find_snipe() is a separate ROI-ranked scan so these aren't
# starved out by the main signal.
SNIPE_MIN_ENTRY_PRICE = 0.10     # 2026-07-07: floor added — trade log showed 1-9c snipes were
                                  # a coin flip (2 of 3 resolved outcomes settled for a total
                                  # loss of stake), and none had ever reached the 75c
                                  # near_settlement tier. Raising the floor screens out the
                                  # deepest-OTM tickets where the ask is cheap because Kalshi's
                                  # own model already prices them near-zero, not because of lag.
SNIPE_MAX_ENTRY_PRICE = 0.25     # widened from 0.10 now that 0.10 is the floor
SNIPE_MIN_EDGE_RATIO  = 0.30     # true_prob must beat ask by >= 30% (true_prob/ask - 1)
SNIPE_TRADE_PCT       = 0.01     # sized down vs MAX_TRADE_PCT — tail-probability estimates
                                  # are noisier, so size the bet down rather than Kelly-size
                                  # off an uncertain edge. 2026-07-16: cut 0.02 → 0.01 after
                                  # a single 516-contract paper snipe lost $117 (~1.2% of
                                  # $10K account) — one bad snipe was wiping out weeks of
                                  # small wins. 1% caps single-snipe max loss to ~$100
                                  # while backtest still shows the tier remains net-positive.
# TIER 3.75 — snipe reversal lock. Fires when a snipe that has already run gives
# back into a 2-tick true_prob fade (same signal as gamma_lock), NOT a fixed price
# cap: a snipe still climbing without a reversal is untouched.
#
# PEAK raised 0.50 -> 1.50 on 2026-08-04, resolving the design question the
# previous comment here deliberately left open. Evidence: a 2026-08-04 paper
# session saw this tier fire twice at peak 50.0%/54.6%, exiting at +50%/+37%
# pnl — essentially at first wobble past the old 50% gate. Snipes enter at
# 10-13c targeting settlement near $1.00 (a 700-900% gain); locking a third
# to half of that is capturing a sliver of the position's designed upside, and
# reacting to any 2-tick fade the moment peak crosses 50% behaves like
# gamma_lock (fast, convexity-driven) rather than a patient lock that only
# protects against a genuine reversal after a genuine run. 1.50 restores the
# ORIGINAL documented intent (the dead SNIPE_PROFIT_LOCK_PCT this tier's
# thresholds were hardcoded around before being wired to config on Jul 28) —
# a snipe must have actually run before this tier can even become eligible.
SNIPE_PROFIT_LOCK_PEAK    = 1.50 # peak_pnl must have reached this
SNIPE_PROFIT_LOCK_MIN_PNL = 0.15 # and current pnl must still be at least this
SNIPE_PROFIT_LOCK_MIN_BID = 0.12 # absolute price floor — same rationale as GAMMA_LOCK_MIN_BID

# MISPRICE_NO entry filters
# Threshold sweep (Jul 22): synthetic backtest can't discriminate thresholds — SMA/EWMA spread
# always exceeds 1.40, so all values fire identical trades. Starting at 1.18 for paper to let
# real Kalshi pricing tell us where the edge actually lives.
NO_OVERPRICING_MIN  = 1.18       # YES_ask / true_prob must exceed this (was 1.40)
NO_YES_ASK_MIN      = 0.30
NO_YES_ASK_MAX      = 0.72
NO_TRUE_PROB_MAX    = 0.55
NO_HOURS_MIN        = 0.08
NO_HOURS_MAX        = 0.35
NO_DIST_MIN         = -300
NO_DIST_MAX         = 100
NO_CASH_MIN_PCT     = 0.20       # available cash > start_total * 0.20

# MISPRICE_NO exit thresholds
# Backtest (Jul 22): no_stop at -30% was the dominant drag (-$90k on 188 stops vs +$60k wins).
# Tightened to -20% to cut reversals sooner — BTC spiking into the range rarely recovers.
NO_PROFIT_CAPTURE   = 0.80       # 80% gain → misprice_captured
NO_TIME_PROFIT      = 0.40       # 40% gain + near expiry → misprice_time
NO_STOP             = 0.40       # 40% loss → misprice_failed (sweep Jul 22: z2.5/stop0.40 best overall return 1407% + best NO P&L)
NO_EDGE_GONE_RATIO  = 1.05       # overpricing ratio drops here → edge_gone

# BOUNDARY_NO — sell OTM premium at range extremes
# When z-score shows BTC at the top or bottom of its recent range, fade the
# OTM contracts in the continuation direction. Market overprices a breakout
# that mean reversion says won't happen. Same `is_no` position + exit logic
# as MISPRICE_NO, different entry gate (z-score extremes instead of raw overpricing).
ENABLE_BOUNDARY_NO          = True
BOUNDARY_NO_ZSCORE_MIN      = 2.5    # |z| must exceed this to count as a range extreme (was 1.5 — sweep Jul 22 showed 2.5 best)
BOUNDARY_NO_OTM_MIN         = -250   # don't go deeper than 250 OTM (premium too thin)
BOUNDARY_NO_OTM_MAX         = -10    # small buffer — not right at the current boundary
BOUNDARY_NO_OVERPRICING_MIN = 1.15   # lower than MISPRICE_NO — z-score adds independent conviction
NO_EXEMPT_FROM_COOLDOWN     = True   # let NO scans see cooled-off tickers.
                                  # The re-entry cooldown exists to stop MOMENTUM
                                  # chasing: a YES position exits on scalp_lock /
                                  # peak_giveback, which are PRICE-based, so the move
                                  # is over and re-entering buys the fade — measured
                                  # at -24.1% per $ risked over 27 round trips.
                                  # NO exits are EDGE-based (edge_gone: the
                                  # overpricing corrected), and a NO re-entry cannot
                                  # happen unless |z| >= BOUNDARY_NO_ZSCORE_MIN, the
                                  # overpricing ratio clears, AND net edge clears
                                  # BOUNDARY_NO_MIN_NET_EDGE — all on current prices.
                                  # The cooldown was guarding against something those
                                  # gates already prevent, while blocking re-entry
                                  # into a mispricing that had genuinely returned.
                                  # Evidence is thin (1 observed NO re-entry: bought
                                  # 0.78 at +14.5% edge, exited +$1.44, re-entered
                                  # 0.87 at +4.8% edge, exited +$0.22 — smaller edge,
                                  # smaller profit, still positive). Note that
                                  # re-entry would now be blocked anyway by the 5%
                                  # net-edge floor, so this mainly frees genuinely
                                  # strong repeat signals. Set False to restore.
BOUNDARY_NO_MIN_NET_EDGE    = 0.05   # minimum ABSOLUTE edge on the NO side:
                                  #     (1 - true_prob) - no_cost  >=  this
                                  # The overpricing gate above is a RATIO, which is
                                  # scale-free and therefore blind to what the position
                                  # costs. At true_prob=0.066 / yes_ask=0.10 the ratio is
                                  # 1.52 — sails through — while the absolute edge is
                                  # 3.4pp on a 92c position.
                                  # 2026-08-13, the eight BOUNDARY_NO entries taken so far,
                                  # by NO cost: risk/reward and edge degrade together.
                                  #   cost 0.65  R:R 1.9:1  edge +18.8%
                                  #   cost 0.78  R:R 3.5:1  edge +14.5%
                                  #   cost 0.82  R:R 4.6:1  edge  +5.6%
                                  #   cost 0.86  R:R 6.1:1  edge  +3.2%
                                  #   cost 0.92  R:R 11.5:1 edge  +1.4%   <- +$0.10 realised
                                  # A 92c NO risks 92c to win 8c: one loss erases eleven
                                  # wins. Worse, 1.4pp sits INSIDE our own model error —
                                  # calibration_check.py measured true_prob predicting
                                  # 13.6% where 18.3% occurred, a ~4.7pp miss. An edge
                                  # smaller than the model's known error is noise with a
                                  # sign, not edge. 0.05 is set at roughly that error
                                  # floor; it would have kept the +18.8% and +14.5%
                                  # entries and rejected the four thinnest.
BOUNDARY_NO_HOURS_MIN       = 0.08
BOUNDARY_NO_HOURS_MAX       = 0.50   # wider window than plain NO — more time decay to harvest
BOUNDARY_NO_YES_ASK_MIN     = 0.10
BOUNDARY_NO_YES_ASK_MAX     = 0.65

# Regime
# Momentum lookback the regime classifier measures trend/acceleration over.
# 2026-08-11: RegimeEngine hardcoded feed.momentum(60) — 60 SECONDS on 2s live
# ticks. Measured against 212k recorded live ticks, |mom| clears
# TREND_THRESHOLD on 0.5% of them, which is why live classified TRENDING 0.37%
# of the time and BREAKOUT literally never (0 of 212,331). Sixty-second
# momentum on hourly contracts is microstructure noise, and the classifier was
# correctly finding no trend in it. Same thresholds on longer windows:
#     60s -> 0.5%   5m -> 6.7%   10m -> 13.4%   30m -> 31.3%   60m -> 41.2%
MOMENTUM_WINDOW_SECS = 600
# 2026-08-11 sweep (momentum_window_sweep.py, 40d tune / 19d held out), real-time
# windows vs the 60s-scaled baseline:
#                  TUNING                     VALIDATION
#   baseline 60s   +24.9%  Sharpe 3.71  WR 33.1%   +21.6%  Sharpe  9.99  WR 51.4%
#   600s           +72.7%  Sharpe 5.61  WR 44.4%   +22.9%  Sharpe 15.30  WR 56.8%
#   900s           +14.0%  Sharpe 2.18            +25.8%  Sharpe 15.37
#   1800s          +59.3%  Sharpe 4.70            +17.1%  Sharpe  7.85
#   3600s          +16.5%  Sharpe 2.29            +12.4%  Sharpe  6.18
# 900s edges 600s on validation but is the WORST on tuning — erratic, so noise.
# 600s is the only window strong on both, and beats the baseline on both for
# Sharpe, return and win rate. Note it does NOT trade more: validation goes
# 70 -> 37 trades. The gain is selection (WR 51.4% -> 56.8%, PF 1.91 -> 2.75),
# not volume.
# Whether the BACKTEST stretches that window by TIME_SCALE (150x at 5-min bars
# / 2s polling). True reproduces the historical behaviour, in which the same
# constant meant 60s live but 2.5 HOURS in the backtest — which is why the two
# reported near-opposite regime mixes (backtest 43% TRENDING / 18% BREAKOUT vs
# live 0.37% / 0.00%) and why nothing regime-dependent has ever been validly
# backtested. False makes both sides measure the same real-time window.
MOMENTUM_WINDOW_SCALED = False
TREND_BARS          = 3
TREND_THRESHOLD     = 0.0015
REVERT_ZSCORE       = 1.5
BREAKOUT_ACCEL      = 0.004
BREAKOUT_MOM_MULT   = 2.0        # BREAKOUT needs |mom| > TREND_THRESHOLD * this.
                                  # Was hardcoded 2 in regime.py, which coupled it to
                                  # TRENDING's threshold — you could not calibrate one
                                  # without moving the other. Split out so BREAKOUT can
                                  # be tuned while TRENDING (now firing 20.8% of ticks
                                  # after the 2026-08-11 window fixes) stays put.
                                  # 2026-08-13, measured on 38,257 paired live
                                  # observations at MOMENTUM_WINDOW_SECS=600, the JOINT
                                  # probability of both BREAKOUT gates clearing:
                                  #   accel 0.0040 / mom 0.0030 -> 0.144%  (current)
                                  #   accel 0.0025 / mom 0.0020 -> 1.012%
                                  #   accel 0.0017 / mom 0.0015 -> 3.550%
                                  #   accel 0.0013 / mom 0.0015 -> 5.617%
                                  #   accel 0.0013 / mom 0.0010 -> 7.227%
                                  # |accel| p99 is 0.00267, so the 0.004 gate sits near
                                  # the 99.8th percentile — BREAKOUT has never once
                                  # fired in 267,868 recorded live ticks.

# Kelly position sizing
KELLY_FRACTION      = 0.25    # quarter-Kelly multiplier
KELLY_CAP           = 0.025   # hard cap on Kelly-derived fraction (matches MAX_TRADE_PCT)

# Vol regime thresholds (hourly vol units = per-bar vol × sqrt(900))
VOL_REGIME_LOW_H    = 0.005   # < LOW  → calm market (~50% annualized)
VOL_REGIME_HIGH_H   = 0.015   # > HIGH → stressed market (~150% annualized)

# Vol compression (Kalshi pricing-lag) signal
# When fast EWMA << slow EWMA, Kalshi's lagged model overestimates vol →
# RANGE contracts are mispriced cheap → buy YES, target 80¢+ or full settlement
VOL_RATIO_COMPRESSION = 0.55  # fast/slow EWMA ratio below this → compressed
MIN_EDGE_COMPRESSION  = 0.010  # lower entry bar when compressed (structural edge is larger)
TRADE_ONLY_COMPRESSION = True  # gate the YES entries (find_best and find_snipe) on the
                              # compression regime, rather than merely lowering the edge
                              # bar inside it. Deliberately does NOT gate find_no_scalp or
                              # find_boundary_no: those sell overpriced OTM premium at
                              # z-score range extremes, a different edge thesis from the
                              # EWMA-vs-SMA vol lag this gate is derived from, and the
                              # validation below was YES-only (the backtest omits NO
                              # signals unless --no-threshold is passed). Gating them
                              # would extend an unvalidated constraint to a strategy it
                              # was never tested against. See [NO strategy status] —
                              # BOUNDARY_NO is still unproven on its own tiny sample.
                              # 2026-08-08: once RANGE_WIDTH was corrected
                              # to the real 100-wide band (see kalshi_btc_backtest.py),
                              # segmenting the 59d/$500 run showed the entire loss comes
                              # from trading OUTSIDE compression:
                              #     compression  n=132  WR 43.2%  +$88.44  (+18.4%/$ risked)
                              #     normal vol   n=338  WR 17.5%  -$443.86 (-47.3%/$ risked)
                              # Normal-vol trades also carry the LARGEST model edge
                              # (median 0.0645 vs 0.0163) while losing — outside
                              # compression the EWMA/SMA gap is noise, not mispricing, so
                              # the model manufactures fake edge exactly where it is least
                              # reliable. That is the mechanism behind the
                              # "edge is anti-predictive" result in calibration_check.py.
                              # Gating on compression flips the sign on BOTH windows of a
                              # 40d-tune/19d-validate split, which no other change this
                              # session managed:
                              #     tuning      -53.6% Sharpe -6.91  ->  +41.2% Sharpe 4.03
                              #     validation  -32.1% Sharpe -9.92  ->  +23.8% Sharpe 9.49
                              # with max drawdown cut ~4-5x (-66%/-32% -> -14%/-6.9%).
                              # Set False to restore the old all-regimes behaviour.
BID_EXIT_THRESHOLD    = 0.75  # exit any position when bid hits 75¢ (near full settlement)

# Intervals
# Market-data recording (KALSHI_RECORD=1). Book capture runs on its own thread
# and its own cadence so it never sits on the trading path, and so its API rate
# can be tuned independently of SCAN_INTERVAL. Each cycle costs one orderbook
# request per visible ladder contract plus one per open position — typically
# 2-8 calls.
RECORD_BOOK_INTERVAL = 5
SCAN_INTERVAL        = 2
POSITION_CHECK       = 2
PRICE_FETCH          = 2
SYNC_INTERVAL        = 20
LADDER_CACHE_SECONDS = 2

# Ticks per hour, derived from the actual polling interval. model.py and
# regime.py annualize per-tick vol with sqrt(BARS_PER_HOUR); this was
# hardcoded 900 (4s ticks) after PRICE_FETCH dropped to 2s, silently
# understating hourly vol by sqrt(2) (~29%) and inflating every RANGE edge.
BARS_PER_HOUR = 3600 // PRICE_FETCH
