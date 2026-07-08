# ─────────────────────────────────────────────
# MODE — switch here before running
# ─────────────────────────────────────────────
PAPER_TRADING = True    # True = paper mode (no real orders), False = live trading
PAPER_CAPITAL = 10000.00   # simulated capital for paper mode

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
ENABLE_MISPRICE_NO = False      # disabled live until pending NO order reconciliation is fixed
MAX_POSITIONS       = 4
STRIKE_CLUSTER_DIST = 150        # skip a new entry if its strike is within this many
                                  # dollars of an existing open position's strike in the
                                  # same expiry window — MAX_POSITIONS caps capital
                                  # concentration but not directional correlation. 2026-07-07:
                                  # observed live — 4 RANGE positions opened within 2 min on
                                  # adjacent strikes (62550-62850), then one BTC breakout
                                  # busted all 4 simultaneously and filled every slot with
                                  # dead positions, locking out a genuinely better ATM entry.
SESSION_STOP_PCT    = 0.03       # stop NEW entries if down 3% (was 0.05)
MIN_CASH_FLOOR      = 0.25       # never trade with less than $0.25
UNTRACKED_EXPOSURE_LIMIT = 0.25  # block new trades if live exposure exceeds tracked exposure by this much
EXIT_RETRY_COOLDOWN = 10         # seconds to wait before retrying an unfilled live exit
STOP_COOLDOWN_SECS  = 300        # block re-entry on same ticker for 5 min after stop loss
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
MIN_RANGE_BOUNDARY_BUFFER = 20   # skip RANGE entries within $20 of either boundary (ITM or
                                  # OTM side), all regimes. Old logic only guarded the OTM side
                                  # (dist < -20) — near-money ITM entries like dist +1..+38 with
                                  # no directional confirmation were let through and flipped OTM
                                  # by expiry on ordinary spot drift (observed: B61650 losers,
                                  # 2026-07-01/02 overnight session). Widened to 40 and applied
                                  # both-sides/all-regimes on 2026-07-06, which fixed the whipsaw
                                  # but cut entry frequency ~4x vs the Sharpe-5.66 baseline
                                  # (601 trades/wk -> 143/wk). Narrowed back to 20 same day —
                                  # matches the old gate's magnitude while keeping the
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
PEAK_GIVEBACK_FRACTION = 0.50    # exit once current pnl has faded to <= 50% of that peak
PEAK_GIVEBACK_MIN_BID  = 0.20    # same rationale as GAMMA_LOCK_MIN_BID — don't lock trivial cents

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
STOP_MIN_HOURS      = 0.30       # TIER 6 gate: stop only fires if > 18 min left.
                                  # Below this, TIME_EXIT_MINS handles OTM exits and
                                  # expiry_settle captures ITM wins — don't stop binary
                                  # options in their last bars when the binary payoff
                                  # hasn't resolved yet.

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
SNIPE_TRADE_PCT       = 0.02     # sized down vs MAX_TRADE_PCT — tail-probability estimates
                                  # are noisier, so size the bet down rather than Kelly-size
                                  # off an uncertain edge
SNIPE_PROFIT_LOCK_PCT     = 1.50 # TIER 3.75 gate: lock in profit only when a big snipe run
                                  # (150%+) reverses — gated on true_prob fading (2-tick signal,
                                  # same as gamma_lock), NOT a fixed price cap. A snipe that keeps
                                  # climbing without reversing is untouched and can still ride to
                                  # 1000%+. This only catches the failure mode observed live
                                  # 2026-07-02: B61250 (+300%) and B61350 (+195%) both held peak
                                  # gains for 7-8 min then fully round-tripped to near-total losses
                                  # with no tier between "hold" and 75c near_settlement.
SNIPE_PROFIT_LOCK_MIN_BID = 0.12 # absolute price floor — same rationale as GAMMA_LOCK_MIN_BID

# MISPRICE_NO entry filters
NO_OVERPRICING_MIN  = 1.40       # YES_ask / true_prob must exceed this
NO_YES_ASK_MIN      = 0.30
NO_YES_ASK_MAX      = 0.72
NO_TRUE_PROB_MAX    = 0.55
NO_HOURS_MIN        = 0.08
NO_HOURS_MAX        = 0.35
NO_DIST_MIN         = -300
NO_DIST_MAX         = 100
NO_CASH_MIN_PCT     = 0.20       # available cash > start_total * 0.20

# MISPRICE_NO exit thresholds
NO_PROFIT_CAPTURE   = 0.80       # 80% gain → misprice_captured
NO_TIME_PROFIT      = 0.40       # 40% gain + near expiry → misprice_time
NO_STOP             = 0.30       # 30% loss → misprice_failed
NO_EDGE_GONE_RATIO  = 1.05       # overpricing ratio drops here → edge_gone

# Regime
TREND_BARS          = 3
TREND_THRESHOLD     = 0.0015
REVERT_ZSCORE       = 1.5
BREAKOUT_ACCEL      = 0.004

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
BID_EXIT_THRESHOLD    = 0.75  # exit any position when bid hits 75¢ (near full settlement)

# Intervals
SCAN_INTERVAL        = 2
POSITION_CHECK       = 2
PRICE_FETCH          = 2
SYNC_INTERVAL        = 20
LADDER_CACHE_SECONDS = 2
