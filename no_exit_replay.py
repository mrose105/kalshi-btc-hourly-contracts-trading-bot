"""Replay the REAL exit ladder against recorded Kalshi quotes and book depth.

This closes docs/BACKTEST_INTEGRITY.md §3 for the NO book.

WHY THIS EXISTS
---------------
kalshi_btc_backtest.py prices exits with `_exit_bid(true_prob, hours)` — the
bot's own DistModel, discounted. There is no order book anywhere in that
pipeline, so whenever a tier exits, the simulation books a profit that depends
on the model being right, which is the thing the backtest is supposed to test.

For the NO ladder that is not merely a bias, it is a structural blind spot.
The synthetic NO exit price is built as

    yes_ask_now = yes_tp + KALSHI_SPREAD + markup          (backtest line 663-671)

from the same DistModel that then supplies `true_prob_curr`, so

    overpricing = yes_ask_now / true_p  ~=  1 + (0.015 + markup) / true_p

is structurally >= 1 and only dips under NO_EDGE_GONE_RATIO (1.05) when true_p
exceeds ~0.30 — while `edge_gone` ALSO requires the position to be up, which for
a NO means true_p has fallen small, which makes the ratio large. The two
conditions are mathematically opposed. Measured 2026-08-31 over 60 days:
`no_edge_gone` fired 0 times in 101 trades, and 89 of 101 exits were
hold-to-expiry — while live over the same period edge_gone is the DOMINANT exit
(6 of 12 round trips on 08-31 alone).

So the synthetic backtest has never tested the exit ladder at all. It measures
"enter on BOUNDARY_NO and hold to expiry", which is not the strategy the bot
runs. The exits are where the design lives, and they had no backtest.

WHAT THIS DOES INSTEAD
----------------------
Drives the bot's ACTUAL kalshi_btc_bot.positions.PositionManager and
kalshi_btc_bot.portfolio.Portfolio over the recorded streams:

    universe_*.jsonl.gz   per-tick ladder, every contract, real top-of-book
    books_*.jsonl.gz      real resting depth, every RECORD_BOOK_INTERVAL secs
    quotes_*.jsonl.gz     per-tick regime + spot (settlement source)

Nothing about the ladder is reimplemented. `Portfolio._orderbook` is overridden
to return the recorded book, so both entry (`_walk_book` over real yes_levels)
and exit (`_walk_book` over real no_levels) fill against depth that actually
rested on the exchange. Every exit tier — edge_gone, misprice_captured,
misprice_time, the stop, time_forced_no, the catastrophe floor — runs its real
code against real prices.

WHAT IT STILL CANNOT TELL YOU
-----------------------------
* Latency. The recorded book is up to RECORD_BOOK_INTERVAL (5s) stale, and the
  replay assumes an exit decided at tick T fills against the last book at or
  before T. Live, the bot re-fetches. Reported as `stale_book_secs`.
* Market impact beyond the top of the recorded ladder. Walking the book assumes
  our own order does not move it, same assumption live paper trading makes.
* Queue position. A resting size of 80 does not mean 80 were available to us.
* Coverage. Depth is only recorded for a subset of tickers; exits with no
  recorded book at all are counted and reported separately rather than filled
  on a guess. See `no_book` in the output.

Usage:
    python3 no_exit_replay.py --start 2026-08-12 --end 2026-08-31
    python3 no_exit_replay.py --start 2026-08-12 --end 2026-08-31 --no-stop 0.30
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import glob
import gzip
import json
import sys
import zlib
import contextlib
import io
import time as _wall
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from kalshi_btc_bot import config as C
from kalshi_btc_bot import portfolio as portfolio_mod
from kalshi_btc_bot import positions as positions_mod
from kalshi_btc_bot import recorder as recorder_mod
from kalshi_btc_bot.model import DistModel
from kalshi_btc_bot.portfolio import Portfolio
from kalshi_btc_bot.positions import PositionManager
from kalshi_btc_bot.signals import SignalEngine

from boundary_no_quote_replay import (
    join_regimes,
    normalize_universe,
    tolerant_jsonl_gz,
)

UTC = dt.timezone.utc


# ─────────────────────────────────────────────────────────────
# SIMULATED CLOCK
#
# positions.py and portfolio.py both read wall-clock time directly: MIN_HOLD_SECS
# compares against time.time(), and _hours_from() compares close_time against
# datetime.now(). Replaying recorded ticks under a real clock would make every
# position look infinitely old and every contract long expired, so both modules
# are pointed at a clock this driver advances tick by tick.
# ─────────────────────────────────────────────────────────────
class _Clock:
    now: float = 0.0


CLOCK = _Clock()


class _TimeShim:
    @staticmethod
    def time() -> float:
        return CLOCK.now

    @staticmethod
    def sleep(_seconds) -> None:
        return None


def _hours_from_sim(close_time: str) -> float:
    """positions._hours_from against the simulated clock. Same contract as the
    original, including the 1.0 fallback on an unparseable close_time."""
    try:
        ct = dt.datetime.fromisoformat(str(close_time).replace("Z", "+00:00"))
        now = dt.datetime.fromtimestamp(CLOCK.now, UTC)
        return (ct - now).total_seconds() / 3600
    except Exception:
        return 1.0


def install_sim_clock() -> None:
    positions_mod.time = _TimeShim
    portfolio_mod.time = _TimeShim
    positions_mod._hours_from = _hours_from_sim


# ─────────────────────────────────────────────────────────────
# RECORDED DEPTH
# ─────────────────────────────────────────────────────────────
class BookStore:
    """Recorded order books, looked up as 'the most recent book at or before t'.

    Never interpolates and never looks forward: a book stamped after the moment
    being priced is not knowledge the bot could have had.
    """

    def __init__(self) -> None:
        self._ts: dict[str, list[float]] = defaultdict(list)
        self._bk: dict[str, list[dict]] = defaultdict(list)
        self.rows = 0

    def load(self, paths: list[str]) -> None:
        staged: dict[str, list[tuple[float, dict]]] = defaultdict(list)
        for path in paths:
            for row in tolerant_jsonl_gz(path):
                tk = row.get("tk")
                if not tk:
                    continue
                try:
                    ts = dt.datetime.fromisoformat(row["t"]).timestamp()
                except Exception:
                    continue
                staged[tk].append((ts, {"yes": row.get("yes") or [],
                                        "no": row.get("no") or []}))
                self.rows += 1
        for tk, items in staged.items():
            items.sort(key=lambda x: x[0])
            self._ts[tk] = [t for t, _ in items]
            self._bk[tk] = [b for _, b in items]

    def at(self, ticker: str, when: float) -> tuple[dict | None, float]:
        """Returns (book, staleness_seconds). (None, inf) when nothing was
        recorded for this ticker at or before `when`."""
        ts = self._ts.get(ticker)
        if not ts:
            return None, float("inf")
        i = bisect.bisect_right(ts, when) - 1
        if i < 0:
            return None, float("inf")
        return self._bk[ticker][i], when - ts[i]


# ─────────────────────────────────────────────────────────────
# REPLAY SHIMS
# ─────────────────────────────────────────────────────────────
class ReplayClient:
    """Serves PositionManager.get_price()'s per-ticker fallback from recordings.

    That fallback is not an edge case here, it is the near-expiry path. The
    universe feed drops a contract ~5 minutes before its close (recorder blind
    spot, fixed for recording in 3b8459a but the feed still stops listing them),
    so a held position disappears from `_quotes` exactly when time_forced_no and
    the stop fire. get_price() then falls through to /markets/<ticker>, and a
    raising client would be swallowed by its bare `except:` and return
    (0, 0, "", "") — silently pricing the most important window at zero.

    The books stream still covers those minutes, because the recorder marks held
    positions (`held: true`) and captures them every RECORD_BOOK_INTERVAL. So
    answer from real recorded top-of-book, and count how often it was needed.
    """

    def __init__(self, books: "BookStore", close_times: dict[str, str]) -> None:
        self.calls = 0
        self.served = 0
        self.unserved = 0
        self._books = books
        self._ct = close_times

    def _request(self, method, endpoint, *args, **kwargs):
        self.calls += 1
        if not endpoint.startswith("/markets/"):
            raise RuntimeError(f"replay attempted an unsupported request: {endpoint}")
        ticker = endpoint.split("/markets/", 1)[1].split("/")[0]
        book, _stale = self._books.at(ticker, CLOCK.now)
        if book is None:
            self.unserved += 1
            raise RuntimeError(f"no recorded book for {ticker}")
        self.served += 1
        yes = book.get("yes") or []
        no = book.get("no") or []
        # Top of book from resting depth: best YES bid is the highest yes level;
        # the YES ask is the complement of the best NO bid (Kalshi books are
        # buy-side only on both sides).
        yes_bid = max((p for p, _ in yes), default=0.0) / 100.0
        no_bid = max((p for p, _ in no), default=0.0) / 100.0
        yes_ask = (1.0 - no_bid) if no_bid > 0 else 0.0
        return {"market": {
            "yes_bid_dollars": yes_bid,
            "yes_ask_dollars": yes_ask,
            "close_time": self._ct.get(ticker, ""),
            "status": "active",
        }}

    def login(self):
        return None


class ReplayLadder:
    """Stands in for ladder.Ladder as PositionManager.get_price() sees it: a
    `_quotes` map of ticker -> (yes_bid, yes_ask, close_time, status) plus the
    time it was refreshed. Rebuilt from the universe row every tick, so it is
    always fresh and get_price never falls through to a direct fetch."""

    def __init__(self) -> None:
        self._quotes: dict[str, tuple] = {}
        self._quotes_t: float = 0.0

    def publish(self, row: dict, now_epoch: float) -> None:
        quotes = {}
        for m in row.get("m") or []:
            tk = m.get("tk")
            if not tk:
                continue
            quotes[tk] = (float(m.get("b") or 0.0), float(m.get("a") or 0.0),
                          m.get("ct", ""), "active")
        self._quotes = quotes
        self._quotes_t = now_epoch


class ReplayPortfolio(Portfolio):
    """The bot's Portfolio with its two market-data reads redirected at the
    recordings. Everything else — sizing, fees, _walk_book, the sell path,
    settle_paper_position — is the production code, unmodified."""

    def __init__(self, client, books: BookStore, ladder: ReplayLadder) -> None:
        super().__init__(client)
        self._books = books
        self._ladder = ladder
        self.stale_samples: list[float] = []
        self.no_book_events = 0
        self.log: list[dict] = []

    def _log_trade(self, action, ticker, side, count, price, true_prob=None,
                   pnl=None, peak_pnl_pct=None, reason="", **kw):
        """Capture to memory instead of appending to trades.csv.

        The base implementation appends to the LIVE trade log. On the first run
        of this harness that wrote 18 replayed 08-29/08-30 round trips into
        trades.csv stamped with the wall-clock date, mixed in among the running
        bot's real rows — the same file every P&L number in this repo is
        computed from. Removed by hand; this override is why it cannot recur.
        """
        self.log.append({
            "t": dt.datetime.fromtimestamp(CLOCK.now, UTC),
            "action": action, "ticker": ticker, "count": count,
            "price": price, "pnl": pnl, "reason": (reason or "").strip(),
        })

    def _orderbook(self, ticker: str) -> dict:
        book, stale = self._books.at(ticker, CLOCK.now)
        if book is None:
            self.no_book_events += 1
            return {}
        self.stale_samples.append(stale)
        return book

    def _fresh_quote(self, ticker: str, attempts: int = 3) -> tuple:
        hit = self._ladder._quotes.get(ticker)
        if not hit:
            return 0.0, 0.0
        bid, ask = hit[0], hit[1]
        if bid > 0 and ask > 0:
            return bid, ask
        return 0.0, 0.0


# ─────────────────────────────────────────────────────────────
# DRIVER
# ─────────────────────────────────────────────────────────────
def daterange_paths(stream: str, start: str, end: str) -> list[str]:
    out = []
    for p in sorted(glob.glob(f"recordings/{stream}_*.jsonl.gz")):
        day = Path(p).stem.split("_")[1][:10]
        if start <= day <= end:
            out.append(p)
    return out


def build_spot_series(quote_paths: list[str]) -> tuple[list[float], list[float]]:
    ts, sp = [], []
    for path in quote_paths:
        for row in tolerant_jsonl_gz(path):
            s = row.get("spot")
            if s is None:
                continue
            try:
                ts.append(dt.datetime.fromisoformat(row["t"]).timestamp())
            except Exception:
                continue
            sp.append(float(s))
    order = sorted(range(len(ts)), key=lambda i: ts[i])
    return [ts[i] for i in order], [sp[i] for i in order]


def spot_at(ts: list[float], sp: list[float], when: float,
            tolerance: float = 120.0) -> float | None:
    """Recorded spot nearest `when`. test_expiring_window.py documents the quotes
    stream as the settlement source — it is the only one continuous through
    expiry, since the universe feed drops contracts ~5 min before close."""
    if not ts:
        return None
    i = bisect.bisect_left(ts, when)
    best, bestd = None, float("inf")
    for j in (i - 1, i, i + 1):
        if 0 <= j < len(ts):
            d = abs(ts[j] - when)
            if d < bestd:
                best, bestd = sp[j], d
    return best if bestd <= tolerance else None


def run(start: str, end: str, capital: float, verbose: bool) -> dict:
    install_sim_clock()

    uni_paths = daterange_paths("universe", start, end)
    bk_paths = daterange_paths("books", start, end)
    q_paths = daterange_paths("quotes", start, end)
    if not uni_paths:
        raise SystemExit(f"no universe recordings in {start}..{end} "
                         "(universe starts 2026-08-12)")

    books = BookStore()
    books.load(bk_paths)
    s_ts, s_sp = build_spot_series(q_paths)
    print(f"  loaded {len(uni_paths)} universe / {len(bk_paths)} book / "
          f"{len(q_paths)} quote days · {books.rows:,} book rows · "
          f"{len(s_ts):,} spot samples")

    ticks: list[dict] = []
    for p in uni_paths:
        ticks.extend(tolerant_jsonl_gz(p))
    ticks.sort(key=lambda r: r.get("t", ""))

    # The universe stream carries {t, spot, win, m} and NO regime — regime lives
    # in the quotes stream. Reading row["rg"] off a universe row yields None for
    # every tick, which skips the signal call entirely and reports a clean zero
    # rather than an error. Same failure shape as the no_threshold gate.
    qrows: list[dict] = []
    for p in q_paths:
        qrows.extend(tolerant_jsonl_gz(p))
    qrows.sort(key=lambda r: r.get("t", ""))
    before = len(ticks)
    ticks = join_regimes(ticks, qrows, tolerance_secs=5)
    print(f"  {before:,} universe ticks -> {len(ticks):,} with a regime joined")

    # Recorded ticker -> close_time, so the near-expiry fallback can answer with
    # the real close rather than "", which _hours_from() turns into 1.0 and which
    # would make an expired contract look like it had an hour left.
    close_times: dict[str, str] = {}
    for r in ticks:
        for m in r.get("m") or []:
            tk, ct = m.get("tk"), m.get("ct")
            if tk and ct:
                close_times[tk] = ct

    # Never write recordings from a replay.
    recorder_mod.ENABLED = False

    client = ReplayClient(books, close_times)
    ladder = ReplayLadder()
    pf = ReplayPortfolio(client, books, ladder)
    pf.sync()                      # paper baseline: cash = PAPER_CAPITAL
    pf.real_cash = capital
    pf.start_total = capital
    pf.peak_total = capital

    dist = DistModel()
    engine = SignalEngine(dist, use_market_posterior=True)
    pm = PositionManager(client, pf, dist, None, ladder=ladder)

    # The production ladder print()s a watch line per position per tick and a
    # banner per fill. Over ~800k replayed ticks that is the single largest cost
    # in the run and it buries the summary. Swallow it; progress goes to stderr.
    class _Null(io.TextIOBase):
        def write(self, _s):
            return 0

    entries = 0
    started = _wall.time()
    day = None
    sink = _Null()
    for i, row in enumerate(ticks):
        try:
            now = dt.datetime.fromisoformat(row["t"])
        except Exception:
            continue
        if now.date() != day:
            day = now.date()
            print(f"    {day}  tick {i:>7,}/{len(ticks):,}  "
                  f"entries={entries}  P&L=${pf.realized_pnl:+.2f}  "
                  f"[{_wall.time()-started:.0f}s]", file=sys.stderr, flush=True)
        CLOCK.now = now.timestamp()
        ladder.publish(row, CLOCK.now)

        rg = row.get("rg") or {}
        spot, vol = row.get("spot"), rg.get("v")
        if spot is None or not vol:
            continue
        regime = {
            "regime": rg.get("r"), "direction": rg.get("d"), "vol": vol,
            "zscore": rg.get("z") or 0.0, "mom": rg.get("m") or 0.0,
        }

        # 1. EXITS FIRST, exactly as app.py orders them: exits are never gated.
        with contextlib.redirect_stdout(sink):
            pm.manage(float(spot), float(vol), regime)

        # 2. Settle anything whose close_time has passed. Live, Kalshi's status
        #    flips and positions.manage() catches it; the recordings carry no
        #    status field, so resolve from recorded spot at close instead.
        for tk in list(pf.positions.keys()):
            pos = pf.positions[tk]
            ct = pos.get("close_time") or ""
            if not ct or _hours_from_sim(ct) > 0:
                continue
            try:
                close_epoch = dt.datetime.fromisoformat(
                    ct.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            ss = spot_at(s_ts, s_sp, close_epoch)
            if ss is None:
                pf.positions.pop(tk, None)      # unresolvable, drop uncounted
                continue
            c = pos["contract"]
            yes_won = float(c["low"]) <= ss < float(c["high"])
            with contextlib.redirect_stdout(sink):
                pf.settle_paper_position(tk, 0.0 if yes_won else 1.0)

        # 3. ENTRIES, behind the same gate live uses.
        with contextlib.redirect_stdout(sink):
            tradeable = pf.can_trade()
        if not tradeable:
            continue
        lad = normalize_universe(row, now)
        sig = engine.find_boundary_no(
            float(spot), float(vol), regime, lad, pf.positions,
            real_cash=pf.real_cash, start_total=pf.start_total,
        )
        if sig is None:
            continue
        with contextlib.redirect_stdout(sink):
            ok = pf.buy_no(sig, float(sig.get("true_prob") or 0.0), dist,
                           float(spot), float(vol), regime)
        if ok:
            entries += 1
            if verbose:
                print(f"  [{now:%m-%d %H:%M}] BUY_NO {sig['ticker'][-18:]} "
                      f"true={float(sig.get('true_prob') or 0):.0%}")

    return {
        "entries": entries, "portfolio": pf, "client": client,
        "ticks": len(ticks),
    }


def report(res: dict, capital: float) -> None:
    pf: ReplayPortfolio = res["portfolio"]
    sells = [r for r in pf.log if r["action"] == "sell"]
    print(f"\n{'═'*62}\n  EXIT-LADDER REPLAY — real quotes, real depth\n{'─'*62}")
    print(f"  Entries:            {res['entries']}")
    print(f"  Still open at end:  {len(pf.positions)}")
    print(f"  Realized P&L:       ${pf.realized_pnl:+.2f}")
    print(f"  Return:             {pf.realized_pnl / capital:+.2%}")
    if pf.stale_samples:
        s = sorted(pf.stale_samples)
        print(f"  Book staleness:     median {s[len(s)//2]:.1f}s  "
              f"p90 {s[int(len(s)*0.9)]:.1f}s  n={len(s):,}")
    print(f"  Exits with no recorded book: {pf.no_book_events}")
    print(f"  Near-expiry book fallback:   {res['client'].served} served, "
          f"{res['client'].unserved} unserved")
    if sells:
        wins = [r for r in sells if (r["pnl"] or 0) > 0]
        print(f"  Round trips:        {len(sells)}   win rate {len(wins)/len(sells):.1%}")
        agg: dict[str, list] = {}
        for r in sells:
            a = agg.setdefault(r["reason"] or "?", [0, 0.0])
            a[0] += 1
            a[1] += r["pnl"] or 0.0
        print(f"{'─'*62}\n  Exit breakdown:")
        for k, (n, p) in sorted(agg.items(), key=lambda x: -x[1][1]):
            print(f"    {k:24s} {n:4d} trades   ${p:+9.2f}   avg ${p/n:+6.2f}")
    print(f"{'═'*62}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2026-08-12")
    ap.add_argument("--end", default="2026-08-31")
    ap.add_argument("--capital", type=float, default=500.0)
    ap.add_argument("--no-stop", type=float, default=None,
                    help="override NO_STOP for this run")
    ap.add_argument("--edge-gone-ratio", type=float, default=None,
                    help="override NO_EDGE_GONE_RATIO for this run")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.no_stop is not None:
        C.NO_STOP = args.no_stop
    if args.edge_gone_ratio is not None:
        C.NO_EDGE_GONE_RATIO = args.edge_gone_ratio

    print(f"\n  🔁 EXIT-LADDER REPLAY  {args.start} -> {args.end}  "
          f"${args.capital:.0f}")
    print(f"  NO_STOP={C.NO_STOP}  NO_EDGE_GONE_RATIO={C.NO_EDGE_GONE_RATIO}  "
          f"MIN_HOLD_SECS={C.MIN_HOLD_SECS}")
    res = run(args.start, args.end, args.capital, args.verbose)
    report(res, args.capital)


if __name__ == "__main__":
    main()
