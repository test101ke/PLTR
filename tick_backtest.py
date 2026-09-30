"""
Second-by-second backtest of the ORB trader, 16:30:00-16:45:00 EAT only.
=======================================================================
The ORB trader decides many times a second, so 1-minute candles are too coarse
to judge it. No exchange reachable from GitHub serves 1-second candles for PLTR,
so this builds them from the exchange's own TRADE HISTORY: every print in the
window, with price, size and taker side.

Window: 09:30:00-09:45:00 New York = 16:30:00-16:45:00 EAT while the US is on
summer time (and 17:30-17:45 EAT in US winter, when the US open moves).

Per day:
  1. download every trade of the PLTR USDT perpetual in the window;
  2. aggregate into 1-second bars: open/high/low/close/volume, plus the taker
     BUY share of volume over the trailing 10 seconds (order-flow pressure);
  3. replay every ORB preset (orb_backtest.replay) on those bars, twice:
       - flow filter ON: enter only when taker flow leans with the breakout
         (the closest historical stand-in for the live order-book lean);
       - flow filter OFF.

Sources, first that answers with trades: Bitget, OKX, Gate.io. Each keeps
roughly the last few months of trades, so the history is shorter than the
1-minute study; the log says exactly which days were covered.

Run:  python tick_backtest.py      (writes static/tick_backtest.json)
Env:  TICK_DAYS=60  TICK_VENUE=bitget|okx|gate
"""
import os, sys, json, asyncio, datetime as dt
import httpx
import backtest as bt
import orb_backtest as ob
import trader as tr

NY = bt.NY
HERE = os.path.dirname(os.path.abspath(__file__))
FLOW_SEC = 10
WIN_MIN = 15


def log(msg):
    print("[tick] " + msg, file=sys.stderr, flush=True)


async def _get(client, url):
    for i in range(4):
        try:
            r = await client.get(url, headers=bt.UA, timeout=20)
            if r.status_code == 429:
                await asyncio.sleep(1.5 * (i + 1)); continue
            r.raise_for_status()
            return r.json()
        except Exception:
            await asyncio.sleep(0.6)
    return None


# ------------------------------------------------------------------ trade sources
# Every fetcher returns [(ts_ms, price, size, +1 buy / -1 sell)] for [start, end).

async def bitget_trades(client, sym, start, end):
    out, seen, hi = [], set(), end
    for _ in range(200):
        j = await _get(client, f"https://api.bitget.com/api/v2/mix/market/fills-history?symbol={sym}"
                               f"&productType=usdt-futures&startTime={start}&endTime={hi}&limit=1000")
        rows = (j or {}).get("data") or []
        new = [r for r in rows if r.get("tradeId") not in seen]
        for r in new:
            seen.add(r.get("tradeId"))
            t = int(r["ts"])
            if start <= t < end:
                out.append((t, float(r["price"]), float(r["size"]), 1 if str(r["side"]).lower() == "buy" else -1))
        if len(rows) < 1000 or not new:
            break
        hi = min(int(r["ts"]) for r in rows)          # page backwards in time
        if hi <= start:
            break
        await asyncio.sleep(0.12)
    return out


async def okx_trades(client, sym, start, end):
    out, after = [], end
    for _ in range(600):
        j = await _get(client, f"https://www.okx.com/api/v5/market/history-trades?instId={sym}"
                               f"&type=2&after={after}&limit=100")
        rows = (j or {}).get("data") or []
        if not rows:
            break
        for r in rows:
            t = int(r["ts"])
            if start <= t < end:
                out.append((t, float(r["px"]), float(r["sz"]), 1 if r["side"] == "buy" else -1))
        oldest = min(int(r["ts"]) for r in rows)
        if oldest <= start or oldest >= after:
            break
        after = oldest
        await asyncio.sleep(0.12)
    return out


async def gate_trades(client, sym, start, end):
    out, seen, to = [], set(), end // 1000
    for _ in range(200):
        j = await _get(client, f"https://api.gateio.ws/api/v4/futures/usdt/trades?contract={sym}"
                               f"&from={start // 1000}&to={to}&limit=1000")
        if not isinstance(j, list) or not j:
            break
        new = [r for r in j if r.get("id") not in seen]
        for r in new:
            seen.add(r.get("id"))
            t = int(float(r.get("create_time_ms") or float(r["create_time"]) * 1000))
            sz = float(r["size"])
            if start <= t < end:
                out.append((t, float(r["price"]), abs(sz), 1 if sz > 0 else -1))
        if len(j) < 1000 or not new:
            break
        to = min(int(float(r["create_time"])) for r in j)
        if to <= start // 1000:
            break
        await asyncio.sleep(0.12)
    return out


SOURCES = [
    ("bitget", bt.bitget_disc, bitget_trades),
    ("okx", bt.okx_disc, okx_trades),
    ("gate", bt.gate_disc, gate_trades),
]


