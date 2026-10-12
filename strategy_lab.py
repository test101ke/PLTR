"""
Strategy lab: which simple PLTR rules beat 0.12% round-trip fees, honestly?
===========================================================================
    python strategy_lab.py              # BT_DAYS (default 500) sessions of 1-minute candles

Every rule makes at most one trade a day, with fixed entry and exit times (no
stops), so nothing can be tuned to the past. Rules are ranked on the OLDER 60%
of days only, then judged on the NEWEST 40% that were never looked at. A rule
counts as real only if it makes money after fees in both, and its t-statistic
over all days clears 2.

Times are New York: 09:30 = 16:30 EAT (the open), 09:45 = 16:45 EAT.
"""
import os, sys, math, asyncio, datetime as dt
import reel_backtest as rb

FEE = float(os.getenv("BT_FEE_PCT", "0.12"))
TRAIN = 0.6


def minute_closes(bars):
    """{minutes after 09:30: (open, close)} for one session."""
    out = {}
    for t, o, h, l, c in bars:
        out[(t.hour - 9) * 60 + t.minute - 30] = (o, c)
    return out


def px(m, k, field=1):
    """Close of minute k (field 1) or open (field 0); nearest earlier minute if missing."""
    for j in range(k, max(k - 5, -1), -1):
        if j in m:
            return m[j][field]
    return None


def rules():
    """name -> fn(m, prev_close) -> (side, entry, exit) or None. side +1 long / -1 short."""
    R = {}

    def hold(side_fn, enter_min, exit_min, enter_open=False):
        def f(m, pc):
            s = side_fn(m, pc)
            if not s:
                return None
            e = px(m, enter_min, 0 if enter_open else 1)
            x = px(m, exit_min)
            return (s, e, x) if e and x else None
        return f

    sgn = lambda x, th=0.0: 1 if x > th else -1 if x < -th else 0
    # 1. the open window you trade (16:30-16:45 EAT): momentum vs fade of the first k minutes
    for k in (1, 2, 3, 5):
        for h in (15, 30, 60, 389):
            hn = "close" if h == 389 else f"{h}m"
            R[f"follow first {k}m, exit {hn}"] = hold(lambda m, pc, k=k: sgn(px(m, k - 1) / px(m, 0, 0) - 1), k - 1, h)
            R[f"fade first {k}m, exit {hn}"] = hold(lambda m, pc, k=k: -sgn(px(m, k - 1) / px(m, 0, 0) - 1), k - 1, h)
    # 2. the overnight gap (perp price at 09:30 vs the previous 15:59)
    for th in (0.0, 0.5, 1.0, 2.0):
        for h in (15, 60, 389):
            hn = "close" if h == 389 else f"{h}m"
            gap = lambda m, pc: px(m, 0, 0) / pc - 1 if pc else 0
            R[f"gap follow >{th}%, exit {hn}"] = hold(lambda m, pc, th=th, g=gap: sgn(g(m, pc) * 100, th), 0, h, True)
            R[f"gap fade >{th}%, exit {hn}"] = hold(lambda m, pc, th=th, g=gap: -sgn(g(m, pc) * 100, th), 0, h, True)
    # 3. plain drift
    R["always long, open to close"] = hold(lambda m, pc: 1, 0, 389, True)
    R["always short, open to close"] = hold(lambda m, pc: -1, 0, 389, True)
    R["always long, first 15m"] = hold(lambda m, pc: 1, 0, 15, True)
    R["always short, first 15m"] = hold(lambda m, pc: -1, 0, 15, True)
    # 4. intraday momentum (published on US index ETFs): the move from yesterday's close to
    #    10:00 predicts the last half hour
    for a in (30, 60):
        R[f"first {a}m (from prev close) -> last 30m"] = \
            lambda m, pc, a=a: (lambda s: (s, px(m, 359), px(m, 389)) if s and px(m, 359) else None)(
                sgn(px(m, a - 1) / pc - 1) if pc else 0)
        R[f"first {a}m (from prev close) -> fade last 30m"] = \
            lambda m, pc, a=a: (lambda s: (s, px(m, 359), px(m, 389)) if s and px(m, 359) else None)(
                -sgn(px(m, a - 1) / pc - 1) if pc else 0)
    # 5. after the 15-minute window: follow / fade its direction to the close
    R["follow 16:30-16:45 move, hold to close"] = hold(lambda m, pc: sgn(px(m, 14) / px(m, 0, 0) - 1), 14, 389)
    R["fade 16:30-16:45 move, hold to close"] = hold(lambda m, pc: -sgn(px(m, 14) / px(m, 0, 0) - 1), 14, 389)
    return R


def evaluate(days):
    """days: [bars per session] oldest first. Returns rows sorted by train net."""
    ms = [minute_closes(b) for b in days]
    prev = [None] + [px(m, 389) or px(m, 385) for m in ms[:-1]]
    cut = int(len(ms) * TRAIN)
    rows = []
    for name, fn in rules().items():
        res = []
        for i, (m, pc) in enumerate(zip(ms, prev)):
            try:
                r = fn(m, pc)
            except (TypeError, ZeroDivisionError):
                r = None
            if r:
                s, e, x = r
                res.append((i, s * (x / e - 1) * 100))
        if len(res) < 20:
            continue
        net = lambda xs: [g - FEE for _, g in xs]
        tr, te = [r for r in res if r[0] < cut], [r for r in res if r[0] >= cut]
        allnet = net(res)
        mu = sum(allnet) / len(allnet)
        sd = math.sqrt(sum((x - mu) ** 2 for x in allnet) / max(1, len(allnet) - 1)) or 1e-9
        rows.append({"rule": name, "n": len(res), "gross": sum(g for _, g in res) / len(res), "net": mu,
                     "win": sum(x > 0 for x in allnet) / len(allnet) * 100,
                     "t": mu / sd * math.sqrt(len(allnet)),
                     "train": sum(net(tr)) / len(tr) if tr else 0, "test": sum(net(te)) / len(te) if te else 0,
                     "nTest": len(te), "usd": sum(allnet) * rb.NOTIONAL / 100})
    rows.sort(key=lambda r: -r["train"])
    return rows, cut


def main():
    src, days = asyncio.run(rb.load(int(os.getenv("BT_DAYS", "500"))))
    if not days:
        print("no data"); return 1
    rows, cut = evaluate(days)
    d0, dc, d1 = days[0][0][0].date(), days[cut][0][0].date(), days[-1][0][0].date()
    print(f"STRATEGY LAB on {src}: {len(days)} sessions {d0} -> {d1}; rank on {d0}..{dc} (train), "
          f"judge on {dc}..{d1} (unseen). Costs {FEE}% round trip; $ at $1,000 a trade.\n")
    print(f"{'rule':44} {'n':>4} {'win':>5} {'gross%':>7} {'net%':>7} {'t':>5} {'train%':>7} {'UNSEEN%':>8} {'total $':>8}")
    for r in rows:
        flag = "  <-- real?" if r["train"] > 0 and r["test"] > 0 and r["t"] > 2 else ""
        print(f"{r['rule'][:44]:44} {r['n']:>4} {r['win']:>4.0f}% {r['gross']:>7.3f} {r['net']:>7.3f} {r['t']:>5.1f} "
              f"{r['train']:>7.3f} {r['test']:>8.3f} {r['usd']:>8.0f}{flag}")
    good = [r for r in rows if r["train"] > 0 and r["test"] > 0 and r["t"] > 2]
    print(f"\n{len(rows)} rules tested; {sum(r['train'] > 0 for r in rows)} made money on the train days; "
          f"{len(good)} also on the unseen days with t > 2.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
