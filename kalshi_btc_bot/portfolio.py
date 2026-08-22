import csv
import datetime
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

_LOG_PATH = Path(__file__).parent.parent / "trades.csv"
_LOG_FIELDS = ["timestamp", "action", "ticker", "side", "count", "price", "true_prob", "pnl", "peak_pnl_pct", "reason", "mode"]


def _ensure_log_schema() -> None:
    """Guarantee trades.csv on disk carries the current column set.

    The header was only ever written when the file did not already exist, so
    adding a field (peak_pnl_pct) left every row written afterwards one column
    wider than the header it would be read back against. csv.DictReader shifts
    silently rather than erroring: `reason` came back holding the peak number,
    `mode` held the reason, and the real mode was dropped. 66 of 300 rows were
    corrupt on read — enough to poison any analysis of the trade log.

    Migrate legacy rows up to the current field set instead of rotating the
    file, so no trade history is lost, and keep a one-time backup alongside.
    Matching by column name means future field additions or reorders migrate
    on the next start rather than silently desyncing again.
    """
    if not _LOG_PATH.exists():
        with open(_LOG_PATH, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=_LOG_FIELDS).writeheader()
        return

    with open(_LOG_PATH, newline="") as f:
        rows = list(csv.reader(f))
    if rows and rows[0] == _LOG_FIELDS:
        return

    header, body = (rows[0], rows[1:]) if rows else (_LOG_FIELDS, [])
    migrated = []
    for r in body:
        # A row as wide as the current schema was written by current code and
        # is already in _LOG_FIELDS order; anything else predates the change
        # and maps by the on-disk header.
        names = _LOG_FIELDS if len(r) == len(_LOG_FIELDS) else header
        rec   = dict(zip(names, r))
        migrated.append({k: rec.get(k, "") for k in _LOG_FIELDS})

    backup = _LOG_PATH.with_name(f"{_LOG_PATH.stem}.pre-migration.csv")
    if not backup.exists():
        shutil.copyfile(_LOG_PATH, backup)
    with open(_LOG_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_LOG_FIELDS)
        w.writeheader()
        w.writerows(migrated)
    print(f"  🗃  trades.csv migrated to current schema "
          f"({len(migrated)} rows, backup: {backup.name})")

# Only true process-mode flags are bound at import. Every TUNABLE constant is
# read module-qualified as _C.X at call time instead.
#
# WHY THIS MATTERS: `from .config import X` snapshots the value once, at import.
# A sweep that does `config.X = v` then rebinds nothing here, so the sweep runs
# the ORIGINAL value for every candidate and reports a flat/byte-identical
# curve — which reads as "this parameter does not matter" when it was never
# actually varied. That exact bug has now been found three times in this
# codebase (regime.py MOMENTUM_WINDOW_SECS, signals.py, and here). Live
# behaviour was never wrong; the TOOLING silently lied, which is worse, because
# a no-op sweep looks like a finished experiment.
#
# SCOPE, checked rather than assumed: no PAST result is void. Every existing
# sweep drives run_backtest(), and the backtest reads C.X module-qualified
# already (kalshi_btc_backtest.py:885 for the cooldowns), so cooldown_sweep.py
# and friends did vary what they claimed to vary. This closes a latent trap for
# anything that sweeps against the LIVE portfolio, which nothing does yet.
from . import config as _C
from .config import PAPER_CAPITAL, PAPER_TRADING
from .fees import taker_fee
from . import live_view
from . import recorder

_ORDER_CREATE_ENDPOINT = "/portfolio/events/orders"

