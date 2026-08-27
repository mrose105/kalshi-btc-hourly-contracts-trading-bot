# ─────────────────────────────────────────────
# MODE — switch here before running
# ─────────────────────────────────────────────
PAPER_TRADING               = True                             # True = paper mode (no real orders), False = live trading
PAPER_CAPITAL               = 500.00                           # simulated capital for paper mode  → docs/CONFIG_RATIONALE.md#paper_capital

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
# Price-DISTANCE constants come from the active instrument, not from literals
# here. They were BTC dollar values tuned to a ~$300 hourly sigma; SPX's is ~26
# points, so a literal ported across is off by ~12x. See instrument.py.
from .instrument import ACTIVE as _INST

MAX_EXPOSURE_PCT            = 0.18                             # max 18% of real portfolio value in positions (was 0.40)  → docs/CONFIG_RATIONALE.md#max_exposure_pct
MIN_CASH_PCT                = 0.05                             # keep 5% as cash reserve
MAX_TRADE_PCT               = 0.025                            # max 2.5% of real portfolio per trade (was 0.05)
NO_TRADE_PCT                = 0.02                             # max 2% of real portfolio per MISPRICE_NO trade (was 0.04)
ENABLE_YES                  = False                            # NO-only paper test: disable normal YES RANGE entries
ENABLE_SNIPE                = False                            # SNIPE is also a YES buy; keep off for clean NO attribution
ENABLE_MISPRICE_NO          = False                            # disabled — BOUNDARY_NO is the active NO strategy
MAX_POSITIONS               = 4
STRIKE_CLUSTER_DIST         = _INST.strike_cluster_dist        # skip a new entry if its strike is  → docs/CONFIG_RATIONALE.md#strike_cluster_dist
SESSION_STOP_PCT            = 0.03                             # stop NEW entries if down 3% (was 0.05)
MIN_CASH_FLOOR              = 0.25                             # never trade with less than $0.25
UNTRACKED_EXPOSURE_LIMIT    = 0.25                             # block new trades if live exposure exceeds tracked  → docs/CONFIG_RATIONALE.md#untracked_exposure_limit
EXIT_RETRY_COOLDOWN         = 10                               # seconds to wait before retrying an unfilled live exit
STOP_COOLDOWN_SECS          = 300                              # block re-entry on same ticker for 5 min after stop loss
EXIT_COOLDOWN_SECS          = 300                              # block re-entry on same ticker after a *profitable* exit.  → docs/CONFIG_RATIONALE.md#exit_cooldown_secs
FORCE_EXIT_SLIPPAGE_CENTS   = 2                                # cross stale bids by this many cents on urgent exits

MIN_EDGE                    = 0.015                            # docs/CONFIG_RATIONALE.md#min_edge
MIN_VOLUME                  = 50

# ---------------------------------------------------------------------------
# Return distribution used by DistModel.true_prob
# ---------------------------------------------------------------------------
DIST_TAIL_DF                = 3.0                              # docs/CONFIG_RATIONALE.md#dist_tail_df
MAX_ASK                     = 0.45
MAX_SPREAD                  = 0.05                             # max 5c bid/ask spread
MAX_SPREAD_PCT              = 0.25                             # max spread as 25% of ask

# ---------------------------------------------------------------------------
# Kalshi trading fees
# ---------------------------------------------------------------------------
KALSHI_TAKER_FEE_RATE       = 0.07                             # docs/CONFIG_RATIONALE.md#kalshi_taker_fee_rate
KALSHI_FEE_MULTIPLIER       = 1.0                              # KXBTC public series metadata, verified 2026-08-18
CHARGE_FEES                 = True                             # False restores the historical fee-free accounting

BAYES_MARKET_WEIGHT_BASE    = 0.15                             # docs/CONFIG_RATIONALE.md#bayes_market_weight_base
BAYES_MARKET_WEIGHT_MAX     = 0.35
BAYES_MAX_MOVE              = 0.10
MIN_HOURS                   = 0.10                             # 6 min — keeps entries clear of the TIME_EXIT_MINS kill  → docs/CONFIG_RATIONALE.md#min_hours
MAX_HOURS                   = 4.0
MAX_OTM_T                   = _INST.max_otm_t
MAX_OTM_B                   = _INST.max_otm_b
MIN_RANGE_BOUNDARY_BUFFER   = _INST.min_range_boundary_buffer  # skip RANGE entries within $40 of either boundary (ITM  → docs/CONFIG_RATIONALE.md#min_range_boundary_buffer

