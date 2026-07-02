import datetime
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from kalshi_es_analysis import KalshiClient

from .config import (
    MAX_EXPOSURE_PCT, MAX_TRADE_PCT, MIN_CASH_PCT, NO_TRADE_PCT,
    ENABLE_MISPRICE_NO, PAPER_TRADING, POSITION_CHECK, PRICE_FETCH, SCAN_INTERVAL, SYNC_INTERVAL,
)
from .feed import BTCFeed
from .ladder import Ladder
from .model import DistModel
from .portfolio import Portfolio
from .positions import PositionManager
from .regime import RegimeEngine
from .signals import SignalEngine

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    print("="*62)
    print("  🧠 KALSHI BTC QUANT v4.3")
    print(f"  Sizing: {MAX_TRADE_PCT:.0%} per trade | Max exposure: {MAX_EXPOSURE_PCT:.0%} | Reserve: {MIN_CASH_PCT:.0%}")
    print(f"  YES exits: scalp→momentum→strong→mega→time→stop")
    print(f"  NO scalp:  fade YES≥1.4x overpriced, 8-21min window, {NO_TRADE_PCT:.0%} sizing")
    print(f"  Portfolio syncs from real Kalshi every {SYNC_INTERVAL}s")
    print("="*62 + "\n")

    client    = KalshiClient(
        api_key_id=os.environ["KALSHI_API_KEY_ID"],
        private_key_path=os.environ["KALSHI_PRIVATE_KEY_PATH"],
        base_url=os.environ.get("KALSHI_BASE_URL",
                                "https://api.elections.kalshi.com/trade-api/v2"),
    )
    client.login()

    feed      = BTCFeed()
    regime_e  = RegimeEngine()
    dist      = DistModel()
    ladder_e  = Ladder(client)
    portfolio = Portfolio(client)
    signal_e  = SignalEngine(dist)
    pos_mgr   = PositionManager(client, portfolio, dist, feed)

    print("  Warming up (60s)...")
    for _ in range(15):
        feed.fetch()
        time.sleep(4)

    portfolio.sync()
    if not portfolio.startup_safety_check():
        return
    print(f"  Ready. BTC=${feed.last:,.0f} | "
          f"Cash=${portfolio.real_cash:.2f} | Port=${portfolio.real_port:.2f}\n")

    # ── Independent daemon loops ──────────────────────────────────
    # Each subsystem runs on its own timer/thread so a slow API call in one
    # (e.g. a laggy ladder fetch) can no longer delay another (e.g. an exit
    # check on an open position). Shared mutable state (feed.prices,
    # portfolio.real_cash/real_port/positions) is either append-only/GIL-safe
    # (feed) or protected by portfolio.lock (see portfolio.py).
    stop_event = threading.Event()

    def _loop(fn, interval, name):
        while not stop_event.is_set():
            try:
                fn()
            except Exception as e:
                print(f"  ⚠️  {name} loop error: {e}")
            stop_event.wait(interval)

    def price_step():
        feed.fetch()

    def sync_step():
        if PAPER_TRADING:
            portfolio.real_port = portfolio.exposure()
        else:
            portfolio.sync()

    def position_step():
        spot = feed.last
        vol  = feed.volatility(300)
        if spot > 0:
            regime = regime_e.detect(feed)
            pos_mgr.manage(spot, vol, regime)

    def scan_step():
        spot = feed.last
        vol  = feed.volatility(300)
        if spot <= 0:
            return
        t      = datetime.datetime.now().strftime("%H:%M:%S")
        regime = regime_e.detect(feed)
        ladder = ladder_e.get(spot)

        print(f"[{t}] BTC=${spot:,.0f} | "
              f"{regime['regime']} {regime['direction']} "
              f"conf={regime['conf']:.0%} | "
              f"mom={regime['mom']:+.3%} z={regime['zscore']:+.2f} | "
              f"cash=${portfolio.real_cash:.2f} pos=${portfolio.exposure():.2f} "
              f"n={len(portfolio.positions)}")

        if ladder:
            for c in sorted(ladder, key=lambda x: abs(x["otm_dist"]))[:8]:
                print(f"  📋 {c['ticker'][-18:]} ask=${c['ask']:.2f} bid=${c['bid']:.2f} "
                      f"dist={c['otm_dist']:+.0f} vol={c['vol']:.0f} hrs={c['hours']:.2f}")

        if portfolio.can_trade() and ladder:
            # Filter out recently-stopped tickers. Reassignment happens under
            # portfolio.lock since sell() can concurrently add a cooldown entry
            # to this same dict from the position-management thread.
            _now = time.time()
            with portfolio.lock:
                portfolio.stop_cooldowns = {
                    tk: e for tk, e in portfolio.stop_cooldowns.items() if _now < e
                }
                _cd = set(portfolio.stop_cooldowns)
            _ladder = [c for c in ladder if c["ticker"] not in _cd]

            # YES signal
            sig = signal_e.find_best(
                spot, vol, regime, _ladder, portfolio.positions)
            if sig:
                print(f"\n  🎯 [{sig['type']}] {sig['ticker'][-22:]}")
                print(f"     Window: {sig['label']} | "
                      f"Ask: ${sig['ask']:.4f} | "
                      f"True: {sig['true_prob']:.0%} | "
                      f"Edge: {sig['edge']:.0%}")
                print(f"     ITM: {'✅' if sig['itm'] else '❌'} "
                      f"dist={sig['otm_dist']:+.0f} | "
                      f"Hours: {sig['hours']:.2f}h | "
                      f"Vol: {sig['vol']:.0f}\n")
                portfolio.buy(sig, sig["true_prob"])

            # NO scalp signal
            no_sig = None
            if ENABLE_MISPRICE_NO:
                no_sig = signal_e.find_no_scalp(
                    spot, vol, regime, _ladder, portfolio.positions,
                    portfolio.real_cash, portfolio.start_total)
            if no_sig:
                print(f"\n  🎯 [MISPRICE_NO] {no_sig['ticker'][-22:]}")
                print(f"     YES ask=${no_sig['ask']:.2f} true={no_sig['true_prob']:.0%} "
                      f"overpriced_by={no_sig['edge_pct']:.0f}% "
                      f"NO_cost=${no_sig['no_cost']:.2f} "
                      f"dist={no_sig['otm_dist']:+.0f} "
                      f"{no_sig['hours']*60:.0f}m left")
                portfolio.buy_no(no_sig, no_sig["true_prob"])

            # SNIPE signal — deep-OTM lottery tickets, ROI-ranked, separate scan from
            # find_best() (see config.py SNIPE_* comment for why they'd otherwise be
            # starved out by the main edge ranking)
            snipe_sig = signal_e.find_snipe(spot, vol, regime, _ladder, portfolio.positions)
            if snipe_sig:
                print(f"\n  🎯 [SNIPE] {snipe_sig['ticker'][-22:]}")
                print(f"     Window: {snipe_sig['label']} | "
                      f"Ask: ${snipe_sig['ask']:.4f} | "
                      f"True: {snipe_sig['true_prob']:.0%} | "
                      f"ROI edge: {snipe_sig['edge_ratio']:.0%} | "
                      f"dist={snipe_sig['otm_dist']:+.0f} | "
                      f"Hours: {snipe_sig['hours']:.2f}h\n")
                portfolio.buy(snipe_sig, snipe_sig["true_prob"], is_snipe=True)

            if not sig and not no_sig and not snipe_sig:
                cd_str = f" [{len(_cd)} cooling]" if _cd else ""
                print(f"  — No edge (ladder: {len(_ladder)} contracts{cd_str})")

    def summary_step():
        portfolio.summary()

    threads = [
        threading.Thread(target=_loop, args=(price_step, PRICE_FETCH, "price"),
                          daemon=True, name="price"),
        threading.Thread(target=_loop, args=(sync_step, SYNC_INTERVAL, "sync"),
                          daemon=True, name="sync"),
        threading.Thread(target=_loop, args=(position_step, POSITION_CHECK, "position"),
                          daemon=True, name="position"),
        threading.Thread(target=_loop, args=(scan_step, SCAN_INTERVAL, "scan"),
                          daemon=True, name="scan"),
        threading.Thread(target=_loop, args=(summary_step, 180, "summary"),
                          daemon=True, name="summary"),
    ]
    for th in threads:
        th.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n  Shutting down...")
        stop_event.set()
