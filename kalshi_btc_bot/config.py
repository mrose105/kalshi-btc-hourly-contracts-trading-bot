# ─────────────────────────────────────────────
# MODE — switch here before running
# ─────────────────────────────────────────────
PAPER_TRADING = False   # True = paper mode (no real orders), False = live trading
PAPER_CAPITAL = 25.00   # simulated capital for paper mode

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

# Risk controls
MAX_EXPOSURE_PCT    = 0.40       # max 40% of real portfolio value in positions
MIN_CASH_PCT        = 0.05       # keep 5% as cash reserve
MAX_TRADE_PCT       = 0.05       # max 5% of real portfolio per trade
NO_TRADE_PCT        = 0.04       # max 4% of real portfolio per MISPRICE_NO trade
ENABLE_MISPRICE_NO = False      # disabled live until pending NO order reconciliation is fixed
MAX_POSITIONS       = 4
SESSION_STOP_PCT    = 0.05       # stop NEW entries if down 5%
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
STRONG_EDGE_PRICE_IMPROVE = 0.00 # always cross the ask to ensure IOC fills
ENTRY_PRICE_IMPROVE_CENTS = 4    # cross by 4c on all IOC entries
MIN_HOURS           = 0.10       # 6 min — keeps entries clear of the TIME_EXIT_MINS kill zone
MAX_HOURS           = 4.0
MAX_OTM_T           = 100
MAX_OTM_B           = 150

# Exit thresholds — unified tiered ladder
# TIER 0.5: Gamma-aware convexity lock — closes the asymmetry where YES positions
# had no "sell when overpriced" check (the NO side already has one via NO_EDGE_GONE_RATIO).
# Fires when profitable + true_prob is reversing (2-tick fade) + gamma is high, i.e. we're
# in the near-strike/near-expiry zone where the model's edge can flip faster than the fixed
# P&L tiers below would catch. GAMMA_HIGH_THRESHOLD is an initial estimate, not backtested —
# tune it from the "gam=" values printed in the live position ticker once you've watched a
# session or two.
GAMMA_LOCK_MIN_PROFIT = 0.15     # don't fire on noise — require at least 15% gain first
GAMMA_HIGH_THRESHOLD  = 50.0     # dollar-gamma magnitude considered high convexity risk
SCALP_LOCK_PCT      = 0.40       # TIER 1: up 40% + < 15 min left
MOMENTUM_LOCK_PCT   = 1.00       # TIER 2: up 100% + < 9 min
STRONG_PROFIT_PCT   = 1.50       # TIER 3: up 150% + < 15 min
PROFIT_EXIT_MEGA    = 3.00       # TIER 4: up 300%, no conditions
TIME_EXIT_MINS      = 3          # TIER 5: OTM with < 3 min left — let late-window mispricing play out
STOP_LOSS_PCT       = 0.60       # TIER 6: base stop
STOP_MIN_HOURS      = 0.30       # TIER 6 gate: stop only fires if > 18 min left.
                                  # Below this, TIME_EXIT_MINS handles OTM exits and
                                  # expiry_settle captures ITM wins — don't stop binary
                                  # options in their last bars when the binary payoff
                                  # hasn't resolved yet.

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
KELLY_CAP           = 0.05    # hard cap on Kelly-derived fraction (matches MAX_TRADE_PCT)

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
SCAN_INTERVAL        = 8
POSITION_CHECK       = 6
PRICE_FETCH          = 4
SYNC_INTERVAL        = 20
LADDER_CACHE_SECONDS = 5
