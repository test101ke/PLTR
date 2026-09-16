"""
Walk-forward replay of the desk's own rules over recent history.
================================================================

The dashboard keeps no signal log — it holds live state and overwrites it — so
"what did it call last week" cannot be read back. This reconstructs the calls
from candles instead, replaying the SAME code the live desk runs (amd.compute)
with each decision made using only data available at that moment.

What can honestly be replayed:
  * AMD / Power of 3   — pure price logic, fully reconstructible.
  * 1-15M timeframes   — pure price logic, fully reconstructible.
What cannot:
  * the composite signal — it blends live order-book flow and AI-scored news,
    neither of which is stored. Any "backtest" of it would be invented.
"""
import sys, math, json, asyncio, datetime as dt
import httpx
import amd as amdlib

UA = {"User-Agent": "Mozilla/5.0 (PLTR-Signal-Desk-Replay)"}
SYM = "PLTRUSDT"


async def klines(client, interval, days):
    """Binance USDⓈ-M futures klines, paginated back `days`."""
    end = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    per = {"1m": 60_000, "5m": 300_000}[interval]
    want = int(days * 24 * 60 * 60_000 / per)
    out, cur = [], end
    while len(out) < want:
        n = min(1500, want - len(out))
        start = cur - n * per
        u = (f"https://fapi.binance.com/fapi/v1/klines?symbol={SYM}&interval={interval}"
             f"&startTime={start}&endTime={cur}&limit=1500")
        r = await client.get(u, headers=UA, timeout=25)
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        out = rows + out
        cur = rows[0][0] - 1
        if len(rows) < 2:
            break
    seen, clean = set(), []
    for k in sorted(out, key=lambda x: x[0]):
        if k[0] in seen:
            continue
        seen.add(k[0])
        clean.append({"t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
                      "l": float(k[3]), "c": float(k[4]), "v": float(k[5])})
    return clean


# ----------------------------------------------------------------- AMD replay
def replay_amd(c5, c1, days):
    """For each day: decide at the reclaim, then walk forward to T1 or stop."""
    today = dt.datetime.now(dt.timezone.utc).date()
    rows = []
    for back in range(days, 0, -1):
        day = today - dt.timedelta(days=back)
        asia, london, ny = amdlib.session_windows(day)
        # pass 1 — everything up to the London close, to locate the reclaim
        upto_london = [k for k in c5 if k["t"] < london[1]]
        if len(upto_london) < 40:
            continue
        probe = amdlib.compute(upto_london, now_ms=london[1])
        if not probe.get("ok"):
            rows.append({"day": str(day), "outcome": "no-data"}); continue
        if not probe.get("swept"):
            rows.append({"day": str(day), "outcome": "no-signal", "why": "London never swept"}); continue
        if not probe.get("reclaimed") or not probe.get("reclaimT"):
            rows.append({"day": str(day), "outcome": "no-signal", "why": "swept but never reclaimed",
                         "swept": probe["swept"]}); continue
        rt = probe["reclaimT"]
        # pass 2 — recompute using ONLY candles up to the reclaim (no lookahead)
        at_decision = amdlib.compute([k for k in c5 if k["t"] <= rt], now_ms=rt)
        lv = at_decision.get("levels")
        if not lv:
            rows.append({"day": str(day), "outcome": "no-signal", "why": "no levels at decision"}); continue
        side, entry, stop, t1, t2 = lv["side"], lv["entry"], lv["stop"], lv["t1"], lv["t2"]
        risk = abs(entry - stop)
        if risk <= 0:
            rows.append({"day": str(day), "outcome": "no-signal", "why": "zero risk"}); continue
        # walk 1m candles from the reclaim to the NY close
        fwd = [k for k in c1 if rt <= k["t"] <= ny[1]]
        outcome, hit_t, rmult = "open", None, None
        for k in fwd:
            hit_stop = (k["l"] <= stop) if side == "long" else (k["h"] >= stop)
            hit_t1 = (k["h"] >= t1) if side == "long" else (k["l"] <= t1)
            if hit_stop and hit_t1:
                outcome, hit_t, rmult = "loss", k["t"], -1.0; break   # same bar: assume the worse
            if hit_stop:
                outcome, hit_t, rmult = "loss", k["t"], -1.0; break
            if hit_t1:
                outcome, hit_t, rmult = "win", k["t"], abs(t1 - entry) / risk; break
        if outcome == "open" and fwd:
            last = fwd[-1]["c"]
            rmult = ((last - entry) / risk) if side == "long" else ((entry - last) / risk)
        rows.append({"day": str(day), "outcome": outcome, "side": side, "swept": probe["swept"],
                     "bias": at_decision.get("bias"), "entry": entry, "stop": stop, "t1": t1, "t2": t2,
                     "risk": round(risk, 2), "R": round(rmult, 2) if rmult is not None else None,
                     "asiaRangePct": round((at_decision["range"] / entry) * 100, 3) if entry else None,
                     "barsToResolve": (len([1 for k in fwd if k["t"] <= hit_t]) if hit_t else None)})
    return rows


