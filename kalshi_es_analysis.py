"""
Kalshi → ES Mini Futures Lead-Lag Analysis
==========================================
Tests whether Kalshi prediction market contract prices are a leading
indicator for ES mini / SPY intraday price action.

Statistical methods:
  1. Cross-correlation of returns at various lags
  2. Granger causality (VAR-based F-test)
  3. Impulse response function
  4. Rolling window stability analysis

Requirements:
  pip install pandas numpy requests statsmodels matplotlib scipy yfinance

Usage:
  python kalshi_es_analysis.py --mode live     # Real data (needs API keys)
  python kalshi_es_analysis.py --mode backtest  # Historical analysis
  python kalshi_es_analysis.py --mode demo      # Simulated data demo
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sp_stats

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG = {
    # Kalshi API (RSA-PSS signed requests)
    # Production: https://api.elections.kalshi.com/trade-api/v2
    # Demo:       https://demo-api.kalshi.co/trade-api/v2
    "kalshi_base_url": os.environ.get(
        "KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2"
    ),
    "kalshi_api_key_id": os.environ.get("KALSHI_API_KEY_ID", ""),
    "kalshi_private_key_path": os.environ.get("KALSHI_PRIVATE_KEY_PATH", ""),

    # Contracts to track (ticker symbols on Kalshi)
    # Update these to match current active contracts
    "kalshi_tickers": {
        "recession": "KXRECSSNBER-26",            # US recession in 2026
        "fed_rate": "KXFED-26MAR",                 # Fed rate March 2026 meeting
        "rate_cut": "KXRATECUT-26DEC31",           # Fed rate cut by end of 2026
        "fed_hike": "KXFEDHIKE",                   # Next Fed rate hike
    },

    # ES / SPY data
    "spy_ticker": "SPY",
    "es_ticker": "ES=F",

    # Analysis params
    "max_lag_minutes": 60,
    "granger_max_order": 15,
    "resample_freq": "1min",       # Align to 1-minute bars
    "min_observations": 100,
    "significance_level": 0.05,

    # Output
    "output_dir": "results",
}


# ---------------------------------------------------------------------------
# Kalshi API Client (RSA-PSS Signed Auth)
# ---------------------------------------------------------------------------

class KalshiClient:
    """
    Client for Kalshi's REST API using RSA-PSS request signing.

    Setup:
      1. Go to https://kalshi.com/account/profile → API Keys
      2. Click "Create New API Key"
      3. Save the private key .pem file and note your Key ID
      4. Set env vars:
           export KALSHI_API_KEY_ID="your-key-id"
           export KALSHI_PRIVATE_KEY_PATH="/path/to/your-key.pem"

    For demo environment:
           export KALSHI_BASE_URL="https://demo-api.kalshi.co/trade-api/v2"
    """

    def __init__(self, api_key_id: str, private_key_path: str, base_url: str):
        self.base_url = base_url
        self.api_key_id = api_key_id
        self.private_key_path = private_key_path
        self.private_key = None
        self.session = None

    def _load_private_key(self):
        """Load RSA private key from PEM file."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.backends import default_backend

        with open(self.private_key_path, "rb") as f:
            self.private_key = serialization.load_pem_private_key(
                f.read(),
                password=None,
                backend=default_backend(),
            )
        print(f"[Kalshi] Loaded private key from {self.private_key_path}")

    def _sign_request(self, method: str, path: str) -> dict:
        """
        Generate signed headers for a Kalshi API request.
        Signature = RSA-PSS(timestamp_ms + HTTP_METHOD + path_without_query)
        """
        import base64
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        timestamp_ms = str(int(datetime.now().timestamp() * 1000))

        # Strip query params from path for signing
        path_for_signing = path.split("?")[0]
        message = f"{timestamp_ms}{method.upper()}{path_for_signing}"

        signature = self.private_key.sign(
            message.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )

        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "Content-Type": "application/json",
        }

    def _ensure_session(self):
        import requests
        if self.session is None:
            self.session = requests.Session()

    def login(self) -> bool:
        """Load key and verify connectivity (no login endpoint needed)."""
        self._ensure_session()
        try:
            if not self.api_key_id or not self.private_key_path:
                print("[Kalshi] Missing API key config. Set env vars:")
                print("  export KALSHI_API_KEY_ID='your-key-id'")
                print("  export KALSHI_PRIVATE_KEY_PATH='/path/to/key.pem'")
                return False

            self._load_private_key()

            # Verify auth works with a lightweight call
            path = "/trade-api/v2/exchange/status"
            headers = self._sign_request("GET", path)
            # Build full URL: base_url already ends with /trade-api/v2
            # so we need to use the host portion + path
            host = self.base_url.replace("/trade-api/v2", "")
            resp = self.session.get(f"{host}{path}", headers=headers)
            resp.raise_for_status()
            print(f"[Kalshi] Authenticated successfully (RSA-PSS)")
            return True
        except FileNotFoundError:
            print(f"[Kalshi] Private key file not found: {self.private_key_path}")
            return False
        except Exception as e:
            print(f"[Kalshi] Auth failed: {e}")
            return False

    def _request(self, method: str, endpoint: str, params: dict = None, json_body: dict = None, timeout: int = 15):
        """Make a signed request to Kalshi API."""
        self._ensure_session()
        path = f"/trade-api/v2{endpoint}"
        host = self.base_url.replace("/trade-api/v2", "")
        url = f"{host}{path}"

        if params:
            query_str = "&".join(f"{k}={v}" for k, v in params.items())
            url_with_params = f"{url}?{query_str}"
        else:
            url_with_params = url

        headers = self._sign_request(method.upper(), path)

        if method.upper() == "GET":
            resp = self.session.get(url_with_params, headers=headers, timeout=timeout)
        elif method.upper() == "POST":
            resp = self.session.post(url, headers=headers, json=json_body, timeout=timeout)
        else:
            resp = self.session.request(method, url_with_params, headers=headers)

        resp.raise_for_status()
        return resp.json()

    def get_market(self, ticker: str) -> dict:
        """Get current market snapshot for a ticker."""
        data = self._request("GET", f"/markets/{ticker}")
        return data.get("market", {})

    def get_markets_by_event(self, event_ticker: str) -> list:
        """Get all markets for an event ticker."""
        data = self._request("GET", "/markets", params={"event_ticker": event_ticker})
        return data.get("markets", [])

    def resolve_ticker(self, ticker: str) -> tuple:
        """
        Resolve a user-provided ticker into (series_ticker, market_ticker).
        Handles event tickers like KXRECSSNBER-26 or direct market tickers.
        Returns (series_ticker, market_ticker) or raises.
        """
        # First try as a direct market ticker
        try:
            market = self.get_market(ticker)
            if market:
                # Extract series from event_ticker (series is the base before the dash)
                event_tk = market.get("event_ticker", ticker)
                series_tk = market.get("series_ticker", "")
                if not series_tk:
                    # Derive series: everything before the last dash segment
                    parts = event_tk.rsplit("-", 1)
                    series_tk = parts[0] if len(parts) > 1 else event_tk
                print(f"  [Kalshi] Resolved market: {ticker}")
                print(f"           Series: {series_tk}, Event: {event_tk}")
                return series_tk, ticker
        except Exception:
            pass

        # Try as an event ticker — look up markets under it
        try:
            markets = self.get_markets_by_event(ticker)
            if markets:
                # Pick the most liquid (highest volume) market
                best = max(markets, key=lambda m: float(m.get("volume_24h_fp", "0") or "0"))
                market_tk = best["ticker"]
                event_tk = best.get("event_ticker", ticker)
                series_tk = best.get("series_ticker", "")
                if not series_tk:
                    parts = event_tk.rsplit("-", 1)
                    series_tk = parts[0] if len(parts) > 1 else event_tk
                print(f"  [Kalshi] Event {ticker} → market {market_tk}")
                print(f"           Series: {series_tk}")
                return series_tk, market_tk
        except Exception:
            pass

        # Last resort: try splitting ticker as series + event
        parts = ticker.rsplit("-", 1)
        if len(parts) == 2:
            series_tk = parts[0]
            print(f"  [Kalshi] Guessing series={series_tk}, market={ticker}")
            return series_tk, ticker

        raise ValueError(f"Could not resolve Kalshi ticker: {ticker}")

    def get_market_history(
        self, ticker: str, start_ts: int = None, end_ts: int = None,
        period_interval: int = 1  # minutes
    ) -> pd.DataFrame:
        """
        Fetch OHLCV candlestick history for a Kalshi market.
        Resolves the series ticker automatically.
        start_ts and end_ts are REQUIRED by the API — defaults to last 5 days.
        Returns DataFrame with columns: [timestamp, open, high, low, close, volume]
        """
        series_tk, market_tk = self.resolve_ticker(ticker)

        # Default time range: last 3 days (API max ~5000 candles at 1min)
        if end_ts is None:
            end_ts = int(datetime.now().timestamp())
        if start_ts is None:
            start_ts = end_ts - (3 * 24 * 60 * 60)  # 3 days back

        params = {
            "start_ts": start_ts,
            "end_ts": end_ts,
            "period_interval": period_interval,
        }

        # Try the series-based endpoint first (current API)
        try:
            data = self._request(
                "GET",
                f"/series/{series_tk}/markets/{market_tk}/candlesticks",
                params=params,
            )
        except Exception as e1:
            # Fallback: batch candlesticks endpoint
            print(f"  [Kalshi] Series endpoint failed ({e1}), trying batch...")
            try:
                params["tickers"] = market_tk
                data = self._request("GET", "/markets/candlesticks", params=params)
                # Batch returns {"markets": [{"market_ticker": ..., "candlesticks": [...]}]}
                markets_data = data.get("markets", [])
                if markets_data:
                    data = {"candlesticks": markets_data[0].get("candlesticks", [])}
                else:
                    return pd.DataFrame()
            except Exception as e2:
                print(f"  [Kalshi] Batch endpoint also failed: {e2}")
                return pd.DataFrame()

        candles = data.get("candlesticks", [])
        if not candles:
            print(f"  [Kalshi] No candlestick data returned for {market_tk}")
            return pd.DataFrame()

        print(f"  [Kalshi] Got {len(candles)} candlesticks for {market_tk}")

        # Parse the new dollar-string format
        rows = []
        for c in candles:
            ts = c.get("end_period_ts")
            price = c.get("price", {})
            row = {
                "timestamp": pd.to_datetime(ts, unit="s") if ts else None,
                "open": _parse_dollar(price.get("open_dollars", price.get("open"))),
                "high": _parse_dollar(price.get("high_dollars", price.get("high"))),
                "low": _parse_dollar(price.get("low_dollars", price.get("low"))),
                "close": _parse_dollar(price.get("close_dollars", price.get("close"))),
                "volume": float(c.get("volume_fp", c.get("volume", 0)) or 0),
            }
            rows.append(row)

        df = pd.DataFrame(rows)
        df = df.dropna(subset=["timestamp"]).set_index("timestamp").sort_index()

        # Drop rows where all OHLC are null (synthetic candles)
        df = df.dropna(subset=["close"])
        return df[["open", "high", "low", "close", "volume"]]

    def get_orderbook(self, ticker: str) -> dict:
        """Get current orderbook (bid/ask depth). Tries the fp-nested, plain-nested,
        and flat top-level shapes since the exact wrapper key isn't pinned down
        against a live response — falls back to `data` itself if neither
        "orderbook_fp" nor "orderbook" is present."""
        data = self._request("GET", f"/markets/{ticker}/orderbook")
        book = data.get("orderbook_fp") or data.get("orderbook") or data

        # Kalshi returns yes_dollars/no_dollars with dollar-unit prices;
        # _walk_book accumulates in cents and divides by 100, so scale up.
        if book.get("yes_dollars") is not None or book.get("no_dollars") is not None:
            def _to_cents(levels):
                return [[float(p) * 100, q] for p, q in (levels or [])]
            return {"yes": _to_cents(book.get("yes_dollars", [])),
                    "no":  _to_cents(book.get("no_dollars",  []))}

        if not book.get("yes") and not book.get("no"):
            print(f"  ⚠️  orderbook unrecognized shape {ticker[-18:]}: keys={list(book.keys())}")
        return {"yes": book.get("yes") or [], "no": book.get("no") or []}


