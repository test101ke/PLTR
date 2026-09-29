"""
Replay of the ORB trader (trader.py) on real 1-minute candles, preset by preset.
================================================================================
Uses the exact OrbStrategy the live and paper trader run, fed with the same
09:30-09:45 New York candles the open-session backtest downloads.

How a candle becomes ticks: each 1-minute bar is replayed as four prices,
open -> low -> high -> close for an up bar (open -> high -> low -> close for a
down bar), at 0s, 15s, 30s and 59s. Bid/ask sit 0.01% either side, fills pay
PAPER slippage and the configured fee (0.06% a side by default).

Limits, stated plainly:
  * the order book is not in the history, so the "book must lean" entry filter
    cannot be tested and is switched off here (live trading still uses it);
  * inside a minute the real path is unknown; four ticks is an approximation,
    so stop-confirmation and grace timings are coarser than live;
  * presets are fixed choices, not fitted to this data, so every day is
    out-of-sample for them; the newest 30% is also shown on its own.
"""
import datetime as dt
import trader as tr

NY = tr.NY
HALF_SPREAD = 0.0001


def bar_ticks(t_ms, o, h, l, c):
    path = [o, l, h, c] if c >= o else [o, h, l, c]
    return [(t_ms + off * 1000, p) for off, p in zip((0, 15, 30, 59), path)]


def _fill(side, bid, ask, cfg):
    slip = cfg["slippagePct"] / 100
    return ask * (1 + slip) if side == "buy" else bid * (1 - slip)


def replay(cfg, raw):
    """raw: [(day, {minute_ms: [t,o,h,l,c,v]}, close)] as backtest.load_days returns.
    Returns one row per closed position: {day, side, pnl, exits}."""
    cfg = tr.clean_config({**cfg, "imbalanceMin": 50})
    s = tr.OrbStrategy(cfg, cfg["paperEquity"])
    fee = tr.fee_pct(cfg) / 100
    trades, cur = [], None
    for day, win, _ in raw:
        op = int(dt.datetime.combine(day, dt.time(9, 30), tzinfo=NY).timestamp() * 1000)
        for t in sorted(win):
            if t < op:
                continue
            _, o, h, l, c = win[t][:5]
            for ts, px in bar_ticks(t, o, h, l, c):
                now = dt.datetime.fromtimestamp(ts / 1000, NY)
                bid, ask = px * (1 - HALF_SPREAD), px * (1 + HALF_SPREAD)
                for act in s.on_tick(now, bid, ask, 50.0):
                    if act[0] == "open":
                        side = act[1]
                        price = _fill("buy" if side == "long" else "sell", bid, ask, cfg)
                        qty = s.size(cfg["paperEquity"], price)
                        s.opened(side, qty, price, now, price * qty * fee)
                        cur = {"day": str(day), "side": side, "pnl": 0.0, "exits": []}
                    elif s.pos:
                        frac = act[2] if len(act) > 2 else 1.0
                        side = s.pos["side"]
                        price = _fill("sell" if side == "long" else "buy", bid, ask, cfg)
                        q = s.pos["qty"] * frac
                        cur["pnl"] += s.closed(price, price * q * fee, frac)
                        cur["exits"].append(act[1])
                        if s.pos is None:
                            trades.append(cur); cur = None
        if s.pos:                                   # safety: never carry a position overnight
            p = s.pos
            last = win[max(win)][4]
            cur["pnl"] += s.closed(last, last * p["qty"] * fee)
            cur["exits"].append("end of data"); trades.append(cur); cur = None
    return trades


def summarize(trades, days):
    n = len(trades)
    pnl = [t["pnl"] for t in trades]
    by_day = {}
    for t in trades:
        by_day[t["day"]] = by_day.get(t["day"], 0.0) + t["pnl"]
    run = peak = dd = 0.0
    for p in pnl:
        run += p; peak = max(peak, run); dd = min(dd, run - peak)
    return {"days": days, "trades": n, "tradeDays": len(by_day),
            "winRate": round(sum(1 for p in pnl if p > 0) / n * 100, 1) if n else None,
            "total": round(sum(pnl), 2), "perTrade": round(sum(pnl) / n, 3) if n else None,
            "perDay": round(sum(pnl) / days, 3) if days else None,
            "bestDay": round(max(by_day.values()), 2) if by_day else None,
            "worstDay": round(min(by_day.values()), 2) if by_day else None,
            "maxDrawdown": round(dd, 2)}


def study(raw, base_cfg=None):
    """Every preset on the same days. P&L is in USDT per $100-margin batch at 10x."""
    raw = [r for r in raw if r[1]]
    cut = int(len(raw) * 0.7)
    out = {}
    for name, p in tr.PRESETS.items():
        cfg = {**(base_cfg or {}), **{k: v for k, v in p.items() if k not in ("label", "note")}}
        trades = replay(cfg, raw)
        newest_days = {str(d) for d, _, _ in raw[cut:]}
        out[name] = {"label": p["label"], "all": summarize(trades, len(raw)),
                     "newest30": summarize([t for t in trades if t["day"] in newest_days], len(raw) - cut)}
    return out
