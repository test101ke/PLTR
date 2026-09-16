"""
Long-horizon walk-forward replay, with an out-of-sample split.

The 7-day run gave 5 AMD setups — enough to see HOW the rules fail, nowhere near
enough to say whether they work. This runs the same walk-forward replay over
months, then splits older-70% / newer-30% so any parameter choice is judged on
days it never saw. Fees are charged on every trade.
"""
import sys, math, json, asyncio, time, datetime as dt
import httpx
import amd as amdlib

UA = {"User-Agent": "Mozilla/5.0 (PLTR-Signal-Desk-Replay)"}
SYM = "PLTRUSDT"
FEE_PCT = float(sys.argv[2]) if len(sys.argv) > 2 else 0.05    # round-trip, percent


def log(*a):
    print(*a, file=sys.stderr, flush=True)


async def klines(client, interval, days, compact=False):
    end = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    per = {"1m": 60_000, "5m": 300_000, "15m": 900_000}[interval]
    want = int(days * 24 * 60 * 60_000 / per)
    out, cur, calls = [], end, 0
    while len(out) < want:
        n = min(1500, want - len(out))
        u = (f"https://fapi.binance.com/fapi/v1/klines?symbol={SYM}&interval={interval}"
             f"&startTime={cur - n * per}&endTime={cur}&limit=1500")
        for attempt in range(3):
            try:
                r = await client.get(u, headers=UA, timeout=30)
                r.raise_for_status()
                rows = r.json(); break
            except Exception as e:
                if attempt == 2:
                    log(f"  {interval}: giving up after {calls} calls ({e})"); rows = []
                await asyncio.sleep(1.5)
        if not rows:
            break
        out = rows + out
        cur = rows[0][0] - 1
        calls += 1
        if calls % 20 == 0:
            log(f"  {interval}: {len(out)}/{want} candles")
        await asyncio.sleep(0.12)          # stay well inside the weight limit
    seen, clean = set(), []
    for k in sorted(out, key=lambda x: x[0]):
        t = int(k[0])
        if t in seen:
            continue
        seen.add(t)
        if compact:
            clean.append((t, float(k[2]), float(k[3]), float(k[4])))   # t, high, low, close
        else:
            clean.append({"t": t, "o": float(k[1]), "h": float(k[2]),
                          "l": float(k[3]), "c": float(k[4]), "v": float(k[5])})
    return clean


def wilson(w, n, z=1.96):
    if not n:
        return (0.0, 0.0)
    p = w / n; d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round((c - m) * 100, 1), round((c + m) * 100, 1))


def z_p(w, n, p0=0.5):
    if not n:
        return None, None
    p = w / n; se = math.sqrt(p0 * (1 - p0) / n)
    z = (p - p0) / se
    return round(z, 2), round(math.erfc(abs(z) / math.sqrt(2)), 4)


