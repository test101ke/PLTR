"""
The "15-minute ORB" from the Instagram reel, tested on PLTR.
=============================================================
    python reel_backtest.py            # BT_DAYS (default 250) sessions, best reachable venue

Rules as shown in the reel (TradingView strategy on NQ, 15-minute chart):
  * opening range = the first 15 minutes, 09:30-09:45 New York (16:30-16:45 EAT);
  * a candle closing above the range high -> long, below the range low -> short;
  * stop on the other side of the range; target a multiple of that risk, else
    close the position before the bell. One trade a day.

Here it runs on 1-minute PLTR perpetual candles (entry at the next minute's open,
a minute that touches both stop and target counts as the stop) with OUR costs:
0.12% round trip, $100 margin at 10x = $1,000 per trade. Every variant is also
shown before fees, and split into older / newer halves: an edge that only lives
in one half is luck.
"""
import os, sys, asyncio, datetime as dt
import httpx
import backtest as bt

NY = bt.NY
FEE_PCT = float(os.getenv("BT_FEE_PCT", "0.12"))
NOTIONAL = 1000.0
RANGE_MIN = 15
LAST_ENTRY = dt.time(12, 0)          # no new entries after noon New York
FLAT_AT = dt.time(15, 55)            # close everything before the bell


def simulate(bars, target_r=None, stop="range", fee_pct=FEE_PCT):
    """bars: [(datetime NY, o, h, l, c)] for one session, in time order.
    Returns None (no trade) or {side, entry, exit, why, gross, net, riskPct}; P&L in % of notional."""
    day = bars[0][0].date()
    op = dt.datetime.combine(day, dt.time(9, 30), tzinfo=NY)
    rng = [b for b in bars if op <= b[0] < op + dt.timedelta(minutes=RANGE_MIN)]
    if len(rng) < RANGE_MIN - 2:
        return None
    hi, lo = max(b[2] for b in rng), min(b[3] for b in rng)
    after = [b for b in bars if b[0] >= op + dt.timedelta(minutes=RANGE_MIN)]
    for i, b in enumerate(after[:-1]):
        if b[0].time() >= LAST_ENTRY:
            return None
        side = "long" if b[4] > hi else "short" if b[4] < lo else None
        if side:
            break
    else:
        return None
    entry = after[i + 1][1]
    sl = (lo if side == "long" else hi) if stop == "range" else (hi + lo) / 2
    risk = (entry - sl) if side == "long" else (sl - entry)
    if risk <= 0:                                  # gapped through the stop already
        return None
    tp = None if target_r is None else entry + (risk * target_r if side == "long" else -risk * target_r)
    exit_px, why = None, "close"
    for b in after[i + 1:]:
        if b[0].time() >= FLAT_AT:
            exit_px = b[1]; break
        hit_sl = b[3] <= sl if side == "long" else b[2] >= sl
        hit_tp = tp is not None and (b[2] >= tp if side == "long" else b[3] <= tp)
        if hit_sl:                                 # pessimistic: stop first when both are touched
            exit_px, why = sl, "stop"; break
        if hit_tp:
            exit_px, why = tp, "target"; break
    if exit_px is None:
        exit_px = after[-1][4]
    gross = (exit_px / entry - 1) * 100 * (1 if side == "long" else -1)
    return {"day": str(day), "side": side, "entry": entry, "exit": exit_px, "why": why,
            "gross": gross, "net": gross - fee_pct, "riskPct": risk / entry * 100}


def summarize(trades):
    n = len(trades)
    if not n:
        return {"trades": 0}
    net = [t["net"] for t in trades]
    run = peak = dd = 0.0
    for p in net:
        run += p * NOTIONAL / 100; peak = max(peak, run); dd = min(dd, run - peak)
    return {"trades": n, "win": round(sum(p > 0 for p in net) / n * 100, 1),
            "grossPct": round(sum(t["gross"] for t in trades) / n, 3), "netPct": round(sum(net) / n, 3),
            "usd": round(sum(net) * NOTIONAL / 100, 2), "maxDD": round(dd, 2),
            "risk": round(sum(t["riskPct"] for t in trades) / n, 2)}


VARIANTS = [(f"{r}R target" if r else "hold to close", r, "range") for r in (1, 1.5, 2, 3, None)] + \
           [("2R, stop at mid-range", 2, "mid")]


def study(days_bars):
    """days_bars: [[(dt, o, h, l, c), ...] per session], oldest first."""
    half = len(days_bars) // 2
    out = []
    for name, r, stop in VARIANTS:
        tr = [t for t in (simulate(b, r, stop) for b in days_bars if b) if t]
        old = {str(b[0][0].date()) for b in days_bars[:half] if b}
        out.append({"name": name, "all": summarize(tr),
                    "older": summarize([t for t in tr if t["day"] in old]),
                    "newer": summarize([t for t in tr if t["day"] not in old])})
    return out


async def load(days):
    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as c:
        venue, sym, fetch = await bt.resolve_source(c)
        if not fetch:
            return None, []
        sem = asyncio.Semaphore(4)

        async def one(day):
            async with sem:
                op = dt.datetime.combine(day, dt.time(9, 30), tzinfo=NY)
                rows = {}
                for k in range(3):                 # venues return <= 200 candles per call
                    a = op + dt.timedelta(minutes=135 * k)
                    rows.update(await fetch(a, a + dt.timedelta(minutes=136)))
                return sorted((dt.datetime.fromtimestamp(t / 1000, NY), r[1], r[2], r[3], r[4])
                              for t, r in rows.items()
                              if dt.time(9, 30) <= dt.datetime.fromtimestamp(t / 1000, NY).time() < dt.time(16, 0))
        bars = await asyncio.gather(*[one(d) for d in bt.trading_days(days)])
        return f"{venue}:{sym}", [b for b in bars if len(b) > 300]


def main():
    src, days = asyncio.run(load(int(os.getenv("BT_DAYS", "250"))))
    if not days:
        print("no data"); return 1
    print(f"REEL 15-MIN ORB on {src}: {len(days)} full sessions {days[0][0][0]:%Y-%m-%d} -> {days[-1][0][0]:%Y-%m-%d}")
    print(f"costs {FEE_PCT}% round trip, ${NOTIONAL:.0f} per trade ($100 x 10)\n")
    print(f"{'variant':22} {'trades':>6} {'win':>6} {'gross/tr':>9} {'net/tr':>8} {'total $':>9} {'maxDD $':>9} {'risk':>6}"
          f"   older half net$ | newer half net$")
    for v in study(days):
        a, o, n = v["all"], v["older"], v["newer"]
        if not a["trades"]:
            print(f"{v['name']:22} no trades"); continue
        print(f"{v['name']:22} {a['trades']:>6} {a['win']:>5}% {a['grossPct']:>8}% {a['netPct']:>7}% "
              f"{a['usd']:>9} {a['maxDD']:>9} {a['risk']:>5}%   {o.get('usd', 0):>9} | {n.get('usd', 0):>9}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
