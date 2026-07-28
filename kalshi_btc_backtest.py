"""
kalshi_btc_backtest.py — Walk-forward simulation of the KXBTC binary strategy

Data:   yfinance BTC-USD (5-minute OHLCV bars, up to 60 days available)
Ladder: synthetic KXBTC RANGE contracts reconstructed from spot + DistModel
Fills:  entry at ask, exit at bid  (pessimistic market-impact assumption)
Model:  same DistModel + RegimeEngine + SignalEngine as live bot

Edge source: Kalshi prices with a long-window lagged vol estimate (SMA_VOL_WINDOW bars).
             We use fast EWMA. When current vol drops below the lagged average, Kalshi
             still prices as if vol is elevated → RANGE contracts are cheap → BUY YES.
             During vol compression, 2-4¢ contracts can settle at $1.00 (25-50x payoff).

Stop simulation: intrabar stop uses bar High/Low via Brownian-bridge worst-case.
                 This matches live-bot behavior (8s polling) far better than end-of-bar only.

Usage:
    python3 kalshi_btc_backtest.py
    python3 kalshi_btc_backtest.py --days 30
    python3 kalshi_btc_backtest.py --days 7 --capital 100 --min-edge 0.025
    python3 kalshi_btc_backtest.py --days 14 --no-kelly --verbose
"""

import argparse
import json
import math
import sys
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from kalshi_btc_bot.model       import DistModel
from kalshi_btc_bot.regime      import RegimeEngine
from kalshi_btc_bot.signals     import SignalEngine
from kalshi_btc_bot.vol_surface import KalshiVolTerm
from kalshi_btc_bot.contracts   import otm_distance
from kalshi_btc_bot             import config as C


# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────
BAR_MINUTES   = 5
BARS_PER_HOUR = 60 // BAR_MINUTES

# Scale windows so the regime engine sees sample coverage equivalent to live
# trading. Derived from the live polling interval (config.PRICE_FETCH) — was
# hardcoded // 4 after live polling moved to 2s, leaving both the regime
# windows and the vol down-scaling (sqrt(TIME_SCALE) below) at half the
# correct ratio vs model.py's BARS_PER_HOUR annualization.
TIME_SCALE = (BAR_MINUTES * 60) // C.PRICE_FETCH   # = 150 at 2s polling

# Kalshi's lagged vol window (24h of 5-min bars).
# They price with historical average vol, not moment-to-moment EWMA.
# Wider window = more edge during consolidation periods.
SMA_VOL_WINDOW  = 288   # 24h  — Kalshi's conservative lagged estimate
SHORT_VOL_WINDOW = 12   # 1h   — our "recent" vol for vol_ratio computation

EXPIRY_WINDOWS_H = [0.083, 0.117, 0.167, 0.25, 0.5, 1.0, 2.0, 3.0]
# Kalshi's real KXBTC RANGE bands are 250 wide on the hourlies (500 on the
# weekly), verified against floor_strike/cap_strike on all 200 open markets
# 2026-07-28. 100 simulated a narrower, harder-to-win contract than the one
# that actually trades — see kalshi_btc_bot/contracts.py for the live-side fix.
RANGE_WIDTH      = 250
RANGE_SPAN       = 1250   # same dollar reach as the old 500/100 ladder (5 strikes either side)
KALSHI_SPREAD    = 0.015
SUMMARY_INTERVAL = 50


# ─────────────────────────────────────────────
# SYNTHETIC FEED
# ─────────────────────────────────────────────
class SyntheticFeed:
    """Historical bar data wrapped to mirror the BTCFeed interface."""

    def __init__(self):
        self.prices = []          # (datetime, close)
        self.highs  = []          # (datetime, high)
        self.lows   = []          # (datetime, low)
        self.last   = 0.0
        self.last_high = 0.0
        self.last_low  = 0.0

    def push(self, ts: datetime, close: float,
             high: float = None, low: float = None):
        self.last      = close
        self.last_high = high if high is not None else close
        self.last_low  = low  if low  is not None else close
        self.prices.append((ts, close))
        self.highs.append((ts, self.last_high))
        self.lows.append((ts,  self.last_low))
        cap = max(SMA_VOL_WINDOW + 10, 520)
        if len(self.prices) > cap:
            self.prices = self.prices[-cap:]
            self.highs  = self.highs[-cap:]
            self.lows   = self.lows[-cap:]

    def recent(self, seconds: int) -> list:
        scaled = seconds * TIME_SCALE
        cutoff = (self.prices[-1][0] - timedelta(seconds=scaled)
                  if self.prices else datetime.min)
        return [p for t, p in self.prices if t >= cutoff]

    def momentum(self, seconds: int = 60) -> float:
        r = self.recent(seconds)
        return (r[-1] - r[0]) / r[0] if len(r) >= 2 else 0.0

    def acceleration(self) -> float:
        return self.momentum(30) - self.momentum(60)

    def ewma_volatility(self, lam: float = 0.94) -> float:
        prices = [p for _, p in self.prices[-300:]]
        if len(prices) < 3:
            return 0.001
        rets = [math.log(prices[i] / prices[i-1])
                for i in range(1, len(prices)) if prices[i-1] > 0]
        if len(rets) < 2:
            return 0.001
        var = rets[0] ** 2
        for r in rets[1:]:
            var = lam * var + (1.0 - lam) * r ** 2
        return max(1e-6, math.sqrt(var))

    def sma_volatility(self, window: int = SMA_VOL_WINDOW) -> float:
        """Rolling-window vol — simulates Kalshi's lagged market pricing."""
        prices = [p for _, p in self.prices[-(window + 1):]]
        if len(prices) < 3:
            return self.ewma_volatility()
        rets = [math.log(prices[i] / prices[i-1])
                for i in range(1, len(prices)) if prices[i-1] > 0]
        return max(1e-6, float(np.std(rets))) if len(rets) >= 2 else 0.001

    def vol_ratio(self) -> float:
        """Fast EWMA / SMA(24h Kalshi window). < 0.55 → compressed, Kalshi prices stale."""
        slow = self.sma_volatility(window=SMA_VOL_WINDOW)   # Kalshi's lagged 24h estimate
        fast = self.ewma_volatility()
        return fast / slow if slow > 0 else 1.0

    def volatility(self, seconds: int = 300) -> float:
        return self.ewma_volatility()

    def zscore(self, seconds: int = 300) -> float:
        r = self.recent(seconds)
        if len(r) < 5:
            return 0.0
        arr  = np.array(r)
        std  = arr.std()
        return float((r[-1] - arr.mean()) / std) if std > 0 else 0.0

    def consecutive(self) -> tuple:
        if len(self.prices) < 4:
            return 0, "FLAT"
        recent = [p for _, p in self.prices[-10:]]
        dirs = []
        for i in range(1, len(recent)):
            chg = (recent[i] - recent[i-1]) / recent[i-1]
            dirs.append("UP" if chg > 0.0001 else "DN" if chg < -0.0001 else "FLAT")
        if not dirs:
            return 0, "FLAT"
        last  = dirs[-1]
        count = sum(1 for _ in reversed(dirs) if _ == last)
        return count, last


