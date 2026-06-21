import csv
import threading
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify

app = Flask(__name__)
_state = {}
_lock  = threading.Lock()

_LOG_PATH = Path(__file__).parent.parent / "trades.csv"

def update(data: dict):
    with _lock:
        _state.update(data)
        _state["updated"] = datetime.now().strftime("%H:%M:%S")

def _recent_trades(n=20):
    try:
        with open(_LOG_PATH, newline="") as f:
            rows = list(csv.DictReader(f))
        return list(reversed(rows[-n:]))
    except Exception:
        return []

def start(port=5001):
    t = threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port,
                                                 debug=False, use_reloader=False),
                         daemon=True)
    t.start()
    print(f"  🌐 Dashboard: http://localhost:{port}")

@app.route("/api/status")
def api_status():
    with _lock:
        return jsonify({**_state, "trades": _recent_trades()})

_HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>BTC QUANT</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #0d0d0d; color: #e0e0e0; font-family: 'Courier New', monospace;
            font-size: 14px; padding: 12px; }}
    h1 {{ color: #f90; font-size: 18px; margin-bottom: 10px; }}
    .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 12px; }}
    .card {{ background: #1a1a1a; border: 1px solid #333; border-radius: 6px; padding: 10px; }}
    .card .label {{ color: #888; font-size: 11px; text-transform: uppercase; margin-bottom: 2px; }}
    .card .value {{ font-size: 20px; font-weight: bold; }}
    .pos  {{ color: #4caf50; }} .neg {{ color: #f44336; }} .neu {{ color: #e0e0e0; }}
    .tag  {{ display: inline-block; padding: 2px 6px; border-radius: 4px;
             font-size: 11px; background: #333; margin-right: 4px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
    th    {{ color: #888; text-align: left; padding: 4px 6px; border-bottom: 1px solid #333; }}
    td    {{ padding: 4px 6px; border-bottom: 1px solid #222; }}
    .buy  {{ color: #4caf50; }} .sell {{ color: #f44336; }}
    #upd  {{ color: #555; font-size: 11px; text-align: right; margin-top: 8px; }}
    .pos-card {{ background: #1a1a1a; border: 1px solid #333; border-radius: 6px;
                 padding: 10px; margin-bottom: 8px; }}
  </style>
</head>
<body>
<h1>🧠 BTC QUANT v4.3</h1>
<div class="grid">
  <div class="card"><div class="label">BTC Price</div><div class="value neu" id="btc">—</div></div>
  <div class="card"><div class="label">Regime</div><div class="value neu" id="regime">—</div></div>
  <div class="card"><div class="label">Cash</div><div class="value neu" id="cash">—</div></div>
  <div class="card"><div class="label">Total / P&L</div><div class="value" id="pnl">—</div></div>
</div>
<div id="positions"></div>
<table>
  <thead><tr><th>Time</th><th>Action</th><th>Ticker</th><th>Qty</th><th>Price</th><th>P&L</th><th>Reason</th></tr></thead>
  <tbody id="trades"></tbody>
</table>
<div id="upd"></div>
<script>
async function refresh() {{
  try {{
    const d = await fetch('/api/status').then(r => r.json());
    document.getElementById('btc').textContent = d.btc ? '$' + d.btc.toLocaleString() : '—';
    document.getElementById('regime').textContent = (d.regime || '—') + ' ' + (d.direction || '');
    document.getElementById('cash').textContent = d.cash != null ? '$' + d.cash.toFixed(2) : '—';
    const pnl = d.pnl || 0;
    const pnlEl = document.getElementById('pnl');
    pnlEl.textContent = d.total != null ? '$' + d.total.toFixed(2) + '  ' + (pnl >= 0 ? '+' : '') + pnl.toFixed(2) : '—';
    pnlEl.className = 'value ' + (pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : 'neu');

    const posDiv = document.getElementById('positions');
    if (d.positions && d.positions.length) {{
      posDiv.innerHTML = d.positions.map(p =>
        `<div class="pos-card"><b>${{p.ticker}}</b> x${{p.count}} @ $${{p.entry.toFixed(2)}}
         &nbsp; <span class="${{p.pnl_pct >= 0 ? 'pos' : 'neg'}}">${{(p.pnl_pct * 100).toFixed(0)}}%</span>
         &nbsp; true=${{(p.true_prob * 100).toFixed(0)}}% &nbsp; ${{p.mins_left.toFixed(0)}}m left</div>`
      ).join('');
    }} else {{
      posDiv.innerHTML = '<div style="color:#555;margin-bottom:8px;">No open positions</div>';
    }}

    const tbody = document.getElementById('trades');
    tbody.innerHTML = (d.trades || []).map(t =>
      `<tr><td>${{t.timestamp.slice(11,16)}}</td>
       <td class="${{t.action}}">${{t.action.toUpperCase()}}</td>
       <td>${{t.ticker.slice(-12)}}</td>
       <td>${{t.count}}</td>
       <td>$${{parseFloat(t.price).toFixed(2)}}</td>
       <td class="${{parseFloat(t.pnl||0) >= 0 ? 'pos' : 'neg'}}">${{t.pnl ? (parseFloat(t.pnl) >= 0 ? '+' : '') + parseFloat(t.pnl).toFixed(2) : ''}}</td>
       <td>${{t.reason || ''}}</td></tr>`
    ).join('');
    document.getElementById('upd').textContent = 'Updated ' + (d.updated || '');
  }} catch(e) {{ document.getElementById('upd').textContent = 'Disconnected'; }}
}}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""

@app.route("/")
def index():
    return _HTML
