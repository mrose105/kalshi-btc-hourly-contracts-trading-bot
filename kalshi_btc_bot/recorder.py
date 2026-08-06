"""Market-data recorder — captures what the bot actually saw, so the backtest
can eventually replay against a real book instead of pricing off its own model.

Enable with KALSHI_RECORD=1. Writes gzipped JSONL under recordings/, one file
per stream per UTC day.

Three streams, in ascending order of importance to the problem they exist to
solve:

  quotes  one line per scan tick — spot, regime, and the whole visible ladder.
          Reconstructs the entry-side decision set.

  marks   one line per position-management cycle — the live bid/ask on every
          open position. THIS is the stream that fixes exit pricing. The
          backtest currently derives its exit bid from DistModel.true_prob,
          so a simulated exit books a profit that depends on the model being
          right — the exact thing the backtest is meant to be testing. Replaying
          against recorded marks removes that circularity.

  orders  one line per buy/sell attempt — full order-book depth at the moment
          of the decision, the limit sent, and what actually filled. Lets
          realized slippage be measured against top-of-book rather than assumed,
          and lets the adverse-selection haircut in the backtest be calibrated
          from data instead of guessed.

Writing happens on a background thread behind a bounded queue. The trading
threads only ever do a non-blocking put, so recording cannot slow an entry or —
more importantly — delay an exit. If the queue fills (disk stall), records are
dropped and counted rather than blocking the bot.
"""
import atexit
import datetime
import gzip
import json
import os
import queue
import threading
from pathlib import Path

ENABLED = os.getenv("KALSHI_RECORD") == "1"

_DIR = Path(__file__).parent.parent / "recordings"
_QUEUE: "queue.Queue[tuple[str, dict] | None]" = queue.Queue(maxsize=10000)
_worker: threading.Thread | None = None
_handles: dict[str, gzip.GzipFile] = {}
_lock = threading.Lock()

# Flush cadence, per stream. quotes/books run into the tens of thousands of
# writes a day and get batched to bound disk I/O; orders and marks are the two
# streams the exit-pricing fix actually depends on (realized slippage, real
# bid at exit time) and are flushed close to immediately. A single flush
# counter shared across all four streams was tried first and is wrong: with
# ~25,000 quotes writes against 6 order writes in one session, the odds an
# order write lands on a shared global threshold are near zero, so the orders
# stream sat unflushed in memory all day — invisible to anything reading the
# file, and one kill -9 away from actually being lost (a graceful shutdown
# still flushes via close(), wired to both KeyboardInterrupt and atexit).
# walls: flushed immediately like orders. Snapshots arrive every ~5 min from a
# standalone poller, so the default (50) would leave hours of irreplaceable
# data unflushed — and unlike quotes it can never be re-fetched, since Deribit
# publishes no historical open interest.
_FLUSH_EVERY = {"orders": 1, "walls": 1, "marks": 5, "books": 50, "quotes": 200}
_counts: dict[str, int] = {}

dropped = 0
written = 0


def _path(stream: str, day: str) -> Path:
    return _DIR / f"{stream}_{day}.jsonl.gz"


def _handle(stream: str, day: str):
    """One open handle per stream, rolled when the UTC day changes."""
    key = f"{stream}:{day}"
    h = _handles.get(key)
    if h is None:
        for k in [k for k in _handles if k.startswith(f"{stream}:")]:
            try:
                _handles.pop(k).close()
            except Exception:
                pass
        _DIR.mkdir(exist_ok=True)
        h = gzip.open(_path(stream, day), "at", encoding="utf-8")
        _handles[key] = h
    return h


def _run() -> None:
    global written
    while True:
        item = _QUEUE.get()
        if item is None:
            break
        stream, rec = item
        try:
            day = rec["t"][:10]
            h = _handle(stream, day)
            h.write(json.dumps(rec, separators=(",", ":")) + "\n")
            written += 1
            n = _counts.get(stream, 0) + 1
            _counts[stream] = n
            if n % _FLUSH_EVERY.get(stream, 50) == 0:
                h.flush()
        except Exception:
            pass


def _start() -> None:
    global _worker
    if not ENABLED or _worker is not None:
        return
    with _lock:
        if _worker is not None:
            return
        _worker = threading.Thread(target=_run, daemon=True, name="recorder")
        _worker.start()
        atexit.register(close)


def _emit(stream: str, rec: dict) -> None:
    global dropped
    if not ENABLED:
        return
    if _worker is None:
        _start()
    try:
        _QUEUE.put_nowait((stream, rec))
    except queue.Full:
        dropped += 1


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


