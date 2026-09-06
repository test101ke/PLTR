"""
PLTR Signal Desk — open-window backtest engine (multi-venue futures)
====================================================================
Tests the "US market open" behaviour on a PLTR crypto contract:
at 09:30 America/New_York (16:30 EAT) the tokenized/derivative price jumps to
catch up with the reopening NASDAQ stock, then trends. This module measures,
over the available 1-minute history, whether a rule seen in the first ~2 minutes
predicts the move through 09:45 ET with >=70% reliability.

DATA SOURCE — PLTR futures/perpetuals first, spot as fallback.
It auto-discovers a PLTR contract across venues, in order:
  1. Bybit USDT perpetual   (category=linear)
  2. Binance USDⓈ-M futures (fapi)
  3. KuCoin futures
  4. Bybit spot xStock      (category=spot)   ← fallback
The first venue that resolves a PLTR symbol AND returns candles is used;
the chosen "source" is reported in the output. Override with BT_VENUE /
BT_SYMBOL if you want to pin one.

HONEST STATS (unchanged): train/test split, Wilson CI, binomial p-value,
and a "passed" flag only when the TEST hit rate >= 0.70, its 95% lower bound
> 0.50, and n_test >= 30. Testing many rules inflates the best in-sample
number — the out-of-sample figure is the honest one. Gross of fees/slippage;
these contracts can be thin at the open, so paper-trade before automating.

Run:  python backtest.py            (writes static/backtest.json)
"""
import os, json, math, asyncio, datetime as dt
from zoneinfo import ZoneInfo
import httpx
try:
    import numpy as np
except Exception:
    np = None

UA = {"User-Agent": "Mozilla/5.0 (PLTR-Signal-Desk backtest)"}
NY = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))
SIG_MIN = int(os.getenv("BT_SIGNAL_MIN", "2"))
WIN_MIN = int(os.getenv("BT_WINDOW_MIN", "15"))


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n; d = 1 + z * z / n; c = p + z * z / (2 * n)
    m = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return ((c - m) / d, (c + m) / d)


def binom_p(k, n, p0=0.5):
    if n == 0:
        return 1.0
    mu = n * p0; sd = math.sqrt(n * p0 * (1 - p0))
    if sd == 0:
        return 1.0
    z = (k - mu) / sd
    return 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))