# ─────────────────────────────────────────────
# SYNTHETIC LADDER
# ─────────────────────────────────────────────
_FLAT_REGIME = {
    "regime": "RANGING", "direction": "NEUTRAL", "conf": 0.5,
    "use_t": False, "mom": 0.0, "accel": 0.0,
    "vol": 0.0002, "vol_h": 0.006, "vol_regime": "NORMAL",
    "zscore": 0.0, "consec": "0FLAT",
    "vol_ratio": 1.0, "vol_compression": False,
}


def build_ladder(spot: float, bar_ts: datetime,
                 dist: DistModel, regime: dict,
                 kalshi_vol: float = None) -> list:
    """
    Reconstruct what the KXBTC RANGE ladder would look like.

    Edge source: Kalshi prices with their long-window SMA vol (lagged/average).
    We use current EWMA. When EWMA << SMA (vol compression), Kalshi overestimates
    vol → assigns too-low probability to RANGE contracts → they're cheap → buy YES.
    """
    our_vol    = regime.get("vol", 0.001)
    kalshi_vol = kalshi_vol or our_vol
    base       = int(round(spot / RANGE_WIDTH)) * RANGE_WIDTH
    flat_regime = {**_FLAT_REGIME, "vol": kalshi_vol}
    ladder = []

    # Kalshi lists KXBTC contracts expiring on the hour. Generating them at fixed
    # offsets from every bar instead made each bar mint a brand-new contract, and
    # since the ticker embedded the BAR timestamp the same strike/expiry got a
    # different ticker string every 5 minutes. That silently defeated three live
    # gates that all key on ticker identity: the `c["ticker"] in existing` skip,
    # _clustered(), and re-entry cooldowns — so the sim could hold and re-buy the
    # same contract repeatedly. Snap to the hourly grid so a contract persists
    # across bars and its hours_left decays exactly as it does live.
    _next_hour = (bar_ts.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    for _h_i in range(1, 6):
        expiry = _next_hour + timedelta(hours=_h_i - 1)
        hours  = (expiry - bar_ts).total_seconds() / 3600.0
        if not (C.MIN_HOURS <= hours <= C.MAX_HOURS):
            continue

        for low in range(base - RANGE_SPAN, base + RANGE_SPAN + RANGE_WIDTH, RANGE_WIDTH):
            if low <= 0:
                continue
            high = low + RANGE_WIDTH
            itm  = low <= spot < high

            if itm:
                otm_d = min(spot - low, high - spot)
            elif spot < low:
                otm_d = spot - low
            else:
                otm_d = high - spot

            if otm_d < -C.MAX_OTM_B:
                continue

            c = {
                "type": "RANGE", "direction": "NEUTRAL",
                "strike": float(low + RANGE_WIDTH / 2), "low": float(low), "high": float(high),
                "label":     f"${low:,}-${high:,}",
                "ticker":    f"KXBTC-SIM-{expiry:%y%b%d%H}-B{low}".upper(),
                "hours":     hours,
                "close_time": expiry.isoformat(),
                "itm":       itm,
                "otm_dist":  otm_d,
            }

            kalshi_p = dist.true_prob(c, spot, kalshi_vol, hours, flat_regime)
            if kalshi_p <= 0:
                continue

            spread  = KALSHI_SPREAD if kalshi_p > 0.20 else KALSHI_SPREAD * 2
            yes_ask = min(0.90, kalshi_p + spread)
            yes_bid = max(0.01, kalshi_p - spread)

            if yes_ask > C.MAX_ASK:
                continue

            c["ask"]    = round(yes_ask, 4)
            c["bid"]    = round(yes_bid, 4)
            c["spread"] = round(spread * 2, 4)
            c["vol"]    = 500
            ladder.append(c)

    return ladder


# ─────────────────────────────────────────────
# INTRABAR STOP HELPER
# ─────────────────────────────────────────────
def _worst_spot(contract: dict, bar_close: float,
                bar_high: float, bar_low: float) -> float:
    """
    Return the spot price within the bar that most hurts this OTM contract.
    If the contract is ITM at bar close, return bar_close (no stop risk from below).
    """
    t = contract["type"]
    if t == "RANGE":
        if bar_close < contract["low"]:    # OTM below range → worst = bar moved lower
            return bar_low
        if bar_close >= contract["high"]:  # OTM above range → worst = bar moved higher
            return bar_high
    elif t == "ABOVE":
        return bar_low     # worst = BTC dropped further below strike
    elif t == "BELOW":
        return bar_high    # worst = BTC rose further above strike
    return bar_close       # ITM — no intrabar stop exposure


def _exit_spread(true_p: float, hours_left: float) -> float:
    """Bid/ask spread — widens near expiry to reflect thinning Kalshi
    liquidity. NOT sufficient on its own to prevent model-priced exits
    from looking artificially profitable — use _exit_bid() for that."""
    base  = 0.010 if true_p > 0.35 else 0.020 if true_p > 0.15 else 0.030
    widen = 1.0 + max(0.0, (0.25 - hours_left) / 0.25) * 2.0  # up to 3x inside final 15 min
    return base * widen


def _ask_at_open(sig: dict, open_spot: float, dist, regime: dict,
                  kalshi_vol: float) -> float | None:
    """Recompute a signal's ASK at next bar's OPEN price. The signal was
    generated at prev bar's CLOSE, but a real order placed at that moment
    would fill on the next tick (approximated by next-bar open). Returns
    None if the recomputed ask is invalid."""
    hours = sig.get("hours", 0)
    if hours <= 0:
        return None
    flat_regime = {**_FLAT_REGIME, "vol": kalshi_vol}
    kalshi_p    = dist.true_prob(sig, open_spot, kalshi_vol, hours, flat_regime)
    if kalshi_p <= 0:
        return None
    spread = KALSHI_SPREAD if kalshi_p > 0.20 else KALSHI_SPREAD * 2
    return min(0.90, kalshi_p + spread)


def _exit_bid(true_p: float, hours_left: float) -> float:
    """Realistic exit bid with adverse-selection haircut.

    The straw-man `bid = true_p - spread/2` used to look profitable near
    expiry only because DistModel.true_prob mechanically converges to 0/1
    as vol_t = vol_h*sqrt(hours_left) -> 0. That's a math artifact, not a
    market you can actually sell into: a Kalshi maker seeing "0.98 with
    3 min left" won't post a $0.97 bid — a last-minute spot flip could still
    flip it to $0. Real markets discount extreme, near-expiry model prices.

    Haircut has two components:
      tau_penalty: grows from 0 (hours >= 0.5) to ~1 at expiry
      extremeness: 0 at true_p=0.5, 1 at true_p in {0, 1}
    Applied jointly so the discount only bites when the model is BOTH
    confident AND close to settlement — the exact regime where the old
    exit price was fabricating profit.
    """
    spread      = _exit_spread(true_p, hours_left)
    tau_penalty = max(0.0, min(1.0, (0.5 - hours_left) / 0.5))
    extremeness = abs(true_p - 0.5) * 2.0
    discount    = 0.15 * tau_penalty * extremeness   # up to 15%
    bid         = true_p - spread / 2 - discount * true_p
    return max(0.01, bid)


# ─────────────────────────────────────────────
# BACKTEST PORTFOLIO
# ─────────────────────────────────────────────
class BacktestPortfolio:
    def __init__(self, capital: float, use_kelly: bool = True):
        self.capital    = capital
        self.cash       = capital
        self.use_kelly  = use_kelly
        self.positions  = {}
        self.cooldowns: dict = {}
        self.trades     = []
        self.realized   = 0.0
        self.peak_total = capital
        self.trade_count = 0
        self._session_day = None

    # Live bot is restarted each session, so SESSION_STOP_PCT resets daily.
    # Without this the backtest treats the full window as one session — a single
    # -3% intraday drawdown halts trading for every remaining bar.
    def reset_session_if_new_day(self, bar_ts: datetime):
        day = bar_ts.date()
        if self._session_day is None:
            self._session_day = day
            return
        if day != self._session_day:
            self._session_day = day
            self.peak_total = self.total()

    def total(self) -> float:
        port = sum(p.get("bid_now", p["entry"]) * p["count"]
                   for p in self.positions.values())
        return self.cash + port

    def exposure(self) -> float:
        return sum(p["cost"] for p in self.positions.values())

    def cooled_off(self, ticker: str, now: datetime) -> bool:
        """False while `ticker` is inside its post-exit re-entry cooldown."""
        until = self.cooldowns.get(ticker)
        return until is None or now >= until

    def can_trade(self) -> bool:
        t = self.total()
        if len(self.positions) >= C.MAX_POSITIONS:
            return False
        if self.peak_total > 0 and t < self.peak_total * (1 - C.SESSION_STOP_PCT):
            return False
        if self.cash < C.MIN_CASH_FLOOR:
            return False
        if self.exposure() >= t * C.MAX_EXPOSURE_PCT:
            return False
        return True

    def _budget(self, ask: float, true_prob: float, is_snipe: bool = False) -> float:
        # Snipes skip Kelly and use a fixed fraction, exactly as live
        # Portfolio.buy() does — tail-probability estimates are too noisy to
        # size off.
        if is_snipe:
            pct = C.SNIPE_TRADE_PCT
        elif self.use_kelly and ask > 0 and ask < 1 and true_prob > ask:
            edge   = true_prob - ask
            f_star = edge / (1.0 - ask)
            pct    = min(C.KELLY_CAP, max(0.005, f_star * C.KELLY_FRACTION))
        else:
            pct = C.MAX_TRADE_PCT
        # Mirrors live Portfolio.budget() exactly: percentage of current total,
        # capped by cash on hand and remaining exposure room. The old version
        # additionally held back a MIN_CASH_PCT reserve (removed live as
        # redundant — MAX_EXPOSURE_PCT already caps deployment at 18%) and
        # clamped every trade to 2x the INITIAL max trade, which stopped the
        # simulation compounding at all while live sizes off current equity.
        t             = self.total()
        max_trade     = t * pct
        exposure_room = t * C.MAX_EXPOSURE_PCT - self.exposure()
        return max(0, min(max_trade, self.cash, exposure_room))

    def buy(self, contract: dict, true_prob: float, bar_ts: datetime,
            is_snipe: bool = False) -> bool:
        ticker = contract["ticker"]
        ask    = contract["ask"]
        if ticker in self.positions:
            return False
        budget = self._budget(ask, true_prob, is_snipe=is_snipe)
        count  = int(budget / ask) if ask > 0 else 0
        cost   = ask * count
        if count <= 0 or cost > self.cash:
            return False
        self.cash -= cost
        self.trade_count += 1
        self.positions[ticker] = {
            "count":          count,
            "entry":          ask,
            "cost":           cost,
            "peak":           ask,
            "true_prob":      true_prob,
            "edge":           true_prob - ask,
            "vol_compression": contract.get("vol_compression", False),
            "vol_term_edge":  contract.get("vol_term_edge", 0.0),
            "contract":       contract,
            "entered_at":     bar_ts.isoformat(),
            "entry_hours":    contract["hours"],
            "bars_held":      0,
            "bid_now":        ask,
            "true_prob_prev": true_prob,
            "true_prob_curr": true_prob,
            "gam":            0.0,
            "is_snipe":       is_snipe,
        }
        return True

    def buy_no(self, contract: dict, true_prob: float, bar_ts: datetime) -> bool:
        ticker  = contract["ticker"]
        # BUY NO matches against YES bid — pay NO_ask = 1 - YES_bid (not 1 - YES_ask)
        yes_bid = contract.get("bid", contract["ask"] - KALSHI_SPREAD * 2)
        no_cost = max(0.01, 1.0 - yes_bid)
        if ticker in self.positions:
            return False
        if no_cost <= 0 or no_cost >= 1.0:
            return False
        budget = min(self.total() * C.NO_TRADE_PCT,
                     self.cash - self.total() * C.MIN_CASH_PCT)
        count  = int(budget / no_cost) if no_cost > 0 else 0
        cost   = no_cost * count
        if count <= 0 or cost > self.cash:
            return False
        self.cash -= cost
        self.trade_count += 1
        self.positions[ticker] = {
            "count":          count,
            "entry":          no_cost,
            "cost":           cost,
            "peak":           no_cost,
            "true_prob":      true_prob,
            "edge":           contract["ask"] - true_prob,
            "contract":       contract,
            "entered_at":     bar_ts.isoformat(),
            "entry_hours":    contract["hours"],
            "bars_held":      0,
            "bid_now":        no_cost,
            "is_no":          True,
            "no_yes_ask":     contract["ask"],
            "true_prob_prev": true_prob,
            "true_prob_curr": true_prob,
            "gam":            0.0,
            "vol_compression": False,
            "vol_term_edge":   0.0,
        }
        return True

    def update(self, spot: float, bar_high: float, bar_low: float,
               bar_i: int, dist: DistModel, regime: dict, bar_ts: datetime,
               kalshi_vol: float = None):
        """Reprice all open positions, tick down time, simulate intrabar stops."""
        vol = regime.get("vol", 0.001)
        for ticker, pos in list(self.positions.items()):
            pos["bars_held"] += 1
            hours_held = pos["bars_held"] / BARS_PER_HOUR
            hours_left = max(0.0, pos["entry_hours"] - hours_held)
            pos["hours_left"] = hours_left

            c = pos["contract"]

            if pos.get("is_no"):
                kv = kalshi_vol or vol
                flat_r = {**_FLAT_REGIME, "vol": kv}
                yes_tp = dist.true_prob(c, spot, kv, hours_left, flat_r)
                # NO exit bid comes from YES ASK. Apply the same haircut logic
                # in reverse — when YES's true_p mechanically collapses toward
                # 0 near expiry, the NO position looks like a guaranteed $1
                # payout, which is a fabricated exit price. Extremeness applies
                # symmetrically: a true_p near 0 means NO is "certain" to pay.
                yes_ask_naive = min(0.95, yes_tp + KALSHI_SPREAD)
                yes_ask_now   = yes_ask_naive
                # Adverse-selection markup on the counterparty side: as YES
                # approaches 0 near expiry, real makers keep the ask elevated
                # rather than collapse it — they don't sell "free money".
                tau_penalty = max(0.0, min(1.0, (0.5 - hours_left) / 0.5))
                extremeness = abs(yes_tp - 0.5) * 2.0
                markup      = 0.15 * tau_penalty * extremeness * (1.0 - yes_tp)
                yes_ask_now = min(0.95, yes_ask_naive + markup)
                no_bid      = max(0.01, 1.0 - yes_ask_now)
                pos["bid_now"]    = no_bid
                pos["no_yes_ask"] = yes_ask_now
                if no_bid > pos["peak"]:
                    pos["peak"] = no_bid
                true_p = dist.true_prob(c, spot, vol, hours_left, regime)
                pos["true_prob_prev"] = pos.get("true_prob_curr", true_p)
                pos["true_prob_curr"] = true_p
                continue

            true_p = dist.true_prob(c, spot, vol, hours_left, regime)
            bid    = _exit_bid(true_p, hours_left)
            pos["bid_now"] = bid
            if bid > pos["peak"]:
                pos["peak"] = bid

            # 2-tick true_prob fade tracking + gamma — mirrors live PositionManager,
            # feeds gamma_lock/peak_giveback/boundary_risk below.
            pos["true_prob_prev"] = pos.get("true_prob_curr", true_p)
            pos["true_prob_curr"] = true_p
            pos["gam"] = dist.gamma(c, spot, vol, hours_left, regime)

            # Intrabar stop simulation: check if the worst intrabar spot would
            # have triggered the stop before end-of-bar. Mirrors live-bot behavior
            # where position_check runs every 8s.
            worst = _worst_spot(c, spot, bar_high, bar_low)
            if worst != spot:
                w_tp  = dist.true_prob(c, worst, vol, hours_left, regime)
                w_bid = _exit_bid(w_tp, hours_left)
                w_pnl = (w_bid - pos["entry"]) / pos["entry"] if pos["entry"] > 0 else 0
                _eh   = pos.get("entry_hours")
                _cov  = (hours_left > C.STOP_MIN_HOURS
                         or (_eh is not None and _eh <= C.STOP_MIN_HOURS))
                # Live gates TIER 6 on `not is_snipe` and on stop coverage; the
                # intrabar simulation stopped everything unconditionally, so the
                # backtest was cutting snipes live would have let run.
                if (not pos.get("is_snipe", False)) and _cov and w_pnl <= -C.STOP_LOSS_PCT:
                    # Stop triggered during the bar. Real stops slip past the
                    # threshold — fill 2c worse than the theoretical stop price
                    # (matches FORCE_EXIT_SLIPPAGE_CENTS used live).
                    stop_bid = pos["entry"] * (1.0 - C.STOP_LOSS_PCT) \
                                - C.FORCE_EXIT_SLIPPAGE_CENTS / 100.0
                    pos["intrabar_stop_bid"] = max(0.01, stop_bid)
                else:
                    pos.pop("intrabar_stop_bid", None)
            else:
                pos.pop("intrabar_stop_bid", None)

    def manage_exits(self, spot: float, bar_ts: datetime, settle_spot: float = None):
        # settle_spot: the spot to use for ITM/OTM check at expiry. If not
        # provided, uses `spot` (current bar close) — but that's a lookahead
        # since expiry happens DURING the bar, not at its end. Callers should
        # pass bar_open (spot at start of bar, closest to expiry timestamp).
        settle_spot = spot if settle_spot is None else settle_spot
        for ticker in list(self.positions.keys()):
            pos = self.positions.get(ticker)
            if not pos:
                continue

            # Intrabar stop fires first — exit at the stop price, not worst bid
            intrabar_bid = pos.pop("intrabar_stop_bid", None)
            if intrabar_bid is not None:
                self._close(ticker, intrabar_bid, "stop_loss", bar_ts)
                continue

            bid        = pos["bid_now"]
            entry      = pos["entry"]
            peak       = pos["peak"]
            hours_left = pos.get("hours_left", 0)
            mins_left  = hours_left * 60
            c          = pos["contract"]
            itm        = c["low"] <= spot < c["high"]
            settle_itm = c["low"] <= settle_spot < c["high"]

            if pos.get("is_no"):
                no_pnl       = (bid - entry) / entry if entry > 0 else 0
                yes_ask_now  = pos.get("no_yes_ask", 1.0 - entry)
                true_p       = pos.get("true_prob_curr", 0.0)
                overpricing  = yes_ask_now / true_p if true_p > 0 else 0.0
                if hours_left <= 0:
                    self._close(ticker, 0.0 if settle_itm else 1.0, "no_expiry_settle", bar_ts)
                elif no_pnl >= C.NO_PROFIT_CAPTURE:
                    self._close(ticker, bid, "no_misprice_captured", bar_ts)
                elif no_pnl >= C.NO_TIME_PROFIT and hours_left < 0.08:
                    self._close(ticker, bid, "no_misprice_time", bar_ts)
                elif overpricing < C.NO_EDGE_GONE_RATIO and no_pnl > 0:
                    self._close(ticker, bid, "no_edge_gone", bar_ts)
                elif no_pnl <= -C.NO_STOP:
                    self._close(ticker, bid, "no_stop", bar_ts)
                continue

            pnl_pct      = (bid - entry) / entry if entry > 0 else 0
            peak_pnl_pct = (peak - entry) / entry if entry > 0 else 0
            drop_peak    = (peak - bid)  / peak   if peak  > 0 else 0
            tp_prev  = pos.get("true_prob_prev", 0.0)
            tp_curr  = pos.get("true_prob_curr", 0.0)
            gam      = pos.get("gam", 0.0)
            dist_val = otm_distance(c, spot)

            # Tier order, gating and thresholds below mirror
            # kalshi_btc_bot/positions.py exactly. Any divergence here shows up
            # as P&L attribution that cannot be compared to live.
            is_snipe = pos.get("is_snipe", False)
            # Live measures the stop against the bid/ask MID so the spread paid
            # at entry is not itself counted as a loss. The backtest has no book,
            # so the model's fair value (true_prob, pre-haircut) is the analogous
            # quantity: _exit_bid() is that value minus spread and haircut.
            mid_pnl_pct = (tp_curr - entry) / entry if entry > 0 else 0
            if hours_left < 0.08:   time_urgency = 1.5
            elif hours_left < 0.15: time_urgency = 1.2
            else:                   time_urgency = 1.0
            entry_hours   = pos.get("entry_hours")
            never_covered = entry_hours is not None and entry_hours <= C.STOP_MIN_HOURS

            reason = None
            # TIER 0.5 — gamma-aware convexity lock
            if (not is_snipe and bid >= C.GAMMA_LOCK_MIN_BID
                    and pnl_pct >= C.GAMMA_LOCK_MIN_PROFIT
                    and tp_curr < tp_prev and abs(gam) >= C.GAMMA_HIGH_THRESHOLD):
                reason = "gamma_lock"
            # TIER 0.75 — peak giveback (snipes only while OTM)
            elif ((not is_snipe or not itm)
                    and peak_pnl_pct >= C.PEAK_GIVEBACK_MIN_PEAK
                    and bid >= C.PEAK_GIVEBACK_MIN_BID
                    and pnl_pct <= peak_pnl_pct * C.PEAK_GIVEBACK_FRACTION):
                reason = "peak_giveback"
            # TIER 1 — scalp lock
            elif (not is_snipe and bid >= C.SCALP_LOCK_MIN_BID
                    and pnl_pct >= C.SCALP_LOCK_PCT and hours_left < 0.25):
                reason = "scalp_lock"
            # TIER 2 — momentum lock
            elif not is_snipe and pnl_pct >= C.MOMENTUM_LOCK_PCT and hours_left < 0.15:
                reason = "momentum_locked"
            # TIER 3 — strong profit
            elif not is_snipe and pnl_pct >= C.STRONG_PROFIT_PCT and hours_left < 0.25:
                reason = "profit_extracted"
            # TIER 3.75 — snipe reversal lock (snipes only)
            elif (is_snipe and bid >= C.SNIPE_PROFIT_LOCK_MIN_BID
                    and peak_pnl_pct >= C.SNIPE_PROFIT_LOCK_PEAK
                    and pnl_pct >= C.SNIPE_PROFIT_LOCK_MIN_PNL
                    and tp_curr < tp_prev):
                reason = "snipe_lock"
            # TIER 3.5 — near settlement
            elif bid >= C.BID_EXIT_THRESHOLD:
                reason = "near_settlement"
                bid    = C.BID_EXIT_THRESHOLD   # exit at 75¢, don't wait for spread noise
            # TIER 4 — mega profit
            elif not is_snipe and pnl_pct >= C.PROFIT_EXIT_MEGA:
                reason = "mega_profit"
            # TIER 5 — time exit while far enough OTM to not flip back
            elif (mins_left < C.TIME_EXIT_MINS and not itm
                    and abs(dist_val) > C.TIME_EXIT_NEAR_DIST):
                reason = "time_exit_OTM"
            # TIER 5.25 — boundary risk: ITM but marginal + underwater + near expiry
            elif (not is_snipe and itm and pnl_pct <= C.BOUNDARY_RISK_MIN_LOSS
                    and mins_left < C.BOUNDARY_RISK_MINS
                    and abs(dist_val) <= C.BOUNDARY_RISK_DIST
                    and (tp_curr < tp_prev or pnl_pct <= C.BOUNDARY_RISK_HARD_STOP)):
                reason = "boundary_risk"
            # TIER 6 — stop loss
            elif (not is_snipe and mid_pnl_pct <= -(C.STOP_LOSS_PCT / time_urgency)
                    and (hours_left > C.STOP_MIN_HOURS or never_covered)
                    and not (itm and mins_left < C.TIME_EXIT_MINS)):
                reason = "stop_loss"
            elif hours_left <= 0:
                reason = "expiry_settle"
                # Use bar_open-based ITM check (settle_spot), not bar_close —
                # expiry happens during the bar so end-of-bar spot is a
                # lookahead. bar_open is a defensible proxy for spot at the
                # actual expiry moment.
                bid    = 1.0 if settle_itm else 0.0
            elif not is_snipe and bid <= 0.005:
                reason = "near_zero"

            if reason:
                self._close(ticker, bid, reason, bar_ts)

    def _close(self, ticker: str, bid: float, reason: str, bar_ts: datetime):
        pos = self.positions.pop(ticker, None)
        if not pos:
            return
        # Live blocks re-entry on the same ticker after every exit — 300s for a
        # loss-cut, 120s otherwise. The backtest had no cooldown at all, so it
        # could re-buy a contract the moment it sold it.
        _loss = reason.startswith("stop_") or "boundary_risk" in reason
        self.cooldowns[ticker] = bar_ts + timedelta(
            seconds=C.STOP_COOLDOWN_SECS if _loss else C.EXIT_COOLDOWN_SECS)
        count = pos["count"]
        pnl   = (bid - pos["entry"]) * count
        self.cash    += bid * count
        self.realized += pnl
        t = self.total()
        if t > self.peak_total:
            self.peak_total = t
        self.trades.append({
            "entered_at":     pos["entered_at"],
            "exited_at":      bar_ts.isoformat(),
            "ticker":         ticker,
            "entry":          round(pos["entry"], 4),
            "exit":           round(bid, 4),
            "count":          count,
            "pnl":            round(pnl, 4),
            "pnl_pct":        round(pnl / pos["cost"] * 100 if pos["cost"] > 0 else 0, 1),
            "reason":         reason,
            "true_prob":      round(pos["true_prob"], 4),
            "edge":           round(pos["edge"], 4),
            "itm_entry":       pos["contract"]["itm"],
            "bars_held":       pos["bars_held"],
            "vol_compression": pos.get("vol_compression", False),
            "vol_term_edge":   round(pos.get("vol_term_edge", 0.0), 5),
            "is_no":           pos.get("is_no", False),
        })


# ─────────────────────────────────────────────
# PERFORMANCE METRICS
# ─────────────────────────────────────────────
def compute_metrics(trades: list, portfolio: BacktestPortfolio) -> dict:
    if not trades:
        return {"total_trades": 0}

    pnls   = [t["pnl"] for t in trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    equity = np.cumsum(pnls) + portfolio.capital
    peak_e = np.maximum.accumulate(equity)
    dd     = (equity - peak_e) / peak_e
    max_dd = float(dd.min()) * 100

    # Sharpe on DAILY equity returns, annualized sqrt(365) (crypto trades every
    # day). Previous calc was mean/std of per-trade dollar P&L x sqrt(252) —
    # wrong on two counts: dollar P&L scales with compounding capital (std
    # inflated by growth, not risk), and sqrt(252) assumes 252 trades/year when
    # the bot does ~25/day.
    daily_pnl: dict = {}
    for t in trades:
        ts = t["exited_at"]
        day = ts[:10] if isinstance(ts, str) else ts.date().isoformat()
        daily_pnl[day] = daily_pnl.get(day, 0.0) + t["pnl"]
    day_keys  = sorted(daily_pnl)
    day_ends  = np.array([daily_pnl[d] for d in day_keys]).cumsum() + portfolio.capital
    day_start = np.concatenate(([portfolio.capital], day_ends[:-1]))
    daily_ret = (day_ends - day_start) / day_start
    sharpe    = (float(daily_ret.mean() / daily_ret.std() * math.sqrt(365))
                 if len(daily_ret) > 1 and daily_ret.std() > 0 else 0.0)

    by_reason: dict = {}
    for t in trades:
        r = t["reason"]
        if r not in by_reason:
            by_reason[r] = {"count": 0, "pnl": 0.0, "wins": 0}
        by_reason[r]["count"] += 1
        by_reason[r]["pnl"]   += t["pnl"]
        if t["pnl"] > 0:
            by_reason[r]["wins"] += 1

    # Vol-term surface trades (entered at preferred expiry by surface signal)
    vt_trades    = [t for t in trades if t.get("vol_term_edge", 0) > 0.0001]
    novt_trades  = [t for t in trades if t.get("vol_term_edge", 0) <= 0.0001]
    vt_wr   = (sum(1 for t in vt_trades  if t["pnl"] > 0) / len(vt_trades)  * 100
               if vt_trades  else 0)
    novt_wr = (sum(1 for t in novt_trades if t["pnl"] > 0) / len(novt_trades) * 100
               if novt_trades else 0)

    # Compression vs normal split
    comp_trades  = [t for t in trades if t.get("vol_compression")]
    norm_trades  = [t for t in trades if not t.get("vol_compression")]
    comp_wr = (sum(1 for t in comp_trades if t["pnl"] > 0) / len(comp_trades) * 100
               if comp_trades else 0)
    norm_wr = (sum(1 for t in norm_trades if t["pnl"] > 0) / len(norm_trades) * 100
               if norm_trades else 0)
    comp_pnl = sum(t["pnl"] for t in comp_trades)
    norm_pnl = sum(t["pnl"] for t in norm_trades)

    win_trades  = [t for t in trades if t["pnl"] > 0]
    loss_trades = [t for t in trades if t["pnl"] <= 0]

    return {
        "total_trades":     len(trades),
        "win_rate":         round(len(wins) / len(trades) * 100, 1),
        "total_pnl":        round(sum(pnls), 4),
        "return_pct":       round((portfolio.total() - portfolio.capital) / portfolio.capital * 100, 2),
        "avg_win":          round(float(np.mean(wins)),   4) if wins   else 0,
        "avg_loss":         round(float(np.mean(losses)), 4) if losses else 0,
        "profit_factor":    round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else float("inf"),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe":           round(sharpe, 2),
        "avg_bars_held":    round(float(np.mean([t["bars_held"] for t in trades])), 1),
        "avg_edge_winners": round(float(np.mean([t["edge"] for t in win_trades])),  4) if win_trades  else 0,
        "avg_edge_losers":  round(float(np.mean([t["edge"] for t in loss_trades])), 4) if loss_trades else 0,
        "final_capital":    round(portfolio.total(), 2),
        "vol_term_trades":    len(vt_trades),
        "vol_term_wr":        round(vt_wr,   1),
        "vol_term_pnl":       round(sum(t["pnl"] for t in vt_trades),  4),
        "no_vol_term_trades": len(novt_trades),
        "no_vol_term_wr":     round(novt_wr, 1),
        "no_vol_term_pnl":    round(sum(t["pnl"] for t in novt_trades), 4),
        "compression_trades": len(comp_trades),
        "compression_wr":   round(comp_wr,  1),
        "compression_pnl":  round(comp_pnl, 4),
        "normal_trades":    len(norm_trades),
        "normal_wr":        round(norm_wr,  1),
        "normal_pnl":       round(norm_pnl, 4),
        "by_exit_reason":   {k: {**v, "win_rate": round(v["wins"]/v["count"]*100, 1)}
                             for k, v in by_reason.items()},
        "no_trades":        sum(1 for t in trades if t.get("is_no")),
        "no_pnl":           round(sum(t["pnl"] for t in trades if t.get("is_no")), 4),
        "no_wr":            round(sum(1 for t in trades if t.get("is_no") and t["pnl"] > 0)
                                  / max(1, sum(1 for t in trades if t.get("is_no"))) * 100, 1),
        "yes_trades":       sum(1 for t in trades if not t.get("is_no")),
        "yes_pnl":          round(sum(t["pnl"] for t in trades if not t.get("is_no")), 4),
        "yes_wr":           round(sum(1 for t in trades if not t.get("is_no") and t["pnl"] > 0)
                                  / max(1, sum(1 for t in trades if not t.get("is_no"))) * 100, 1),
    }


def print_summary(metrics: dict, capital: float):
    print(f"\n{'═'*62}")
    print(f"  📊 BACKTEST RESULTS")
    print(f"{'─'*62}")
    print(f"  Trades:        {metrics['total_trades']}")
    print(f"  Win rate:      {metrics.get('win_rate', 0):.1f}%")
    print(f"  Total P&L:     ${metrics.get('total_pnl', 0):+.4f}")
    print(f"  Return:        {metrics.get('return_pct', 0):+.2f}%")
    print(f"  Profit factor: {metrics.get('profit_factor', 0):.2f}")
    print(f"  Sharpe:        {metrics.get('sharpe', 0):.2f}")
    print(f"  Max drawdown:  {metrics.get('max_drawdown_pct', 0):.1f}%")
    print(f"  Avg hold:      {metrics.get('avg_bars_held', 0):.1f} bars "
          f"({metrics.get('avg_bars_held', 0) * BAR_MINUTES:.0f} min)")
    print(f"  Avg win:       ${metrics.get('avg_win', 0):+.4f}  "
          f"Avg loss: ${metrics.get('avg_loss', 0):+.4f}")
    print(f"  Edge@entry — winners: {metrics.get('avg_edge_winners',0):.1%}  "
          f"losers: {metrics.get('avg_edge_losers',0):.1%}")
    print(f"  Final capital: ${metrics.get('final_capital', capital):.2f} "
          f"(started ${capital:.2f})")
    n_vt   = metrics.get("vol_term_trades", 0)
    n_novt = metrics.get("no_vol_term_trades", 0)
    if n_vt > 0:
        print(f"{'─'*62}")
        print(f"  Vol-surface boosted trades: {n_vt}  "
              f"WR={metrics.get('vol_term_wr', 0):.1f}%  "
              f"P&L=${metrics.get('vol_term_pnl', 0):+.4f}")
        print(f"  Non-boosted trades:         {n_novt}  "
              f"WR={metrics.get('no_vol_term_wr', 0):.1f}%  "
              f"P&L=${metrics.get('no_vol_term_pnl', 0):+.4f}")
    n_comp = metrics.get("compression_trades", 0)
    n_norm = metrics.get("normal_trades", 0)
    if n_comp + n_norm > 0:
        print(f"{'─'*62}")
        print(f"  Vol compression trades: {n_comp}  "
              f"WR={metrics.get('compression_wr',0):.1f}%  "
              f"P&L=${metrics.get('compression_pnl',0):+.4f}")
        print(f"  Normal vol trades:      {n_norm}  "
              f"WR={metrics.get('normal_wr',0):.1f}%  "
              f"P&L=${metrics.get('normal_pnl',0):+.4f}")
    n_no  = metrics.get("no_trades", 0)
    n_yes = metrics.get("yes_trades", 0)
    if n_no > 0:
        print(f"{'─'*62}")
        print(f"  YES trades: {n_yes}  WR={metrics.get('yes_wr',0):.1f}%  P&L=${metrics.get('yes_pnl',0):+.4f}")
        print(f"  NO  trades: {n_no}  WR={metrics.get('no_wr',0):.1f}%  P&L=${metrics.get('no_pnl',0):+.4f}")
    if metrics.get("by_exit_reason"):
        print(f"{'─'*62}")
        print(f"  Exit breakdown:")
        for reason, s in sorted(metrics["by_exit_reason"].items(),
                                 key=lambda x: -x[1]["count"]):
            print(f"    {reason:<28} {s['count']:>3} trades  "
                  f"WR={s['win_rate']:>5.1f}%  P&L=${s['pnl']:+.4f}")
    print(f"{'═'*62}\n")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def run_backtest(days: int = 7, capital: float = 50.0,
                 min_edge: float = None, use_kelly: bool = True,
                 no_stop: bool = False, verbose: bool = False,
                 use_vol_surface: bool = False,
                 no_threshold: float = None):
    try:
        import yfinance as yf
    except ImportError:
        print("yfinance not installed. Run: pip install yfinance")
        sys.exit(1)

    if min_edge is not None:
        C.MIN_EDGE = min_edge
    if no_threshold is not None:
        C.NO_OVERPRICING_MIN = no_threshold
    if no_stop:
        C.STOP_LOSS_PCT  = 999.0   # effectively infinite — stop never fires
        C.STOP_MIN_HOURS = 0.0

    print(f"\n{'═'*62}")
    print(f"  🧪 KALSHI BTC BACKTEST")
    print(f"  Period: {days} days  |  Capital: ${capital:.2f}")
    stop_str = "OFF" if no_stop else f"ON ({C.STOP_LOSS_PCT:.0%})"
    vs_str   = "ON"  if use_vol_surface else "OFF"
    print(f"  Min edge: {C.MIN_EDGE:.1%}  |  Kelly: {'ON' if use_kelly else 'OFF'}  |  Stop: {stop_str}")
    print(f"  Kalshi vol window: {SMA_VOL_WINDOW} bars ({SMA_VOL_WINDOW*BAR_MINUTES//60}h)")
    print(f"  Intrabar stop sim: ON  |  Vol compression: ON  |  Vol surface: {vs_str}")
    print(f"{'═'*62}")

    print("  Fetching BTC-USD 5m OHLCV from yfinance...")
    btc = yf.download("BTC-USD", period=f"{days}d", interval="5m",
                      progress=False, auto_adjust=True)
    if btc.empty:
        print("  ✗ No data returned.")
        sys.exit(1)

    if isinstance(btc.columns, pd.MultiIndex):
        btc.columns = btc.columns.get_level_values(0)

    closes = btc["Close"].dropna()
    highs  = btc["High"].reindex(closes.index).fillna(closes)
    lows   = btc["Low"].reindex(closes.index).fillna(closes)
    opens  = btc["Open"].reindex(closes.index).fillna(closes)
    print(f"  ✓ {len(closes)} bars  ({closes.index[0]} → {closes.index[-1]})")

    feed      = SyntheticFeed()
    regime_e  = RegimeEngine()
    dist      = DistModel()
    signal_e  = SignalEngine(dist)
    portfolio = BacktestPortfolio(capital, use_kelly=use_kelly)
    vol_term  = KalshiVolTerm() if use_vol_surface else None

    # Warm up enough bars for SMA_VOL_WINDOW to stabilize (cap at 1/3 of data)
    WARMUP_BARS = min(SMA_VOL_WINDOW, len(closes) // 3)
    bars_list   = list(zip(closes.index, closes.values,
                           highs.values, lows.values, opens.values))
    warmup      = bars_list[:WARMUP_BARS]
    simulation  = bars_list[WARMUP_BARS:]

    for ts, close, high, low, _open in warmup:
        ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        feed.push(ts, float(close), float(high), float(low))

    print(f"  Warmed up ({WARMUP_BARS} bars = {WARMUP_BARS*BAR_MINUTES//60}h). "
          f"Running {len(simulation)} simulation bars...\n")

    scale = math.sqrt(TIME_SCALE)

    # Pending signals from the PREVIOUS bar's decision, filled at THIS bar's
    # open — removes the lookahead where a signal generated at bar close was
    # also filled at that same bar's close (which requires knowing the close
    # before deciding).
    pending: list = []

    for bar_i, (ts, close, bar_high, bar_low, bar_open) in enumerate(simulation):
        ts       = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        spot     = float(close)
        bar_high = float(bar_high)
        bar_low  = float(bar_low)
        bar_open = float(bar_open)
        feed.push(ts, spot, bar_high, bar_low)

        # Fill any pending signals from the previous bar at this bar's OPEN,
        # using the actual open as the fill price (not the ladder ask that
        # was computed at prev close). Slippage from open vs prev-close is
        # already price movement between decision and fill — realistic.
        for sig_type, sig in pending:
            open_ask = _ask_at_open(sig, bar_open, dist, regime_bt if bar_i > 0 else regime,
                                     kalshi_vol if bar_i > 0 else regime.get("vol", 0.001))
            if open_ask is None or open_ask > C.MAX_ASK:
                continue
            sig_at_open = {**sig, "ask": open_ask,
                           "bid": max(0.01, open_ask - (sig["ask"] - sig["bid"]))}
            if sig_type == "yes":
                portfolio.buy(sig_at_open, sig["true_prob"], ts)
            elif sig_type == "snipe":
                portfolio.buy(sig_at_open, sig["true_prob"], ts, is_snipe=True)
            elif sig_type == "no":
                portfolio.buy_no(sig_at_open, sig["true_prob"], ts)
        pending = []

        regime = regime_e.detect(feed)

        # DistModel expects per-tick vol (config.BARS_PER_HOUR ticks/hour).
        # Backtest bars are 5-min = TIME_SCALE× wider → scale vol down by
        # sqrt(TIME_SCALE); model re-annualizes with sqrt(BARS_PER_HOUR),
        # netting the correct sqrt(12) for 5-min bars.
        regime_bt  = {**regime, "vol": regime["vol"] / scale}
        kalshi_vol = feed.sma_volatility(SMA_VOL_WINDOW) / scale

        portfolio.reset_session_if_new_day(ts)
        portfolio.update(spot, bar_high, bar_low, bar_i, dist, regime_bt, ts,
                         kalshi_vol=kalshi_vol)
        # bar_open is closer to the actual expiry moment than bar_close (which
        # would peek at data from AFTER expiry). Used only for settle_itm.
        portfolio.manage_exits(spot, ts, settle_spot=bar_open)

        if portfolio.can_trade():
            ladder = build_ladder(spot, ts, dist, regime_bt, kalshi_vol)
            # app.py filters recently-exited tickers out of the ladder before
            # every signal scan; mirror that here.
            ladder = [c for c in ladder if portfolio.cooled_off(c["ticker"], ts)]

            # Vol term surface: fit once per bar when ladder is ready.
            # Gives per-expiry Kalshi implied vols → ranks expiries by vol-lag edge.
            # our_vol_h: 5-min EWMA vol scaled to hourly units (12 bars/hour × sqrt(12))
            our_vol_h = feed.ewma_volatility() * math.sqrt(BARS_PER_HOUR)
            if vol_term is not None:
                vs_fitted = vol_term.fit(ladder, spot, our_vol_h)
                if verbose and vs_fitted:
                    print(f"  [{ts:%H:%M}] {vol_term.summary()}")

            sig = signal_e.find_best(
                spot, regime_bt["vol"], regime_bt, ladder, portfolio.positions,
                vol_term=vol_term,
            )
            if sig:
                # Queue for fill at NEXT bar's open (not this bar's close).
                pending.append(("yes", sig))
                if verbose:
                    itm_tag  = "ITM" if sig["itm"] else f"OTM {sig['otm_dist']:+.0f}"
                    comp_tag = " [COMP]" if sig.get("vol_compression") else ""
                    vte_tag  = (f" [VT+{sig.get('vol_term_edge', 0):.4f}]"
                                if sig.get("vol_term_edge", 0) > 0.0001 else "")
                    print(f"  [{ts:%H:%M}] SIG {sig['ticker'][-20:]} "
                          f"ask={sig['ask']:.3f} true={sig['true_prob']:.0%} "
                          f"edge={sig['edge']:.1%} vr={regime_bt.get('vol_ratio',1):.2f} "
                          f"{itm_tag}{comp_tag}{vte_tag}")

            if no_threshold is not None:
                no_sig = signal_e.find_no_scalp(
                    spot, regime_bt["vol"], regime_bt, ladder, portfolio.positions,
                    portfolio.cash, portfolio.capital,
                )
                if no_sig:
                    pending.append(("no", no_sig))

                bno_sig = signal_e.find_boundary_no(
                    spot, regime_bt["vol"], regime_bt, ladder, portfolio.positions,
                    portfolio.cash, portfolio.capital,
                )
                if bno_sig:
                    pending.append(("no", bno_sig))

            # SNIPE — deep-OTM ROI-ranked scan. app.py runs this every tick
            # alongside find_best; the backtest omitted it entirely, so an
            # entire live entry strategy went unmodelled (3 of 6 entries and
            # the only profitable exits in the 2026-07-28 session).
            snipe_sig = signal_e.find_snipe(
                spot, regime_bt["vol"], regime_bt, ladder, portfolio.positions,
            )
            if snipe_sig:
                pending.append(("snipe", snipe_sig))

        if bar_i % SUMMARY_INTERVAL == 0:
            t       = portfolio.total()
            pnl_pct = (t - capital) / capital * 100
            vr      = regime_bt.get("vol_ratio", 1.0)
            comp    = "COMP" if regime_bt.get("vol_compression") else "norm"
            print(f"  [{ts:%m/%d %H:%M}] BTC=${spot:,.0f} | "
                  f"{regime_bt['regime']:9} {regime_bt['direction']:7} "
                  f"vr={vr:.2f}({comp}) | "
                  f"total=${t:.2f} ({pnl_pct:+.1f}%) "
                  f"trades={portfolio.trade_count} open={len(portfolio.positions)}")

    # Force-settle open positions at last bar
    final_ts   = simulation[-1][0]
    final_ts   = final_ts.to_pydatetime() if hasattr(final_ts, "to_pydatetime") else final_ts
    final_spot = float(simulation[-1][1])
    for ticker in list(portfolio.positions.keys()):
        pos = portfolio.positions[ticker]
        c   = pos["contract"]
        itm = c["low"] <= final_spot < c["high"]
        if pos.get("is_no"):
            portfolio._close(ticker, 0.0 if itm else 1.0, "no_force_settle_eob", final_ts)
        else:
            portfolio._close(ticker, 1.0 if itm else pos["bid_now"],
                             "force_settle_eob", final_ts)

    metrics = compute_metrics(portfolio.trades, portfolio)
    print_summary(metrics, capital)

    Path("results").mkdir(exist_ok=True)
    fname = f"results/backtest_{datetime.now():%Y%m%d_%H%M}.json"
    with open(fname, "w") as f:
        json.dump({
            "config": {
                "days": days, "capital": capital,
                "min_edge": C.MIN_EDGE, "use_kelly": use_kelly,
                "sma_vol_window": SMA_VOL_WINDOW,
            },
            "metrics": metrics,
            "trades":  portfolio.trades,
        }, f, indent=2, default=str)
    print(f"  Saved: {fname}\n")
    return metrics


def sweep_no_thresholds(days: int = 60, capital: float = 5000.0,
                        thresholds: list = None):
    if thresholds is None:
        thresholds = [1.10, 1.15, 1.18, 1.20, 1.25, 1.30, 1.40]

    print(f"\n{'═'*74}")
    print(f"  MISPRICE_NO THRESHOLD SWEEP — {len(thresholds)} runs × {days} days  "
          f"capital=${capital:.0f}")
    print(f"{'═'*74}\n")

    results = []
    for thr in thresholds:
        print(f"\n── Threshold {thr:.2f} ──────────────────────────────────────────")
        m = run_backtest(days=days, capital=capital, no_threshold=thr, verbose=False)
        results.append((thr, m))

    print(f"\n{'═'*74}")
    print(f"  SWEEP SUMMARY")
    print(f"{'─'*74}")
    print(f"  {'Thresh':>7}  {'Trades':>6}  {'YES':>4}  {'NO':>4}  "
          f"{'WR%':>5}  {'NO WR%':>7}  {'Return%':>8}  {'Sharpe':>6}  "
          f"{'MaxDD%':>7}  {'NO P&L':>8}")
    print(f"{'─'*74}")
    for thr, m in results:
        print(f"  {thr:>7.2f}  {m.get('total_trades',0):>6}  "
              f"{m.get('yes_trades',0):>4}  {m.get('no_trades',0):>4}  "
              f"{m.get('win_rate',0):>5.1f}  {m.get('no_wr',0):>7.1f}  "
              f"{m.get('return_pct',0):>8.1f}  {m.get('sharpe',0):>6.2f}  "
              f"{m.get('max_drawdown_pct',0):>7.1f}  "
              f"${m.get('no_pnl',0):>+8.2f}")
    print(f"{'═'*74}")

    best_thr, best_m = max(results, key=lambda x: x[1].get("sharpe", 0))
    print(f"\n  Best by Sharpe: threshold={best_thr:.2f}  "
          f"Sharpe={best_m.get('sharpe',0):.2f}  "
          f"Return={best_m.get('return_pct',0):+.1f}%  "
          f"NO trades={best_m.get('no_trades',0)}  "
          f"NO WR={best_m.get('no_wr',0):.1f}%\n")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Kalshi BTC strategy backtest")
    parser.add_argument("--days",         type=int,   default=60,   help="History window (max 60 for 5m). 60 is the default because a 7-day window is far too small to be robust.")
    parser.add_argument("--capital",      type=float, default=10000.0, help="Starting capital ($)")
    parser.add_argument("--min-edge",     type=float, default=None, help="Override MIN_EDGE (e.g. 0.025)")
    parser.add_argument("--no-kelly",     action="store_true",      help="Fixed MAX_TRADE_PCT sizing")
    parser.add_argument("--no-stop",      action="store_true",      help="Disable stop loss")
    parser.add_argument("--vol-surface",  action="store_true",      help="Fit Kalshi implied vol term structure per bar")
    parser.add_argument("--verbose",      action="store_true",      help="Print every trade entry")
    parser.add_argument("--no-threshold", type=float, default=None,
                        help="Enable MISPRICE_NO with this overpricing threshold (e.g. 1.18)")
    parser.add_argument("--no-sweep",     action="store_true",
                        help="Sweep MISPRICE_NO thresholds [1.10,1.15,1.18,1.20,1.25,1.30,1.40]")
    args = parser.parse_args()

    if args.no_sweep:
        sweep_no_thresholds(days=args.days, capital=args.capital)
    else:
        run_backtest(
            days=args.days,
            capital=args.capital,
            min_edge=args.min_edge,
            use_kelly=not args.no_kelly,
            no_stop=args.no_stop,
            verbose=args.verbose,
            use_vol_surface=args.vol_surface,
            no_threshold=args.no_threshold,
        )