# ------------------------------------------------------- timeframe replay
def replay_timeframes(c1, bias=0.0):
    """Replay the 1/3/5/10/15M calls and score each against its own horizon."""
    px = [(k["t"], k["c"]) for k in c1]
    idx = {t: i for i, (t, _) in enumerate(px)}
    res = {}
    for tf in (1, 3, 5, 10, 15):
        k = math.sqrt(tf); buy, strong = 0.12 * k, 0.40 * k
        w = l = flat = 0; rets = []; longs = shorts = 0
        base_up = base_n = 0
        for i in range(tf, len(px) - tf):
            past = px[i - tf][1]; now = px[i][1]; fut = px[i + tf][1]
            if not past or not now or not fut:
                continue
            r = (now / past - 1) * 100
            radj = r + bias * 0.06 * k
            fwd = (fut / now - 1) * 100
            base_n += 1; base_up += 1 if fwd > 0 else 0
            if radj >= buy:
                longs += 1; rets.append(fwd); w += 1 if fwd > 0 else 0; l += 1 if fwd < 0 else 0
                flat += 1 if fwd == 0 else 0
            elif radj <= -buy:
                shorts += 1; rets.append(-fwd); w += 1 if fwd < 0 else 0; l += 1 if fwd > 0 else 0
                flat += 1 if fwd == 0 else 0
        n = w + l
        res[f"{tf}M"] = {"signals": longs + shorts, "long": longs, "short": shorts,
                         "wins": w, "losses": l, "hit": round(w / n * 100, 1) if n else None,
                         "avgRetPct": round(sum(rets) / len(rets), 4) if rets else None,
                         "baseRateUpPct": round(base_up / base_n * 100, 1) if base_n else None}
    return res


def wilson(w, n, z=1.96):
    if not n:
        return (0.0, 0.0)
    p = w / n; d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round((c - m) * 100, 1), round((c + m) * 100, 1))


def binom_p(w, n, p0=0.5):
    if not n:
        return 1.0
    from math import comb
    return round(sum(comb(n, i) * p0**i * (1 - p0)**(n - i) for i in range(w, n + 1)), 4)


async def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    async with httpx.AsyncClient(follow_redirects=True) as cl:
        c5 = await klines(cl, "5m", days + 2)
        c1 = await klines(cl, "1m", days + 1)
    out = {"symbol": SYM, "days": days,
           "candles5m": len(c5), "candles1m": len(c1),
           "from": dt.datetime.fromtimestamp(c1[0]["t"] / 1000, dt.timezone.utc).isoformat() if c1 else None,
           "to": dt.datetime.fromtimestamp(c1[-1]["t"] / 1000, dt.timezone.utc).isoformat() if c1 else None}
    amd_rows = replay_amd(c5, c1, days)
    out["amd"] = amd_rows
    traded = [r for r in amd_rows if r["outcome"] in ("win", "loss")]
    wins = sum(1 for r in traded if r["outcome"] == "win")
    out["amdSummary"] = {
        "days": len(amd_rows),
        "noSignal": sum(1 for r in amd_rows if r["outcome"] == "no-signal"),
        "open": sum(1 for r in amd_rows if r["outcome"] == "open"),
        "resolved": len(traded), "wins": wins, "losses": len(traded) - wins,
        "hitPct": round(wins / len(traded) * 100, 1) if traded else None,
        "wilson95": wilson(wins, len(traded)),
        "pValueVsCoinflip": binom_p(wins, len(traded)),
        "totalR": round(sum(r["R"] for r in amd_rows if r.get("R") is not None), 2),
    }
    out["timeframes"] = replay_timeframes(c1)
    print(json.dumps(out, indent=1))

asyncio.run(main())
