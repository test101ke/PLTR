"""Second pass: WHY the rules failed, and what a fix would have done."""
import sys, math, json, asyncio, statistics as stats, datetime as dt
import httpx, amd as amdlib
from replay import klines, wilson, binom_p

def z_test(w, n, p0=0.5):
    if not n: return None, None
    p = w/n; se = math.sqrt(p0*(1-p0)/n)
    z = (p-p0)/se
    # two-sided normal p
    pv = math.erfc(abs(z)/math.sqrt(2))
    return round(z,2), round(pv,4)

def tf_test(c1, invert=False, fee_pct=0.0):
    px=[(k["t"],k["c"]) for k in c1]; out={}
    for tf in (1,3,5,10,15):
        k=math.sqrt(tf); buy=0.12*k
        w=l=0; rets=[]
        for i in range(tf,len(px)-tf):
            past,now,fut = px[i-tf][1],px[i][1],px[i+tf][1]
            r=(now/past-1)*100; fwd=(fut/now-1)*100
            sig = 1 if r>=buy else -1 if r<=-buy else 0
            if not sig: continue
            if invert: sig=-sig
            pnl = (fwd*sig) - fee_pct
            rets.append(pnl); w+= 1 if pnl>0 else 0; l+= 1 if pnl<0 else 0
        n=w+l; zz,pv=z_test(w,n)
        out[f"{tf}M"]={"n":n,"hit":round(w/n*100,1) if n else None,
                       "avgPct":round(sum(rets)/len(rets),4) if rets else None,
                       "totalPct":round(sum(rets),2) if rets else None,
                       "z":zz,"p":pv}
    return out

def amd_stops(c5,c1,days,min_stop_pct=None,buffer_pct=0.0):
    """Re-run AMD with a MINIMUM stop distance / buffer beyond the sweep."""
    today=dt.datetime.now(dt.timezone.utc).date(); rows=[]
    for back in range(days,0,-1):
        day=today-dt.timedelta(days=back)
        asia,london,ny = amdlib.session_windows(day)
        upto=[k for k in c5 if k["t"]<london[1]]
        if len(upto)<40: continue
        probe=amdlib.compute(upto,now_ms=london[1])
        if not probe.get("ok") or not probe.get("reclaimed") or not probe.get("reclaimT"): continue
        rt=probe["reclaimT"]
        d=amdlib.compute([k for k in c5 if k["t"]<=rt],now_ms=rt)
        lv=d.get("levels")
        if not lv: continue
        side,entry,t1=lv["side"],lv["entry"],lv["t1"]
        stop=lv["stop"]
        if buffer_pct:
            stop = stop*(1-buffer_pct/100) if side=="long" else stop*(1+buffer_pct/100)
        if min_stop_pct:
            need=entry*min_stop_pct/100
            if abs(entry-stop)<need:
                stop = entry-need if side=="long" else entry+need
        risk=abs(entry-stop)
        if risk<=0: continue
        fwd=[k for k in c1 if rt<=k["t"]<=ny[1]]
        res,R="open",None
        for k in fwd:
            hs=(k["l"]<=stop) if side=="long" else (k["h"]>=stop)
            ht=(k["h"]>=t1) if side=="long" else (k["l"]<=t1)
            if hs: res,R="loss",-1.0; break
            if ht: res,R="win",abs(t1-entry)/risk; break
        if res=="open" and fwd:
            last=fwd[-1]["c"]; R=((last-entry)/risk) if side=="long" else ((entry-last)/risk)
        rows.append({"day":str(day),"res":res,"riskPct":round(risk/entry*100,3),"R":round(R,2) if R is not None else None})
    tr=[r for r in rows if r["res"] in ("win","loss")]
    w=sum(1 for r in tr if r["res"]=="win")
    return {"rows":rows,"resolved":len(tr),"wins":w,
            "hit":round(w/len(tr)*100,1) if tr else None,
            "totalR":round(sum(r["R"] for r in rows if r["R"] is not None),2)}

async def main():
    days=int(sys.argv[1]) if len(sys.argv)>1 else 7
    async with httpx.AsyncClient(follow_redirects=True) as cl:
        c5=await klines(cl,"5m",days+2); c1=await klines(cl,"1m",days+1)
    first,last=c1[0]["c"],c1[-1]["c"]
    out={"weekMovePct":round((last/first-1)*100,2),"from":first,"to":last,
         "realisedVol1mPct":round(stats.pstdev([ (c1[i]["c"]/c1[i-1]["c"]-1)*100 for i in range(1,len(c1))]),4)}
    out["timeframes_asis"]=tf_test(c1)
    out["timeframes_inverted"]=tf_test(c1,invert=True)
    out["timeframes_inverted_withFees"]=tf_test(c1,invert=True,fee_pct=0.05)
    out["amd_asis"]=amd_stops(c5,c1,days)
    out["amd_minStop_0p25"]=amd_stops(c5,c1,days,min_stop_pct=0.25)
    out["amd_minStop_0p50"]=amd_stops(c5,c1,days,min_stop_pct=0.50)
    out["amd_minStop_1p00"]=amd_stops(c5,c1,days,min_stop_pct=1.00)
    print(json.dumps(out,indent=1))
asyncio.run(main())