GAMMA_LOCK_MIN_PROFIT       = 0.15                             # don't fire on noise — require at least 15% gain first  → docs/CONFIG_RATIONALE.md#gamma_lock_min_profit
GAMMA_HIGH_THRESHOLD        = 40000.0                          # dollar-gamma magnitude considered high convexity risk.  → docs/CONFIG_RATIONALE.md#gamma_high_threshold
GAMMA_LOCK_MIN_BID          = 0.35                             # TIER 0.5 gate: don't lock gamma risk below this absolute  → docs/CONFIG_RATIONALE.md#gamma_lock_min_bid

PEAK_GIVEBACK_MIN_PEAK      = 0.25                             # only protect peaks of at least 25% unrealized gain  → docs/CONFIG_RATIONALE.md#peak_giveback_min_peak
PEAK_GIVEBACK_FRACTION      = 0.75                             # exit once current pnl has faded to <= 75% of that peak  → docs/CONFIG_RATIONALE.md#peak_giveback_fraction
PEAK_GIVEBACK_MIN_BID       = 0.20                             # same rationale as GAMMA_LOCK_MIN_BID  → docs/CONFIG_RATIONALE.md#peak_giveback_min_bid
PEAK_GIVEBACK_MIN_BID_MULT  = 1.30                             # the floor above is applied as min(abs, MULT * entry).  → docs/CONFIG_RATIONALE.md#peak_giveback_min_bid_mult
SNIPE_PEAK_GIVEBACK_MIN_BID = 0.20                             # snipe-specific floor for the same tier  → docs/CONFIG_RATIONALE.md#snipe_peak_giveback_min_bid
PEAK_GIVEBACK_HARD_LOSS_PCT = 1.50                             # TIER 0.75b: once a position has cleared MIN_PEAK  → docs/CONFIG_RATIONALE.md#peak_giveback_hard_loss_pct

SCALP_LOCK_MIN_BID          = 0.30                             # TIER 1 gate: same rationale  → docs/CONFIG_RATIONALE.md#scalp_lock_min_bid
SCALP_LOCK_PCT              = 0.40                             # TIER 1: up 40% + < 15 min left
MOMENTUM_LOCK_PCT           = 1.00                             # TIER 2: up 100% + < 9 min
STRONG_PROFIT_PCT           = 1.50                             # TIER 3: up 150% + < 15 min
PROFIT_EXIT_MEGA            = 3.00                             # TIER 4: up 300%, no conditions
TIME_EXIT_MINS              = 3                                # TIER 5: OTM with < 3 min left  → docs/CONFIG_RATIONALE.md#time_exit_mins
TIME_EXIT_NEAR_DIST         = _INST.time_exit_near_dist        # TIER 5 override: skip the force-exit above if still  → docs/CONFIG_RATIONALE.md#time_exit_near_dist
STOP_LOSS_PCT               = 0.35                             # TIER 6: base stop  → docs/CONFIG_RATIONALE.md#stop_loss_pct
STOP_UNCOVERED_PCT          = 0.65                             # TIER 6 floor for positions OPENED inside STOP_MIN_HOURS.  → docs/CONFIG_RATIONALE.md#stop_uncovered_pct
CUT_NEVER_GREEN_MINS        = 0                                # REJECTED BY EVIDENCE — kept at 0, do not enable.  → docs/CONFIG_RATIONALE.md#cut_never_green_mins
REENTRY_SIZE_DECAY          = 0.0                              # 0 = disabled  → docs/CONFIG_RATIONALE.md#reentry_size_decay
STOP_MIN_HOURS              = 0.30                             # TIER 6 gate: stop only fires if > 18 min left.  → docs/CONFIG_RATIONALE.md#stop_min_hours
SNIPE_STOP_PCT              = 0.50                             # TIER 6-snipe catastrophe floor. Snipes skip TIER 5.25/6  → docs/CONFIG_RATIONALE.md#snipe_stop_pct

BOUNDARY_RISK_DIST          = _INST.boundary_risk_dist         # points from boundary considered "at risk" while ITM  → docs/CONFIG_RATIONALE.md#boundary_risk_dist
BOUNDARY_RISK_MINS          = 10                               # window before expiry this tier is active  → docs/CONFIG_RATIONALE.md#boundary_risk_mins
BOUNDARY_RISK_MIN_LOSS      = -0.10                            # ignore trivial pnl noise, require real drawdown first
BOUNDARY_RISK_HARD_STOP     = -0.65                            # unconditional cap — fires even without momentum confirm

