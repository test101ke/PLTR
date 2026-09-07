"""
Multi-exchange PLTR ticker pool.
================================
~10 free public APIs across Bybit, Binance, KuCoin, Kraken, Gate, MEXC, Bitget
and OKX. Each provider discovers its own PLTR symbol (perp preferred) and returns
a normalised ticker with the latest last/mark price. The caller rotates across
the live providers (round-robin) so no single venue is polled too often — that's
the rate-limit "wiggle room". Any provider that lacks PLTR or errors is dropped.

Normalised ticker dict:
  {name, kind, symbol, price, mark, bid, ask, pct24h, vol24h}
"""
import asyncio
UA = {"User-Agent": "Mozilla/5.0 (PLTR-Signal-Desk)"}
_SYM = {}   # provider name -> discovered symbol (or False if none)


async def _gj(client, url, headers=None, timeout=8):
    try:
        r = await client.get(url, headers=headers or UA, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _f(x):
    try:
        return float(x)
    except Exception:
        return None


def _pick(cands):
    for s in cands:
        u = s.upper()
        if u.endswith("USDT") or u.endswith("USDTM") or u.endswith("USDT-PERP") or "USDT" in u:
            return s
    return cands[0] if cands else None


# ---- Bybit (linear perp + spot) ----
async def _bybit(client, cat, name):
    if name not in _SYM:
        j = await _gj(client, f"https://api.bybit.com/v5/market/instruments-info?category={cat}")
        cands = [x["symbol"] for x in j["result"]["list"]] if j and j.get("result") else []
        _SYM[name] = _pick([s for s in cands if "PLTR" in s.upper()]) or False
    if not _SYM[name]:
        return None
    sym = _SYM[name]
    j = await _gj(client, f"https://api.bybit.com/v5/market/tickers?category={cat}&symbol={sym}")
    if not j or not j.get("result", {}).get("list"):
        return None
    t = j["result"]["list"][0]
    return {"name": name, "kind": "perp" if cat == "linear" else "spot", "symbol": sym,
            "price": _f(t.get("lastPrice")), "mark": _f(t.get("markPrice")) or _f(t.get("lastPrice")),
            "bid": _f(t.get("bid1Price")), "ask": _f(t.get("ask1Price")),
            "pct24h": (_f(t.get("price24hPcnt")) or 0) * 100, "vol24h": _f(t.get("volume24h"))}


# ---- Binance USDⓈ-M futures ----
async def _binance(client):
    n = "binance-perp"
    if n not in _SYM:
        j = await _gj(client, "https://fapi.binance.com/fapi/v1/exchangeInfo")
        cands = [s["symbol"] for s in j.get("symbols", [])
                 if "PLTR" in s["symbol"].upper() and s.get("status") == "TRADING"] if j else []
        _SYM[n] = _pick(cands) or False
    if not _SYM[n]:
        return None
    sym = _SYM[n]
    mk = await _gj(client, f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={sym}")
    tk = await _gj(client, f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={sym}")
    if not tk:
        return None
    return {"name": n, "kind": "perp", "symbol": sym,
            "price": _f(tk.get("lastPrice")), "mark": _f((mk or {}).get("markPrice")) or _f(tk.get("lastPrice")),
            "bid": None, "ask": None, "pct24h": _f(tk.get("priceChangePercent")), "vol24h": _f(tk.get("volume"))}


# ---- KuCoin futures ----
async def _kucoin(client):
    n = "kucoin-perp"
    if n not in _SYM:
        j = await _gj(client, "https://api-futures.kucoin.com/api/v1/contracts/active")
        cands = [c["symbol"] for c in j.get("data", [])] if j and j.get("data") else []
        _SYM[n] = _pick([s for s in cands if "PLTR" in s.upper()]) or False
    if not _SYM[n]:
        return None
    sym = _SYM[n]
    tk = await _gj(client, f"https://api-futures.kucoin.com/api/v1/ticker?symbol={sym}")
    mk = await _gj(client, f"https://api-futures.kucoin.com/api/v1/mark-price/{sym}/current")
    d = (tk or {}).get("data") or {}
    if not d:
        return None
    return {"name": n, "kind": "perp", "symbol": sym, "price": _f(d.get("price")),
            "mark": _f((mk or {}).get("data", {}).get("value")) or _f(d.get("price")),
            "bid": _f(d.get("bestBidPrice")), "ask": _f(d.get("bestAskPrice")),
            "pct24h": None, "vol24h": None}


# ---- Kraken spot (xStock) ----
async def _kraken(client):
    n = "kraken-spot"
    if n not in _SYM:
        j = await _gj(client, "https://api.kraken.com/0/public/AssetPairs")
        cands = [k for k, v in (j.get("result", {}) if j else {}).items()
                 if "PLTR" in (v.get("altname", "") + v.get("wsname", "")).upper()]
        _SYM[n] = (cands[0] if cands else False)
    if not _SYM[n]:
        return None
    sym = _SYM[n]
    j = await _gj(client, f"https://api.kraken.com/0/public/Ticker?pair={sym}")
    res = (j or {}).get("result", {})
    if not res:
        return None
    t = list(res.values())[0]
    last = _f(t["c"][0]); op = _f(t["o"])
    return {"name": n, "kind": "spot", "symbol": sym, "price": last, "mark": last,
            "bid": _f(t["b"][0]), "ask": _f(t["a"][0]),
            "pct24h": ((last - op) / op * 100) if (last and op) else None, "vol24h": _f(t["v"][1])}


# ---- Gate.io USDT futures ----
async def _gate_perp(client):
    n = "gate-perp"
    if n not in _SYM:
        j = await _gj(client, "https://api.gateio.ws/api/v4/futures/usdt/contracts")
        cands = [c["name"] for c in j] if isinstance(j, list) else []
        _SYM[n] = _pick([s for s in cands if "PLTR" in s.upper()]) or False
    if not _SYM[n]:
        return None
    sym = _SYM[n]
    j = await _gj(client, f"https://api.gateio.ws/api/v4/futures/usdt/tickers?contract={sym}")
    if not isinstance(j, list) or not j:
        return None
    t = j[0]
    return {"name": n, "kind": "perp", "symbol": sym, "price": _f(t.get("last")),
            "mark": _f(t.get("mark_price")) or _f(t.get("last")), "bid": None, "ask": None,
            "pct24h": _f(t.get("change_percentage")), "vol24h": _f(t.get("volume_24h"))}


# ---- MEXC futures ----
async def _mexc(client):
    n = "mexc-perp"
    if n not in _SYM:
        j = await _gj(client, "https://contract.mexc.com/api/v1/contract/detail")
        cands = [c["symbol"] for c in j.get("data", [])] if j and j.get("data") else []
        _SYM[n] = _pick([s for s in cands if "PLTR" in s.upper()]) or False
    if not _SYM[n]:
        return None
    sym = _SYM[n]
    j = await _gj(client, f"https://contract.mexc.com/api/v1/contract/ticker?symbol={sym}")
    d = (j or {}).get("data") or {}
    if not d:
        return None
    return {"name": n, "kind": "perp", "symbol": sym, "price": _f(d.get("lastPrice")),
            "mark": _f(d.get("fairPrice")) or _f(d.get("lastPrice")),
            "bid": _f(d.get("bid1")), "ask": _f(d.get("ask1")),
            "pct24h": (_f(d.get("riseFallRate")) or 0) * 100, "vol24h": _f(d.get("volume24"))}


# ---- Bitget USDT futures ----
async def _bitget(client):
    n = "bitget-perp"
    if n not in _SYM:
        j = await _gj(client, "https://api.bitget.com/api/v2/mix/market/contracts?productType=usdt-futures")
        cands = [c["symbol"] for c in j.get("data", [])] if j and j.get("data") else []
        _SYM[n] = _pick([s for s in cands if "PLTR" in s.upper()]) or False
    if not _SYM[n]:
        return None
    sym = _SYM[n]
    j = await _gj(client, f"https://api.bitget.com/api/v2/mix/market/ticker?symbol={sym}&productType=usdt-futures")
    d = (j or {}).get("data") or []
    if not d:
        return None
    t = d[0]
    return {"name": n, "kind": "perp", "symbol": sym, "price": _f(t.get("lastPr")),
            "mark": _f(t.get("markPrice")) or _f(t.get("lastPr")),
            "bid": _f(t.get("bidPr")), "ask": _f(t.get("askPr")),
            "pct24h": (_f(t.get("change24h")) or 0) * 100, "vol24h": _f(t.get("baseVolume"))}


# ---- OKX swap ----
async def _okx(client):
    n = "okx-swap"
    if n not in _SYM:
        j = await _gj(client, "https://www.okx.com/api/v5/public/instruments?instType=SWAP")
        cands = [c["instId"] for c in j.get("data", [])] if j and j.get("data") else []
        _SYM[n] = _pick([s for s in cands if "PLTR" in s.upper()]) or False
    if not _SYM[n]:
        return None
    sym = _SYM[n]
    tk = await _gj(client, f"https://www.okx.com/api/v5/market/ticker?instId={sym}")
    mk = await _gj(client, f"https://www.okx.com/api/v5/public/mark-price?instType=SWAP&instId={sym}")
    d = ((tk or {}).get("data") or [{}])[0]
    if not d:
        return None
    last = _f(d.get("last")); op = _f(d.get("open24h"))
    md = ((mk or {}).get("data") or [{}])[0]
    return {"name": n, "kind": "perp", "symbol": sym, "price": last,
            "mark": _f(md.get("markPx")) or last, "bid": _f(d.get("bidPx")), "ask": _f(d.get("askPx")),
            "pct24h": ((last - op) / op * 100) if (last and op) else None, "vol24h": _f(d.get("vol24h"))}


# ---- Gate.io spot ----
async def _gate_spot(client):
    n = "gate-spot"
    if n not in _SYM:
        j = await _gj(client, "https://api.gateio.ws/api/v4/spot/currency_pairs")
        cands = [c["id"] for c in j] if isinstance(j, list) else []
        _SYM[n] = _pick([s for s in cands if "PLTR" in s.upper()]) or False
    if not _SYM[n]:
        return None
    sym = _SYM[n]
    j = await _gj(client, f"https://api.gateio.ws/api/v4/spot/tickers?currency_pair={sym}")
    if not isinstance(j, list) or not j:
        return None
    t = j[0]
    return {"name": n, "kind": "spot", "symbol": sym, "price": _f(t.get("last")), "mark": _f(t.get("last")),
            "bid": _f(t.get("highest_bid")), "ask": _f(t.get("lowest_ask")),
            "pct24h": _f(t.get("change_percentage")), "vol24h": _f(t.get("base_volume"))}


PROVIDERS = [
    ("bybit-perp",  lambda c: _bybit(c, "linear", "bybit-perp")),
    ("binance-perp", _binance),
    ("kucoin-perp",  _kucoin),
    ("okx-swap",     _okx),
    ("gate-perp",    _gate_perp),
    ("mexc-perp",    _mexc),
    ("bitget-perp",  _bitget),
    ("kraken-spot",  _kraken),
    ("gate-spot",    _gate_spot),
    ("bybit-spot",  lambda c: _bybit(c, "spot", "bybit-spot")),
]


async def probe_all(client):
    """Return list of live provider names (those that resolve a PLTR ticker)."""
    async def go(name, fn):
        try:
            r = await fn(client)
            return name if (r and r.get("price")) else None
        except Exception:
            return None
    res = await asyncio.gather(*[go(n, f) for n, f in PROVIDERS])
    return [x for x in res if x]


async def fetch_one(client, name):
    for n, fn in PROVIDERS:
        if n == name:
            try:
                return await fn(client)
            except Exception:
                return None
    return None


# ============================================================ DEPTH / TAPE / KLINES
# Order book, recent trades, and 1-minute closes from the venues that actually list PLTR.
# Bybit/Kraken usually DON'T have it, so we prefer the perp venues.
DEPTH_PRIORITY = ["binance-perp", "okx-swap", "bitget-perp", "gate-perp",
                  "kucoin-perp", "mexc-perp", "gate-spot", "bybit-perp", "bybit-spot"]


def _top(bids, asks, n=25):
    bids = sorted([b for b in bids if b[0]], key=lambda x: -x[0])[:n]
    asks = sorted([a for a in asks if a[0]], key=lambda x: x[0])[:n]
    return bids, asks


async def depth(client, name):
    """Return {'bids':[[p,s]], 'asks':[[p,s]]} for a venue, or None."""
    sym = _SYM.get(name)
    if not sym:
        return None
    try:
        if name == "binance-perp":
            j = await _gj(client, f"https://fapi.binance.com/fapi/v1/depth?symbol={sym}&limit=50")
            b = [[_f(p), _f(q)] for p, q in j.get("bids", [])]; a = [[_f(p), _f(q)] for p, q in j.get("asks", [])]
        elif name == "okx-swap":
            j = await _gj(client, f"https://www.okx.com/api/v5/market/books?instId={sym}&sz=50")
            d = (j.get("data") or [{}])[0]
            b = [[_f(x[0]), _f(x[1])] for x in d.get("bids", [])]; a = [[_f(x[0]), _f(x[1])] for x in d.get("asks", [])]
        elif name == "bitget-perp":
            j = await _gj(client, f"https://api.bitget.com/api/v2/mix/market/merge-depth?symbol={sym}&productType=usdt-futures&limit=50")
            d = j.get("data", {}); b = [[_f(x[0]), _f(x[1])] for x in d.get("bids", [])]; a = [[_f(x[0]), _f(x[1])] for x in d.get("asks", [])]
        elif name == "gate-perp":
            j = await _gj(client, f"https://api.gateio.ws/api/v4/futures/usdt/order_book?contract={sym}&limit=30")
            b = [[_f(x["p"]), _f(x["s"])] for x in j.get("bids", [])]; a = [[_f(x["p"]), _f(x["s"])] for x in j.get("asks", [])]
        elif name == "gate-spot":
            j = await _gj(client, f"https://api.gateio.ws/api/v4/spot/order_book?currency_pair={sym}&limit=30")
            b = [[_f(x[0]), _f(x[1])] for x in j.get("bids", [])]; a = [[_f(x[0]), _f(x[1])] for x in j.get("asks", [])]
        elif name == "kucoin-perp":
            j = await _gj(client, f"https://api-futures.kucoin.com/api/v1/level2/depth20?symbol={sym}")
            d = j.get("data", {}); b = [[_f(x[0]), _f(x[1])] for x in d.get("bids", [])]; a = [[_f(x[0]), _f(x[1])] for x in d.get("asks", [])]
        elif name == "mexc-perp":
            j = await _gj(client, f"https://contract.mexc.com/api/v1/contract/depth/{sym}")
            d = j.get("data", {}); b = [[_f(x[0]), _f(x[1])] for x in d.get("bids", [])]; a = [[_f(x[0]), _f(x[1])] for x in d.get("asks", [])]
        elif name in ("bybit-perp", "bybit-spot"):
            cat = "linear" if name == "bybit-perp" else "spot"
            j = await _gj(client, f"https://api.bybit.com/v5/market/orderbook?category={cat}&symbol={sym}&limit=50")
            d = j.get("result", {}); b = [[_f(p), _f(s)] for p, s in d.get("b", [])]; a = [[_f(p), _f(s)] for p, s in d.get("a", [])]
        else:
            return None
        b, a = _top(b, a)
        return {"bids": b, "asks": a} if (b or a) else None
    except Exception:
        return None


async def tape(client, name):
    """Return recent trades [{'px','sz','side','ts','usd'}] newest first, or []."""
    sym = _SYM.get(name)
    if not sym:
        return []
    try:
        out = []
        if name == "binance-perp":
            j = await _gj(client, f"https://fapi.binance.com/fapi/v1/aggTrades?symbol={sym}&limit=60")
            for t in (j or []):
                px, sz = _f(t["p"]), _f(t["q"]); out.append({"px": px, "sz": sz, "side": "sell" if t.get("m") else "buy", "ts": int(t["T"]), "usd": px * sz})
        elif name == "okx-swap":
            j = await _gj(client, f"https://www.okx.com/api/v5/market/trades?instId={sym}&limit=60")
            for t in (j.get("data") or []):
                px, sz = _f(t["px"]), _f(t["sz"]); out.append({"px": px, "sz": sz, "side": t.get("side", "buy"), "ts": int(t["ts"]), "usd": px * sz})
        elif name == "bitget-perp":
            j = await _gj(client, f"https://api.bitget.com/api/v2/mix/market/fills?symbol={sym}&productType=usdt-futures&limit=60")
            for t in (j.get("data") or []):
                px, sz = _f(t.get("price")), _f(t.get("size")); out.append({"px": px, "sz": sz, "side": t.get("side", "buy"), "ts": int(t.get("ts", 0)), "usd": (px or 0) * (sz or 0)})
        elif name == "gate-perp":
            j = await _gj(client, f"https://api.gateio.ws/api/v4/futures/usdt/trades?contract={sym}&limit=60")
            for t in (j or []):
                sz = _f(t.get("size")) or 0; px = _f(t.get("price")); out.append({"px": px, "sz": abs(sz), "side": "buy" if sz > 0 else "sell", "ts": int(float(t.get("create_time_ms", 0))), "usd": px * abs(sz)})
        elif name == "gate-spot":
            j = await _gj(client, f"https://api.gateio.ws/api/v4/spot/trades?currency_pair={sym}&limit=60")
            for t in (j or []):
                px, sz = _f(t.get("price")), _f(t.get("amount")); out.append({"px": px, "sz": sz, "side": t.get("side", "buy"), "ts": int(float(t.get("create_time_ms", 0))), "usd": (px or 0) * (sz or 0)})
        elif name == "kucoin-perp":
            j = await _gj(client, f"https://api-futures.kucoin.com/api/v1/trade/history?symbol={sym}")
            for t in (j.get("data") or []):
                px, sz = _f(t.get("price")), _f(t.get("size")); out.append({"px": px, "sz": sz, "side": t.get("side", "buy"), "ts": int(t.get("ts", 0)) // 1_000_000, "usd": (px or 0) * (sz or 0)})
        elif name == "mexc-perp":
            j = await _gj(client, f"https://contract.mexc.com/api/v1/contract/deals/{sym}")
            for t in (j.get("data") or []):
                px, sz = _f(t.get("p")), _f(t.get("v")); out.append({"px": px, "sz": sz, "side": "buy" if t.get("T") == 1 else "sell", "ts": int(t.get("t", 0)), "usd": (px or 0) * (sz or 0)})
        return [t for t in out if t["px"]][:60]
    except Exception:
        return []


async def klines(client, name, limit=20):
    """Return recent 1-minute close prices (oldest -> newest)."""
    sym = _SYM.get(name)
    if not sym:
        return []
    try:
        if name == "binance-perp":
            j = await _gj(client, f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval=1m&limit={limit}")
            return [_f(r[4]) for r in j] if isinstance(j, list) else []
        if name == "okx-swap":
            j = await _gj(client, f"https://www.okx.com/api/v5/market/candles?instId={sym}&bar=1m&limit={limit}")
            return [_f(r[4]) for r in reversed(j.get("data", []))]
        if name == "bitget-perp":
            j = await _gj(client, f"https://api.bitget.com/api/v2/mix/market/candles?symbol={sym}&productType=usdt-futures&granularity=1m&limit={limit}")
            return [_f(r[4]) for r in j.get("data", [])]
        if name == "gate-perp":
            j = await _gj(client, f"https://api.gateio.ws/api/v4/futures/usdt/candlesticks?contract={sym}&interval=1m&limit={limit}")
            return [_f(r.get("c")) for r in j] if isinstance(j, list) else []
        if name == "gate-spot":
            j = await _gj(client, f"https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair={sym}&interval=1m&limit={limit}")
            return [_f(r[2]) for r in j] if isinstance(j, list) else []
        if name == "kucoin-perp":
            import time as _t
            to = int(_t.time()); frm = to - limit * 60
            j = await _gj(client, f"https://api-futures.kucoin.com/api/v1/kline/query?symbol={sym}&granularity=1&from={frm*1000}&to={to*1000}")
            return [_f(r[4]) for r in j.get("data", [])]
        if name == "mexc-perp":
            j = await _gj(client, f"https://contract.mexc.com/api/v1/contract/kline/{sym}?interval=Min1")
            cl = (j.get("data") or {}).get("close", []); return [_f(x) for x in cl[-limit:]]
    except Exception:
        return []
    return []


async def resolve_depth(client, live_names):
    """First venue (by priority) among the live pool that returns a usable order book."""
    for name in DEPTH_PRIORITY:
        if name in live_names:
            b = await depth(client, name)
            if b and (b["bids"] or b["asks"]):
                return name
    return None


# ============================================================ OHLC (for session models)
async def ohlc(client, name, interval="5m", limit=300):
    """Return [{'t':ms,'o','h','l','c','v'}] oldest->newest for a venue, or []."""
    sym = _SYM.get(name)
    if not sym:
        return []
    def row(t, o, h, l, c, v):
        t = int(t)
        return {"t": t if t > 1e12 else t * 1000, "o": _f(o), "h": _f(h), "l": _f(l), "c": _f(c), "v": _f(v) or 0}
    try:
        if name == "binance-perp":
            j = await _gj(client, f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval={interval}&limit={limit}")
            return [row(r[0], r[1], r[2], r[3], r[4], r[5]) for r in j] if isinstance(j, list) else []
        if name == "okx-swap":
            j = await _gj(client, f"https://www.okx.com/api/v5/market/candles?instId={sym}&bar={interval}&limit={min(limit,300)}")
            return [row(r[0], r[1], r[2], r[3], r[4], r[5]) for r in reversed(j.get("data", []))]
        if name == "bitget-perp":
            j = await _gj(client, f"https://api.bitget.com/api/v2/mix/market/candles?symbol={sym}&productType=usdt-futures&granularity={interval}&limit={limit}")
            return [row(r[0], r[1], r[2], r[3], r[4], r[5]) for r in j.get("data", [])]
        if name == "gate-perp":
            j = await _gj(client, f"https://api.gateio.ws/api/v4/futures/usdt/candlesticks?contract={sym}&interval={interval}&limit={limit}")
            return [row(r["t"], r["o"], r["h"], r["l"], r["c"], r.get("v", 0)) for r in j] if isinstance(j, list) else []
        if name == "gate-spot":
            j = await _gj(client, f"https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair={sym}&interval={interval}&limit={limit}")
            # gate spot rows: [t, quoteVol, close, high, low, open, baseVol, ...]
            return [row(r[0], r[5], r[3], r[4], r[2], r[6] if len(r) > 6 else 0) for r in j] if isinstance(j, list) else []
        if name == "mexc-perp":
            iv = {"1m": "Min1", "5m": "Min5", "15m": "Min15"}.get(interval, "Min5")
            j = await _gj(client, f"https://contract.mexc.com/api/v1/contract/kline/{sym}?interval={iv}")
            d = j.get("data") or {}
            ts, op, hi, lo, cl = d.get("time", []), d.get("open", []), d.get("high", []), d.get("low", []), d.get("close", [])
            vo = d.get("vol", [0] * len(ts))
            return [row(ts[i], op[i], hi[i], lo[i], cl[i], vo[i] if i < len(vo) else 0) for i in range(len(ts))][-limit:]
        if name == "kucoin-perp":
            import time as _t
            gran = {"1m": 1, "5m": 5, "15m": 15}.get(interval, 5)
            to = int(_t.time() * 1000); frm = to - limit * gran * 60000
            j = await _gj(client, f"https://api-futures.kucoin.com/api/v1/kline/query?symbol={sym}&granularity={gran}&from={frm}&to={to}")
            return [row(r[0], r[1], r[2], r[3], r[4], r[5] if len(r) > 5 else 0) for r in j.get("data", [])]
        if name in ("bybit-perp", "bybit-spot"):
            cat = "linear" if name == "bybit-perp" else "spot"
            iv = {"1m": "1", "5m": "5", "15m": "15"}.get(interval, "5")
            j = await _gj(client, f"https://api.bybit.com/v5/market/kline?category={cat}&symbol={sym}&interval={iv}&limit={min(limit,1000)}")
            rows = j.get("result", {}).get("list", [])
            return [row(r[0], r[1], r[2], r[3], r[4], r[5]) for r in sorted(rows, key=lambda x: int(x[0]))]
    except Exception:
        return []
    return []


async def ohlc_any(client, names, interval="5m", limit=300):
    """First venue in `names` that returns a usable candle series."""
    for n in names:
        rows = await ohlc(client, n, interval, limit)
        rows = [r for r in rows if r["c"]]
        if len(rows) >= 30:
            return n, rows
    return None, []
