import csv
import datetime
import os
import threading
import time
import uuid
from pathlib import Path

_LOG_PATH = Path(__file__).parent.parent / "trades.csv"
_LOG_FIELDS = ["timestamp", "action", "ticker", "side", "count", "price", "true_prob", "pnl", "reason", "mode"]

from .config import (
    MAX_EXPOSURE_PCT, MAX_POSITIONS, MAX_TRADE_PCT, MIN_CASH_FLOOR,
    MIN_CASH_PCT, NO_TRADE_PCT, PAPER_CAPITAL, PAPER_TRADING,
    EXIT_RETRY_COOLDOWN, FORCE_EXIT_SLIPPAGE_CENTS, SESSION_STOP_PCT,
    UNTRACKED_EXPOSURE_LIMIT, MAX_ASK, STRONG_EDGE_PRICE_IMPROVE,
    ENTRY_PRICE_IMPROVE_CENTS, KELLY_FRACTION, KELLY_CAP, STOP_COOLDOWN_SECS,
    SNIPE_TRADE_PCT,
)

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

        # Guards real_cash/real_port/positions/stop_cooldowns mutation now that
        # entry scanning and position management run on independent threads.
        # Network calls (order placement) stay OUTSIDE this lock so a slow buy
        # never blocks an exit — see positions.py "exits NEVER blocked".
        self.lock = threading.Lock()

        if not _LOG_PATH.exists():
            with open(_LOG_PATH, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=_LOG_FIELDS).writeheader()

    def _log_trade(self, action, ticker, side, count, price, true_prob=None, pnl=None, reason=""):
        row = {
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "action":    action,
            "ticker":    ticker,
            "side":      side,
            "count":     count,
            "price":     round(price, 4),
            "true_prob": round(true_prob, 4) if true_prob is not None else "",
            "pnl":       round(pnl, 4) if pnl is not None else "",
            "reason":    reason,
            "mode":      "paper" if PAPER_TRADING else "live",
        }
        with open(_LOG_PATH, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=_LOG_FIELDS).writerow(row)

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
                    self.peak_total = max(self.peak_total, self.total_value())
        except Exception as e:
            print(f"  ⚠️  Sync failed: {e}")

    def total_value(self) -> float:
        return self.real_cash + self.real_port

    def exposure(self) -> float:
        return sum(p["cost"] for p in self.positions.values())

    def current_exposure(self) -> float:
        return max(self.real_port, self.exposure())

    def can_trade(self) -> bool:
        total = self.total_value()
        if self.peak_total > 0:
            loss_pct = 1 - total / self.peak_total
            if loss_pct > SESSION_STOP_PCT:
                print(f"  🛑 Session stop ({loss_pct:.0%} down, ${total:.2f} vs peak ${self.peak_total:.2f})")
                return False
        if len(self.positions) >= MAX_POSITIONS:
            print(f"  🛑 Max positions ({MAX_POSITIONS})")
            return False
        if self.real_cash < MIN_CASH_FLOOR:
            print(f"  🛑 Cash floor (${self.real_cash:.2f})")
            return False
        if (
            not PAPER_TRADING
            and not self.positions
            and self.real_port > UNTRACKED_EXPOSURE_LIMIT
        ):
            print(f"  🛑 Untracked live exposure (${self.real_port:.2f}); reconcile before new entries")
            return False
        exposure = self.current_exposure()
        if exposure >= total * MAX_EXPOSURE_PCT:
            print(f"  🛑 Max exposure (${exposure:.2f} / ${total * MAX_EXPOSURE_PCT:.2f})")
            return False
        if self.real_cash < total * MIN_CASH_PCT:
            print(f"  🛑 Cash reserve (${self.real_cash:.2f} < ${total * MIN_CASH_PCT:.2f})")
            return False
        return True

    @staticmethod
    def kelly_fraction(true_prob: float, ask: float) -> float:
        """Quarter-Kelly fraction for binary bet, capped at KELLY_CAP.

        Binary Kelly: f* = (p × (1/ask) − 1) / ((1−ask)/ask)
                        = (p − ask) / (1 − ask)
        Quarter-Kelly multiplier keeps us well inside the Kelly curve.
        Falls back to MAX_TRADE_PCT when edge is zero or negative."""
        if ask <= 0 or ask >= 1 or true_prob <= ask:
            return MAX_TRADE_PCT
        edge   = true_prob - ask
        f_star = edge / (1.0 - ask)
        return min(KELLY_CAP, max(0.005, f_star * KELLY_FRACTION))

    def budget(self, trade_pct: float = MAX_TRADE_PCT) -> float:
        total         = self.total_value()
        max_trade     = total * trade_pct
        reserve       = total * MIN_CASH_PCT
        available     = self.real_cash - reserve
        exposure_room = total * MAX_EXPOSURE_PCT - self.current_exposure()
        return max(0, min(max_trade, available, exposure_room))

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
                self.client._request("DELETE", f"/portfolio/orders/{order_id}")
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
            self.client._request("DELETE", f"/portfolio/orders/{order_id}")
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
        price_str = f"{max(0.01, min(0.99, price)):.4f}"
        v2_side = "bid" if action == "buy" else "ask"
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

    def entry_limit_price(self, ask: float, true_prob: float) -> float:
        edge = true_prob - ask
        if edge < STRONG_EDGE_PRICE_IMPROVE:
            return ask
        return min(MAX_ASK, ask + ENTRY_PRICE_IMPROVE_CENTS / 100)

    def buy(self, contract: dict, true_prob: float, is_snipe: bool = False) -> bool:
        """Buy YES contracts. Position size is Kelly-derived (quarter-Kelly, capped)
        for normal entries, or fixed SNIPE_TRADE_PCT for is_snipe entries — Kelly
        sizing off a noisy deep-OTM tail probability isn't trustworthy enough to
        let it drive size on a lottery-ticket bet."""
        ticker = contract["ticker"]
        ask    = contract["ask"]
        limit  = self.entry_limit_price(ask, true_prob)

        with self.lock:
            if ticker in self.positions:
                return False
            kelly_pct = SNIPE_TRADE_PCT if is_snipe else Portfolio.kelly_fraction(true_prob, ask)
            budget    = self.budget(trade_pct=kelly_pct)
            count     = int(budget / limit) if limit > 0 else 0

            # Kelly rounds to 0 — fall back to 1 contract within MAX_TRADE_PCT
            if count <= 0:
                fallback_pct = SNIPE_TRADE_PCT if is_snipe else MAX_TRADE_PCT
                budget = self.budget(trade_pct=fallback_pct)
                count  = int(budget / limit) if limit > 0 else 0

            cost = limit * count
            if cost > self.real_cash or cost > budget or count <= 0:
                return False

            if PAPER_TRADING:
                ask = limit
                cost = ask * count
                self.real_cash -= cost

        if not PAPER_TRADING:
            try:
                result = self.client._request(
                    "POST",
                    "/portfolio/events/orders",
                    json_body=self.order_payload(ticker, "buy", "yes", count, limit),
                )
                filled = float(result.get("fill_count", 0))
                if filled <= 0:
                    improve = f" limit=${limit:.4f}" if limit > ask else ""
                    print(f"  ⚠️  BUY IOC not filled{improve}")
                    return False
                ask   = float(result.get("average_fill_price", ask))
                count = int(filled)
                cost  = ask * count
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
                "contract":       contract,
                "close_time":     contract.get("close_time", ""),
                "is_no":          False,
                "is_snipe":       is_snipe,
            }
        edge     = true_prob - ask
        itm_str  = "✅ITM" if contract["itm"] else ("❌OTM " + str(round(contract["otm_dist"])))
        mode     = "[PAPER] " if PAPER_TRADING else ""
        improve  = f" limit=${limit:.4f}" if limit > contract["ask"] else ""
        tag      = "🎯SNIPE " if is_snipe else ""
        print(f"  📥 {mode}{tag}BUY [{contract['type']:5}] {ticker[-22:]} "
              f"x{count} @ ${ask:.4f}{improve} true={true_prob:.0%} edge={edge:.0%} {itm_str}")
        self._log_trade("buy", ticker, "yes", count, ask, true_prob,
                         reason="snipe" if is_snipe else "")
        return True

    def buy_no(self, contract: dict, true_prob: float) -> bool:
        """Buy NO contracts (fade an overpriced YES)."""
        ticker  = contract["ticker"]
        yes_ask = contract["ask"]
        no_cost = 1.0 - yes_ask

        with self.lock:
            if ticker in self.positions:
                return False
            if no_cost <= 0 or no_cost >= 1.0:
                return False

            budget = self.budget(NO_TRADE_PCT)
            count  = int(budget / no_cost) if no_cost > 0 else 0
            cost   = no_cost * count

            if cost > self.real_cash or cost > budget or count <= 0:
                return False

            if PAPER_TRADING:
                self.real_cash -= cost

        if not PAPER_TRADING:
            try:
                result = self.client._request(
                    "POST",
                    "/portfolio/orders",
                    json_body=self.order_payload(ticker, "buy", "no", count, no_cost),
                )
                order  = result.get("order", {})
                filled = float(order.get("fill_count_fp", 0))
                if filled <= 0:
                    print(f"  ⚠️  BUY_NO IOC not filled: {order.get('status')}")
                    self.cancel_order(order, "BUY_NO")
                    return False
                count = int(filled)
                cost  = no_cost * count
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
                "contract":       contract,
                "close_time":     contract.get("close_time", ""),
                "is_no":          True,
            }
        mode = "[PAPER] " if PAPER_TRADING else ""
        print(f"  📥 {mode}BUY_NO [MISPRICE] {ticker[-22:]} "
              f"x{count} @ NO=${no_cost:.4f} (YES_ask=${yes_ask:.4f}) true={true_prob:.0%}")
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

            if PAPER_TRADING:
                self.real_cash += bid * count
            else:
                now = time.time()
                last_attempt = pos.get("last_exit_attempt", 0)
                if now - last_attempt < EXIT_RETRY_COOLDOWN:
                    return False
                self.positions[ticker]["last_exit_attempt"] = now

        if not PAPER_TRADING:
            filled_count = 0
            proceeds  = 0.0
            side      = "no" if is_no else "yes"
            urgent = any(token in reason for token in (
                "stop", "time", "near_zero", "failed", "forced",
            ))
            order_bid = bid
            if urgent:
                order_bid = max(0.01, bid - FORCE_EXIT_SLIPPAGE_CENTS / 100)
            try:
                result = self.client._request(
                    "POST",
                    "/portfolio/events/orders",
                    json_body=self.order_payload(
                        ticker,
                        "sell",
                        side,
                        requested,
                        order_bid,
                        reduce_only=True,
                    ),
                )
                filled = float(result.get("fill_count", 0))
                if filled > 0:
                    fill_price   = float(result.get("average_fill_price", bid))
                    filled_count = int(filled)
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
                                "/portfolio/events/orders",
                                json_body=self.order_payload(
                                    ticker,
                                    "sell",
                                    "yes",
                                    remaining,
                                    retry_price / 100,
                                    reduce_only=True,
                                ),
                            )
                            r2_filled = float(r2.get("fill_count", 0))
                            if r2_filled > 0:
                                r2_price      = float(r2.get("average_fill_price", retry_price / 100))
                                filled_count += int(r2_filled)
                                proceeds     += r2_price * r2_filled
                                print(f"  🔄 Retry filled {r2_filled:.0f} more @ ${r2_price:.4f}")
                        except:
                            pass

            if filled_count <= 0:
                print(f"  ⚠️  SELL IOC not filled: {ticker[-22:]} reason={reason}")
                return False
            count = min(filled_count, requested)
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
        print(f"  📤 {mode}SELL {emoji} [{reason:22}] {ticker[-22:]} "
              f"x{count} @ ${bid:.4f} pnl=${pnl:+.4f}")
        self._log_trade("sell", ticker, "no" if is_no else "yes", count, bid,
                        pnl=pnl, reason=reason)

        # Any loss-cutting exit (not just literal stop_*) means the signal that
        # justified re-entry is still there — without a cooldown here, the same
        # ticker gets immediately re-bought at escalating Kelly size and whipsaws
        # (observed live 2026-07-03: boundary_risk exits with no cooldown led to
        # 3 re-entries on B62050 in 36 min, -$4.98).
        is_loss_cut = reason.startswith("stop_") or "boundary_risk" in reason
        with self.lock:
            self.positions[ticker]["count"] -= count
            done = self.positions[ticker]["count"] <= 0
            if done:
                del self.positions[ticker]
                if is_loss_cut:
                    self.stop_cooldowns[ticker] = time.time() + STOP_COOLDOWN_SECS
        if done and is_loss_cut:
            print(f"  🚫 Stop cooldown: {ticker[-22:]} blocked for {STOP_COOLDOWN_SECS//60}m")
        return True

    def summary(self):
        total    = self.total_value()
        pnl      = total - self.start_total if self.start_total > 0 else 0
        mode_tag = "📝 PAPER MODE" if PAPER_TRADING else "🔴 LIVE TRADING"
        print(f"\n{'═'*62}")
        print(f"  💰 BTC QUANT v4.3 | {datetime.datetime.now().strftime('%H:%M:%S')} | {mode_tag}")
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