async def _get(client, url, headers=None):
    for _ in range(3):
        try:
            r = await client.get(url, headers=headers or UA, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception:
            await asyncio.sleep(0.5)
    return None


def _pick(cands):
    if not cands:
        return None
    for s in cands:
        if s.upper().endswith("USDT") or s.upper().endswith("USDTM"):
            return s
    return cands[0]


# ---------- per-venue: discover symbol + fetch 1-min klines as [ms,o,h,l,c,v] ----------
async def bybit_disc(client, cat):
    j = await _get(client, f"https://api.bybit.com/v5/market/instruments-info?category={cat}")
    if not j or j.get("retCode") not in (0, "0", None):
        return None
    return _pick([x["symbol"] for x in j["result"]["list"] if "PLTR" in x["symbol"].upper()])

async def bybit_kl(client, cat, sym, start, end):
    j = await _get(client, f"https://api.bybit.com/v5/market/kline?category={cat}&symbol={sym}&interval=1&start={start}&end={end}&limit=60")
    if not j or not j.get("result", {}).get("list"):
        return []
    return [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in j["result"]["list"]]

async def binance_disc(client):
    j = await _get(client, "https://fapi.binance.com/fapi/v1/exchangeInfo")
    if not j:
        return None
    return _pick([s["symbol"] for s in j.get("symbols", [])
                  if "PLTR" in s["symbol"].upper() and s.get("status") == "TRADING"])

async def binance_kl(client, sym, start, end):
    j = await _get(client, f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval=1m&startTime={start}&endTime={end}&limit=60")
    if not isinstance(j, list):
        return []
    return [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in j]

async def kucoin_disc(client):
    j = await _get(client, "https://api-futures.kucoin.com/api/v1/contracts/active")
    if not j or j.get("code") not in ("200000", None):
        return None
    return _pick([c["symbol"] for c in j.get("data", []) if "PLTR" in c["symbol"].upper()])

async def kucoin_kl(client, sym, start, end):
    j = await _get(client, f"https://api-futures.kucoin.com/api/v1/kline/query?symbol={sym}&granularity=1&from={start}&to={end}")
    if not j or not j.get("data"):
        return []
    # KuCoin: [time(ms), open, high, low, close, volume]
    return [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in j["data"]]


VENUES = [
    ("bybit-perp",   lambda c: bybit_disc(c, "linear"), lambda c, s, a, b: bybit_kl(c, "linear", s, a, b)),
    ("binance-perp", binance_disc,                      lambda c, s, a, b: binance_kl(c, s, a, b)),
    ("kucoin-perp",  kucoin_disc,                       lambda c, s, a, b: kucoin_kl(c, s, a, b)),
    ("bybit-spot",   lambda c: bybit_disc(c, "spot"),   lambda c, s, a, b: bybit_kl(c, "spot", s, a, b)),
]


async def resolve_source(client):
    """Return (venue_name, symbol, klfn) for the first venue that has PLTR data."""
    forced_v = os.getenv("BT_VENUE", "").strip()
    forced_s = os.getenv("BT_SYMBOL", "").strip()
    probe_day = dt.datetime.now(NY).date() - dt.timedelta(days=1)
    while probe_day.weekday() >= 5:
        probe_day -= dt.timedelta(days=1)
    op = dt.datetime.combine(probe_day, dt.time(9, 30), tzinfo=NY)
    a, b = int((op - dt.timedelta(minutes=2)).timestamp() * 1000), int((op + dt.timedelta(minutes=10)).timestamp() * 1000)
    for name, disc, klfn in VENUES:
        if forced_v and forced_v != name:
            continue
        try:
            sym = forced_s or await disc(client)
            if not sym:
                continue
            # KuCoin uses seconds for from/to
            aa, bb = (a, b) if "kucoin" not in name else (a // 1000, b // 1000)
            rows = await klfn(client, sym, aa, bb)
            if rows:
                return name, sym, klfn
        except Exception:
            continue
    return None, None, None


async def run_backtest(days=200):
    if np is None:
        return {"error": "numpy not installed", "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat()}
    async with httpx.AsyncClient(follow_redirects=True) as client:
        venue, symbol, klfn = await resolve_source(client)
        if not symbol:
            return {"error": "no PLTR futures/spot contract reachable on Bybit, Binance or KuCoin",
                    "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat()}
        is_kucoin = "kucoin" in venue
        today = dt.datetime.now(NY).date()
        dates, d = [], today - dt.timedelta(days=1)
        while len(dates) < days:
            if d.weekday() < 5:
                dates.append(d)
            d -= dt.timedelta(days=1)
        dates.reverse()

        sem = asyncio.Semaphore(4)

        async def one(day):
            async with sem:
                op = dt.datetime.combine(day, dt.time(9, 30), tzinfo=NY)
                a = int((op - dt.timedelta(minutes=5)).timestamp() * 1000)
                b = int((op + dt.timedelta(minutes=WIN_MIN + 1)).timestamp() * 1000)
                aa, bb = (a, b) if not is_kucoin else (a // 1000, b // 1000)
                rows = await klfn(client, symbol, aa, bb)
                await asyncio.sleep(0.12)
                if not rows:
                    return None
                open_ms = int(op.timestamp() * 1000)
                cw = {}
                for r in rows:
                    t = r[0] if r[0] > 1e12 else r[0] * 1000  # normalise s->ms
                    off = round((t - open_ms) / 60000)
                    cw[off] = {"o": r[1], "c": r[4], "v": r[5]}
                if not all(k in cw for k in (0, SIG_MIN, WIN_MIN)):
                    return None
                pre = cw.get(-1, cw[0]); o0 = cw[0]["o"]
                p_sig = cw[SIG_MIN]["c"]; p_end = cw[WIN_MIN]["c"]
                gap = (o0 / pre["c"] - 1) if pre["c"] else 0.0
                r_sig = (p_sig / o0 - 1) if o0 else 0.0
                r_rem = (p_end / p_sig - 1) if p_sig else 0.0
                rng2 = max((abs(cw[m]["c"] / o0 - 1) for m in range(0, SIG_MIN + 1) if m in cw), default=0.0)
                vol2 = sum(cw[m]["v"] for m in range(0, SIG_MIN + 1) if m in cw)
                profile = []
                for m in range(1, WIN_MIN + 1):
                    if m in cw:
                        prev = cw.get(m - 1, cw[0])["c"]
                        profile.append((m, (cw[m]["c"] / prev - 1) if prev else 0.0))
                return {"day": day.isoformat(), "gap": gap, "r_sig": r_sig, "r_rem": r_rem,
                        "rng2": rng2, "vol2": vol2, "profile": profile}

        results = await asyncio.gather(*[one(dd) for dd in dates])
        samples = [s for s in results if s]

    n = len(samples)
    out = {"generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
           "source": f"{venue}:{symbol}", "venue": venue, "symbol": symbol,
           "nDays": n, "signalMin": SIG_MIN, "windowMin": WIN_MIN,
           "dateRange": [samples[0]["day"], samples[-1]["day"]] if samples else None,
           "notes": [], "rules": [], "minuteProfile": [], "best": None, "passed70": False}
    if n < 20:
        out["notes"].append(f"Only {n} usable days from {venue}:{symbol} — too few to trust any edge. "
                            "The contract's 1-minute history may be short, or the open window had gaps.")
        _save(out); return out

    for m in range(1, WIN_MIN + 1):
        vals = [dict(s["profile"]).get(m) for s in samples]
        vals = [v for v in vals if v is not None]
        if vals:
            arr = np.array(vals)
            out["minuteProfile"].append({"m": m, "avgAbs": float(np.mean(np.abs(arr))),
                                         "avgSigned": float(np.mean(arr)), "upShare": float(np.mean(arr > 0))})

    cut = int(n * 0.7); train, test = samples[:cut], samples[cut:]

    def eval_rule(fn, name, param=None):
        def hit(sub):
            picks = [(fn(s), s["r_rem"]) for s in sub]
            picks = [(d, r) for d, r in picks if d != 0]
            if not picks:
                return (0, 0, 0.0)
            k = sum(1 for d, r in picks if (r > 0) == (d > 0))
            exp = float(np.mean([abs(r) if (r > 0) == (d > 0) else -abs(r) for d, r in picks]))
            return (k, len(picks), exp)
        ktr, ntr, _ = hit(train); kte, nte, expte = hit(test); lo, _ = wilson(kte, nte)
        return {"name": name, "param": param,
                "trainHit": round(ktr / ntr, 3) if ntr else None, "nTrain": ntr,
                "testHit": round(kte / nte, 3) if nte else None, "nTest": nte,
                "ciLow": round(lo, 3), "p": round(binom_p(kte, nte), 4),
                "expectancy": round(expte * 100, 3)}

    for th in (0.0, 0.001, 0.002, 0.003, 0.005, 0.008):
        out["rules"].append(eval_rule(lambda s, th=th: (1 if s["r_sig"] > th else -1 if s["r_sig"] < -th else 0),
                                      f"Momentum: first-{SIG_MIN}min move > {th*100:.1f}% continues", th))
    for th in (0.002, 0.005, 0.01):
        out["rules"].append(eval_rule(lambda s, th=th: (-1 if s["gap"] > th else 1 if s["gap"] < -th else 0),
                                      f"Gap fade: fade gap > {th*100:.1f}%", th))
    for th in (0.005, 0.01):
        out["rules"].append(eval_rule(lambda s, th=th: (1 if s["gap"] > th else -1 if s["gap"] < -th else 0),
                                      f"Gap-and-go: follow gap > {th*100:.1f}%", th))
    try:
        X = np.array([[s["gap"], s["r_sig"], s["rng2"], s["vol2"]] for s in samples], float)
        y = np.array([1.0 if s["r_rem"] > 0 else 0.0 for s in samples])
        mu, sd = X[:cut].mean(0), X[:cut].std(0) + 1e-9
        Xn = (X - mu) / sd; Xtr, Xte, ytr, yte = Xn[:cut], Xn[cut:], y[:cut], y[cut:]
        w = np.zeros(Xtr.shape[1]); b = 0.0
        for _ in range(4000):
            p = 1 / (1 + np.exp(-(Xtr @ w + b))); g = p - ytr
            w -= 0.1 * (Xtr.T @ g / len(ytr) + 0.01 * w); b -= 0.1 * g.mean()
        pred = (1 / (1 + np.exp(-(Xte @ w + b))) > 0.5).astype(float)
        k = int((pred == yte).sum()); nte = len(yte); lo, _ = wilson(k, nte)
        out["rules"].append({"name": "Logistic reg [gap, r_sig, range, vol]", "param": None,
                             "trainHit": None, "nTrain": len(ytr),
                             "testHit": round(k / nte, 3) if nte else None, "nTest": nte,
                             "ciLow": round(lo, 3), "p": round(binom_p(k, nte), 4), "expectancy": None})
    except Exception as e:
        out["notes"].append(f"logistic model skipped: {e}")

    valid = [r for r in out["rules"] if r.get("testHit") is not None and r.get("nTest", 0) >= 30]
    if valid:
        best = max(valid, key=lambda r: r["testHit"]); out["best"] = best
        passes = best["testHit"] >= 0.70 and best["ciLow"] > 0.50 and best["nTest"] >= 30
        out["passed70"] = bool(passes)
        out["notes"].append(
            (f"Best out-of-sample rule: '{best['name']}' — {best['testHit']*100:.0f}% on {best['nTest']} "
             f"held-out days (95% floor {best['ciLow']*100:.0f}%, p={best['p']}). ")
            + ("CLEARS the 70% bar out-of-sample — validate live before automating."
               if passes else "Does NOT clear a trustworthy 70% out-of-sample. Treat as no reliable edge yet."))
    else:
        out["notes"].append("No rule had >=30 out-of-sample trades — need more history to judge.")
    out["notes"].append(f"Source: {venue} contract {symbol}. Tested {len(out['rules'])} rules; the out-of-sample figure is the honest one.")
    out["notes"].append("Gross of fees/slippage; these contracts can be thin at the open — model real fills before automating.")
    _save(out); return out


def _save(out):
    try:
        os.makedirs(os.path.join(HERE, "static"), exist_ok=True)
        with open(os.path.join(HERE, "static", "backtest.json"), "w") as f:
            json.dump(out, f, indent=2)
    except Exception:
        pass


if __name__ == "__main__":
    r = asyncio.run(run_backtest(days=int(os.getenv("BT_DAYS", "200"))))
    print(json.dumps({k: r.get(k) for k in ("source", "nDays", "dateRange", "passed70", "best", "error")}, indent=2))
    for note in r.get("notes", []):
        print("•", note)
