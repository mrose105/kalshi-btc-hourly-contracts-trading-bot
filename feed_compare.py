#!/usr/bin/env python3
"""Live side-by-side of every BTC price source, with a rolling lag estimate.

    python3 feed_compare.py                 # Coinbase + Kalshi
    python3 feed_compare.py --webull        # also poll Webull (see below)
    python3 feed_compare.py --csv out.csv   # log every tick for later analysis

WHY THIS EXISTS. Kalshi's contract prices trail the underlying. Measured over
1.2M recorded observations, correlating a PAST Coinbase move against Kalshi's
SUBSEQUENT repricing of the same contract: +0.026 at 2s, +0.133 at 10s, +0.180
at 20s (peak), +0.083 at 60s, +0.049 at 120s. Positive on all 10 days and all
8 hour-buckets measured, and STRONGER on larger moves (+0.255 for moves >= $30
vs +0.179 below). It is structural, not episodic.

There is no persistent basis: the band Kalshi prices highest is the band
containing Coinbase spot 75% of the time, median offset exactly $0. So an
observed gap is TIMING, and it should close.

KALSHI has no public "BTC price" endpoint, so its view is backed out of the
band ladder: nearest expiry, find the highest-priced band, then take a
price-weighted mean of that band and its three neighbours either side.

Do NOT weight the whole ladder. The window carries ~118 bands including deep
tails; weighting all of them put the estimate $893 ABOVE Coinbase in a live
check while the peak band sat within $25. The local version reproduces Kalshi's
own displayed price to the dollar (measured $77,749.99 against the app's
$77,750.80). Interpolating also beats taking the peak band alone, which jumps a
full $100 whenever two neighbouring bands are priced closely — observed twice
in consecutive calls.

WEBULL needs credentials this script does not ship. Their OpenAPI wants an app
key/secret; the desktop app exposes nothing readable. Set WEBULL_QUOTE_URL to
any endpoint returning JSON with a price, and adjust _webull() to taste — it is
deliberately a small, obvious function to edit.
"""
import argparse
import collections
import json
import math
import os
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

COINBASE = "https://api.coinbase.com/v2/prices/BTC-USD/spot"


def _get(url, headers=None, timeout=6):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def coinbase():
    """What the bot itself trades on — kalshi_btc_bot/feed.py:115."""
    try:
        return float(_get(COINBASE)["data"]["amount"])
    except Exception:
        return None


def _webull():
    """Fill this in with your own endpoint. Returns a float or None.

    Webull's OpenAPI needs an app key/secret; there is no local desktop feed to
    read. If you have a quote URL that returns JSON, set WEBULL_QUOTE_URL and
    adjust the extraction below to match its shape.
    """
    url = os.environ.get("WEBULL_QUOTE_URL")
    if not url:
        return None
    try:
        d = _get(url)
        for k in ("close", "price", "last", "lastPrice", "deal", "pPrice"):
            if isinstance(d, dict) and k in d:
                return float(d[k])
        if isinstance(d, list) and d and isinstance(d[0], dict):
            for k in ("close", "price", "last", "lastPrice"):
                if k in d[0]:
                    return float(d[0][k])
    except Exception:
        return None
    return None


