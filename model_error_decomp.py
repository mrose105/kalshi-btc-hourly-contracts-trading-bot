"""Is the model's 1.95x underestimate the DRIFT term or the vol scaling?

THE ERROR. Measured 2026-09-01 over 176 live NO round trips resolved against
Kalshi settlement: DistModel said those bands get hit 14.0% of the time; they
were hit 27.3%. Every gate downstream of true_prob is tuned against that number.

TWO SUSPECTS, DIFFERENT SIGNATURES.

  DRIFT: for a REVERTING regime, model.true_prob applies
              drift = -zscore * vol_t * 0.15
         a hardcoded mean-reversion prior shifting the distribution AWAY from
         the direction of the move. BOUNDARY_NO buys NO on OTM bands in exactly
         that direction. Signature: ASYMMETRIC by side, scales with |z|.

  VOL SCALING: vol_t = vol_h * sqrt(hours), the IID assumption. If real
         short-horizon dispersion is not sqrt(t), the error is SYMMETRIC by
         side and varies with HOURS.

Decisive test: recompute every observation with the drift disabled (RANGING
takes no drift branch), holding vol, tail shape and floors identical.

KNOWN GAP: `vol_regime` is not in the recorded regime dict, so it defaults to
NORMAL here while live may have used HIGH (x1.15) or LOW (x0.92). Reported.
"""
from __future__ import annotations
import argparse, datetime as dt, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from kalshi_btc_bot import config as C
from kalshi_btc_bot.model import DistModel
from boundary_no_quote_replay import join_regimes, normalize_universe, tolerant_jsonl_gz
from wing_calibration import (percentile_bootstrap_interval, daterange, spot_at, spot_series)

HOURS = ((0,10),(10,20),(20,30),(30,45),(45,70))
ZB = ((1.4,1.8),(1.8,2.3),(2.3,3.0),(3.0,99.0))

