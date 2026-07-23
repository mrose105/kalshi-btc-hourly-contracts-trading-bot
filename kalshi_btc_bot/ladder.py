import datetime
import time

from .config import (
    LADDER_CACHE_SECONDS, MAX_ASK, MAX_HOURS, MAX_SPREAD, MAX_SPREAD_PCT,
    MIN_HOURS, MIN_VOLUME,
)
from .contracts import is_in_money, otm_distance, parse_contract

# ─────────────────────────────────────────────
# KALSHI LADDER
# ─────────────────────────────────────────────
class Ladder:
    def __init__(self, client):
        self.client   = client
        self._cache   = []
        self._cache_t = 0
        self._window  = ""

    def find_window(self) -> str:
        try:
            data = self.client._request("GET", "/markets",
                     params={"limit": 200, "series_ticker": "KXBTC",
                             "status": "open"}, timeout=10)
            now  = datetime.datetime.now(datetime.timezone.utc)
            best_t, best_w = None, ""
            for m in data.get("markets", []):
                close = m.get("close_time", "")
                try:
                    ct = datetime.datetime.fromisoformat(close.replace("Z","+00:00"))
                    h  = (ct - now).total_seconds() / 3600
                    if MIN_HOURS <= h <= MAX_HOURS:
                        if best_t is None or h < best_t:
                            best_t = h
                            best_w = m["ticker"].split("-")[1]
                except:
                    pass
            return best_w
        except:
            return ""

    def get(self, spot: float, force: bool = False) -> list:
        now = time.time()
        if not force and now - self._cache_t < LADDER_CACHE_SECONDS:
            return self._cache
        if not self._window or now - self._cache_t > 120:
            self._window = self.find_window()
        if not self._window:
            print(f"  ⏳ No active window (no open KXBTC markets in {MIN_HOURS:.2f}–{MAX_HOURS:.1f}h range)")
            return []
        try:
            data = self.client._request("GET", "/markets",
                     params={"limit": 200, "series_ticker": "KXBTC",
                             "status": "open"}, timeout=10)
            ladder = []
            for m in data.get("markets", []):
                if self._window not in m.get("ticker", ""):
                    continue
                ya  = float(m.get("yes_ask_dollars") or 0)
                yb  = float(m.get("yes_bid_dollars") or 0)
                vol = float(m.get("volume_fp") or 0)
                if ya <= 0 or yb <= 0 or vol < MIN_VOLUME or ya > MAX_ASK:
                    continue
                spread = ya - yb
                if spread > MAX_SPREAD or spread / ya > MAX_SPREAD_PCT:
                    continue
                contract = parse_contract(m["ticker"], spot)
                if contract["type"] == "UNKNOWN":
                    continue
                close = m.get("close_time", "")
                try:
                    ct    = datetime.datetime.fromisoformat(close.replace("Z","+00:00"))
                    hours = max(0.01, (ct - datetime.datetime.now(
                        datetime.timezone.utc)).total_seconds() / 3600)
                except Exception:
                    # Unparseable close time — skip rather than fabricate 1.0h,
                    # which let expired/odd contracts pass the expiry gates.
                    continue
                dist = otm_distance(contract, spot)
                ladder.append({
                    **contract,
                    "ticker":     m["ticker"],
                    "ask":        ya,
                    "bid":        yb,
                    "spread":     round(spread, 4),
                    "vol":        vol,
                    "hours":      round(hours, 3),
                    "close_time": close,
                    "itm":        is_in_money(contract, spot),
                    "otm_dist":   dist,
                })
            self._cache   = ladder
            self._cache_t = now
            return ladder
        except Exception as e:
            print(f"  ⚠️  Ladder: {e}")