def _parse_dollar(val) -> float | None:
    """Parse a dollar string like '0.5600' or cent int to float."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Market Data Fetcher (SPY / ES)
# ---------------------------------------------------------------------------

class MarketDataFetcher:
    """Fetch intraday data for SPY or ES futures."""

    @staticmethod
    def get_intraday(
        ticker: str = "SPY",
        period: str = "5d",
        interval: str = "1m",
    ) -> pd.DataFrame:
        """
        Fetch intraday bars via yfinance.
        For ES futures use ticker='ES=F'.
        yfinance provides 1m data for the last 7 days.
        """
        import yfinance as yf

        print(f"[Market] Fetching {ticker} intraday ({interval}, {period})...")
        data = yf.download(ticker, period=period, interval=interval, progress=False)

        if data.empty:
            print(f"[Market] Warning: No data returned for {ticker}")
            return pd.DataFrame()

        # Flatten multi-level columns if present
        if hasattr(data.columns, 'levels'):
            data.columns = data.columns.get_level_values(0)

        data.index = pd.to_datetime(data.index)
        data.index = data.index.tz_localize(None)  # Remove timezone for alignment
        print(f"[Market] Got {len(data)} bars for {ticker}")
        return data

    @staticmethod
    def get_historical_daily(
        ticker: str = "SPY",
        start: str = "2024-01-01",
        end: str = None,
    ) -> pd.DataFrame:
        """Fetch daily OHLCV for longer backtests."""
        import yfinance as yf

        data = yf.download(ticker, start=start, end=end, progress=False)
        if hasattr(data.columns, 'levels'):
            data.columns = data.columns.get_level_values(0)
        return data


# ---------------------------------------------------------------------------
# Data Alignment
# ---------------------------------------------------------------------------

def align_series(
    kalshi_df: pd.DataFrame,
    market_df: pd.DataFrame,
    freq: str = "1min",
    price_col_kalshi: str = "close",
    price_col_market: str = "Close",
) -> pd.DataFrame:
    """
    Align Kalshi contract prices and ES/SPY prices to common timestamps.
    Resamples both to the specified frequency, forward-fills gaps,
    and returns a merged DataFrame with columns: [kalshi_price, market_price]
    """
    # Extract price series
    k = kalshi_df[[price_col_kalshi]].rename(columns={price_col_kalshi: "kalshi_price"})
    m = market_df[[price_col_market]].rename(columns={price_col_market: "market_price"})

    # Resample to common frequency
    k = k.resample(freq).last().ffill()
    m = m.resample(freq).last().ffill()

    # Inner join on timestamp
    merged = k.join(m, how="inner").dropna()

    # Filter to market hours only (9:30 AM - 4:00 PM ET)
    if len(merged) > 0:
        hours = merged.index.hour * 100 + merged.index.minute
        market_hours = (hours >= 930) & (hours <= 1600)
        merged = merged[market_hours]

    print(f"[Align] {len(merged)} aligned observations")
    return merged


def compute_returns(df: pd.DataFrame, method: str = "log") -> pd.DataFrame:
    """Compute returns for all price columns."""
    ret = pd.DataFrame(index=df.index[1:])
    for col in df.columns:
        if method == "log":
            ret[f"{col}_ret"] = np.log(df[col] / df[col].shift(1)).dropna()
        else:
            ret[f"{col}_ret"] = df[col].pct_change().dropna()
    return ret.dropna()


# ---------------------------------------------------------------------------
# Statistical Tests
# ---------------------------------------------------------------------------

def cross_correlation_analysis(
    x: np.ndarray,
    y: np.ndarray,
    max_lag: int = 60,
) -> pd.DataFrame:
    """
    Compute cross-correlation between x and y at various lags.
    Positive lag = x leads y (what we want to test: Kalshi leads ES).
    Returns DataFrame with [lag, correlation, p_value, ci_lower, ci_upper].
    """
    n = len(x)
    results = []

    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            x_slice = x[:n - lag] if lag > 0 else x
            y_slice = y[lag:] if lag > 0 else y
        else:
            x_slice = x[-lag:]
            y_slice = y[:n + lag]

        if len(x_slice) < 10:
            continue

        corr, pval = sp_stats.pearsonr(x_slice, y_slice)

        # Confidence interval (Fisher z-transform)
        z = np.arctanh(corr)
        se = 1 / np.sqrt(len(x_slice) - 3)
        ci_lo = np.tanh(z - 1.96 * se)
        ci_hi = np.tanh(z + 1.96 * se)

        results.append({
            "lag": lag,
            "correlation": corr,
            "p_value": pval,
            "ci_lower": ci_lo,
            "ci_upper": ci_hi,
            "n_obs": len(x_slice),
        })

    return pd.DataFrame(results)


def granger_causality_test(
    x: np.ndarray,
    y: np.ndarray,
    max_order: int = 15,
) -> pd.DataFrame:
    """
    Test if x Granger-causes y using statsmodels VAR framework.
    Returns DataFrame with [lag_order, f_stat, p_value, significant].
    """
    data = np.column_stack([y, x])  # grangercausalitytests expects [effect, cause]

    results = []
    try:
        from statsmodels.tsa.stattools import grangercausalitytests
        gc_results = grangercausalitytests(data, maxlag=max_order, verbose=False)

        for lag_order, test_result in gc_results.items():
            ssr_ftest = test_result[0]["ssr_ftest"]
            f_stat = ssr_ftest[0]
            p_value = ssr_ftest[1]

            results.append({
                "lag_order": lag_order,
                "f_stat": f_stat,
                "p_value": p_value,
                "significant": p_value < CONFIG["significance_level"],
            })
    except ImportError:
        print("[Granger] statsmodels not available, using manual OLS F-test")
        results = _manual_granger(x, y, max_order)
    except Exception as e:
        print(f"[Granger] Error: {e}")
        results = _manual_granger(x, y, max_order)

    return pd.DataFrame(results)


def _manual_granger(x, y, max_order):
    """Fallback manual Granger causality if statsmodels fails."""
    from numpy.linalg import lstsq

    results = []
    n = len(y)

    for p in range(1, max_order + 1):
        if n - p < 2 * p + 5:
            break

        # Restricted model: y_t ~ y_{t-1}, ..., y_{t-p}
        Y = y[p:]
        X_r = np.column_stack([y[p - j - 1:n - j - 1] for j in range(p)])
        X_r = np.column_stack([X_r, np.ones(len(Y))])

        # Unrestricted model: add lagged x
        X_u = np.column_stack([
            X_r,
            *[x[p - j - 1:n - j - 1] for j in range(p)]
        ])

        beta_r, ssr_r, _, _ = lstsq(X_r, Y, rcond=None)
        beta_u, ssr_u, _, _ = lstsq(X_u, Y, rcond=None)

        ssr_r = np.sum((Y - X_r @ beta_r) ** 2)
        ssr_u = np.sum((Y - X_u @ beta_u) ** 2)

        n_eff = len(Y)
        k_u = X_u.shape[1]

        f_stat = ((ssr_r - ssr_u) / p) / (ssr_u / (n_eff - k_u))
        p_value = 1 - sp_stats.f.cdf(f_stat, p, n_eff - k_u)

        results.append({
            "lag_order": p,
            "f_stat": max(0, f_stat),
            "p_value": p_value,
            "significant": p_value < CONFIG["significance_level"],
        })

    return results


def impulse_response(
    x: np.ndarray,
    y: np.ndarray,
    lag_order: int = 5,
    horizon: int = 30,
) -> pd.DataFrame:
    """
    Estimate impulse response of y to a shock in x using a bivariate VAR.
    """
    try:
        from statsmodels.tsa.api import VAR
        data = pd.DataFrame({"kalshi": x, "es": y})
        model = VAR(data)
        fitted = model.fit(lag_order)
        irf = fitted.irf(horizon)
        response = irf.irfs[:, 1, 0]
        ci_lower = irf.ci[:, 1, 0, 0]
        ci_upper = irf.ci[:, 1, 0, 1]
    except ImportError:
        print(f"[IRF] statsmodels not available, using lag-correlation estimate")
        response = np.zeros(horizon + 1)
        ci_lower = np.zeros(horizon + 1)
        ci_upper = np.zeros(horizon + 1)
        for h in range(horizon + 1):
            if h < len(x):
                valid_len = min(len(x) - h, len(y) - h)
                if valid_len > 3:
                    corr = np.corrcoef(x[:valid_len], y[h:h + valid_len])[0, 1]
                    response[h] = corr
                    se = 1 / np.sqrt(valid_len - 3)
                    ci_lower[h] = corr - 1.96 * se
                    ci_upper[h] = corr + 1.96 * se
    except Exception as e:
        print(f"[IRF] VAR failed: {e}, using simple lag regression")
        response = np.zeros(horizon + 1)
        ci_lower = np.zeros(horizon + 1)
        ci_upper = np.zeros(horizon + 1)
        for h in range(horizon + 1):
            if h < len(x):
                corr = np.corrcoef(x[:len(x) - h], y[h:])[0, 1]
                response[h] = corr
                se = 1 / np.sqrt(len(x) - h - 3)
                ci_lower[h] = corr - 1.96 * se
                ci_upper[h] = corr + 1.96 * se

    return pd.DataFrame({
        "horizon": range(horizon + 1),
        "response": response,
        "ci_lower": ci_lower,
        "ci_upper": ci_upper,
    })


def rolling_lead_lag(
    x: np.ndarray,
    y: np.ndarray,
    window: int = 60,
    step: int = 15,
    max_lag: int = 30,
) -> pd.DataFrame:
    """
    Rolling window analysis to check if the lead-lag relationship is stable.
    Returns the optimal lag and peak correlation for each window.
    """
    results = []
    n = len(x)

    for start in range(0, n - window, step):
        end = start + window
        x_w = x[start:end]
        y_w = y[start:end]

        best_corr = 0
        best_lag = 0

        for lag in range(0, min(max_lag, window // 3)):
            if lag == 0:
                c = np.corrcoef(x_w, y_w)[0, 1]
            else:
                c = np.corrcoef(x_w[:-lag], y_w[lag:])[0, 1]

            if abs(c) > abs(best_corr):
                best_corr = c
                best_lag = lag

        results.append({
            "window_start": start,
            "window_end": end,
            "optimal_lag": best_lag,
            "peak_correlation": best_corr,
        })

    return pd.DataFrame(results)


# ---------------------------------------------------------------------------
# Visualization
# ---------------------------------------------------------------------------

def plot_results(
    aligned: pd.DataFrame,
    xcorr: pd.DataFrame,
    granger: pd.DataFrame,
    irf: pd.DataFrame,
    rolling: pd.DataFrame,
    contract_name: str,
    output_dir: str = "results",
):
    """Generate publication-quality plots of all analysis results."""
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(3, 2, figsize=(16, 14))
    fig.suptitle(
        f"Kalshi ({contract_name}) → ES/SPY Lead-Lag Analysis",
        fontsize=16, fontweight="bold", y=0.98,
    )

    # 1. Price series (dual axis)
    ax1 = axes[0, 0]
    ax1r = ax1.twinx()
    ax1.plot(aligned.index, aligned["kalshi_price"], color="#534AB7", linewidth=1, label="Kalshi")
    ax1r.plot(aligned.index, aligned["market_price"], color="#1D9E75", linewidth=1, label="ES/SPY")
    ax1.set_ylabel("Kalshi contract price (cents)", color="#534AB7")
    ax1r.set_ylabel("ES/SPY price", color="#1D9E75")
    ax1.set_title("Intraday price series")
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax1r.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left", fontsize=9)

    # 2. Cross-correlation
    ax2 = axes[0, 1]
    colors = ["#534AB7" if l > 0 else "#B4B2A9" for l in xcorr["lag"]]
    ax2.bar(xcorr["lag"], xcorr["correlation"], color=colors, width=0.8)
    ax2.axhline(y=0, color="black", linewidth=0.5)
    # Significance band
    n_obs = xcorr["n_obs"].iloc[0] if "n_obs" in xcorr.columns else 200
    sig_bound = 1.96 / np.sqrt(n_obs)
    ax2.axhline(y=sig_bound, color="#E24B4A", linestyle="--", linewidth=1, alpha=0.7)
    ax2.axhline(y=-sig_bound, color="#E24B4A", linestyle="--", linewidth=1, alpha=0.7)
    ax2.set_xlabel("Lag (minutes)")
    ax2.set_ylabel("Cross-correlation")
    ax2.set_title("Cross-correlation (positive lag = Kalshi leads)")

    # 3. Granger causality
    ax3 = axes[1, 0]
    bar_colors = ["#534AB7" if s else "#D3D1C7" for s in granger["significant"]]
    ax3.bar(granger["lag_order"], granger["f_stat"], color=bar_colors)
    # Critical value line (F distribution, 5%)
    crit = sp_stats.f.ppf(0.95, 1, max(50, n_obs - 20))
    ax3.axhline(y=crit, color="#E24B4A", linestyle="--", linewidth=1.5, label=f"5% critical ({crit:.1f})")
    ax3.set_xlabel("Lag order")
    ax3.set_ylabel("F-statistic")
    ax3.set_title("Granger causality: Kalshi → ES")
    ax3.legend(fontsize=9)

    # 4. Impulse response
    ax4 = axes[1, 1]
    ax4.plot(irf["horizon"], irf["response"], color="#534AB7", linewidth=2)
    ax4.fill_between(irf["horizon"], irf["ci_lower"], irf["ci_upper"],
                     color="#534AB7", alpha=0.15)
    ax4.axhline(y=0, color="black", linewidth=0.5)
    ax4.set_xlabel("Horizon (minutes)")
    ax4.set_ylabel("Response")
    ax4.set_title("Impulse response: ES response to Kalshi shock")

    # 5. Rolling optimal lag
    ax5 = axes[2, 0]
    ax5.plot(rolling["window_start"], rolling["optimal_lag"], color="#534AB7", linewidth=1.5)
    ax5.set_xlabel("Window start (bar index)")
    ax5.set_ylabel("Optimal lag (minutes)")
    ax5.set_title("Rolling window: stability of lead time")

    # 6. Rolling correlation strength
    ax6 = axes[2, 1]
    ax6.plot(rolling["window_start"], rolling["peak_correlation"], color="#1D9E75", linewidth=1.5)
    ax6.axhline(y=0, color="black", linewidth=0.5)
    ax6.set_xlabel("Window start (bar index)")
    ax6.set_ylabel("Peak correlation")
    ax6.set_title("Rolling window: signal strength over time")

    plt.tight_layout(rect=[0, 0, 1, 0.96])
    outpath = os.path.join(output_dir, f"leadlag_{contract_name}_{datetime.now():%Y%m%d_%H%M}.png")
    plt.savefig(outpath, dpi=150, bbox_inches="tight")
    print(f"\n[Output] Saved plot: {outpath}")
    plt.close()
    return outpath


# ---------------------------------------------------------------------------
# Summary Report
# ---------------------------------------------------------------------------

def generate_report(
    xcorr: pd.DataFrame,
    granger: pd.DataFrame,
    irf: pd.DataFrame,
    rolling: pd.DataFrame,
    contract_name: str,
    output_dir: str = "results",
) -> str:
    """Generate a text summary of the analysis."""
    # Best positive-lag cross-correlation
    pos_lags = xcorr[xcorr["lag"] > 0]
    if len(pos_lags) > 0:
        best_xcorr = pos_lags.loc[pos_lags["correlation"].abs().idxmax()]
    else:
        best_xcorr = pd.Series({"lag": 0, "correlation": 0, "p_value": 1})

    # Best Granger result
    if len(granger) > 0:
        best_granger = granger.loc[granger["p_value"].idxmin()]
        n_significant = granger["significant"].sum()
    else:
        best_granger = pd.Series({"lag_order": 0, "f_stat": 0, "p_value": 1})
        n_significant = 0

    # Rolling stability
    if len(rolling) > 0:
        lag_std = rolling["optimal_lag"].std()
        corr_mean = rolling["peak_correlation"].mean()
        corr_std = rolling["peak_correlation"].std()
    else:
        lag_std = corr_mean = corr_std = 0

    report = f"""