SNIPE_MIN_ENTRY_PRICE       = 0.10                             # 2026-07-07: floor added  → docs/CONFIG_RATIONALE.md#snipe_min_entry_price
SNIPE_MAX_ENTRY_PRICE       = 0.25                             # widened from 0.10 now that 0.10 is the floor
SNIPE_MIN_EDGE_RATIO        = 0.30                             # true_prob must beat ask by >= 30% (true_prob/ask - 1)
SNIPE_TRADE_PCT             = 0.01                             # sized down vs MAX_TRADE_PCT  → docs/CONFIG_RATIONALE.md#snipe_trade_pct
SNIPE_PROFIT_LOCK_PEAK      = 1.50                             # peak_pnl threshold that arms the lock  → docs/CONFIG_RATIONALE.md#snipe_profit_lock_peak
SNIPE_PROFIT_LOCK_MIN_PNL   = 0.15                             # and current pnl must still be at least this
SNIPE_PROFIT_LOCK_MIN_BID   = 0.12                             # absolute price floor  → docs/CONFIG_RATIONALE.md#snipe_profit_lock_min_bid

NO_OVERPRICING_MIN          = 1.18                             # YES_ask / true_prob must exceed this (was 1.40)  → docs/CONFIG_RATIONALE.md#no_overpricing_min
NO_YES_ASK_MIN              = 0.30
NO_YES_ASK_MAX              = 0.72
NO_TRUE_PROB_MAX            = 0.55
NO_HOURS_MIN                = 0.08
NO_HOURS_MAX                = 0.35
NO_DIST_MIN                 = _INST.no_dist_min
NO_DIST_MAX                 = _INST.no_dist_max
NO_CASH_MIN_PCT             = 0.20                             # available cash > start_total * 0.20

NO_PROFIT_CAPTURE           = 0.80                             # 80% gain → misprice_captured  → docs/CONFIG_RATIONALE.md#no_profit_capture
NO_TIME_PROFIT              = 0.40                             # 40% gain + near expiry → misprice_time
NO_STOP                     = 0.30                             # 30% loss → misprice_failed  → docs/CONFIG_RATIONALE.md#no_stop
NO_EDGE_GONE_RATIO          = 1.05                             # overpricing ratio drops here → edge_gone
NO_EDGE_GONE_MIN_GAIN       = 0.15                             # edge_gone needs a real gain, not just >0  → docs/CONFIG_RATIONALE.md#no_edge_gone_min_gain

# ---------------------------------------------------------------------------
# Minimum hold — don't round-trip on quote noise
# ---------------------------------------------------------------------------
MIN_HOLD_SECS               = 60.0                             # docs/CONFIG_RATIONALE.md#min_hold_secs
MIN_HOLD_CATASTROPHE        = 0.65                             # bypass the hold below this loss, as a fraction

# ---------------------------------------------------------------------------
# Confirm profit exits against real depth
# ---------------------------------------------------------------------------
CONFIRM_EXIT_DEPTH          = True                             # docs/CONFIG_RATIONALE.md#confirm_exit_depth

ENABLE_BOUNDARY_NO          = True                             # docs/CONFIG_RATIONALE.md#enable_boundary_no
BOUNDARY_NO_ZSCORE_MIN      = 1.40                             # |z| must exceed this to count as a range extreme (Aug 17  → docs/CONFIG_RATIONALE.md#boundary_no_zscore_min
BOUNDARY_NO_OTM_MIN         = _INST.boundary_no_otm_min        # don't go deeper than 250 OTM (premium too thin)
BOUNDARY_NO_OTM_MAX         = _INST.boundary_no_otm_max        # small buffer — not right at the current boundary
BOUNDARY_NO_OVERPRICING_MIN = 1.25                             # raising this selected for model error  → docs/CONFIG_RATIONALE.md#boundary_no_overpricing_min
NO_EXEMPT_FROM_COOLDOWN     = True                             # let NO scans see cooled-off tickers.  → docs/CONFIG_RATIONALE.md#no_exempt_from_cooldown
BOUNDARY_NO_MIN_NET_EDGE    = 0.05                             # minimum ABSOLUTE edge on the NO side: (1  → docs/CONFIG_RATIONALE.md#boundary_no_min_net_edge
# ---------------------------------------------------------------------------
# Shadow recorder — price the looser gate set, do not trade it
# ---------------------------------------------------------------------------
SHADOW_ENABLED              = True                             # off = zero extra API calls  → docs/CONFIG_RATIONALE.md#shadow_enabled
SHADOW_HOURS_MAX            = 1.00                             # look at the whole hour, not the last 15 min
SHADOW_OVERPRICING_MIN      = 1.25                             # vs 1.60 live
SHADOW_ZSCORE_MIN           = 1.20                             # vs 1.40 live
SHADOW_MAX_PER_SCAN         = 1                                # caps the added API load
SHADOW_TICKER_COOLDOWN      = 120                              # seconds before re-sampling the same contract

BOUNDARY_NO_HOURS_MIN       = 0.08
BOUNDARY_NO_HOURS_MAX       = 0.25                             # 15 min; was 0.50  → docs/CONFIG_RATIONALE.md#boundary_no_hours_max
BOUNDARY_NO_YES_ASK_MIN     = 0.10
BOUNDARY_NO_YES_ASK_MAX     = 0.65