def record_quotes(spot: float, regime: dict, ladder: list) -> None:
    """Scan-tick snapshot: spot, regime read, and every visible ladder row."""
    if not ENABLED:
        return
    _emit("quotes", {
        "t": _now(),
        "spot": round(spot, 2),
        "rg": {
            "r":  regime.get("regime"),
            "d":  regime.get("direction"),
            "v":  regime.get("vol"),
            "vh": regime.get("vol_h"),
            "vr": regime.get("vol_ratio"),
            "vc": regime.get("vol_compression"),
            "z":  regime.get("zscore"),
            "m":  regime.get("mom"),
        },
        "l": [{
            "tk":   c.get("ticker"),
            "b":    c.get("bid"),
            "a":    c.get("ask"),
            "v":    c.get("vol"),
            "h":    c.get("hours"),
            "lo":   c.get("low"),
            "hi":   c.get("high"),
            "d":    c.get("otm_dist"),
            "itm":  c.get("itm"),
        } for c in ladder],
    })


def record_mark(ticker: str, bid: float, ask: float, hours: float,
                pos: dict, true_prob: float, spot: float) -> None:
    """Per-cycle mark on an open position — the real bid an exit would hit."""
    if not ENABLED:
        return
    _emit("marks", {
        "t":     _now(),
        "tk":    ticker,
        "b":     bid,
        "a":     ask,
        "h":     hours,
        "spot":  round(spot, 2),
        "entry": pos.get("entry"),
        "cnt":   pos.get("count"),
        "peak":  pos.get("peak"),
        "tp":    round(true_prob, 4) if true_prob is not None else None,
        "no":    bool(pos.get("is_no")),
        "sn":    bool(pos.get("is_snipe")),
    })


def record_order(event: str, ticker: str, side: str, bid: float, ask: float,
                 book: dict | None, limit: float, want: int,
                 filled: int, fill_px: float, reason: str = "",
                 true_prob: float | None = None) -> None:
    """Order attempt with the book depth that was standing behind it.

    `book` is the raw orderbook payload ({"yes": [...], "no": [...]}) so
    realized fill price can later be checked against the depth that actually
    existed, rather than assumed from top-of-book.
    """
    if not ENABLED:
        return
    _emit("orders", {
        "t":    _now(),
        "ev":   event,
        "tk":   ticker,
        "side": side,
        "b":    bid,
        "a":    ask,
        "book": book,
        "lim":  limit,
        "want": want,
        "fill": filled,
        "px":   fill_px,
        "why":  reason,
        "tp":   round(true_prob, 4) if true_prob is not None else None,
    })


def record_book(ticker: str, book: dict, bid: float, ask: float,
                hours: float, spot: float, held: bool) -> None:
    """Full resting depth for one contract, sampled on its own cadence.

    Capturing depth only at order moments yields a few dozen snapshots a day —
    enough to measure realized slippage, nowhere near enough to replay fills.
    This stream samples the whole visible ladder plus every open position at
    RECORD_BOOK_INTERVAL, so a replay can walk real resting size at any point
    in the session instead of assuming infinite liquidity at top-of-book.

    Levels are stored raw as the API returns them (price_cents, quantity) for
    both sides. On Kalshi both sides are resting BUY orders — yes_levels are
    bids for YES, no_levels are bids for NO — so an exit's true fill is a walk
    down the side being sold into.
    """
    if not ENABLED:
        return
    _emit("books", {
        "t":    _now(),
        "tk":   ticker,
        "b":    bid,
        "a":    ask,
        "h":    hours,
        "spot": round(spot, 2),
        "held": held,
        "yes":  (book or {}).get("yes") or [],
        "no":   (book or {}).get("no") or [],
    })


def record_walls(walls: dict) -> None:
    """Deribit options-wall snapshot (see deribit_walls.py).

    Deliberately NOT called from the bot's polling loop — walls come from an
    external API (Deribit), and putting that network call in the trading hot
    path would add latency and a new failure mode to live trading for a signal
    that is not yet validated. `python3 deribit_walls.py --record` runs this
    standalone instead.

    Exists because Deribit's public API exposes only a CURRENT open-interest
    snapshot — there is no historical OI endpoint — so the walls hypothesis
    cannot be backtested against history the way everything else in this repo
    is. The only path to validating it is accruing snapshots forward from now.
    """
    if not ENABLED:
        return
    _emit("walls", {
        "t": _now(),
        "exp": walls["expiry"],
        "spot": round(walls["spot"], 2),
        "h_exp": round(walls["hours_to_expiry"], 3),
        "gex": round(walls["total_signed_gex"], 1),
        # only strikes with real gamma weight — far-OTM rows are ~0 and would
        # bloat every snapshot for no analytical value
        "k": [
            {
                "s": s["strike"], "c": round(s["call_oi"], 1),
                "p": round(s["put_oi"], 1), "iv": round(s["iv"], 4),
                "w": round(s["wall_strength"], 1),
                "d": round(s["dist_pct"], 3),
            }
            for s in walls["strikes"] if s["wall_strength"] > 0
        ],
    })


def close() -> None:
    """Flush and close. Registered atexit; safe to call more than once."""
    if _worker is None:
        return
    try:
        _QUEUE.put_nowait(None)
    except queue.Full:
        pass
    _worker.join(timeout=5)
    for h in list(_handles.values()):
        try:
            h.close()
        except Exception:
            pass
    _handles.clear()


def stats() -> str:
    return f"recorded={written} dropped={dropped}"
