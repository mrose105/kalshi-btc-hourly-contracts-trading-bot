import datetime

def _hours_from(close_time: str) -> float:
    """Hours until (positive) or since (negative) close_time."""
    try:
        ct = datetime.datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        return (ct - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / 3600
    except Exception:
        return 1.0

from .config import (
    BID_EXIT_THRESHOLD, BOUNDARY_RISK_DIST, BOUNDARY_RISK_HARD_STOP,
    BOUNDARY_RISK_MIN_LOSS, BOUNDARY_RISK_MINS, GAMMA_HIGH_THRESHOLD,
    GAMMA_LOCK_MIN_BID, GAMMA_LOCK_MIN_PROFIT,
    NO_EDGE_GONE_RATIO, NO_PROFIT_CAPTURE, NO_STOP, NO_TIME_PROFIT,
    MOMENTUM_LOCK_PCT, PAPER_TRADING, PEAK_GIVEBACK_FRACTION, PEAK_GIVEBACK_MIN_BID,
    PEAK_GIVEBACK_MIN_PEAK, PROFIT_EXIT_MEGA, SCALP_LOCK_MIN_BID, SCALP_LOCK_PCT,
    SNIPE_PROFIT_LOCK_MIN_BID, SNIPE_PROFIT_LOCK_PCT, STOP_LOSS_PCT,
    STOP_MIN_HOURS, STRONG_PROFIT_PCT, TIME_EXIT_MINS, TIME_EXIT_NEAR_DIST,
)
from .contracts import is_in_money, otm_distance
from . import live_view

# ─────────────────────────────────────────────
# POSITION MANAGER — exits NEVER blocked
# ─────────────────────────────────────────────
class PositionManager:
    def __init__(self, client, portfolio, dist, feed):
        self.client    = client
        self.portfolio = portfolio
        self.dist      = dist
        self.feed      = feed

    def get_price(self, ticker):
        try:
            m   = self.client._request("GET", f"/markets/{ticker}", timeout=8)
            mkt = m.get("market", m)
            bid    = float(mkt.get("yes_bid_dollars") or 0)
            ask    = float(mkt.get("yes_ask_dollars") or 0)
            ct     = mkt.get("close_time", "")
            status = mkt.get("status", "")
            return bid, ask, ct, status
        except:
            return 0.0, 0.0, "", ""

    def manage(self, spot: float, vol: float, regime: dict):
        """
        ALWAYS runs regardless of session stop or any other gate.
        Exits are unconditional.
        """
        for ticker in list(self.portfolio.positions.keys()):
            pos = self.portfolio.positions.get(ticker)
            if not pos:
                continue

            bid, ask, close_time, status = self.get_price(ticker)

            # Detect settled/expired contracts and purge from local tracking.
            # Live mode: Kalshi auto-credits the settlement payout, next portfolio.sync()
            # reflects it, so a plain delete is correct. Paper mode has no real exchange
            # crediting cash — sync_step() never calls portfolio.sync() for paper — so we
            # must credit/debit the settlement value ourselves via portfolio.sell() or
            # paper P&L silently loses the cost basis on every settled position.
            _SETTLED = {"finalized", "settled", "closed", "determined"}
            _expired = _hours_from(close_time) < -0.05 and bid == 0 and ask == 0
            if status in _SETTLED or _expired:
                itm_flag = is_in_money(pos["contract"], spot)
                if PAPER_TRADING:
                    is_no = pos.get("is_no", False)
                    won   = (not itm_flag) if is_no else itm_flag
                    # settle_paper_position() logs the outcome AND removes the
                    # position — no separate print() needed here.
                    self.portfolio.settle_paper_position(
                        ticker, 1.0 if won else 0.0,
                    )
                else:
                    print(f"  🏁 SETTLED {ticker[-22:]} status={status or 'expired'} "
                          f"ITM={'✅' if itm_flag else '❌'} — removing from tracking")
                    with self.portfolio.lock:
                        self.portfolio.positions.pop(ticker, None)
                    live_view.drop_position(ticker)
                continue

            if ask <= 0:
                # No sellers — still refresh snapshot so dashboard shows live dist/mins_left
                if live_view.ENABLED:
                    _e = pos.get("entry", 0)
                    _c = pos.get("contract", "")
                    _h = max(0.0, _hours_from(close_time))
                    live_view.update_position(ticker, {
                        "bid": bid,
                        "pnl_pct": (bid - _e) / _e if _e > 0 else 0,
                        "true_prob": self.dist.true_prob(_c, spot, vol, _h, regime),
                        "itm": is_in_money(_c, spot),
                        "dist": otm_distance(_c, spot),
                        "mins_left": _h * 60,
                        "is_snipe": pos.get("is_snipe", False),
                    })
                continue
            if bid > 0 and bid >= ask:
                print(f"  ⚠️  {ticker[-18:]} crossed quote bid=${bid:.4f} >= ask=${ask:.4f} — skipping this cycle")
                continue

            entry    = pos["entry"]
            peak     = pos.get("peak", entry)
            contract = pos["contract"]
            is_no    = pos.get("is_no", False)

            # YES: track YES mid. NO: track NO value (= 1 - YES ask).
            # Previously used YES mid for both — wrong for NO peak tracking.
            mid = (1.0 - ask) if is_no else ((bid + ask) / 2 if bid > 0 else ask)

            if mid > peak:
                self.portfolio.positions[ticker]["peak"] = mid
                peak = mid

            # Hours left
            hours     = max(0.0, _hours_from(close_time))
            mins_left = hours * 60

            # True prob + rolling 2-tick tracking
            true_prob = self.dist.true_prob(contract, spot, vol, hours, regime)
            tp_prev   = pos.get("true_prob_prev", true_prob)
            tp_curr   = pos.get("true_prob_curr", true_prob)
            self.portfolio.positions[ticker]["true_prob"]      = true_prob
            self.portfolio.positions[ticker]["true_prob_prev"] = tp_curr
            self.portfolio.positions[ticker]["true_prob_curr"] = true_prob

            itm  = is_in_money(contract, spot)
            dist = otm_distance(contract, spot)

            # Time-decay urgency (CHANGE 3)
            if hours < 0.08:   time_urgency = 1.5
            elif hours < 0.15: time_urgency = 1.2
            else:              time_urgency = 1.0

            # ── NO POSITION (MISPRICE_NO exits) ──────────────────────────
            if is_no:
                no_mid     = mid             # = 1.0 - ask, already computed above
                no_bid_px  = mid             # what we receive selling NO
                no_pnl_pct = (no_mid - entry) / entry if entry > 0 else 0
                overprice_r = ask / true_prob if true_prob > 0 else 0

                repriced   = no_pnl_pct > 0.15
                rep_str    = "repriced:YES ⬆" if repriced else "repriced:NO  "

                if live_view.ENABLED:
                    live_view.update_position(ticker, {
                        "bid": bid, "pnl_pct": no_pnl_pct, "true_prob": true_prob,
                        "itm": itm, "dist": dist, "mins_left": mins_left,
                        "is_snipe": False,
                    })
                else:
                    print(f"  👁  {ticker[-22:]:<22} bid=${bid:.4f} "
                          f"pnl={no_pnl_pct:+.0%} true={true_prob:.0%} "
                          f"{'✅' if itm else '❌'} dist={dist:+.0f} "
                          f"{rep_str} {mins_left:.0f}m left")

                if no_pnl_pct >= NO_PROFIT_CAPTURE:
                    self.portfolio.sell(ticker, no_bid_px, reason="misprice_captured ✅")
                    continue
                if no_pnl_pct >= NO_TIME_PROFIT and hours < 0.08:
                    self.portfolio.sell(ticker, no_bid_px, reason="misprice_time 💰")
                    continue
                if overprice_r < NO_EDGE_GONE_RATIO and no_pnl_pct > 0:
                    self.portfolio.sell(ticker, no_bid_px, reason="edge_gone ✅")
                    continue
                if no_pnl_pct <= -NO_STOP:
                    self.portfolio.sell(ticker, no_bid_px, reason="misprice_failed ❌")
                    continue
                if hours < 0.03:
                    self.portfolio.sell(ticker, no_bid_px, reason="time_forced_no")
                    continue
                continue

            # ── YES POSITION (unified tiered ladder) ─────────────────────
            pnl_pct      = (bid - entry) / entry if entry > 0 else 0
            peak_pnl_pct = (peak - entry) / entry if entry > 0 else 0
            gam      = self.dist.gamma(contract, spot, vol, hours, regime)
            is_snipe = pos.get("is_snipe", False)

            repriced = pnl_pct > 0.15
            rep_str  = "repriced:YES ⬆" if repriced else "repriced:NO  "
            snipe_tag = " 🎯SNIPE" if is_snipe else ""

            if live_view.ENABLED:
                live_view.update_position(ticker, {
                    "bid": bid, "pnl_pct": pnl_pct, "true_prob": true_prob,
                    "itm": itm, "dist": dist, "mins_left": mins_left,
                    "is_snipe": is_snipe,
                })
            else:
                print(f"  👁  {ticker[-22:]:<22} bid=${bid:.4f} "
                      f"pnl={pnl_pct:+.0%} true={true_prob:.0%} gam={gam:+.1f} "
                      f"{'✅' if itm else '❌'} dist={dist:+.0f} "
                      f"{rep_str} {mins_left:.0f}m left{snipe_tag}")

            # SNIPE positions skip most early profit-lock/stop tiers by design —
            # those exist to protect ordinary trades, but a snipe's whole thesis
            # is riding a cheap entry to a 1000%+ payout. Fixed % stops defeat
            # that purpose. Peak_giveback IS applied though: it's peak-relative,
            # so it doesn't cap upside, only floors giveback. Without it a snipe
            # can round-trip peak +100% to -100% at expiry (observed live
            # 2026-07-23 B64875). The ITM guard on peak_giveback below keeps
            # settlement plays intact (B65450 same day: peak +125% mid-hold →
            # rode back ITM → settled +733%).
            if not is_snipe:
                # TIER 0.5 — Gamma-aware convexity lock: profitable + true_prob reversing
                # (2-tick fade) + high convexity risk (near strike/expiry) → lock in now
                # rather than wait for a fixed P&L tier, since edge can flip fast here.
                # Gated on absolute bid too — pnl% alone was locking cheap entries at
                # $0.17-$0.37, cutting real winners short before they reached meaningful value.
                if (bid >= GAMMA_LOCK_MIN_BID and pnl_pct >= GAMMA_LOCK_MIN_PROFIT
                        and tp_curr < tp_prev and abs(gam) >= GAMMA_HIGH_THRESHOLD):
                    self.portfolio.sell(ticker, bid, reason="gamma_lock 📐")
                    continue

            # TIER 0.75 — Peak giveback: once a real gain has formed, exit once
            # price has faded back to PEAK_GIVEBACK_FRACTION of its own peak.
            # Now applies to snipes too, but only while OTM — an ITM snipe is
            # on the settlement path and mid-hold fades are expected.
            if ((not is_snipe or not itm)
                    and peak_pnl_pct >= PEAK_GIVEBACK_MIN_PEAK
                    and bid >= PEAK_GIVEBACK_MIN_BID
                    and pnl_pct <= peak_pnl_pct * PEAK_GIVEBACK_FRACTION):
                self.portfolio.sell(ticker, bid, reason="peak_giveback 📉")
                continue

            if not is_snipe:
                # TIER 1 — Scalp lock: up 40% + < 15 min left, bid at a meaningful absolute price
                if bid >= SCALP_LOCK_MIN_BID and pnl_pct >= SCALP_LOCK_PCT and hours < 0.25:
                    self.portfolio.sell(ticker, bid, reason="scalp_lock 🔄")
                    continue

                # TIER 2 — Momentum lock: up 100% + < 9 min
                if bid > 0 and pnl_pct >= MOMENTUM_LOCK_PCT and hours < 0.15:
                    self.portfolio.sell(ticker, bid, reason="momentum_locked 💰")
                    continue

                # TIER 3 — Strong profit: up 150% + < 15 min
                if bid > 0 and pnl_pct >= STRONG_PROFIT_PCT and hours < 0.25:
                    self.portfolio.sell(ticker, bid, reason="profit_extracted 💰")
                    continue

            if is_snipe:
                # TIER 3.75 — Snipe reversal lock. Fires on true_prob reversal
                # once a real run (>=+50%) has formed. Doesn't cap upside — a
                # snipe that keeps climbing without a tp reversal is untouched.
                # Lowered from +150% (which let +100% peaks like B64875 slip
                # through without any protection when peak_giveback was disabled).
                if (bid >= SNIPE_PROFIT_LOCK_MIN_BID
                        and peak_pnl_pct >= 0.50 and pnl_pct >= 0.15
                        and tp_curr < tp_prev):
                    self.portfolio.sell(ticker, bid, reason="snipe_lock 🔒")
                    continue

            # TIER 3.5 — Near-settlement exit: bid at 75¢+ means expiry ITM is near-certain.
            # Critical for vol-compression plays entered at 2-4¢ — without this, PROFIT_EXIT_MEGA
            # (300%) would fire at 8¢ from a 2¢ entry, leaving 92¢ of settlement value on the table.
            # Stays active for snipes too — for a 5-10¢ entry this is already the 650-1400%+
            # payout, and near-certain settlement isn't worth risking for the last few cents.
            if bid >= BID_EXIT_THRESHOLD:
                self.portfolio.sell(ticker, bid, reason="near_settlement 🏆")
                continue

            if not is_snipe:
                # TIER 4 — Mega: up 300%
                if bid > 0 and pnl_pct >= PROFIT_EXIT_MEGA:
                    self.portfolio.sell(ticker, bid, reason="mega_profit 🚀")
                    continue

            # TIER 5 — Time exit OTM. Skipped while still within TIME_EXIT_NEAR_DIST of the
            # boundary — a near-boundary position can still flip ITM by the buzzer, so only
            # force-exit once it's far enough OTM that a flip is no longer realistic.
            if (mins_left < TIME_EXIT_MINS and not itm and bid > 0
                    and abs(dist) > TIME_EXIT_NEAR_DIST):
                self.portfolio.sell(ticker, bid, reason="time_exit_OTM")
                continue

            if not is_snipe:
                # TIER 5.25 — Boundary risk: ITM but marginal + underwater + near
                # expiry. TIER 5 above only protects positions once already OTM;
                # a marginal ITM position carries the same flip risk right up
                # until it crosses. Momentum-gated (2-tick true_prob fade, same
                # signal as gamma_lock) so ordinary chop doesn't trigger it — gives
                # room to be volatile — but exits once the move works against it.
                # Hard floor fires unconditionally as a backstop.
                if (itm and bid > 0 and pnl_pct <= BOUNDARY_RISK_MIN_LOSS
                        and mins_left < BOUNDARY_RISK_MINS
                        and abs(dist) <= BOUNDARY_RISK_DIST
                        and (tp_curr < tp_prev or pnl_pct <= BOUNDARY_RISK_HARD_STOP)):
                    self.portfolio.sell(ticker, bid, reason="boundary_risk ⚠️")
                    continue

                # TIER 6 — Stop loss (gated: only fires with > STOP_MIN_HOURS left).
                # Short-duration contracts are binary — TIME_EXIT_MINS handles OTM exits
                # and expiry_settle captures ITM wins. Stopping in the final bars kills
                # positions that would resolve naturally.
                stop_thr = -(STOP_LOSS_PCT / time_urgency)
                if (bid > 0 and pnl_pct <= stop_thr and hours > STOP_MIN_HOURS
                        and not (itm and mins_left < TIME_EXIT_MINS)):
                    self.portfolio.sell(ticker, bid, reason=f"stop_{abs(stop_thr):.0%}")
                    continue

                # Safety — near zero
                if mid <= 0.005 and bid > 0:
                    self.portfolio.sell(ticker, bid, reason="near_zero")
                    continue
