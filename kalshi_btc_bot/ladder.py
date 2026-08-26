import datetime
import time

# Read config module-qualified, never `from .config import X`.
#
# `from .config import X` binds a name-local snapshot at import time, so any
# later mutation of the module attribute — which is exactly what every sweep
# and every test fixture does — silently never arrives. That is not
# hypothetical here: sweep_no_thresholds() once ran a seven-value grid through
# signals.py and produced byte-identical trades on all seven, because the
# threshold it was setting had been frozen by an import of this shape
# (2026-08-03, docs/QUANT_STANDARDS_AUDIT.md). signals.py and portfolio.py were
# converted then; ladder.py was missed and stayed frozen until 2026-08-26.
#
# Nothing depended on it at the time, which is the point — it was one sweep
# away from silently reporting that a ladder filter made no difference.
from . import config as _C
from . import recorder
from .contracts import is_in_money, otm_distance, parse_contract
from .instrument import ACTIVE as _INSTRUMENT

# ─────────────────────────────────────────────
# KALSHI LADDER
# ─────────────────────────────────────────────
class Ladder:
    """Scans the active instrument's series and returns tradeable contracts.

    An instrument may list more than one series (SPX uses KXINXU for hourly
    above/below digitals and KXINX for daily ranges). Kalshi's /markets endpoint
    accepts a single series_ticker, and each series runs its own expiry
    schedule, so the nearest window is resolved PER SERIES and the results are
    unioned. With a single-series instrument like BTC this reduces to exactly
    the previous single-request behavior.
    """

    def __init__(self, client):
        self.client   = client
        self._cache   = []
        self._cache_t = 0
        # series ticker -> nearest expiry window string
        self._windows: dict[str, str] = {}
        self._quotes: dict = {}   # ticker -> (bid, ask, close_time, status)
        self._quotes_t = 0.0      # when _quotes was last refreshed
        self._window_t = 0.0      # when the expiry windows were last resolved

    @property
    def _window(self) -> str:
        """Combined window label, for display and the recorder's `win` field."""
        return "+".join(w for w in self._windows.values() if w)

    @staticmethod
    def _window_from(markets: list) -> str:
        """Nearest expiry window inside [_C.MIN_HOURS, _C.MAX_HOURS] from a payload
        that has ALREADY been fetched. Split out so get() can derive the window
        from its own /markets response instead of issuing a second identical
        request — find_window() and get() were calling the same endpoint with
        the same params back-to-back every 120s."""
        now = datetime.datetime.now(datetime.timezone.utc)
        best_t, best_w = None, ""
        for m in markets:
            close = m.get("close_time", "")
            try:
                ct = datetime.datetime.fromisoformat(close.replace("Z", "+00:00"))
                h  = (ct - now).total_seconds() / 3600
                if _C.MIN_HOURS <= h <= _C.MAX_HOURS:
                    if best_t is None or h < best_t:
                        best_t = h
                        best_w = m["ticker"].split("-")[1]
            except Exception:
                pass
        return best_w

    def _fetch_series(self, series: str) -> list:
        data = self.client._request("GET", "/markets",
                 params={"limit": 200, "series_ticker": series,
                         "status": "open"}, timeout=10)
        return data.get("markets", [])

    def find_window(self) -> str:
        try:
            windows = []
            for series in _INSTRUMENT.series:
                w = self._window_from(self._fetch_series(series))
                if w:
                    windows.append(w)
            return "+".join(windows)
        except Exception:
            return ""

    def get(self, spot: float, force: bool = False) -> list:
        now = time.time()
        if not force and now - self._cache_t < _C.LADDER_CACHE_SECONDS:
            return self._cache
        try:
            resolve_windows = not self._windows or now - self._window_t > 120

            all_markets: list = []
            win_markets: list = []
            for series in _INSTRUMENT.series:
                markets = self._fetch_series(series)
                all_markets.extend(markets)
                # Derive each series' window from THIS payload rather than a
                # second identical request (see _window_from).
                if resolve_windows:
                    self._windows[series] = self._window_from(markets)
                window = self._windows.get(series, "")
                if not window:
                    continue
                # Capture the raw window BEFORE any entry filter — see
                # recorder.record_universe for why the filtered `quotes` stream
                # cannot be used to evaluate the filters themselves.
                win_markets.extend(m for m in markets
                                   if window in m.get("ticker", ""))
            if resolve_windows:
                self._window_t = now

            if not win_markets:
                print(f"  ⏳ No active window (no open "
                      f"{'/'.join(_INSTRUMENT.series)} markets in "
                      f"{_C.MIN_HOURS:.2f}–{_C.MAX_HOURS:.1f}h range)")
                return []
            recorder.record_universe(spot, self._window, win_markets)

            # Publish the raw window so PositionManager can price open positions
            # from this one bulk response instead of a single-ticker fetch each.
            # Measured 2026-08-13: /markets?limit=200 is 28ms for ALL markets,
            # while /markets/<ticker> is 83ms for one — so N positions cost
            # N*83ms of avoidable latency inside a 2s cycle, plus 30*N calls a
            # minute of rate-limit budget. Keyed by ticker, with the fetch time
            # so a stale snapshot can be rejected rather than silently trusted.
            self._quotes = {
                m.get("ticker"): (
                    float(m.get("yes_bid_dollars") or 0),
                    float(m.get("yes_ask_dollars") or 0),
                    m.get("close_time", ""),
                    m.get("status", ""),
                )
                for m in all_markets
            }
            self._quotes_t = now

            ladder = []
            for m in win_markets:
                ya  = float(m.get("yes_ask_dollars") or 0)
                yb  = float(m.get("yes_bid_dollars") or 0)
                vol = float(m.get("volume_fp") or 0)
                if ya <= 0 or yb <= 0 or vol < _C.MIN_VOLUME or ya > _C.MAX_ASK:
                    continue
                spread = ya - yb
                # The ladder feeds BOTH lanes, so it must not judge a row on one
                # leg's economics. The dollar spread is the same either way, but
                # a YES buyer risks ya while a NO buyer risks 1 - yb, and those
                # differ wildly: yes 0.15/0.20 is 25% of the YES ask but 6% of
                # the 0.85 NO cost. Filtering on ya alone dropped rows that were
                # cheap to trade as NO — which, with ENABLE_YES off, is every row
                # this bot actually uses. Keep a row if EITHER leg is tradeable;
                # buy()/buy_no() then apply that lane's own test at execution.
                #
                # Cent-precision: float subtraction makes an exact 5c spread
                # compare as 0.050000000000000044 for some cent pairs and
                # 0.04999999999999999 for others, so 41% of the 95 possible 5c
                # spreads were wrongly rejected. See portfolio.py.
                _no_cost = 1.0 - yb
                _yes_pct = spread / ya if ya > 0 else 1.0
                _no_pct = spread / _no_cost if _no_cost > 0 else 1.0
                if round(min(_yes_pct, _no_pct), 9) > _C.MAX_SPREAD_PCT:
                    continue
                # Pass the market payload so geometry comes from the exchange's
                # floor/cap strikes rather than being guessed from the ticker.
                contract = parse_contract(m["ticker"], spot, m)
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