# ------------------------------------------------------------------ 1-second bars
def to_bars(trades, start_ms, end_ms):
    """{second_ms: [t, o, h, l, c, vol, buyShare%]}. Seconds with no trade carry the
    last price (flat bar, zero volume) so the replay clock never skips."""
    by_sec = {}
    for t, px, sz, side in sorted(trades):
        s = t - t % 1000
        b = by_sec.get(s)
        if b is None:
            by_sec[s] = [s, px, px, px, px, sz, sz if side > 0 else 0.0]
        else:
            b[2] = max(b[2], px); b[3] = min(b[3], px); b[4] = px
            b[5] += sz; b[6] += sz if side > 0 else 0.0
    bars, last, window = {}, None, []
    for s in range(start_ms - start_ms % 1000, end_ms, 1000):
        b = by_sec.get(s)
        if b is None:
            if last is None:
                continue
            b = [s, last, last, last, last, 0.0, 0.0]
        last = b[4]
        window.append((b[5], b[6]))
        window = window[-FLOW_SEC:]
        vol = sum(v for v, _ in window)
        share = 100.0 * sum(bv for _, bv in window) / vol if vol else 50.0
        bars[s] = b[:6] + [share]
    return bars


# ------------------------------------------------------------------ run
async def collect(days):
    forced = os.getenv("TICK_VENUE", "").strip()
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for name, disc, fetch in SOURCES:
            if forced and forced != name:
                continue
            sym = await disc(client)
            if not sym:
                log(f"{name}: no PLTR perpetual listed (or venue unreachable)"); continue
            raw, have = [], 0
            for day in bt.trading_days(days):
                op = dt.datetime.combine(day, dt.time(9, 30), tzinfo=NY)
                a = int(op.timestamp() * 1000)
                b = a + WIN_MIN * 60_000
                trades = await fetch(client, sym, a, b)
                if trades:
                    have += 1
                    raw.append((day, to_bars(trades, a, b + 60_000), None))
                    log(f"{name} {day} {op.astimezone(bt.EAT):%H:%M} EAT: {len(trades)} trades")
            if have:
                return name, sym, raw
            log(f"{name}: {sym} listed but no trades returned for these windows (history too short?)")
    return None, None, []


def run(days):
    venue, sym, raw = asyncio.run(collect(days))
    out = {"generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(), "window": "16:30:00-16:45:00 EAT",
           "bar": "1 second, built from trades", "source": f"{venue}:{sym}" if venue else None,
           "days": len(raw), "dateRange": [str(raw[0][0]), str(raw[-1][0])] if raw else None,
           "feePct": tr.fee_pct(tr.DEFAULTS), "notes": []}
    if not raw:
        out["error"] = "no trade history reachable for PLTR on " + ", ".join(s[0] for s in SOURCES)
        return out
    out["flowFilterOn"] = ob.study(raw, span_ms=1000, use_flow=True)
    out["flowFilterOff"] = ob.study(raw, span_ms=1000, use_flow=False)
    out["notes"] = [
        "Every preset replays the trader's own OrbStrategy on 1-second bars built from real trades.",
        f"'Flow filter on' needs {tr.DEFAULTS['imbalanceMin']:.0f}%+ of the last {FLOW_SEC}s of taker volume to "
        "lean with the breakout: the nearest historical stand-in for the live order-book filter.",
        "Fills: 0.01% half-spread, 0.01% slippage, 0.06% fee a side. P&L per $100 batch at 10x.",
    ]
    return out


def _print(r):
    if r.get("error"):
        print("ERROR:", r["error"]); return
    print(f"\n1-SECOND ORB BACKTEST  {r['source']}  {r['days']} days {r['dateRange']}  window {r['window']}")
    for key, title in (("flowFilterOn", "taker-flow filter ON (closest to live)"),
                       ("flowFilterOff", "taker-flow filter OFF")):
        print(f"\n  {title}:")
        print(f"  {'preset':11} {'trades':>6} {'win':>5} {'$/trade':>8} {'total $':>8} {'$/day':>7} "
              f"{'best day':>8} {'worst day':>9} {'max DD':>7} | newest 30% total $")
        for v in r[key].values():
            a, n = v["all"], v["newest30"]
            f = lambda x, d=2: "-" if x is None else f"{x:+.{d}f}"
            w = "-" if a["winRate"] is None else f"{a['winRate']:.0f}%"
            print(f"  {v['label']:11} {a['trades']:>6} {w:>5} {f(a['perTrade'], 3):>8} {f(a['total']):>8} "
                  f"{f(a['perDay'], 3):>7} {f(a['bestDay']):>8} {f(a['worstDay']):>9} {f(a['maxDrawdown']):>7} | {f(n['total'])}")
    print()
    for n in r["notes"]:
        print("-", n)


if __name__ == "__main__":
    res = run(int(os.getenv("TICK_DAYS", "60")))
    try:
        with open(os.path.join(HERE, "static", "tick_backtest.json"), "w") as f:
            json.dump(res, f, indent=1)
    except OSError:
        pass
    _print(res)
    if res.get("error"):
        raise SystemExit(1)
