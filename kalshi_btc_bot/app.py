import datetime
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from kalshi_es_analysis import KalshiClient

from .config import (
    MAX_EXPOSURE_PCT, MAX_TRADE_PCT, MIN_CASH_PCT, NO_TRADE_PCT,
    ENABLE_MISPRICE_NO, PAPER_TRADING, POSITION_CHECK, PRICE_FETCH, SCAN_INTERVAL, SYNC_INTERVAL,
)
from .dashboard import start as dashboard_start, update as dashboard_update
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

    scan_t    = 0
    check_t   = 0
    price_t   = 0
    summary_t = 0
    sync_t    = 0

    print("  Warming up (60s)...")
    for _ in range(15):
        feed.fetch()
        time.sleep(4)

    portfolio.sync()
    if not portfolio.startup_safety_check():
        return
    dashboard_start()
    print(f"  Ready. BTC=${feed.last:,.0f} | "
          f"Cash=${portfolio.real_cash:.2f} | Port=${portfolio.real_port:.2f}\n")

    while True:
        now = time.time()
        t   = datetime.datetime.now().strftime("%H:%M:%S")

        if now >= price_t:
            feed.fetch()
            price_t = now + PRICE_FETCH

        if now >= sync_t:
            if PAPER_TRADING:
                portfolio.real_port = portfolio.exposure()
            else:
                portfolio.sync()
            sync_t = now + SYNC_INTERVAL

        spot = feed.last
        vol  = feed.volatility(300)

        if now >= check_t and spot > 0:
            regime = regime_e.detect(feed)
            pos_mgr.manage(spot, vol, regime)
            check_t = now + POSITION_CHECK

        if now >= scan_t and spot > 0:
            regime = regime_e.detect(feed)
            ladder = ladder_e.get(spot)
            total  = portfolio.total_value()

            # Push dashboard state
            _pos_list = []
            for _tk, _p in portfolio.positions.items():
                try:
                    import datetime as _dt
                    _ct  = _dt.datetime.fromisoformat(
                        _p.get("close_time", "").replace("Z", "+00:00"))
                    _min = max(0, (_ct - _dt.datetime.now(
                        _dt.timezone.utc)).total_seconds() / 60)
                except Exception:
                    _min = 0
                _pos_list.append({
                    "ticker":    _tk[-22:],
                    "count":     _p["count"],
                    "entry":     _p["entry"],
                    "true_prob": _p.get("true_prob", 0),
                    "pnl_pct":   0,
                    "mins_left": _min,
                })
            dashboard_update({
                "btc":       spot,
                "regime":    regime["regime"],
                "direction": regime["direction"],
                "cash":      portfolio.real_cash,
                "total":     total,
                "pnl":       total - portfolio.start_total,
                "positions": _pos_list,
            })

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
                # Filter out recently-stopped tickers
                _now = time.time()
                portfolio.stop_cooldowns = {t: e for t, e in portfolio.stop_cooldowns.items() if _now < e}
                _cd  = set(portfolio.stop_cooldowns)
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

                if not sig and not no_sig:
                    cd_str = f" [{len(_cd)} cooling]" if _cd else ""
                    print(f"  — No edge (ladder: {len(_ladder)} contracts{cd_str})")

            scan_t = now + SCAN_INTERVAL

        if now >= summary_t:
            portfolio.summary()
            summary_t = now + 180

        time.sleep(1)
