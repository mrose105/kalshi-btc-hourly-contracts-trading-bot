"""In-place dashboard renderer for live bot viewing.

Enable with KALSHI_LIVE_VIEW=1 env var. When enabled:
  - positions.py suppresses its per-tick 👁 lines and pushes state to snapshots
  - portfolio.py routes buy/sell notifications through log_event
  - app.py scan_step calls render() once per scan tick, which clears the screen
    and reprints a fixed dashboard: header + ladder + open positions + recent
    events. Everything updates in place at the scan interval (2s by default).
"""
import os
import threading
from collections import deque
from datetime import datetime

ENABLED = os.getenv("KALSHI_LIVE_VIEW") == "1"

_lock = threading.Lock()
_events: deque[str] = deque(maxlen=6)
_snapshots: dict[str, dict] = {}


def log_event(msg: str) -> None:
    if not ENABLED:
        return
    ts = datetime.now().strftime("%H:%M:%S")
    with _lock:
        _events.append(f"{ts}  {msg}")


def update_position(ticker: str, snapshot: dict) -> None:
    with _lock:
        _snapshots[ticker] = snapshot


def drop_position(ticker: str) -> None:
    with _lock:
        _snapshots.pop(ticker, None)


def mark_to_market(positions: dict) -> float:
    """Live market value of all open positions using latest bid snapshots."""
    with _lock:
        return sum(
            pos["count"] * _snapshots.get(tk, {}).get("bid", pos["entry"])
            for tk, pos in positions.items()
        )


def _fmt_position(ticker: str, snap: dict) -> str:
    itm = "✅" if snap.get("itm") else "❌"
    snipe = " 🎯" if snap.get("is_snipe") else "  "
    return (
        f"  {ticker[-22:]:<22}{snipe} "
        f"bid=${snap.get('bid', 0):.3f}  "
        f"pnl={snap.get('pnl_pct', 0):+.0%}  "
        f"true={snap.get('true_prob', 0):.0%}  "
        f"{itm} dist={snap.get('dist', 0):+.0f}  "
        f"{snap.get('mins_left', 0):.0f}m"
    )


def render(header: str, ladder_rows: list[str], portfolio) -> None:
    """Called from scan_step at the end of each tick. All args pre-formatted."""
    if not ENABLED:
        return
    lines: list[str] = []
    lines.append("\033[H\033[2J")          # cursor home + clear screen
    lines.append(header)
    lines.append("")
    if ladder_rows:
        lines.append(" LADDER (top 8 by |dist|)")
        lines.extend(ladder_rows)
        lines.append("")
    with _lock:
        snaps = dict(_snapshots)
        evs = list(_events)
    if snaps:
        lines.append(f" OPEN POSITIONS ({len(snaps)})")
        for tk in sorted(snaps):
            lines.append(_fmt_position(tk, snaps[tk]))
        lines.append("")
    lines.append(" RECENT EVENTS")
    if evs:
        for e in evs:
            lines.append(f"  {e}")
    else:
        lines.append("  (none yet)")
    print("\n".join(lines), flush=True)