================================================================================
  LEAD-LAG ANALYSIS REPORT: Kalshi ({contract_name}) → ES/SPY
  Generated: {datetime.now():%Y-%m-%d %H:%M:%S}
================================================================================

CROSS-CORRELATION ANALYSIS
  Peak positive-lag correlation:  {best_xcorr['correlation']:.4f}
  Optimal lead time:              {best_xcorr['lag']:.0f} minutes
  p-value at peak:                {best_xcorr['p_value']:.6f}
  Significant (p < 0.05):         {'YES' if best_xcorr['p_value'] < 0.05 else 'NO'}

GRANGER CAUSALITY
  Best lag order:                 {best_granger['lag_order']:.0f}
  F-statistic:                    {best_granger['f_stat']:.2f}
  p-value:                        {best_granger['p_value']:.6f}
  Significant orders (of {len(granger)}):    {n_significant}

IMPULSE RESPONSE
  Peak ES response:               {irf['response'].abs().max():.4f}
  Response horizon to peak:       {irf.loc[irf['response'].abs().idxmax(), 'horizon']:.0f} minutes
  Response decays to zero by:     ~{_find_decay(irf):.0f} minutes

SIGNAL STABILITY (rolling windows)
  Mean peak correlation:          {corr_mean:.4f}  (std: {corr_std:.4f})
  Lead time std deviation:        {lag_std:.1f} minutes
  Signal stability:               {'STABLE' if lag_std < 5 else 'UNSTABLE'}