def summ(rows, i=0, j=1):
    n=len(rows)
    if not n: return 0,0.0,0.0,0.0
    p=sum(r[i] for r in rows)/n; a=sum(r[j] for r in rows)/n
    return n,p,a,(a/p if p>0 else 0.0)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-08-12"); ap.add_argument("--end", default="2026-09-01")
    ap.add_argument("--every", type=int, default=4)
    a=ap.parse_args()
    if a.every<1: ap.error("--every must be >= 1")
    uni=daterange("universe",a.start,a.end); qs=daterange("quotes",a.start,a.end)
    if not uni: raise SystemExit("no universe recordings")
    s_ts,s_sp=spot_series(qs)
    print(f"  {len(s_ts):,} spot samples / {len(qs)} quote days", flush=True)
    dist=DistModel()
    bh=defaultdict(list); bz=defaultdict(list); bd=defaultdict(list)
    ndh=defaultdict(list); allr=[]; alln=[]; seen=set()
    byday={Path(p).stem.split("_")[1][:10]:p for p in qs}
    for up in uni:
        day=Path(up).stem.split("_")[1][:10]
        if day not in byday: continue
        u=tolerant_jsonl_gz(up); u.sort(key=lambda r:r.get("t",""))
        q=tolerant_jsonl_gz(byday[day]); q.sort(key=lambda r:r.get("t",""))
        ticks=join_regimes(u,q,tolerance_secs=5)
        print(f"    {day} {len(ticks):,}", flush=True)
        for i,row in enumerate(ticks):
            if i%a.every: continue
            rg=row.get("rg") or {}; spot=row.get("spot"); vol=rg.get("v")
            if spot is None or not vol: continue
            if rg.get("r") not in ("RANGING","REVERTING"): continue
            z=rg.get("z") or 0.0
            if abs(z)<C.BOUNDARY_NO_ZSCORE_MIN: continue
            try: now=dt.datetime.fromisoformat(row["t"])
            except Exception: continue
            reg={"regime":rg.get("r"),"direction":rg.get("d"),"vol":vol,"zscore":z,"mom":rg.get("m") or 0.0}
            nd={**reg,"regime":"RANGING"}
            for c in normalize_universe(row,now):
                mins=float(c["hours"])*60.0
                hb=next((t for t in HOURS if t[0]<=mins<t[1]),None)
                if hb is None: continue
                try: close=dt.datetime.fromisoformat(str(c["close_time"]).replace("Z","+00:00")).timestamp()
                except Exception: continue
                ss=spot_at(s_ts,s_sp,close,120.0)
                if ss is None: continue
                k=(c["ticker"],int(mins)//2)
                if k in seen: continue
                seen.add(k)
                lo,hi=float(c["low"]),float(c["high"])
                hit=1.0 if lo<=ss<hi else 0.0
                tp=dist.true_prob(c,float(spot),float(vol),float(c["hours"]),reg)
                tpn=dist.true_prob(c,float(spot),float(vol),float(c["hours"]),nd)
                exp=c["ticker"].rsplit("-",1)[0]
                allr.append((tp,hit,exp)); alln.append((tpn,hit,exp))
                bh[hb].append((tp,hit,exp)); ndh[hb].append((tpn,hit,exp))
                zz=next((t for t in ZB if t[0]<=abs(z)<t[1]),None)
                if zz: bz[zz].append((tp,hit,exp))
                side=("occupied" if lo<=float(spot)<hi
                      else "continuation" if (lo>float(spot))==(z>0) else "counter")
                bd[side].append((tp,tpn,hit,exp))
    n,p,av,r=summ(allr); _,pn,_,rn=summ(alln)
    print(f"\n  {len(allr):,} band-observations at BOUNDARY_NO-qualifying moments\n")
    print(f"  OVERALL        model {p:.4f}  realized {av:.4f}  {r:.2f}x under")
    print(f"  drift REMOVED  model {pn:.4f}  realized {av:.4f}  {rn:.2f}x under")
    if r>1: print(f"  -> removing drift closes {((r-rn)/(r-1)):.0%} of the gap to 1.00x")
    print(f"\n  BY TIME TO EXPIRY  (vol-scaling signature)")
    print(f"  {'bucket':>10s} {'n':>7s} {'model':>8s} {'realized':>9s} {'ratio':>7s} {'no-drift':>9s}")
    for hb in HOURS:
        n_,p_,a_,r_=summ(bh[hb])
        if n_<40: continue
        _,_,_,rn_=summ(ndh[hb])
        print(f"  {f'{hb[0]}-{hb[1]}m':>10s} {n_:7d} {p_:8.4f} {a_:9.4f} {r_:6.2f}x {rn_:8.2f}x")
    print(f"\n  BY |ZSCORE|  (drift signature)")
    print(f"  {'bucket':>10s} {'n':>7s} {'model':>8s} {'realized':>9s} {'ratio':>7s}")
    for zb in ZB:
        n_,p_,a_,r_=summ(bz[zb])
        if n_<40: continue
        print(f"  {(f'{zb[0]}-{zb[1]}' if zb[1]<99 else f'{zb[0]}+'):>10s} {n_:7d} {p_:8.4f} {a_:9.4f} {r_:6.2f}x")
    print(f"\n  BY SIDE  (drift ASYMMETRIC, vol scaling symmetric)")
    print(f"  {'side':>13s} {'n':>7s} {'model':>8s} {'no-drift':>9s} {'realized':>9s} {'ratio':>7s}")
    for s in ("continuation","occupied","counter"):
        v=bd.get(s) or []
        if len(v)<40: continue
        n_=len(v); p_=sum(x[0] for x in v)/n_; pn_=sum(x[1] for x in v)/n_; a_=sum(x[2] for x in v)/n_
        print(f"  {s:>13s} {n_:7d} {p_:8.4f} {pn_:9.4f} {a_:9.4f} {(a_/p_ if p_ else 0):6.2f}x")
    be=defaultdict(list)
    for tp,hit,exp in allr: be[exp].append(hit-tp)
    cm=[sum(v)/len(v) for v in be.values()]
    lo_,hi_=percentile_bootstrap_interval(cm)
    if lo_ is not None:
        m=sum(cm)/len(cm)
        v="POSITIVE" if lo_>0 else "NEGATIVE" if hi_<0 else "spans 0"
        print(f"\n  realized minus model, clustered ({len(be)} expiries)")
        print(f"    mean {m:+.4f}  95% CI [{lo_:+.4f}, {hi_:+.4f}]  {v}")
        print(f"    positive = model UNDER-predicts how often bands are hit")

if __name__=="__main__":
    main()