# ---------------------------------------------------------------------------
# Delayed entry — don't buy the signal, buy the dip after it
# ---------------------------------------------------------------------------
DELAYED_ENTRY_DIP           = 0.0                              # 0.05 = the validated band, see above  → docs/CONFIG_RATIONALE.md#delayed_entry_dip
DELAYED_ENTRY_DIP_MAX       = 0.12                             # None = no cap (the old floor behaviour)  → docs/CONFIG_RATIONALE.md#delayed_entry_dip_max
DELAYED_ENTRY_MAX_WAIT_MINS = 20.0                             # drop the pending entry after this long
DELAYED_ENTRY_SIGNALS       = ("BOUNDARY_NO", "MISPRICE_NO")

# ---------------------------------------------------------------------------
# Watchlist entry — arm strict, fill on the MODEL's valuation
# ---------------------------------------------------------------------------
WATCHLIST_ENTRY_DIP         = 0.0                              # OFF — every dip level measured worse  → docs/CONFIG_RATIONALE.md#watchlist_entry_dip
WATCHLIST_ENTRY_NET_EDGE    = 0.05

# Regime
# Momentum lookback the regime classifier measures trend/acceleration over.
# 2026-08-11: RegimeEngine hardcoded feed.momentum(60) — 60 SECONDS on 2s live
# ticks. Measured against 212k recorded live ticks, |mom| clears
# TREND_THRESHOLD on 0.5% of them, which is why live classified TRENDING 0.37%
# of the time and BREAKOUT literally never (0 of 212,331). Sixty-second
# momentum on hourly contracts is microstructure noise, and the classifier was
# correctly finding no trend in it. Same thresholds on longer windows:
#     60s -> 0.5%   5m -> 6.7%   10m -> 13.4%   30m -> 31.3%   60m -> 41.2%
# ---------------------------------------------------------------------------
# Lag filter — do not buy a quote Kalshi has not repriced yet
# ---------------------------------------------------------------------------
LAG_FILTER_SECS             = 20                               # lookback, matching the measured peak  → docs/CONFIG_RATIONALE.md#lag_filter_secs
LAG_FILTER_MAX_ADVERSE      = 25.0                             # $ of spot movement TOWARD the band; 0 = off

MOMENTUM_WINDOW_SECS        = 600
MOMENTUM_WINDOW_SCALED      = False                            # docs/CONFIG_RATIONALE.md#momentum_window_scaled
TREND_BARS                  = 3
TREND_THRESHOLD             = 0.0015
REVERT_ZSCORE               = 1.5
BREAKOUT_ACCEL              = 0.004
BREAKOUT_MOM_MULT           = 2.0                              # BREAKOUT needs |mom| > TREND_THRESHOLD * this.  → docs/CONFIG_RATIONALE.md#breakout_mom_mult

KELLY_FRACTION              = 0.25                             # quarter-Kelly multiplier  → docs/CONFIG_RATIONALE.md#kelly_fraction
KELLY_CAP                   = 0.025                            # hard cap on Kelly-derived fraction (matches MAX_TRADE_PCT)

# Vol regime thresholds (hourly vol units = per-bar vol × sqrt(900))
# MOVED to instrument.py (2026-08-17). These are instrument facts, not tuning
# knobs: BTC's boundaries classify 99% of SPX's measured hourly vol as LOW,
# which pins the regime to a constant and applies true_prob's 0.92 LOW haircut
# permanently. regime.py now reads them from the active instrument profile.
# BTC values were VOL_REGIME_LOW_H = 0.005 (~50% ann), HIGH = 0.015 (~150% ann).

VOL_RATIO_COMPRESSION       = 0.55                             # fast/slow EWMA ratio below this → compressed  → docs/CONFIG_RATIONALE.md#vol_ratio_compression
MIN_EDGE_COMPRESSION        = 0.010                            # lower entry bar when compressed (structural edge is  → docs/CONFIG_RATIONALE.md#min_edge_compression
TRADE_ONLY_COMPRESSION      = True                             # gate the YES entries (find_best and find_snipe)  → docs/CONFIG_RATIONALE.md#trade_only_compression
BID_EXIT_THRESHOLD          = 0.75                             # exit any position when bid hits 75¢ (near full settlement)

RECORD_BOOK_INTERVAL        = 5                                # docs/CONFIG_RATIONALE.md#record_book_interval
SCAN_INTERVAL               = 2
POSITION_CHECK              = 2
PRICE_FETCH                 = 2
SYNC_INTERVAL               = 20
LADDER_CACHE_SECONDS        = 2

BARS_PER_HOUR               = 3600 // PRICE_FETCH              # docs/CONFIG_RATIONALE.md#bars_per_hour