class Kalshi:
    """Kalshi's implied spot, backed out of the nearest-expiry band ladder."""

    def __init__(self):
        from kalshi_es_analysis import KalshiClient
        self.c = KalshiClient(
            api_key_id=os.environ["KALSHI_API_KEY_ID"],
            private_key_path=os.environ["KALSHI_PRIVATE_KEY_PATH"],
            base_url=os.environ.get(
                "KALSHI_BASE_URL",
                "https://api.elections.kalshi.com/trade-api/v2"),
        )
        self.c.login()

    def implied(self):
        """(implied_spot, top_band_centre, n_bands) or (None, None, 0)."""
        try:
            d = self.c._request("GET", "/markets",
                                params={"series_ticker": "KXBTC",
                                        "status": "open", "limit": 200})
        except Exception:
            return None, None, 0
        by = collections.defaultdict(list)
        for m in (d.get("markets") or []):
            lo, hi = m.get("floor_strike"), m.get("cap_strike")
            a, b = m.get("yes_ask_dollars"), m.get("yes_bid_dollars")
            ct = m.get("close_time")
            if lo is None or hi is None or not a or not b or not ct:
                continue
            by[ct].append((float(lo), float(hi), (float(a) + float(b)) / 2.0))
        if not by:
            return None, None, 0
        ct = min(by)                       # nearest expiry
        bands = by[ct]
        tot = sum(p for _, _, p in bands)
        if tot <= 0:
            return None, None, len(bands)
        # LOCAL weighted mean, around the peak only. A full-ladder mean is
        # badly biased: the window carries ~118 bands including deep tails, and
        # weighting all of them put the estimate $893 above Coinbase in a live
        # check while the peak band was within $25. Restricting to the peak
        # +/- 3 bands recovers Kalshi's own displayed price to the dollar.
        bands.sort(key=lambda x: x[0])
        pk = max(range(len(bands)), key=lambda i: bands[i][2])
        near = bands[max(0, pk - 3):pk + 4]
        w = sum(p for _, _, p in near)
        implied = (sum(((lo + hi) / 2.0) * p for lo, hi, p in near) / w
                   if w > 0 else None)
        top = bands[pk]
        return implied, (top[0] + top[1]) / 2.0, len(bands)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--webull", action="store_true")
    ap.add_argument("--csv")
    ap.add_argument("--no-kalshi", action="store_true")
    a = ap.parse_args()

    k = None
    if not a.no_kalshi:
        try:
            k = Kalshi()
        except Exception as e:
            print(f"  Kalshi unavailable ({e}) — continuing without it\n")

    csv = open(a.csv, "w") if a.csv else None
    if csv:
        csv.write("ts,coinbase,webull,kalshi_implied,kalshi_top_band\n")

    hist = collections.deque(maxlen=400)   # (t, coinbase, kalshi_implied)
    print(f"{'time':<10}{'Coinbase':>12}{'Webull':>12}{'Kalshi impl':>13}"
          f"{'CB-Kalshi':>11}{'CB-WB':>9}  lag corr(20s)")
    print("-" * 82)
    try:
        while True:
            t = datetime.now(timezone.utc)
            cb = coinbase()
            wb = _webull() if a.webull else None
            ki, ktop, nb = (k.implied() if k else (None, None, 0))
            if cb and ki:
                hist.append((t, cb, ki))
            if csv:
                csv.write(f"{t.isoformat()},{cb or ''},{wb or ''},"
                          f"{ki or ''},{ktop or ''}\n")
                csv.flush()

            # rolling lead-lag: past 20s Coinbase move vs next 20s Kalshi move
            corr = ""
            if len(hist) > 30:
                xs, ys = [], []
                H = list(hist)
                for i in range(10, len(H) - 10):
                    dt0 = (H[i][0] - H[i - 10][0]).total_seconds()
                    dt1 = (H[i + 10][0] - H[i][0]).total_seconds()
                    if dt0 > 40 or dt1 > 40:
                        continue
                    xs.append(H[i][1] - H[i - 10][1])
                    ys.append(H[i + 10][2] - H[i][2])
                if len(xs) > 20:
                    mx, my = statistics.mean(xs), statistics.mean(ys)
                    cov = sum((p - mx) * (q - my) for p, q in zip(xs, ys)) / len(xs)
                    sx, sy = statistics.pstdev(xs), statistics.pstdev(ys)
                    if sx > 0 and sy > 0:
                        corr = f"{cov / (sx * sy):+.3f}  (n={len(xs)})"

            f = lambda v: f"{v:,.2f}" if v else "—"
            g1 = f"{cb - ki:+,.0f}" if (cb and ki) else "—"
            g2 = f"{cb - wb:+,.0f}" if (cb and wb) else "—"
            print(f"{t.astimezone():%H:%M:%S}  {f(cb):>12}{f(wb):>12}"
                  f"{f(ki):>13}{g1:>11}{g2:>9}  {corr}")
            time.sleep(a.interval)
    except KeyboardInterrupt:
        if csv:
            csv.close()
        print("\nstopped.")


if __name__ == "__main__":
    main()
