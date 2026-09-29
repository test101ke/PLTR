"""
Recency-weighted rule selection for the open-session trade.
============================================================
Every past open still counts, but a day `k` sessions old counts 0.5**(k/HL) as
much as the newest one (HL = half-life in sessions). HL=None means every day
counts equally, i.e. the old static behaviour.

Used in two places with the same code:
  * backtest.py  walks forward day by day, re-picking the rule each morning
                 from ONLY the days before it, to measure whether recency
                 weighting beats a fixed rule, and which half-life works.
  * main.py      ranks the rules live and updates the ranking the moment a
                 new open's outcome is known.
"""
import math

HALF_LIVES = (10, 20, 40, 80, None)
MIN_TRADES = 20        # a rule needs this many past trades before it can be picked
MIN_T = 1.0            # ...and a weighted t-stat above this, or we stand aside


def matrix(samples, rules, fee_pct):
    """dirs[r][i] in {-1,0,1} and net pnl % for rule r on day i (None = no trade)."""
    dirs, pnl = [], []
    for _, _, fn in rules:
        d_row, p_row = [], []
        for s in samples:
            d = fn(s)
            d_row.append(d)
            p_row.append(d * s["r_rem"] * 100 - fee_pct if d and s.get("r_rem") is not None else None)
        dirs.append(d_row); pnl.append(p_row)
    return dirs, pnl


def wstats(pairs):
    """pairs: [(pnl, weight)]. Weighted mean, hit rate, t-stat on effective n."""
    sw = sum(w for _, w in pairs)
    if not pairs or sw <= 0:
        return None
    mean = sum(x * w for x, w in pairs) / sw
    var = sum(w * (x - mean) ** 2 for x, w in pairs) / sw
    n_eff = sw ** 2 / sum(w * w for _, w in pairs)
    t = mean / math.sqrt(var / n_eff) if var > 0 and n_eff > 1 else None
    hit = sum(w for x, w in pairs if x > 0) / sw
    return {"mean": mean, "hit": hit, "t": t, "nEff": n_eff, "n": len(pairs)}


def rank(pnl_rows, upto, hl, rules):
    """Score each rule on days [0, upto) with recency weights. Best first."""
    out = []
    for r, row in enumerate(pnl_rows):
        pairs = [(row[j], 1.0 if hl is None else 0.5 ** ((upto - 1 - j) / hl))
                 for j in range(upto) if row[j] is not None]
        st = wstats(pairs)
        if not st or st["n"] < MIN_TRADES or st["t"] is None:
            continue
        out.append({"idx": r, "family": rules[r][0], "name": rules[r][1],
                    "wHit": round(st["hit"], 3), "wExp": round(st["mean"], 4),
                    "wT": round(st["t"], 2), "n": st["n"], "nEff": round(st["nEff"], 1)})
    out.sort(key=lambda x: -x["wT"])
    return out


def pick(ranking):
    """Top rule if it is convincingly positive, else None (stand aside)."""
    if ranking and ranking[0]["wT"] >= MIN_T and ranking[0]["wExp"] > 0:
        return ranking[0]
    return None


def walk(samples, pnl_rows, dirs, rules, hl, start, end):
    """Each day i in [start, end): pick using days < i only, then trade day i."""
    trades = []
    for i in range(start, end):
        best = pick(rank(pnl_rows, i, hl, rules))
        if best is None:
            continue
        p = pnl_rows[best["idx"]][i]
        if p is not None:
            trades.append({"day": samples[i]["day"], "rule": best["name"], "pnl": p,
                           "dir": dirs[best["idx"]][i]})
    return trades


def summarize(trades):
    n = len(trades)
    if not n:
        return {"n": 0, "hit": None, "exp": None, "total": 0.0, "t": None}
    xs = [t["pnl"] for t in trades]
    mean = sum(xs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
    return {"n": n, "hit": round(sum(1 for x in xs if x > 0) / n, 3), "exp": round(mean, 4),
            "total": round(sum(xs), 2), "t": round(mean / sd * math.sqrt(n), 2) if sd else None}
