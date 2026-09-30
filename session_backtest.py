"""
Today's session, second by second, and an honest optimisation of the ORB model.
===============================================================================
    python session_backtest.py today      # today's 16:30 EAT open -> now
    python session_backtest.py optimize   # search settings, judge on unseen days
    python session_backtest.py all        # both

TODAY
  Every PLTR perpetual trade from 09:30 New York (16:30 EAT) to the moment it
  runs, built into 1-second bars, then the ORB model replayed on it:
    (a) as designed: trading only the first `exitAfterMin` minutes;
    (b) extended: allowed to keep trading until now.

OPTIMIZE
  A random search over the model's settings on the 16:30-16:45 EAT windows of
  the last TICK_DAYS trading days. Settings are ranked on the OLDER 70% of days
  only; the top ones are then scored on the NEWEST 30% and on today, which they
  never saw. Anything that shines on the old days and fails on the new ones was
  fitted to noise. Nothing is changed in the live trader automatically.
"""
import os, sys, json, random, asyncio, datetime as dt
import httpx
import backtest as bt
import tick_backtest as tb
import orb_backtest as ob
import trader as tr

NY, EAT = bt.NY, bt.EAT
HERE = os.path.dirname(os.path.abspath(__file__))


def log(m):
    print("[session] " + m, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ today
async def today_bars(venue=None):
    now = dt.datetime.now(NY)
    day = now.date()
    op = dt.datetime.combine(day, dt.time(9, 30), tzinfo=NY)
    if not bt.is_session(day) or now < op:
        return None, None, "no US session has opened today yet"
    a, b = int(op.timestamp() * 1000), int(now.timestamp() * 1000)
    forced = venue or os.getenv("TICK_VENUE", "").strip()
    # daily files are published the next day, so today needs a live API
    live = [s for s in tb.SOURCES if s[0] not in ("binance-vision", "bybit-public")]
    order = live[:1] + tb.TODAY_EXTRA + live[1:]
    async with httpx.AsyncClient(follow_redirects=True) as c:
        for name, disc, fetch in order:
            if forced and not name.startswith(forced):
                continue
            try:
                sym = await disc(c)
            except Exception:
                sym = None
            if not sym:
                log(f"today {name}: unreachable or no PLTR perpetual"); continue
            trades = await fetch(c, sym, a, b)
            if trades and min(t[0] for t in trades) > a + 60_000:
                log(f"today {name}: only has trades from {dt.datetime.fromtimestamp(min(t[0] for t in trades)/1000, EAT):%H:%M} EAT, "
                    "not the whole session; skipping"); continue
            if trades:
                log(f"today {name}:{sym}: {len(trades)} trades {op.astimezone(EAT):%H:%M}-{now.astimezone(EAT):%H:%M} EAT")
                return (day, tb.to_bars(trades, a, b), None), f"{name}:{sym}", None
    return None, None, "no trades returned for today's session"


def price_summary(bars):
    ts = sorted(bars)
    first, last = bars[ts[0]], bars[ts[-1]]
    hi = max(b[2] for b in bars.values()); lo = min(b[3] for b in bars.values())
    t_hi = next(t for t in ts if bars[t][2] == hi); t_lo = next(t for t in ts if bars[t][3] == lo)
    eat = lambda t: dt.datetime.fromtimestamp(t / 1000, EAT).strftime("%H:%M:%S")
    return {"open": first[1], "last": last[4], "high": hi, "highAt": eat(t_hi), "low": lo, "lowAt": eat(t_lo),
            "movePct": round((last[4] / first[1] - 1) * 100, 3), "rangePct": round((hi / lo - 1) * 100, 3)}


def run_presets(raw_day, minutes, use_flow=True):
    out = {}
    for name, p in tr.PRESETS.items():
        cfg = {k: v for k, v in p.items() if k not in ("label", "note")}
        if minutes:
            cfg["exitAfterMin"] = minutes
        trades = ob.replay(cfg, [raw_day], span_ms=1000, use_flow=use_flow)
        out[name] = {"label": p["label"], **ob.summarize(trades, 1),
                     "trades_detail": [{"side": t["side"], "pnl": round(t["pnl"], 2), "exits": t["exits"]}
                                       for t in trades]}
    return out


# ------------------------------------------------------------------ optimise
SPACE = {
    "orbMinutes": [1, 2, 3, 5],
    "bufferPct": [0.0, 0.02, 0.05, 0.1, 0.2],
    "imbalanceMin": [50, 55, 60, 65, 70],
    "takeProfitUsd": [0.5, 1.0, 2.0, 3.0, 5.0],
    "partialPct": [0, 30, 50, 100],
    "trailPct": [0.0, 0.05, 0.1, 0.2, 0.3],
    "breakevenUsd": [0, 0.5, 1.0],
    "stopMode": ["range", "usd"],
    "rangeStopFrac": [0.3, 0.5, 1.0],
    "stopLossUsd": [2.2, 3.0, 5.0],
    "graceSec": [0, 3, 10],
    "confirmMs": [0, 500, 1000],
    "reentrySec": [5, 30, 120],
    "maxTrades": [1, 2, 3, 5, 10],
    "exitAfterMin": [5, 10, 15],
}


def sample(rnd):
    return {k: rnd.choice(v) for k, v in SPACE.items()}


def score(trades, n_days):
    s = ob.summarize(trades, n_days)
    return s


def optimize(raw, today=None, n=int(os.getenv("OPT_TRIALS", "400")), seed=7):
    raw = [r for r in raw if r[1]]
    cut = int(len(raw) * 0.7)
    train, test = raw[:cut], raw[cut:]
    rnd = random.Random(seed)
    trials = [{k: v for k, v in tr.PRESETS[p].items() if k not in ("label", "note")} for p in tr.PRESETS]
    trials += [sample(rnd) for _ in range(n)]
    ranked = []
    for i, cfg in enumerate(trials):
        t = ob.replay(cfg, train, span_ms=1000, use_flow=True)
        s = score(t, len(train))
        if s["trades"] >= 15:
            ranked.append((s["total"], cfg, s))
        if i % 50 == 0:
            log(f"optimise: {i}/{len(trials)} settings tried")
    ranked.sort(key=lambda x: -x[0])
    top = []
    for total, cfg, s in ranked[:5]:
        te = score(ob.replay(cfg, test, span_ms=1000, use_flow=True), len(test))
        td = score(ob.replay(cfg, [today], span_ms=1000, use_flow=True), 1) if today else None
        top.append({"settings": cfg, "train": s, "test": te, "today": td})
    positive_train = sum(1 for x in ranked if x[0] > 0)
    return {"trials": len(trials), "trainDays": [str(train[0][0]), str(train[-1][0])],
            "testDays": [str(test[0][0]), str(test[-1][0])], "profitableOnTrain": positive_train,
            "evaluated": len(ranked), "top": top}


# ------------------------------------------------------------------ main
def main(mode, venue=None, trials=None):
    res = {"generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(), "feePctPerSide": tr.fee_pct(tr.DEFAULTS)}
    today, src, why = asyncio.run(today_bars(venue))
    if mode in ("today", "all"):
        if not today:
            res["today"] = {"error": why}
        else:
            bars = today[1]
            mins = int((max(bars) - min(bars)) / 60000) + 1
            res["today"] = {"source": src, "window": f"16:30 EAT to {dt.datetime.now(EAT):%H:%M} EAT",
                            "price": price_summary(bars),
                            "asDesigned": run_presets(today, None),
                            "extended": run_presets(today, min(mins, 390))}
    if mode in ("optimize", "all"):
        v, sym, raw = asyncio.run(tb.collect(int(os.getenv("TICK_DAYS", "60")), venue))
        log(f"optimise data: {v}:{sym}, {len(raw)} days")
        res["optimize"] = (optimize(raw, today, n=trials) if trials else optimize(raw, today)) if raw \
            else {"error": "no trade history"}
        res["optimize"]["source"] = f"{v}:{sym}"
    return res


def text(r):
    """The same report show() prints, as a string (for the Trading page)."""
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        show(r)
    return buf.getvalue()


def show(r):
    t = r.get("today")
    if t:
        if t.get("error"):
            print("TODAY:", t["error"])
        else:
            p = t["price"]
            print(f"\nTODAY  {t['source']}  {t['window']}  (1-second bars from trades)")
            print(f"  price: open {p['open']:.2f}  high {p['high']:.2f} at {p['highAt']}  low {p['low']:.2f} at {p['lowAt']}"
                  f"  now {p['last']:.2f}  move {p['movePct']:+.2f}%  range {p['rangePct']:.2f}%")
            for key, title in (("asDesigned", "model as designed (first 15 min only)"),
                               ("extended", "model allowed to trade until now")):
                print(f"\n  {title}:")
                for v in t[key].values():
                    trades = ", ".join(f"{d['side']} {d['pnl']:+.2f} ({'/'.join(d['exits'])})" for d in v["trades_detail"]) or "no trades"
                    print(f"    {v['label']:11} {v['trades']:>2} trades  total {v['total']:+7.2f} USDT   {trades}")
    o = r.get("optimize")
    if o:
        if o.get("error"):
            print("OPTIMIZE:", o["error"]); return
        print(f"\nOPTIMIZE  {o['source']}  {o['trials']} settings tried on train days {o['trainDays']}, "
              f"judged on unseen days {o['testDays']} and today")
        print(f"  {o['profitableOnTrain']} of {o['evaluated']} settings made money on the train days.")
        f = lambda s: "-" if not s else f"{s['trades']:>3} tr  win {s['winRate'] if s['winRate'] is not None else '-':>4}%  total {s['total']:+8.2f}"
        for i, x in enumerate(o["top"], 1):
            print(f"\n  #{i} TRAIN {f(x['train'])} | UNSEEN {f(x['test'])} | TODAY {f(x['today'])}")
            print("     " + json.dumps(x["settings"], sort_keys=True))


if __name__ == "__main__":
    mode = (sys.argv[1] if len(sys.argv) > 1 else "all").lower()
    r = main(mode)
    try:
        with open(os.path.join(HERE, "static", "session_backtest.json"), "w") as fh:
            json.dump(r, fh, indent=1, default=str)
    except OSError:
        pass
    show(r)
