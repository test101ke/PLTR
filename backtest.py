"""
PLTR Signal Desk - open-session backtest (the 16:30 EAT trade)
==============================================================
Answers one question: if you only trade the US open, which simple rule would
have made money, after fees, on days it was never fitted to?

TIMING. The NASDAQ open is 09:30 America/New_York. In Nairobi (EAT, UTC+3, no
daylight saving) that is 16:30 while the US is on EDT (mid-March to early Nov)
and 17:30 while the US is on EST (early Nov to mid-March). Every day is anchored
to 09:30 New York, and each sample records the EAT clock time it fell on.

TRADE MODEL. Watch the first SIG_MIN minutes, decide at 09:30+SIG_MIN, exit at
09:30+WIN_MIN. Every trade pays FEE_PCT round trip (fees + slippage).

CONTEXT, per day:
  overnight  perp move from the prior US close (15:59 ET) to 09:29 ET. This is
             where overnight news shows up in price.
  prevDay    prior session close-to-close ("daily change").
  event      |overnight| >= EVENT_PCT, or the date is listed in BT_EVENTS.
             A proxy for earnings / big-news days, since free historic news
             with reliable timestamps does not exist.
  news       headline tally the live desk logged just before the open
             (logs/signals.jsonl, kind "open_news"). Empty until the desk has
             been running through some opens; the news rule starts scoring then.

HONEST STATS. Oldest 70% of days = train, newest 30% = test. The headline rule
is the one with the best TRAIN t-statistic of net P&L (min 30 trades); its TEST numbers are
what count. (The old version picked the best TEST score out of 12 rules, which
quietly fits to the test set.) "passed" requires test hit >= 70%, Wilson 95%
floor > 50%, n_test >= 20 and positive net expectancy.

DATA. 1-minute candles from a PLTR perpetual, first venue that answers:
Bybit linear, Binance USD-M, KuCoin, then Bybit spot. Pin with BT_VENUE/BT_SYMBOL.

Run:  python backtest.py            (writes static/backtest.json)
Env:  BT_DAYS=200 BT_SIGNAL_MIN=2 BT_WINDOW_MIN=15 BT_FEE_PCT=0.10
      BT_EVENT_PCT=2.5 BT_EVENTS=2026-08-04,2026-11-03
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
EAT = ZoneInfo("Africa/Nairobi")
HERE = os.path.dirname(os.path.abspath(__file__))
SIG_MIN = int(os.getenv("BT_SIGNAL_MIN", "2"))
WIN_MIN = int(os.getenv("BT_WINDOW_MIN", "15"))
FEE_PCT = float(os.getenv("BT_FEE_PCT", "0.10"))
EVENT_PCT = float(os.getenv("BT_EVENT_PCT", "2.5"))
EVENT_DATES = {d.strip() for d in os.getenv("BT_EVENTS", "").split(",") if d.strip()}
MIN_TRAIN = 30
MIN_TEST = 20      # the Wilson floor does the real small-sample guarding

# NYSE full-day closures. The perp keeps trading on these days, so without this
# list a holiday would be scored as an "open" that never happened.
NYSE_HOLIDAYS = {
    "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
    "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
    "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
    "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}
HOLIDAYS_KNOWN_TO = "2027-12-31"
# NYSE 13:00 ET early closes: the "prior close" for the next day is taken at 12:59.
NYSE_EARLY_CLOSES = {
    "2025-07-03", "2025-11-28", "2025-12-24",
    "2026-11-27", "2026-12-24",
    "2027-11-26",
}


def close_time(day):
    return dt.time(12, 59) if day.isoformat() in NYSE_EARLY_CLOSES else dt.time(15, 59)


# ------------------------------------------------------------------ statistics
def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n; d = 1 + z * z / n; c = p + z * z / (2 * n)
    m = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return ((c - m) / d, (c + m) / d)


def binom_p(k, n, p0=0.5):
    """Two-sided normal approximation."""
    if n == 0:
        return 1.0
    sd = math.sqrt(n * p0 * (1 - p0))
    z = (k - n * p0) / sd
    return math.erfc(abs(z) / math.sqrt(2))


# ------------------------------------------------------------------ data
async def _get(client, url):
    for _ in range(3):
        try:
            r = await client.get(url, headers=UA, timeout=20)
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


async def bybit_disc(client, cat):
    j = await _get(client, f"https://api.bybit.com/v5/market/instruments-info?category={cat}")
    if not j or j.get("retCode") not in (0, "0", None):
        return None
    return _pick([x["symbol"] for x in j["result"]["list"] if "PLTR" in x["symbol"].upper()])


async def bybit_kl(client, cat, sym, start, end):
    j = await _get(client, f"https://api.bybit.com/v5/market/kline?category={cat}&symbol={sym}"
                           f"&interval=1&start={start}&end={end}&limit=100")
    if not j or not j.get("result", {}).get("list"):
        return []
    return [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])]
            for r in j["result"]["list"]]


async def binance_disc(client):
    j = await _get(client, "https://fapi.binance.com/fapi/v1/exchangeInfo")
    if not j:
        return None
    return _pick([s["symbol"] for s in j.get("symbols", [])
                  if "PLTR" in s["symbol"].upper() and s.get("status") == "TRADING"])


async def binance_kl(client, sym, start, end):
    j = await _get(client, f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval=1m"
                           f"&startTime={start}&endTime={end}&limit=100")
    if not isinstance(j, list):
        return []
    return [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in j]


async def kucoin_disc(client):
    j = await _get(client, "https://api-futures.kucoin.com/api/v1/contracts/active")
    if not j or j.get("code") not in ("200000", None):
        return None
    return _pick([c["symbol"] for c in j.get("data", []) if "PLTR" in c["symbol"].upper()])


async def kucoin_kl(client, sym, start, end):
    j = await _get(client, f"https://api-futures.kucoin.com/api/v1/kline/query?symbol={sym}"
                           f"&granularity=1&from={start}&to={end}")
    if not j or not j.get("data"):
        return []
    return [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in j["data"]]


VENUES = [
    ("bybit-perp",   lambda c: bybit_disc(c, "linear"), lambda c, s, a, b: bybit_kl(c, "linear", s, a, b)),
    ("binance-perp", binance_disc,                      binance_kl),
    ("kucoin-perp",  kucoin_disc,                       kucoin_kl),
    ("bybit-spot",   lambda c: bybit_disc(c, "spot"),   lambda c, s, a, b: bybit_kl(c, "spot", s, a, b)),
]


async def resolve_source(client):
    """(venue, symbol, fetch) for the first venue with PLTR 1m candles at a recent open."""
    forced_v = os.getenv("BT_VENUE", "").strip()
    forced_s = os.getenv("BT_SYMBOL", "").strip()
    probe = dt.datetime.now(NY).date() - dt.timedelta(days=1)
    while probe.weekday() >= 5:
        probe -= dt.timedelta(days=1)
    op = dt.datetime.combine(probe, dt.time(9, 30), tzinfo=NY)
    for name, disc, klfn in VENUES:
        if forced_v and forced_v != name:
            continue
        try:
            sym = forced_s or await disc(client)
            if not sym:
                continue
            fetch = _fetcher(client, name, sym, klfn)
            if await fetch(op - dt.timedelta(minutes=2), op + dt.timedelta(minutes=10)):
                return name, sym, fetch
        except Exception:
            continue
    return None, None, None


def _fetcher(client, venue, sym, klfn):
    """fetch(start_dt, end_dt) -> {minute_ms: row}. KuCoin takes seconds, others ms."""
    secs = "kucoin" in venue

    async def fetch(a, b):
        a_ms, b_ms = int(a.timestamp() * 1000), int(b.timestamp() * 1000)
        rows = await klfn(client, sym, a_ms // 1000 if secs else a_ms, b_ms // 1000 if secs else b_ms)
        out = {}
        for r in rows or []:
            t = r[0] if r[0] > 1e12 else r[0] * 1000
            out[int(t)] = r
        return out
    return fetch


def trading_days(n):
    """Last n NYSE sessions before today (New York)."""
    d, out = dt.datetime.now(NY).date() - dt.timedelta(days=1), []
    while len(out) < n:
        if d.weekday() < 5 and d.isoformat() not in NYSE_HOLIDAYS:
            out.append(d)
        d -= dt.timedelta(days=1)
    return out[::-1]


async def load_days(fetch, days):
    """Per day: the open window (09:25 -> 09:30+WIN_MIN) and the close (15:59, or 12:59 on early closes)."""
    sem = asyncio.Semaphore(4)

    async def one(day):
        async with sem:
            op = dt.datetime.combine(day, dt.time(9, 30), tzinfo=NY)
            cl = dt.datetime.combine(day, close_time(day), tzinfo=NY)
            win = await fetch(op - dt.timedelta(minutes=5), op + dt.timedelta(minutes=WIN_MIN + 1))
            close = await fetch(cl - dt.timedelta(minutes=10), cl)
            await asyncio.sleep(0.1)
            return day, win, close
    return await asyncio.gather(*[one(d) for d in days])


# ------------------------------------------------------------------ samples
def open_news_log():
    """{ny_date: {"net": up-down, "n": headlines}} from the live desk's pre-open snapshots."""
    path = os.path.join(os.getenv("LOG_DIR", os.path.join(HERE, "logs")), "signals.jsonl")
    out = {}
    try:
        with open(path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("kind") == "open_news" and r.get("nyDate"):
                    out[r["nyDate"]] = {"net": r.get("net", 0), "n": r.get("n", 0)}
    except Exception:
        pass
    return out


def build_samples(raw, news):
    """Turn per-day candle windows into feature rows. Pure: no I/O, easy to test."""
    samples, prev_close, prev_prev = [], None, None
    for day, win, close in raw:
        op = dt.datetime.combine(day, dt.time(9, 30), tzinfo=NY)
        o_ms = int(op.timestamp() * 1000)
        cw = {round((t - o_ms) / 60000): r for t, r in win.items()}
        day_close = close[max(close)][4] if close else None
        if all(k in cw for k in (0, SIG_MIN, WIN_MIN)) and prev_close:
            o0 = cw[0][1]
            pre = cw.get(-1, cw[0])[4]
            p_sig, p_end = cw[SIG_MIN][4], cw[WIN_MIN][4]
            overnight = pre / prev_close - 1
            prev_day = (prev_close / prev_prev - 1) if prev_prev else 0.0
            nw = news.get(day.isoformat())
            profile = [(m, cw[m][4] / cw[m - 1][4] - 1) for m in range(1, WIN_MIN + 1)
                       if m in cw and (m - 1) in cw and cw[m - 1][4]]
            samples.append({
                "day": day.isoformat(),
                "eat": op.astimezone(EAT).strftime("%H:%M"),
                "overnight": overnight, "prevDay": prev_day,
                "r_sig": p_sig / o0 - 1, "r_rem": p_end / p_sig - 1,
                "event": abs(overnight) * 100 >= EVENT_PCT or day.isoformat() in EVENT_DATES,
                "newsNet": nw["net"] if nw else None, "newsN": nw["n"] if nw else None,
                "profile": profile})
        # Roll the closes forward one session. A day with no close breaks the chain,
        # so the next day is skipped instead of measuring "overnight" across 2+ days.
        prev_prev, prev_close = prev_close, day_close
    return samples


# ------------------------------------------------------------------ rules
def _sgn(x, th):
    return 1 if x > th else -1 if x < -th else 0


def rule_book():
    """(family, name, fn(sample) -> -1/0/+1). Kept small on purpose: every extra
    rule is another lottery ticket for a fluke."""
    R = []
    for th in (0.0, 0.001, 0.002, 0.004):
        R.append(("momentum", f"Follow first {SIG_MIN}m move > {th*100:.1f}%",
                  lambda s, th=th: _sgn(s["r_sig"], th)))
        R.append(("fade", f"Fade first {SIG_MIN}m move > {th*100:.1f}%",
                  lambda s, th=th: -_sgn(s["r_sig"], th)))
    for th in (0.005, 0.01, 0.02):
        R.append(("overnight", f"Follow overnight move > {th*100:.1f}%",
                  lambda s, th=th: _sgn(s["overnight"], th)))
        R.append(("overnight", f"Fade overnight move > {th*100:.1f}%",
                  lambda s, th=th: -_sgn(s["overnight"], th)))
    for th in (0.01, 0.03):
        R.append(("daily", f"Follow prior-day change > {th*100:.0f}%",
                  lambda s, th=th: _sgn(s["prevDay"], th)))
        R.append(("daily", f"Fade prior-day change > {th*100:.0f}%",
                  lambda s, th=th: -_sgn(s["prevDay"], th)))
    R.append(("combo", f"Follow first {SIG_MIN}m move only when it agrees with overnight",
              lambda s: _sgn(s["r_sig"], 0.001) if _sgn(s["r_sig"], 0.001) == _sgn(s["overnight"], 0.002) else 0))
    R.append(("combo", f"Follow first {SIG_MIN}m move, quiet days only (no event)",
              lambda s: 0 if s["event"] else _sgn(s["r_sig"], 0.001)))
    R.append(("combo", f"Follow first {SIG_MIN}m move, event days only",
              lambda s: _sgn(s["r_sig"], 0.001) if s["event"] else 0))
    R.append(("combo", f"Fade first {SIG_MIN}m move, event days only",
              lambda s: -_sgn(s["r_sig"], 0.001) if s["event"] else 0))
    R.append(("news", "Follow pre-open news tally (|up-down| >= 2)",
              lambda s: 0 if s["newsNet"] is None else _sgn(s["newsNet"], 1)))
    R.append(("news", f"Follow first {SIG_MIN}m move only when news agrees",
              lambda s: 0 if not s["newsNet"] else
              (_sgn(s["r_sig"], 0.001) if _sgn(s["r_sig"], 0.001) == _sgn(s["newsNet"], 0) else 0)))
    return R


def score(fn, sub):
    """Hit rate and net expectancy (% per trade after FEE_PCT) of a rule on a set of days."""
    pnl = []
    for s in sub:
        d = fn(s)
        if d:
            pnl.append(d * s["r_rem"] * 100 - FEE_PCT)
    n = len(pnl)
    k = sum(1 for x in pnl if x > 0)
    mean = sum(pnl) / n if n else None
    sd = math.sqrt(sum((x - mean) ** 2 for x in pnl) / (n - 1)) if n > 1 else 0.0
    t = mean / sd * math.sqrt(n) if sd else None
    return {"n": n, "k": k, "hit": k / n if n else None,
            "exp": mean, "t": t, "total": sum(pnl), "pnl": pnl}


def run_rules(samples):
    cut = int(len(samples) * 0.7)
    train, test = samples[:cut], samples[cut:]
    rules = []
    for fam, name, fn in rule_book():
        tr, te = score(fn, train), score(fn, test)
        lo, _ = wilson(te["k"], te["n"])
        rules.append({"family": fam, "name": name,
                      "trainHit": _r(tr["hit"]), "nTrain": tr["n"], "trainExp": _r(tr["exp"], 4), "trainT": _r(tr["t"], 2),
                      "testHit": _r(te["hit"]), "nTest": te["n"], "ciLow": round(lo, 3),
                      "p": round(binom_p(te["k"], te["n"]), 4),
                      "expectancy": _r(te["exp"], 4), "testTotalPct": round(te["total"], 2),
                      "_fn": fn})
    return rules, train, test


def _r(x, nd=3):
    return round(x, nd) if x is not None else None


def regimes(samples):
    """How the basic momentum call behaves under each kind of day. Descriptive only."""
    fn = lambda s: _sgn(s["r_sig"], 0.001)
    groups = {
        "all days": samples,
        "overnight up > 0.5%": [s for s in samples if s["overnight"] > 0.005],
        "overnight down > 0.5%": [s for s in samples if s["overnight"] < -0.005],
        "overnight flat": [s for s in samples if abs(s["overnight"]) <= 0.005],
        "prior day up": [s for s in samples if s["prevDay"] > 0],
        "prior day down": [s for s in samples if s["prevDay"] < 0],
        f"event days (|overnight| >= {EVENT_PCT}%)": [s for s in samples if s["event"]],
        "quiet days": [s for s in samples if not s["event"]],
        "news logged": [s for s in samples if s["newsNet"] is not None],
        "16:30 EAT opens (US summer time)": [s for s in samples if s["eat"] == "16:30"],
        "17:30 EAT opens (US winter time)": [s for s in samples if s["eat"] == "17:30"],
    }
    out = []
    for name, sub in groups.items():
        sc = score(fn, sub)
        out.append({"name": name, "days": len(sub), "trades": sc["n"], "hit": _r(sc["hit"]),
                    "expectancy": _r(sc["exp"], 4),
                    "avgAbsMovePct": _r(sum(abs(s["r_rem"]) for s in sub) / len(sub) * 100, 3) if sub else None})
    return out


# ------------------------------------------------------------------ report
def analyse(samples, meta):
    n = len(samples)
    out = {"generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(), **meta,
           "nDays": n, "signalMin": SIG_MIN, "windowMin": WIN_MIN, "feePct": FEE_PCT,
           "entryEAT": f"16:30 (EDT) / 17:30 (EST) + {SIG_MIN}m", "exitEAT": f"+{WIN_MIN}m after open",
           "dateRange": [samples[0]["day"], samples[-1]["day"]] if samples else None,
           "notes": [], "rules": [], "regimes": [], "minuteProfile": [], "best": None,
           "passed70": False, "equity": [], "recent": []}
    if n < 30:
        out["notes"].append(f"Only {n} usable days - too few to judge any rule.")
        return out

    for m in range(1, WIN_MIN + 1):
        vals = [v for s in samples for mm, v in s["profile"] if mm == m]
        if vals:
            out["minuteProfile"].append({"m": m, "avgAbs": sum(abs(v) for v in vals) / len(vals),
                                         "avgSigned": sum(vals) / len(vals),
                                         "upShare": sum(1 for v in vals if v > 0) / len(vals)})

    rules, train, test = run_rules(samples)
    out["split"] = {"train": [train[0]["day"], train[-1]["day"]], "test": [test[0]["day"], test[-1]["day"]]}
    out["regimes"] = regimes(samples)

    eligible = [r for r in rules if r["nTrain"] >= MIN_TRAIN and r["trainT"] is not None]
    if eligible:
        best = max(eligible, key=lambda r: r["trainT"])
        fn = best["_fn"]
        passes = (best["testHit"] or 0) >= 0.70 and best["ciLow"] > 0.50 and best["nTest"] >= MIN_TEST \
            and (best["expectancy"] or 0) > 0
        out["passed70"] = bool(passes)
        eq, run = [], 0.0
        for s in test:
            d = fn(s)
            if d:
                run += d * s["r_rem"] * 100 - FEE_PCT
                eq.append({"day": s["day"], "cum": round(run, 3)})
        out["equity"] = eq
        for s in samples[-15:]:
            d = fn(s)
            out["recent"].append({"day": s["day"], "eat": s["eat"], "call": {1: "long", -1: "short", 0: "stand aside"}[d],
                                  "overnightPct": round(s["overnight"] * 100, 2),
                                  "prevDayPct": round(s["prevDay"] * 100, 2), "event": s["event"],
                                  "resultPct": round(d * s["r_rem"] * 100 - FEE_PCT, 3) if d else None})
        out["best"] = {k: v for k, v in best.items() if k != "_fn"}
        te = best["testHit"]
        out["notes"].append(
            f"Chosen on train (best t-stat of net P&L): '{best['name']}'. On the {best['nTest']} held-out days it "
            f"hit {te*100:.0f}% (95% floor {best['ciLow']*100:.0f}%), net {best['expectancy']:+.3f}% per trade, "
            f"{best['testTotalPct']:+.2f}% total. " if te is not None else
            f"Chosen on train: '{best['name']}', but it took no trades in the test period. ")
        out["notes"].append("CLEARS the bar out-of-sample. Paper-trade it live before real money."
                            if passes else "Does NOT clear the bar. No reliable open-session edge yet.")
    else:
        out["notes"].append(f"No rule reached {MIN_TRAIN} training trades.")

    logged = sum(1 for s in samples if s["newsNet"] is not None)
    out["notes"].append(f"News: {logged} of {n} days have a pre-open news snapshot. The news rules only score those days; "
                        "keep the desk running through the open to build this up.")
    summer = sum(1 for s in samples if s["eat"] == "16:30")
    out["notes"].append(f"Clock: {summer} opens at 16:30 EAT, {n - summer} at 17:30 EAT. US clocks go back on "
                        "1 Nov 2026, so the open moves to 17:30 EAT until 14 Mar 2027.")
    if samples[-1]["day"] > HOLIDAYS_KNOWN_TO:
        out["notes"].append(f"WARNING: the NYSE holiday list in backtest.py ends {HOLIDAYS_KNOWN_TO}. "
                            "Add later holidays or those days will be scored as fake opens.")
    out["notes"].append(f"Net of {FEE_PCT}% round-trip fees/slippage. {len(rules)} rules tested; only the "
                        "train-chosen rule's test result is a fair estimate. The rest are shown for context.")
    out["rules"] = [{k: v for k, v in r.items() if k != "_fn"} for r in rules]
    return out


def _save(out):
    try:
        os.makedirs(os.path.join(HERE, "static"), exist_ok=True)
        with open(os.path.join(HERE, "static", "backtest.json"), "w") as f:
            json.dump(out, f, indent=2)
    except Exception:
        pass


async def run_backtest(days=200):
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    async with httpx.AsyncClient(follow_redirects=True) as client:
        venue, symbol, fetch = await resolve_source(client)
        if not symbol:
            return {"error": "no PLTR perp/spot contract reachable on Bybit, Binance or KuCoin", "generatedAt": now}
        raw = await load_days(fetch, trading_days(days))
    samples = build_samples(raw, open_news_log())
    out = analyse(samples, {"source": f"{venue}:{symbol}", "venue": venue, "symbol": symbol})
    _save(out)
    return out


if __name__ == "__main__":
    r = asyncio.run(run_backtest(days=int(os.getenv("BT_DAYS", "200"))))
    if r.get("error"):
        print("ERROR:", r["error"]); raise SystemExit(1)
    print(f"source {r['source']}  days {r['nDays']}  {r['dateRange']}  fee {r['feePct']}%\n")
    print(f"{'rule (sorted by train t-stat)':62} {'train':>6} {'test':>6} {'n':>4} {'net%/trade':>10}")
    for x in sorted(r["rules"], key=lambda x: -(x["trainT"] if x["trainT"] is not None else -99)):
        th = f"{x['trainHit']*100:.0f}%" if x["trainHit"] is not None else "-"
        te = f"{x['testHit']*100:.0f}%" if x["testHit"] is not None else "-"
        ex = f"{x['expectancy']:+.3f}" if x["expectancy"] is not None else "-"
        print(f"{x['name'][:62]:62} {th:>6} {te:>6} {x['nTest']:>4} {ex:>10}")
    print("\nregimes (follow first-move):")
    for g in r["regimes"]:
        h = f"{g['hit']*100:.0f}%" if g["hit"] is not None else "-"
        mv = f"{g['avgAbsMovePct']}%" if g["avgAbsMovePct"] is not None else "-"
        print(f"  {g['name']:40} days {g['days']:>4}  hit {h:>4}  avg|move| {mv}")
    print("\nlast days, chosen rule:")
    for d in r["recent"]:
        print(f"  {d['day']} {d['eat']} EAT  overnight {d['overnightPct']:+.2f}%  prev {d['prevDayPct']:+.2f}%"
              f"{'  EVENT' if d['event'] else ''}  -> {d['call']:11} {d['resultPct'] if d['resultPct'] is not None else ''}")
    print()
    for note in r["notes"]:
        print("-", note)