# ─────────────────────────────────────────────
# PORTFOLIO — syncs from real Kalshi API
# ─────────────────────────────────────────────
class Portfolio:
    def __init__(self, client):
        self.client       = client
        self.positions    = {}
        self.trades       = 0
        self.realized_pnl = 0.0
        self.start_total  = 0.0
        self.peak_total   = 0.0   # running high-water mark — SESSION_STOP_PCT checks
                                  # against this, not start_total, so the breaker stays
                                  # a real drawdown guard after the account has grown
                                  # (start_total alone goes stale the moment equity
                                  # compounds past it — see 2026-07-06 60-day backtest
                                  # audit: -14.1% real drawdown while the "3% breaker"
                                  # never fired because it was still comparing against
                                  # the day-one balance).

        self.real_cash    = 0.0
        self.real_port    = 0.0
        self.stop_cooldowns: dict = {}   # ticker → expiry timestamp after stop loss
        self._session_day = None         # UTC date of the current session — see
                                         # _roll_session(). Without this the
                                         # SESSION_STOP_PCT breaker latches for
                                         # the life of the process.

        # Guards real_cash/real_port/positions/stop_cooldowns mutation now that
        # entry scanning and position management run on independent threads.
        # Network calls (order placement) stay OUTSIDE this lock so a slow buy
        # never blocks an exit — see positions.py "exits NEVER blocked".
        self.lock = threading.Lock()

        _ensure_log_schema()

    def _log_trade(self, action, ticker, side, count, price, true_prob=None,
                   pnl=None, peak_pnl_pct=None, reason=""):
        row = {
            "timestamp":    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "action":       action,
            "ticker":       ticker,
            "side":         side,
            "count":        count,
            "price":        round(price, 4),
            "true_prob":    round(true_prob, 4) if true_prob is not None else "",
            "pnl":          round(pnl, 4) if pnl is not None else "",
            "peak_pnl_pct": round(peak_pnl_pct, 4) if peak_pnl_pct is not None else "",
            "reason":       reason,
            "mode":         "paper" if PAPER_TRADING else "live",
        }
        with open(_LOG_PATH, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=_LOG_FIELDS).writerow(row)

    def _roll_session(self) -> None:
        """Re-baseline the SESSION_STOP_PCT high-water mark on a new UTC day.

        WHY THIS EXISTS. can_trade() halts trading when total_value() is
        SESSION_STOP_PCT below peak_total, and peak_total only ever ratchets UP
        (sync() takes a max against it). Nothing lowered it, so the breaker had
        no way to un-trip: once tripped it stayed tripped for the life of the
        process.

        Observed 2026-08-15. Four consecutive stop_35% exits took the paper book
        to -$17.79 on $500 — past the 3% / $15.00 threshold — at 16:39. The
        process then sat alive and halted for 1 day 18 hours, scanning and
        refusing every signal, until it was noticed.

        The backtest never showed this because BacktestPortfolio._roll_session
        (kalshi_btc_backtest.py:467) re-baselines every simulated day, on the
        stated assumption that "the live bot is restarted each session". That
        assumption is what actually broke: the bot is now left up for days, so
        the backtest was modelling a daily reset the live path did not have —
        the same class of backtest/live divergence as RANGE_WIDTH and the
        momentum window, and it made SESSION_STOP_PCT untestable in the only
        place it was ever validated.

        Re-baselining to CURRENT equity rather than to start_total is
        deliberate: it keeps the breaker a real drawdown guard on the day's
        capital instead of resurrecting a stale day-one number, matching the
        reasoning already recorded on peak_total in __init__.
        """
        day = datetime.datetime.now(datetime.timezone.utc).date()
        if self._session_day is None:
            self._session_day = day
            return
        if day != self._session_day:
            self._session_day = day
            prev, self.peak_total = self.peak_total, self.total_value()
            print(f"  🔄 New session day ({day}) — drawdown baseline "
                  f"${prev:.2f} → ${self.peak_total:.2f}")

    def sync(self):
        if PAPER_TRADING:
            with self.lock:
                if self.start_total == 0.0:
                    self.real_cash   = PAPER_CAPITAL
                    self.real_port   = 0.0
                    self.start_total = PAPER_CAPITAL
                    self.peak_total  = PAPER_CAPITAL
                    print(f"  📊 [PAPER] Session baseline: ${self.start_total:.2f}")
                else:
                    self._roll_session()
                    self.peak_total = max(self.peak_total, self.total_value())
            return
        try:
            b = self.client._request("GET", "/portfolio/balance")
            with self.lock:
                self.real_cash = b.get("balance", 0) / 100
                self.real_port = b.get("portfolio_value", 0) / 100
                if self.start_total == 0.0:
                    self.start_total = self.total_value()
                    self.peak_total  = self.start_total
                    print(f"  📊 Session baseline: ${self.start_total:.2f}")
                else:
                    self._roll_session()
                    self.peak_total = max(self.peak_total, self.total_value())
        except Exception as e:
            print(f"  ⚠️  Sync failed: {e}")

    def total_value(self) -> float:
        return self.real_cash + self.real_port

    def exposure(self) -> float:
        return sum(p["cost"] for p in self.positions.values())

    def market_value(self) -> float:
        """Mark-to-market value of open positions, from the latest liquidation
        price PositionManager records on each cycle (`last_bid`).

        exposure() sums what a position *cost*, not what it is *worth*. Paper
        mode set real_port from exposure() (app.py sync_step), so total_value()
        could not see an unrealized loss at all: four positions bought at $250
        each and collapsed to zero still reported a 0% drawdown, and
        SESSION_STOP_PCT only ever fired on realized P&L. Live mode never had
        this problem — real_port there is Kalshi's own mark-to-market
        portfolio_value — which meant the breaker being validated in paper was
        not the breaker that would run live. Falls back to entry for a position
        the manager has not marked yet (first cycle after a fill)."""
        with self.lock:
            return sum(
                p["count"] * p.get("last_bid", p["entry"])
                for p in self.positions.values()
            )

    def current_exposure(self) -> float:
        return max(self.real_port, self.exposure())

    def can_trade(self) -> bool:
        total = self.total_value()
        if self.peak_total > 0:
            loss_pct = 1 - total / self.peak_total
            if loss_pct > _C.SESSION_STOP_PCT:
                print(f"  🛑 Session stop ({loss_pct:.0%} down, ${total:.2f} vs peak ${self.peak_total:.2f})")
                return False
        if len(self.positions) >= _C.MAX_POSITIONS:
            print(f"  🛑 Max positions ({_C.MAX_POSITIONS})")
            return False
        if self.real_cash < _C.MIN_CASH_FLOOR:
            print(f"  🛑 Cash floor (${self.real_cash:.2f})")
            return False
        if (
            not PAPER_TRADING
            and not self.positions
            and self.real_port > _C.UNTRACKED_EXPOSURE_LIMIT
        ):
            print(f"  🛑 Untracked live exposure (${self.real_port:.2f}); reconcile before new entries")
            return False
        exposure = self.current_exposure()
        if exposure >= total * _C.MAX_EXPOSURE_PCT:
            print(f"  🛑 Max exposure (${exposure:.2f} / ${total * _C.MAX_EXPOSURE_PCT:.2f})")
            return False
        # MIN_CASH_PCT reserve check was here — redundant. MAX_EXPOSURE_PCT
        # already caps positions at 18% of total → cash floor is implicitly
        # 82%. MIN_CASH_FLOOR still guards the absolute-dollar minimum above.
        return True

    @staticmethod
    def kelly_fraction(true_prob: float, ask: float) -> float:
        """Quarter-Kelly fraction for binary bet, capped at KELLY_CAP.

        Binary Kelly: f* = (p × (1/ask) − 1) / ((1−ask)/ask)
                        = (p − ask) / (1 − ask)
        Quarter-Kelly multiplier keeps us well inside the Kelly curve.
        Falls back to MAX_TRADE_PCT when edge is zero or negative."""
        if ask <= 0 or ask >= 1 or true_prob <= ask:
            # No edge → no size. Was MAX_TRADE_PCT — i.e. maximum size on a
            # zero/negative-edge input — only saved by the caller's MIN_EDGE
            # recheck upstream.
            return 0.0
        edge   = true_prob - ask
        f_star = edge / (1.0 - ask)
        return min(_C.KELLY_CAP, max(0.005, f_star * _C.KELLY_FRACTION))

    def budget(self, trade_pct: float = _C.MAX_TRADE_PCT) -> float:
        total         = self.total_value()
        max_trade     = total * trade_pct
        exposure_room = total * _C.MAX_EXPOSURE_PCT - self.current_exposure()
        # MIN_CASH_PCT reserve removed — MAX_EXPOSURE_PCT already caps cash
        # deployment at 82% of total (18% max in positions).
        return max(0, min(max_trade, self.real_cash, exposure_room))

    def live_positions(self) -> list[dict]:
        if PAPER_TRADING:
            return []
        data = self.client._request("GET", "/portfolio/positions", params={"limit": 100})
        positions = []
        for pos in data.get("market_positions", []):
            position = abs(float(pos.get("position_fp") or 0))
            exposure = float(pos.get("market_exposure_dollars") or 0)
            if position > 0 or exposure > 0:
                positions.append(pos)
        return positions

    def cancel_resting_orders(self) -> int:
        if PAPER_TRADING:
            return 0
        try:
            data = self.client._request("GET", "/portfolio/orders", params={"status": "resting"})
        except Exception as e:
            print(f"  ⚠️  Could not fetch resting orders: {e}")
            return 0
        canceled = 0
        for order in data.get("orders", []):
            order_id = order.get("order_id") or order.get("id")
            if not order_id:
                continue
            try:
                self.client._request("DELETE", f"{_ORDER_CREATE_ENDPOINT}/{order_id}")
                canceled += 1
            except Exception as e:
                print(f"  ⚠️  Could not cancel order {order_id}: {e}")
        if canceled:
            print(f"  🧯 Canceled {canceled} resting order(s) at startup")
        return canceled

    def startup_safety_check(self) -> bool:
        if PAPER_TRADING:
            return True
        self.cancel_resting_orders()
        positions = self.live_positions()
        if not positions:
            return True
        print("  🛑 Live positions already exist. Refusing to start unmanaged.")
        for pos in positions:
            print(
                f"     {pos.get('ticker')} position={pos.get('position_fp')} "
                f"exposure=${float(pos.get('market_exposure_dollars') or 0):.2f}"
            )
        return False

    def cancel_order(self, order: dict, label: str) -> None:
        if order.get("status") in {"canceled", "cancelled", "executed", "filled"}:
            return
        order_id = (
            order.get("order_id")
            or order.get("id")
        )
        if not order_id:
            print(f"  ⚠️  {label} resting but no order_id returned")
            return
        try:
            self.client._request("DELETE", f"{_ORDER_CREATE_ENDPOINT}/{order_id}")
            print(f"  🧯 Canceled resting {label}: {order_id}")
        except Exception as e:
            print(f"  ⚠️  Cancel {label} failed: {e}")

    def order_payload(
        self,
        ticker: str,
        action: str,
        side: str,
        count: int,
        price: float,
        reduce_only: bool = False,
    ) -> dict:
        if action not in {"buy", "sell"}:
            raise ValueError(f"unsupported order action: {action!r}")
        if side not in {"yes", "no"}:
            raise ValueError(f"unsupported outcome side: {side!r}")

        # The V2 event-order endpoint is a single YES-leg book: bid means buy
        # YES and ask means sell YES. Keep callers expressed in outcome terms
        # and translate only here. Buying NO at 38c is therefore an ASK at a
        # 62c YES-leg price; selling NO is the complementary BID.
        long_yes = (action == "buy" and side == "yes") or (
            action == "sell" and side == "no"
        )
        v2_side = "bid" if long_yes else "ask"
        yes_leg_price = price if side == "yes" else 1.0 - price
        price_str = f"{max(0.01, min(0.99, yes_leg_price)):.4f}"
        payload = {
            "ticker": ticker,
            "side": v2_side,
            "count": str(count),
            "price": price_str,
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": f"btc-v43-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}",
            "cancel_order_on_pause": True,
        }
        if reduce_only:
            payload["reduce_only"] = True
        return payload

    @staticmethod
    def _parse_fill(result: dict, fallback_price: float,
                    outcome_side: str = "yes") -> tuple:
        """Extract (filled_count, avg_fill_price_dollars) from an order
        response. Kalshi wraps the order as {"order": {...}}; fill-count field
        naming varies (fill_count / fill_count_fp / taker_fill_count) and avg
        price may come as average_fill_price (dollars) or taker_fill_cost
        (total cents), so parse defensively rather than pinning one shape."""
        order  = result.get("order", result)
        filled = 0
        for key in ("fill_count", "fill_count_fp", "taker_fill_count"):
            v = order.get(key)
            if v is not None:
                filled = int(float(v))
                break
        if filled <= 0:
            return 0, 0.0
        avg = order.get("average_fill_price")
        if avg is not None:
            yes_price = float(avg)
            return filled, yes_price if outcome_side == "yes" else 1.0 - yes_price
        cost_c = order.get("taker_fill_cost")
        if cost_c is not None:
            return filled, float(cost_c) / filled / 100.0
        return filled, fallback_price

    def _fresh_quote(self, ticker: str, attempts: int = 3) -> tuple:
        """Fetch the live best bid/ask for `ticker` directly, bypassing the
        ladder's up-to-LADDER_CACHE_SECONDS-old snapshot, so entries price off
        the actual current market rather than a quote that may have moved.
        Returns (0.0, 0.0) on failure so the caller aborts the trade rather
        than acting on stale/fallback data.

        Retries before giving up. This is the FIRST gate every entry passes, and
        a single transient timeout here used to kill a fully-validated signal
        silently — no log line, no recorder entry, nothing to distinguish "the
        market moved" from "one HTTP call blipped". Observed 2026-08-11 on a
        BOUNDARY_NO with z=+3.54 and 1.19x overpricing: signal printed, no order,
        no trace. Aborting on a real quote failure is right; aborting on one
        dropped packet is not.
        """
        last_err = None
        for i in range(max(1, attempts)):
            try:
                m   = self.client._request("GET", f"/markets/{ticker}", timeout=8)
                mkt = m.get("market", m)
                bid = float(mkt.get("yes_bid_dollars") or 0)
                ask = float(mkt.get("yes_ask_dollars") or 0)
                if bid > 0 and ask > 0:
                    return bid, ask
                last_err = f"empty quote (bid={bid}, ask={ask})"
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
            if i + 1 < attempts:
                time.sleep(0.35 * (i + 1))
        self._log_reject(ticker, f"fresh quote failed after {attempts} tries — {last_err}")
        return 0.0, 0.0

    def _log_reject(self, ticker: str, why: str) -> None:
        """Say why an entry was dropped. buy()/buy_no() abort through several
        guards that all used to `return False` silently, so a signal could be
        printed and then vanish with no way to tell which gate stopped it."""
        msg = f"🚫 skipped {ticker[-18:]} — {why}"
        if live_view.ENABLED:
            live_view.log_event(msg)
        else:
            print(f"  {msg}")

    def _orderbook(self, ticker: str) -> dict:
        """Fetch live order-book depth for `ticker`. Only resting bids exist on
        Kalshi's book (both sides are buy orders — "selling" YES is a NO bid) —
        yes_levels are resting buy-YES orders, no_levels are resting buy-NO
        orders. Returns {} on failure so callers can treat it as no liquidity."""
        try:
            book = self.client.get_orderbook(ticker) or {}
            return {"yes": book.get("yes") or [], "no": book.get("no") or []}
        except Exception as e:
            print(f"  ⚠️  orderbook fetch failed {ticker[-18:]}: {e}")
            return {"yes": [], "no": []}

    def executable_exit(self, ticker: str, count: int, is_no: bool) -> tuple:
        """(filled, blended_price) that `count` lots would ACTUALLY clear at.

        The exit ladder in positions.py decides against `1 - yes_ask`, a
        top-of-book quote with no quantity attached, while sell() fills by
        walking real depth. On 2026-08-21 that gap turned an `edge_gone`
        take-profit into -$1.82: the decision saw a winner, 14 lots cleared 13c
        lower. Recorded order attempts put the median gap at 0.00% but p10 at
        -13.7% and the worst at -75.5% — usually free, occasionally ruinous.

        Returns (0, None) when the book is empty so callers can distinguish
        "no liquidity" from "priced at zero".
        """
        levels = self._orderbook(ticker).get("no" if is_no else "yes") or []
        filled, px = self._walk_book(levels, max(1, int(count)))
        return (filled, px) if filled > 0 else (0, None)

    @staticmethod
    def _walk_book(levels: list, target_qty: int, transform=None,
                    limit_price_c: float = None) -> tuple:
        """Simulate an IOC-limit fill against real resting depth instead of
        assuming infinite size at the flat top-of-book quote — paper mode was
        sizing positions (hundreds-to-thousands of contracts) purely off
        dollar budget with no check against what's actually resting in the
        book (typically only 1-4k contracts total, spread across many price
        levels). Walks levels best-price-first, consuming worse levels only
        once better ones are exhausted, so size beyond real depth gets a
        realistically worse blended price instead of a fantasy fill.
        `transform` converts a complementary-side price (cents) into the
        effective price for this side (e.g. buying YES matches NO bids at
        effective price 100-p). `limit_price_c` caps how far the walk can go
        (cents) — a real IOC-limit order never fills worse than its limit, so
        without this cap a thin book can blend the fill price far past what
        the signal was validated against (e.g. a snipe quoted at 17c filling
        at an average of 37c, blowing through SNIPE_MAX_ENTRY_PRICE).
        Returns (filled_qty, avg_price_dollars); (0, 0.0) if no depth at all."""
        if not levels or target_qty <= 0:
            return 0, 0.0
        parsed = [Portfolio._parse_level(lv) for lv in levels]
        parsed = [lv for lv in parsed if lv is not None]
        ordered = sorted(parsed, key=lambda lv: lv[0], reverse=True)
        filled  = 0
        cost_c  = 0.0
        for price_c, qty in ordered:
            if filled >= target_qty:
                break
            eff = transform(price_c) if transform else price_c
            if limit_price_c is not None and eff > limit_price_c:
                break
            take = min(int(qty), target_qty - filled)
            if take <= 0:
                continue
            cost_c += take * eff
            filled += take
        if filled == 0:
            return 0, 0.0
        return filled, (cost_c / filled) / 100.0

    @staticmethod
    def _parse_level(lv) -> tuple | None:
        """Normalize one orderbook level to (price_cents, qty) regardless of
        whether the API returns [price, qty] pairs or {"price":.., "quantity":..}
        objects. Verified against a live KXBTC book 2026-07-28: levels come back
        as [price_cents, "qty_string"] — a float price and a STRING quantity —
        sorted ASCENDING, so top-of-book is the last element, not the first.
        _walk_book re-sorts descending and consumes highest-price-first, which is
        correct for both directions: selling YES wants the highest YES bid, and
        buying YES matches NO bids where the highest NO price is the cheapest
        effective YES. Still fails soft on an unexpected format rather than
        crashing a live sell/buy."""
        try:
            if isinstance(lv, (list, tuple)) and len(lv) >= 2:
                return float(lv[0]), float(lv[1])
            if isinstance(lv, dict):
                price = lv.get("price", lv.get("yes_price", lv.get("no_price")))
                qty   = lv.get("quantity", lv.get("qty", lv.get("count")))
                if price is not None and qty is not None:
                    return float(price), float(qty)
        except (TypeError, ValueError):
            pass
        return None

    def buy(self, contract: dict, true_prob: float, dist=None, spot: float = None,
            vol: float = None, regime: dict = None, is_snipe: bool = False) -> bool:
        """Buy YES contracts. Position size is Kelly-derived (quarter-Kelly, capped)
        for normal entries, or fixed SNIPE_TRADE_PCT for is_snipe entries — Kelly
        sizing off a noisy deep-OTM tail probability isn't trustworthy enough to
        let it drive size on a lottery-ticket bet."""
        ticker    = contract["ticker"]
        bid, ask  = self._fresh_quote(ticker)
        if ask <= 0 or bid <= 0 or ask <= bid or ask > _C.MAX_ASK:
            return False
        spread = ask - bid
        if spread > _C.MAX_SPREAD or spread / ask > _C.MAX_SPREAD_PCT:
            return False
        p_info = None
        if dist is not None and spot is not None and vol is not None and regime is not None:
            p_info = dist.posterior_prob(
                contract, spot, vol, contract.get("hours", 0.0), regime,
                bid=bid, ask=ask,
            )
            true_prob = p_info["true_prob"]
        if true_prob - ask < _C.MIN_EDGE:
            return False
        limit = ask

        no_levels = self._orderbook(ticker)["no"] if PAPER_TRADING else []

        with self.lock:
            if ticker in self.positions:
                return False
            # Re-check under lock: can_trade() runs once per scan tick, but up
            # to 4 signals (YES/NO/BOUNDARY_NO/SNIPE) can each call buy in that
            # tick — without this, MAX_POSITIONS could be exceeded by 3.
            if len(self.positions) >= _C.MAX_POSITIONS:
                return False
            kelly_pct = _C.SNIPE_TRADE_PCT if is_snipe else Portfolio.kelly_fraction(true_prob, ask)
            budget    = self.budget(trade_pct=kelly_pct)
            count     = int(budget / limit) if limit > 0 else 0

            # Kelly rounds to 0 — fall back to 1 contract within MAX_TRADE_PCT
            if count <= 0:
                fallback_pct = _C.SNIPE_TRADE_PCT if is_snipe else _C.MAX_TRADE_PCT
                budget = self.budget(trade_pct=fallback_pct)
                count  = int(budget / limit) if limit > 0 else 0

            cost = limit * count
            if cost > self.real_cash or cost > budget or count <= 0:
                return False
            wanted = count

            if PAPER_TRADING:
                # Cap the fill to real resting depth (buying YES matches NO bids,
                # effective yes price = 1 - no_price) instead of assuming the
                # full Kelly-sized count fills at the flat quoted ask.
                filled, fill_price = self._walk_book(
                    no_levels, count, transform=lambda p: 100 - p,
                    limit_price_c=limit * 100)
                if filled <= 0:
                    print(f"  ⚠️  BUY no depth: {ticker[-22:]} "
                          f"wanted={count} no_levels={no_levels[:3]}")
                    return False
                count = filled
                ask   = fill_price
                cost  = ask * count
                recorder.record_order("buy", ticker, "yes", bid, ask,
                                       {"no": no_levels}, limit, wanted,
                                       filled, fill_price,
                                       reason="snipe" if is_snipe else "",
                                       true_prob=true_prob)
                # Kalshi charges the taker on entry. Omitting it overstated
                # every paper result by ~1.8-2.5% of deployed capital.
                self.real_cash -= cost + (taker_fee(filled, fill_price)
                                          if _C.CHARGE_FEES else 0.0)

        if not PAPER_TRADING:
            try:
                result = self.client._request(
                    "POST",
                    _ORDER_CREATE_ENDPOINT,
                    json_body=self.order_payload(ticker, "buy", "yes", count, limit),
                )
                filled, avg_px = Portfolio._parse_fill(result, ask)
                if filled <= 0:
                    print(f"  ⚠️  BUY IOC not filled (limit=${limit:.4f})")
                    self.cancel_order(result.get("order", result), "BUY")
                    return False
                ask   = avg_px
                count = filled
                cost  = ask * count
                recorder.record_order("buy", ticker, "yes", bid, ask, None,
                                       limit, count, filled, ask,
                                       reason="snipe" if is_snipe else "",
                                       true_prob=true_prob)
                with self.lock:
                    self.real_cash -= cost
                    self.real_port += cost
            except Exception as e:
                body = ""
                if hasattr(e, "response") and e.response is not None:
                    body = f" — {e.response.text}"
                print(f"  ❌ BUY {ticker[-18:]}: {e}{body}")
                return False

        with self.lock:
            self.trades += 1
            self.positions[ticker] = {
                "count":          count,
                "entry":          ask,
                "cost":           cost,
                "peak":           ask,
                "true_prob":      true_prob,
                "true_prob_prev": true_prob,
                "true_prob_curr": true_prob,
                "posterior_prob": true_prob,
                "prior_prob":     (p_info or contract).get("prior_prob"),
                "market_prob":    (p_info or contract).get("market_prob"),
                "market_weight":  (p_info or contract).get("market_weight"),
                "contract":       contract,
                "close_time":     contract.get("close_time", ""),
                # Wall-clock open, for config.MIN_HOLD_SECS. Without it nothing
                # stops a position being opened and closed on consecutive 2s
                # scans, which the live book shows is 0-for-7.
                "opened":         time.time(),
                "is_no":          False,
                "is_snipe":       is_snipe,
                # Hours to expiry at entry — TIER 6 uses this to tell a position
                # held into its final bars from one opened there, which never had
                # stop coverage at all. See positions.py.
                "entry_hours":    float(contract.get("hours") or 0.0),
            }
        edge     = true_prob - ask
        itm_str  = "✅ITM" if contract["itm"] else ("❌OTM " + str(round(contract["otm_dist"])))
        mode     = "[PAPER] " if PAPER_TRADING else ""
        tag      = "🎯SNIPE " if is_snipe else ""
        if live_view.ENABLED:
            live_view.log_trade(
                f"📥 {tag}BUY {ticker[-18:]} x{count} @ ${ask:.3f} "
                f"true={true_prob:.0%} edge={edge:.0%} {itm_str}"
            )
        else:
            print(f"  📥 {mode}{tag}BUY [{contract['type']:5}] {ticker[-22:]} "
                  f"x{count} @ ${ask:.4f} true={true_prob:.0%} edge={edge:.0%} {itm_str}")
        self._log_trade("buy", ticker, "yes", count, ask, true_prob,
                         reason="snipe" if is_snipe else "")
        return True

    def buy_no(self, contract: dict, true_prob: float, dist=None,
               spot: float = None, vol: float = None,
               regime: dict = None) -> bool:
        """Buy NO contracts (fade an overpriced YES)."""
        ticker       = contract["ticker"]
        bid, yes_ask = self._fresh_quote(ticker)
        if yes_ask <= 0 or bid <= 0 or yes_ask <= bid:
            # _fresh_quote already logged the failure reason
            return False
        spread = yes_ask - bid
        if spread > _C.MAX_SPREAD or spread / yes_ask > _C.MAX_SPREAD_PCT:
            self._log_reject(ticker, f"spread ${spread:.3f} ({spread/yes_ask:.0%}) "
                                     f"over MAX_SPREAD ${_C.MAX_SPREAD:.2f}/{_C.MAX_SPREAD_PCT:.0%}")
            return False
        p_info = None
        if dist is not None and spot is not None and vol is not None and regime is not None:
            p_info = dist.posterior_prob(
                contract, spot, vol, contract.get("hours", 0.0), regime,
                bid=bid, ask=yes_ask,
            )
            true_prob = p_info["true_prob"]

        # Re-validate the mispricing against the fresh quote before committing.
        # buy() rechecks MIN_EDGE against the live ask for exactly this reason:
        # the signal was ranked off a ladder snapshot up to
        # LADDER_CACHE_SECONDS old, and _fresh_quote() exists precisely so we
        # don't trade a market that has since moved. This side had no such
        # recheck — if YES cheapened between the scan and order submission the
        # overpricing that justified the fade could be entirely gone and the NO
        # was bought anyway. BOUNDARY_NO carries its own (lower) bar because
        # the z-score extreme supplies independent conviction.
        min_ratio = (_C.BOUNDARY_NO_OVERPRICING_MIN
                     if contract.get("signal") == "BOUNDARY_NO"
                     else _C.NO_OVERPRICING_MIN)
        if true_prob <= 0 or bid / true_prob < min_ratio:
            _r = (bid / true_prob) if true_prob > 0 else 0
            self._log_reject(ticker, f"overpricing {_r:.2f}x fell under {min_ratio:.2f}x "
                                     f"on the fresh quote (bid ${bid:.3f})")
            return False

        # The RATIO gate above does not imply the ABSOLUTE edge gate, and for the
        # contracts BOUNDARY_NO actually trades it is close to vacuous. Since
        # no_cost = 1 - bid:
        #     net_edge = (1 - true_p) - (1 - bid) = bid - true_p
        #     ratio    = bid / true_p
        #     =>  net_edge = true_p * (ratio - 1)
        # At BOUNDARY_NO_OVERPRICING_MIN = 1.15, clearing a 0.05 net edge needs
        # true_p >= 0.333 — but this signal deliberately targets OTM continuation
        # contracts far below that. So a contract selected at true_p 0.10 /
        # bid 0.15 (ratio 1.50) still cleared the fresh-quote recheck after the
        # bid decayed to 0.115, a 23% adverse move, entering at net_edge 0.015 —
        # under a third of the bar find_boundary_no (signals.py) required to
        # select it in the first place.
        #
        # The synthetic fill path already re-checks this at the next bar's open
        # (kalshi_btc_backtest.py:1239). Live did not, so live could fill trades
        # the backtest would refuse — a parity gap in the direction that costs
        # money. Mirrors that check, including applying only to BOUNDARY_NO.
        if contract.get("signal") == "BOUNDARY_NO":
            _net_edge = (1.0 - true_prob) - (1.0 - bid)
            if _net_edge < _C.BOUNDARY_NO_MIN_NET_EDGE:
                self._log_reject(
                    ticker,
                    f"net edge ${_net_edge:.3f} under "
                    f"${_C.BOUNDARY_NO_MIN_NET_EDGE:.3f} on the fresh quote "
                    f"(bid ${bid:.3f}, true {true_prob:.3f})")
                return False

        # Buying NO means lifting a resting YES BID, so what you PAY is
        # 1 - yes_bid (the NO ask). 1 - yes_ask is the NO *bid* — the price you
        # would RECEIVE selling NO — and using it as a buy limit is short by the
        # whole spread, so the order can only fill on a crossed book. That is
        # why zero NO buys ever executed in paper mode. Observed 2026-08-12:
        # yes_bid 33c / yes_ask 39c meant the cheapest NO available was $0.67
        # while the limit went out at $0.61, rejected identically on every retry.
        no_cost = 1.0 - bid                      # NO ask — what a buy actually costs
        no_bid  = 1.0 - yes_ask                  # NO bid — for reference/logging

        yes_levels = self._orderbook(ticker)["yes"] if PAPER_TRADING else []

        with self.lock:
            if ticker in self.positions:
                self._log_reject(ticker, "already holding this contract")
                return False
            if len(self.positions) >= _C.MAX_POSITIONS:
                self._log_reject(ticker, f"MAX_POSITIONS {_C.MAX_POSITIONS} already open")
                return False
            if no_cost <= 0 or no_cost >= 1.0:
                self._log_reject(ticker, f"no_cost ${no_cost:.3f} out of range")
                return False

            budget = self.budget(_C.NO_TRADE_PCT)
            count  = int(budget / no_cost) if no_cost > 0 else 0
            cost   = no_cost * count

            if cost > self.real_cash or cost > budget or count <= 0:
                self._log_reject(ticker, f"size: budget ${budget:.2f} / no_cost ${no_cost:.3f} "
                                         f"-> {count} contracts (cash ${self.real_cash:.2f})")
                return False
            wanted = count

            if PAPER_TRADING:
                # Buying NO matches resting YES bids (effective no price = 1 - yes_price).
                filled, fill_price = self._walk_book(
                    yes_levels, count, transform=lambda p: 100 - p,
                    limit_price_c=no_cost * 100)
                if filled <= 0:
                    self._log_reject(ticker, f"no book depth for NO at <=${no_cost:.2f} "
                                             f"(wanted {count}, yes_levels={yes_levels[:3]})")
                    return False
                count   = filled
                no_cost = fill_price
                cost    = no_cost * count
                # BUY_NO order recording — buy_no() previously never called
                # record_order at all, so NO entries were invisible in
                # orders/*.jsonl and the depth behind them unauditable.
                recorder.record_order(
                    "buy", ticker, "no", bid, yes_ask,
                    {"yes": yes_levels}, 1.0 - bid, wanted, filled, fill_price,
                    reason=contract.get("signal", "MISPRICE_NO"),
                    true_prob=true_prob,
                )
                self.real_cash -= cost + (taker_fee(filled, fill_price)
                                          if _C.CHARGE_FEES else 0.0)

        if not PAPER_TRADING:
            try:
                result = self.client._request(
                    "POST",
                    _ORDER_CREATE_ENDPOINT,
                    json_body=self.order_payload(ticker, "buy", "no", count, no_cost),
                )
                filled, avg_px = Portfolio._parse_fill(result, no_cost, "no")
                if filled <= 0:
                    order = result.get("order", result)
                    print(f"  ⚠️  BUY_NO IOC not filled: {order.get('status')}")
                    self.cancel_order(order, "BUY_NO")
                    return False
                count   = filled
                no_cost = avg_px
                cost    = no_cost * count
                recorder.record_order("buy", ticker, "no", bid, yes_ask, None,
                                       no_cost, count, filled, no_cost,
                                       true_prob=true_prob)
                with self.lock:
                    self.real_cash -= cost
                    self.real_port += cost
            except Exception as e:
                print(f"  ❌ BUY NO: {e}")
                return False

        with self.lock:
            self.trades += 1
            self.positions[ticker] = {
                "count":          count,
                "entry":          no_cost,
                "cost":           cost,
                "peak":           no_cost,
                "true_prob":      true_prob,
                "true_prob_prev": true_prob,
                "true_prob_curr": true_prob,
                "posterior_prob": true_prob,
                "prior_prob":     (p_info or contract).get("prior_prob"),
                "market_prob":    (p_info or contract).get("market_prob"),
                "market_weight":  (p_info or contract).get("market_weight"),
                "contract":       contract,
                "close_time":     contract.get("close_time", ""),
                "opened":         time.time(),
                "is_no":          True,
            }
        mode = "[PAPER] " if PAPER_TRADING else ""
        sig  = contract.get("signal", "MISPRICE_NO")
        if live_view.ENABLED:
            live_view.log_trade(
                f"📥 BUY_NO {ticker[-18:]} x{count} @ ${no_cost:.3f} "
                f"(YES_ask=${yes_ask:.3f}) true={true_prob:.0%}"
            )
        else:
            print(f"  📥 {mode}BUY_NO [{sig}] {ticker[-22:]} "
                  f"x{count} @ NO=${no_cost:.4f} (YES_ask=${yes_ask:.4f}) true={true_prob:.0%}")
        # buy() logs every fill to trades.csv; buy_no() only ever called
        # recorder.record_order(), so NO ENTRIES WERE MISSING FROM trades.csv
        # entirely (0 buys logged against 2 sells as of 2026-08-09). That left
        # every NO position as an unmatched sell — which is what produced the
        # phantom "open positions" in FIFO reconstruction, and made NO entry
        # prices and edge unauditable from the trade log.
        self._log_trade("buy", ticker, "no", count, no_cost, true_prob,
                        reason=sig)
        return True

    def settle_paper_position(self, ticker: str, payout: float) -> bool:
        """Credit final settlement payout directly and remove position — bypasses
        the orderbook walk that regular sell() uses. A settled contract has no
        resting depth (there's nothing left to fill against), so routing through
        sell() would fail with "no depth" and leave the position stuck in state
        forever, corrupting cash accounting and the dashboard's OPEN POSITIONS
        panel. This path treats Kalshi's own settlement as the fill: $1 × count
        if won (payout=1.0), $0 if lost (payout=0.0)."""
        with self.lock:
            if ticker not in self.positions:
                return False
            pos     = self.positions[ticker]
            count   = pos["count"]
            proceeds = payout * count
            pnl     = (payout - pos["entry"]) * count
            self.real_cash += proceeds
            self.real_port = max(0, self.real_port - pos["cost"])
            self.realized_pnl += pnl
            is_no = pos.get("is_no", False)
            del self.positions[ticker]
        emoji = "✅" if pnl > 0 else "❌"
        if live_view.ENABLED:
            live_view.log_trade(
                f"🏁 SETTLED {emoji} {ticker[-18:]} x{count} @ ${payout:.2f} pnl=${pnl:+.2f}"
            )
        else:
            print(f"  🏁 [PAPER] SETTLED {emoji} {ticker[-22:]} "
                  f"x{count} @ ${payout:.2f} pnl=${pnl:+.4f}")
        live_view.drop_position(ticker)
        peak_val = pos.get("peak_bid", pos["peak"])
        peak_pct = (peak_val - pos["entry"]) / pos["entry"] if pos["entry"] > 0 else 0
        self._log_trade("sell", ticker, "no" if is_no else "yes", count, payout,
                        pnl=pnl, peak_pnl_pct=peak_pct, reason="expired_settled")
        return True

    def sell(self, ticker: str, bid: float,
             count: int = None, reason: str = "") -> bool:
        with self.lock:
            if ticker not in self.positions:
                return False
            pos   = self.positions[ticker]
            count = count or pos["count"]
            count = min(count, pos["count"])
            requested = count
            is_no = pos.get("is_no", False)

            if not PAPER_TRADING:
                now = time.time()
                last_attempt = pos.get("last_exit_attempt", 0)
                if now - last_attempt < _C.EXIT_RETRY_COOLDOWN:
                    return False
                self.positions[ticker]["last_exit_attempt"] = now

        urgent = any(token in reason for token in (
            "stop", "time", "near_zero", "failed", "forced",
        ))
        if urgent:
            fresh_bid, fresh_ask = self._fresh_quote(ticker, attempts=1)
            fresh_exit = (max(0.0, 1.0 - fresh_ask)
                          if is_no and fresh_ask > 0 else fresh_bid)
            if fresh_exit > bid:
                bid = fresh_exit

        if PAPER_TRADING:
            # Closing a YES long matches resting YES bids directly (no price
            # transform); closing a NO long matches resting NO bids directly.
            levels = self._orderbook(ticker)["no" if is_no else "yes"]
            filled, fill_price = self._walk_book(levels, requested)
            if filled <= 0:
                print(f"  ⚠️  SELL no depth: {ticker[-22:]} reason={reason}")
                return False
            recorder.record_order("sell", ticker, "no" if is_no else "yes",
                                   bid, 0.0,
                                   {"no" if is_no else "yes": levels},
                                   bid, requested, filled, fill_price,
                                   reason=reason)
            with self.lock:
                if ticker not in self.positions:
                    return False
                count = min(filled, self.positions[ticker]["count"])
                # Cash and P&L must use the depth-walk fill recorded above. A
                # fresh top quote has no quantity attached, so crediting the
                # whole exit at max(fill_price, quote) can book proceeds that
                # were never available (and makes orders/*.jsonl disagree with
                # trades.csv).
                bid = fill_price
                # Early exits are taker orders too and pay a SECOND fee.
                # Settlement is free and does not route through here — see
                # settle_paper_position().
                self.real_cash += bid * count - (taker_fee(count, bid)
                                                 if _C.CHARGE_FEES else 0.0)

        if not PAPER_TRADING:
            filled_count = 0
            proceeds  = 0.0
            side      = "no" if is_no else "yes"
            order_bid = bid
            if urgent:
                order_bid = max(0.01, bid - _C.FORCE_EXIT_SLIPPAGE_CENTS / 100)
            try:
                result = self.client._request(
                    "POST",
                    _ORDER_CREATE_ENDPOINT,
                    json_body=self.order_payload(
                        ticker,
                        "sell",
                        side,
                        requested,
                        order_bid,
                        reduce_only=True,
                    ),
                )
                filled, fill_price = Portfolio._parse_fill(result, bid, side)
                if filled > 0:
                    filled_count = filled
                    proceeds    += fill_price * filled_count
            except Exception as e:
                body = ""
                if hasattr(e, "response") and e.response is not None:
                    body = f" — {e.response.text}"
                print(f"  ⚠️  SELL {ticker[-18:]}: {e}{body}")
                return False
            # Retry unfilled remainder (YES only — NO retry pricing is complex)
            if not is_no:
                remaining = requested - filled_count
                if remaining > 0:
                    # Anchor the retry price off the actual primary fill (if any),
                    # not the stale target bid — avoids under/over-cutting the cross.
                    anchor = (proceeds / filled_count) if filled_count > 0 else bid
                    if anchor > 0.01:
                        retry_price = max(1, int(round(anchor * 100)) - 1)
                        try:
                            r2 = self.client._request(
                                "POST",
                                _ORDER_CREATE_ENDPOINT,
                                json_body=self.order_payload(
                                    ticker,
                                    "sell",
                                    "yes",
                                    remaining,
                                    retry_price / 100,
                                    reduce_only=True,
                                ),
                            )
                            r2_filled, r2_price = Portfolio._parse_fill(r2, retry_price / 100)
                            if r2_filled > 0:
                                filled_count += r2_filled
                                proceeds     += r2_price * r2_filled
                                print(f"  🔄 Retry filled {r2_filled} more @ ${r2_price:.4f}")
                        except:
                            pass

            if filled_count <= 0:
                print(f"  ⚠️  SELL IOC not filled: {ticker[-22:]} reason={reason}")
                return False
            count = min(filled_count, requested)
            recorder.record_order("sell", ticker, "no" if is_no else "yes",
                                   bid, 0.0, None, order_bid, requested,
                                   filled_count, proceeds / filled_count,
                                   reason=reason)
            # bid becomes the proceeds-weighted average fill price across the
            # primary + retry orders — previously this stayed pinned to the
            # primary order's price even when the retry filled at a different
            # price, overstating both real_cash and the logged/printed pnl.
            bid = proceeds / filled_count
            with self.lock:
                cost_basis = pos["cost"] * (count / pos["count"]) if pos["count"] else 0
                self.real_cash += proceeds
                self.real_port = max(0, self.real_port - cost_basis)

        pnl = (bid - pos["entry"]) * count
        self.realized_pnl += pnl

        emoji = "✅" if pnl > 0 else "❌"
        mode  = "[PAPER] " if PAPER_TRADING else ""
        if live_view.ENABLED:
            live_view.log_trade(
                f"📤 SELL {emoji} [{reason.strip():18}] {ticker[-18:]} "
                f"x{count} @ ${bid:.3f} pnl=${pnl:+.2f}"
            )
        else:
            print(f"  📤 {mode}SELL {emoji} [{reason:22}] {ticker[-22:]} "
                  f"x{count} @ ${bid:.4f} pnl=${pnl:+.4f}")
        peak_val = pos.get("peak_bid", pos["peak"])
        peak_pct = (peak_val - pos["entry"]) / pos["entry"] if pos["entry"] > 0 else 0
        self._log_trade("sell", ticker, "no" if is_no else "yes", count, bid,
                        pnl=pnl, peak_pnl_pct=peak_pct, reason=reason)

        # Any loss-cutting exit (not just literal stop_*) means the signal that
        # justified re-entry is still there — without a cooldown here, the same
        # ticker gets immediately re-bought at escalating Kelly size and whipsaws
        # (observed live 2026-07-03: boundary_risk exits with no cooldown led to
        # 3 re-entries on B62050 in 36 min, -$4.98).
        #
        # Classified on ACTUAL realized pnl sign, not the exit-reason string.
        # The reason-based version mislabeled time_exit_OTM as a non-loss (0%
        # WR in every 2026-08 backtest run, but its reason string matches
        # neither "stop_" nor "boundary_risk") and couldn't catch an
        # occasional loss from a normally-profitable tier. 2026-08-04
        # cooldown_sweep.py: with EXIT_COOLDOWN_SECS -> 0 for real wins (loss
        # cooldown left at 300s), Sharpe improved on BOTH an in-sample tuning
        # window and a held-out validation window it never touched — a
        # confirmed edge, not an in-sample artifact.
        is_loss_cut = pnl < 0
        with self.lock:
            _pos = self.positions.get(ticker)
            if _pos is None:
                done = True
            else:
                # Retire cost basis alongside the contracts it paid for. Only
                # `count` was decremented here, so a partially-filled exit left
                # the position holding its full original cost: exposure() then
                # overstated the book (400 of 1000 contracts sold still read
                # $250 rather than $150) and the *next* sell's cost_basis —
                # pos["cost"] * (count / pos["count"]) — over-decremented
                # real_port by that same stale amount. Partial fills are
                # routine on both paths: a live IOC can fill short, and
                # _walk_book deliberately fills only to real depth in paper.
                prev = _pos["count"]
                if prev > 0:
                    _pos["cost"] *= (prev - count) / prev
                _pos["count"] = prev - count
                done = _pos["count"] <= 0
            if done:
                self.positions.pop(ticker, None)
                # Every exit now sets a cooldown, not just loss-cuts. A
                # profit-lock previously left the ticker immediately re-buyable,
                # so the bot re-chased the contract it had just sold at a worse
                # price. Loss-cuts still get the longer block.
                cooldown = _C.STOP_COOLDOWN_SECS if is_loss_cut else _C.EXIT_COOLDOWN_SECS
                self.stop_cooldowns[ticker] = time.time() + cooldown
        if done:
            live_view.drop_position(ticker)
        if done:
            _secs = _C.STOP_COOLDOWN_SECS if is_loss_cut else _C.EXIT_COOLDOWN_SECS
            _kind = "Stop" if is_loss_cut else "Re-entry"
            if live_view.ENABLED:
                live_view.log_event(f"🚫 {_kind} cooldown {ticker[-18:]} ({_secs//60}m)")
            else:
                print(f"  🚫 {_kind} cooldown: {ticker[-22:]} blocked for {_secs//60}m")
        return True

    def summary(self):
        if live_view.ENABLED:
            return
        total    = self.total_value()
        pnl      = total - self.start_total if self.start_total > 0 else 0
        mode_tag = "📝 PAPER MODE" if PAPER_TRADING else "🔴 LIVE TRADING"
        print(f"\n{'═'*62}")
        print(f"  💰 BTC QUANT v5.0 | {datetime.datetime.now().strftime('%H:%M:%S')} | {mode_tag}")
        print(f"{'─'*62}")
        label = "Simulated" if PAPER_TRADING else "Real"
        print(f"  Cash ({label}): ${self.real_cash:>7.2f} | Positions:     ${self.exposure():>7.2f}")
        pct = (pnl/self.start_total*100) if self.start_total > 0 else 0.0
        print(f"  Total:        ${total:>7.2f} | P&L:          ${pnl:>+7.2f} ({pct:>+.1f}%)")
        print(f"  Trades: {self.trades} | Realized: ${self.realized_pnl:>+.2f}")
        if self.positions:
            print(f"{'─'*62}")
            for t, p in self.positions.items():
                c    = p["contract"]
                side = "NO" if p.get("is_no") else "YES"
                print(f"  {t[-24:]:<24} x{p['count']:>3} {side} @ ${p['entry']:.4f} "
                      f"[{c['type']}] {c['label']} true={p['true_prob']:.0%}")
        print(f"{'═'*62}\n")