OVERALL ASSESSMENT
  {'STRONG leading indicator signal detected.' if best_xcorr['p_value'] < 0.01 and n_significant >= 3 else
   'MODERATE leading indicator signal.' if best_xcorr['p_value'] < 0.05 and n_significant >= 1 else
   'WEAK or no reliable leading indicator signal.'}

CAVEATS
  - Cross-correlation does not imply causation
  - Intraday microstructure noise may inflate/deflate correlations
  - Kalshi contract liquidity affects price discovery quality
  - Results may not generalize across market regimes
  - Transaction costs and latency erode exploitable edge
================================================================================
"""

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    outpath = os.path.join(output_dir, f"report_{contract_name}_{datetime.now():%Y%m%d_%H%M}.txt")
    with open(outpath, "w") as f:
        f.write(report)
    print(f"[Output] Saved report: {outpath}")
    return report


def _find_decay(irf: pd.DataFrame, threshold: float = 0.1) -> float:
    """Find approximate horizon where IRF decays below threshold."""
    peak = irf["response"].abs().max()
    if peak == 0:
        return 0
    for _, row in irf.iterrows():
        if row["horizon"] > 0 and abs(row["response"]) < threshold * peak:
            return row["horizon"]
    return irf["horizon"].max()


# ---------------------------------------------------------------------------
# Simulation (for demo / testing)
# ---------------------------------------------------------------------------

def simulate_data(
    n_bars: int = 390,
    true_lead: int = 10,
    noise_level: float = 0.5,
    seed: int = 42,
) -> pd.DataFrame:
    """
    Generate realistic simulated intraday data where Kalshi leads ES
    by `true_lead` bars, with configurable noise.
    """
    rng = np.random.default_rng(seed)
    today = pd.Timestamp.now().normalize() + pd.Timedelta(hours=9, minutes=30)
    idx = pd.date_range(today, periods=n_bars, freq="1min")

    # Latent signal (common factor)
    signal = np.cumsum(rng.normal(0, 0.01, n_bars + true_lead + 10))

    # Kalshi sees signal first
    kalshi_price = 50 + signal[:n_bars] * 30 + rng.normal(0, noise_level, n_bars)
    kalshi_price = np.clip(kalshi_price, 1, 99)

    # ES sees signal with delay
    es_price = 5200 + signal[true_lead:true_lead + n_bars] * 500 + rng.normal(0, noise_level * 10, n_bars)

    return pd.DataFrame({
        "kalshi_price": kalshi_price,
        "market_price": es_price,
    }, index=idx)


# ---------------------------------------------------------------------------
# Main Pipeline
# ---------------------------------------------------------------------------

def run_analysis(
    aligned: pd.DataFrame,
    contract_name: str = "macro",
    output_dir: str = "results",
    plot: bool = True,
):
    """Run the full analysis pipeline on aligned data."""
    print(f"\n{'='*60}")
    print(f"  Running lead-lag analysis: {contract_name}")
    print(f"  Observations: {len(aligned)}")
    print(f"{'='*60}\n")

    if len(aligned) < CONFIG["min_observations"]:
        print(f"[Error] Not enough observations ({len(aligned)}). Need {CONFIG['min_observations']}+")
        return None

    # Compute returns
    returns = compute_returns(aligned, method="simple")
    x = returns["kalshi_price_ret"].values
    y = returns["market_price_ret"].values

    print("[1/4] Cross-correlation analysis...")
    xcorr = cross_correlation_analysis(x, y, max_lag=CONFIG["max_lag_minutes"])

    print("[2/4] Granger causality test...")
    granger = granger_causality_test(x, y, max_order=CONFIG["granger_max_order"])

    print("[3/4] Impulse response function...")
    # Pick optimal lag from Granger
    if len(granger[granger["significant"]]) > 0:
        opt_order = int(granger[granger["significant"]].iloc[0]["lag_order"])
    else:
        opt_order = 5
    irf = impulse_response(x, y, lag_order=opt_order, horizon=30)

    print("[4/4] Rolling window stability...")
    rolling = rolling_lead_lag(x, y, window=60, step=15, max_lag=30)

    # Report
    report = generate_report(xcorr, granger, irf, rolling, contract_name, output_dir)
    print(report)

    # Plot
    if plot:
        try:
            plot_results(aligned, xcorr, granger, irf, rolling, contract_name, output_dir)
        except Exception as e:
            print(f"[Plot] Skipped plotting: {e}")

    return {
        "xcorr": xcorr,
        "granger": granger,
        "irf": irf,
        "rolling": rolling,
        "report": report,
    }


def main():
    parser = argparse.ArgumentParser(description="Kalshi → ES Lead-Lag Analysis")
    parser.add_argument("--mode", choices=["live", "backtest", "demo"], default="demo")
    parser.add_argument("--contract", default="recession", help="Kalshi contract key")
    parser.add_argument("--lead", type=int, default=10, help="Simulated lead (demo mode)")
    parser.add_argument("--noise", type=float, default=0.5, help="Noise level (demo mode)")
    parser.add_argument("--no-plot", action="store_true")
    args = parser.parse_args()

    output_dir = CONFIG["output_dir"]

    if args.mode == "demo":
        print("\n[Mode: DEMO] Using simulated data")
        print(f"  True lead: {args.lead} minutes | Noise: {args.noise}")
        aligned = simulate_data(true_lead=args.lead, noise_level=args.noise)
        run_analysis(aligned, f"simulated_lead{args.lead}", output_dir, plot=not args.no_plot)

    elif args.mode == "live":
        print("\n[Mode: LIVE] Fetching real data")

        # Kalshi (RSA-PSS signed auth)
        kalshi = KalshiClient(
            api_key_id=CONFIG["kalshi_api_key_id"],
            private_key_path=CONFIG["kalshi_private_key_path"],
            base_url=CONFIG["kalshi_base_url"],
        )
        if not kalshi.login():
            print("\nFailed to authenticate with Kalshi. Setup steps:")
            print("  1. Go to https://kalshi.com/account/profile → API Keys")
            print("  2. Click 'Create New API Key' and save the .pem file")
            print("  3. Set env vars:")
            print("       export KALSHI_API_KEY_ID='your-key-id'")
            print("       export KALSHI_PRIVATE_KEY_PATH='/path/to/your-key.pem'")
            print("\n  For demo environment, also set:")
            print("       export KALSHI_BASE_URL='https://demo-api.kalshi.co/trade-api/v2'")
            sys.exit(1)

        ticker = CONFIG["kalshi_tickers"].get(args.contract, args.contract)
        print(f"  Fetching Kalshi data for: {ticker}")
        k_data = kalshi.get_market_history(ticker, period_interval=1)

        if k_data.empty:
            print(f"  No Kalshi data for {ticker}. Check if contract is active.")
            sys.exit(1)

        # ES / SPY
        market = MarketDataFetcher()
        m_data = market.get_intraday("SPY", period="5d", interval="1m")

        if m_data.empty:
            print("  No SPY data. Check yfinance connection.")
            sys.exit(1)

        # Align
        aligned = align_series(k_data, m_data, freq="1min")
        run_analysis(aligned, args.contract, output_dir, plot=not args.no_plot)

    elif args.mode == "backtest":
        print("\n[Mode: BACKTEST] Historical analysis")
        print("  For backtesting, you need historical Kalshi data exports.")
        print("  Place CSV files in data/ with columns: timestamp, close, volume")
        print("  Then modify this section to load your data.")

        # Placeholder: load from CSV
        data_dir = Path("data")
        if not data_dir.exists():
            print("  No data/ directory found. Creating with example structure...")
            data_dir.mkdir(exist_ok=True)
            print("  Place your Kalshi historical CSVs in data/")
            sys.exit(0)


if __name__ == "__main__":
    main()