# --------------------------------------------------------------- AMD
def amd_run(c5, c1, days, min_stop_pct=0.0, fee_pct=FEE_PCT, reclaim_buffer_pct=0.0):
    """min_stop_pct: floor on entry-to-stop. reclaim_buffer_pct: require the
    reclaim to close this far back inside the range, not merely inside it."""
    by_t = {}
    for t, h, l, c in c1:
        by_t.setdefault(t // 86_400_000, []).append((t, h, l, c))
    today = dt.datetime.now(dt.timezone.utc).date()
    rows = []
    for back in range(days, 0, -1):
        day = today - dt.timedelta(days=back)
        asia, london, ny = amdlib.session_windows(day)
        upto = [k for k in c5 if k["t"] < london[1]]
        if len(upto) < 60:
            continue
        probe = amdlib.compute(upto, now_ms=london[1])
        if not probe.get("ok"):
            continue
        if not probe.get("swept"):
            rows.append({"day": str(day), "res": "no-signal"}); continue
        if not probe.get("reclaimed") or not probe.get("reclaimT"):
            rows.append({"day": str(day), "res": "no-signal"}); continue
        rt = probe["reclaimT"]
        d = amdlib.compute([k for k in c5 if k["t"] <= rt], now_ms=rt)
        lv = d.get("levels")
        if not lv:
            rows.append({"day": str(day), "res": "no-signal"}); continue
        side, entry, t1 = lv["side"], lv["entry"], lv["t1"]
        stop = lv["stop"]
        if reclaim_buffer_pct:
            need = entry * reclaim_buffer_pct / 100
            ok = (d.get("last", 0) >= entry + need) if side == "long" else (d.get("last", 1e9) <= entry - need)
            if not ok:
                rows.append({"day": str(day), "res": "filtered"}); continue
        if min_stop_pct:
            need = entry * min_stop_pct / 100
            if abs(entry - stop) < need:
                stop = entry - need if side == "long" else entry + need
        risk = abs(entry - stop)
        if risk <= 0:
            rows.append({"day": str(day), "res": "no-signal"}); continue
        fwd = [x for k in range((rt // 86_400_000) - 1, (ny[1] // 86_400_000) + 1)
               for x in by_t.get(k, []) if rt <= x[0] <= ny[1]]
        fwd.sort()
        res, R = "open", None
        for t, h, l, c in fwd:
            hs = (l <= stop) if side == "long" else (h >= stop)
            ht = (h >= t1) if side == "long" else (l <= t1)
            if hs:
                res, R = "loss", -1.0; break
            if ht:
                res, R = "win", abs(t1 - entry) / risk; break
        if res == "open" and fwd:
            last = fwd[-1][3]
            R = ((last - entry) / risk) if side == "long" else ((entry - last) / risk)
        if R is not None:                       # charge the round trip in R terms
            R -= (entry * fee_pct / 100) / risk
        rows.append({"day": str(day), "res": res, "side": side,
                     "riskPct": round(risk / entry * 100, 3),
                     "R": round(R, 2) if R is not None else None})
    return rows


def amd_stats(rows):
    tr = [r for r in rows if r["res"] in ("win", "loss")]
    w = sum(1 for r in tr if r["res"] == "win")
    Rs = [r["R"] for r in rows if r.get("R") is not None]
    z, p = z_p(w, len(tr))
    return {"days": len(rows), "noSignal": sum(1 for r in rows if r["res"] == "no-signal"),
            "filtered": sum(1 for r in rows if r["res"] == "filtered"),
            "resolved": len(tr), "wins": w, "hit": round(w / len(tr) * 100, 1) if tr else None,
            "wilson95": wilson(w, len(tr)), "z": z, "p": p,
            "totalR": round(sum(Rs), 2) if Rs else None,
            "avgR": round(sum(Rs) / len(Rs), 3) if Rs else None,
            "medRiskPct": round(sorted(r["riskPct"] for r in rows if "riskPct" in r)[len([r for r in rows if "riskPct" in r]) // 2], 3) if any("riskPct" in r for r in rows) else None}


# --------------------------------------------------------------- timeframes
def tf_run(c1, invert, fee_pct, thresh_mult=1.0):
    px = [(t, c) for t, h, l, c in c1]
    out = {}
    for tf in (1, 3, 5, 10, 15):
        k = math.sqrt(tf); buy = 0.12 * k * thresh_mult
        w = l = 0; rets = []
        for i in range(tf, len(px) - tf):
            past, now, fut = px[i - tf][1], px[i][1], px[i + tf][1]
            r = (now / past - 1) * 100
            sig = 1 if r >= buy else -1 if r <= -buy else 0
            if not sig:
                continue
            if invert:
                sig = -sig
            pnl = ((fut / now - 1) * 100) * sig - fee_pct
            rets.append(pnl); w += 1 if pnl > 0 else 0; l += 1 if pnl < 0 else 0
        n = w + l; z, p = z_p(w, n)
        out[f"{tf}M"] = {"n": n, "hit": round(w / n * 100, 1) if n else None,
                         "avgPct": round(sum(rets) / len(rets), 4) if rets else None,
                         "totalPct": round(sum(rets), 1) if rets else None, "z": z, "p": p}
    return out


async def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 180
    t0 = time.time()
    log(f"fetching ~{days}d of candles for {SYM} …")
    async with httpx.AsyncClient(follow_redirects=True) as cl:
        c5 = await klines(cl, "5m", days + 2)
        log(f"5m done: {len(c5)}")
        c1 = await klines(cl, "1m", days + 1, compact=True)
        log(f"1m done: {len(c1)}  ({time.time()-t0:.0f}s)")
    if not c5 or not c1:
        print(json.dumps({"error": "no candle data"})); return
    cov_days = round((c1[-1][0] - c1[0][0]) / 86_400_000, 1)
    out = {"symbol": SYM, "requestedDays": days, "actualCoverageDays": cov_days,
           "feePctRoundTrip": FEE_PCT, "candles5m": len(c5), "candles1m": len(c1),
           "from": dt.datetime.fromtimestamp(c1[0][0] / 1000, dt.timezone.utc).isoformat(),
           "to": dt.datetime.fromtimestamp(c1[-1][0] / 1000, dt.timezone.utc).isoformat(),
           "moveOverPeriodPct": round((c1[-1][3] / c1[0][3] - 1) * 100, 2)}

    log("replaying AMD variants …")
    variants = {"asis": dict(min_stop_pct=0.0),
                "minStop_0.25": dict(min_stop_pct=0.25),
                "minStop_0.50": dict(min_stop_pct=0.50),
                "minStop_0.75": dict(min_stop_pct=0.75),
                "minStop_0.50_reclaim_0.10": dict(min_stop_pct=0.50, reclaim_buffer_pct=0.10)}
    out["amd"] = {}
    for name, kw in variants.items():
        rows = amd_run(c5, c1, days, **kw)
        split = int(len(rows) * 0.7)
        out["amd"][name] = {"all": amd_stats(rows),
                            "train_old70": amd_stats(rows[:split]),
                            "test_new30": amd_stats(rows[split:])}
        log(f"  {name}: {out['amd'][name]['all']['hit']}% hit, {out['amd'][name]['all']['totalR']}R")

    log("replaying timeframes …")
    half = int(len(c1) * 0.7)
    out["timeframes"] = {
        "asis_withFees": tf_run(c1, False, FEE_PCT),
        "inverted_noFees": tf_run(c1, True, 0.0),
        "inverted_withFees": tf_run(c1, True, FEE_PCT),
        "inverted_withFees_2xThreshold": tf_run(c1, True, FEE_PCT, thresh_mult=2.0),
        "inverted_withFees_4xThreshold": tf_run(c1, True, FEE_PCT, thresh_mult=4.0),
        "_split": {"train_old70_invertedFees": tf_run(c1[:half], True, FEE_PCT, 4.0),
                   "test_new30_invertedFees": tf_run(c1[half:], True, FEE_PCT, 4.0)},
    }
    log(f"done in {time.time()-t0:.0f}s")
    print(json.dumps(out, indent=1))

asyncio.run(main())
