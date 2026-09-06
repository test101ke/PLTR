"""
PLTR Signal Desk — real-time backend
=====================================
A FastAPI service that powers a live Palantir trading dashboard.

It continuously pulls, caches, and serves:
  * Tokenized Palantir (PLTRX) live market from Bybit (primary) / Kraken (fallback):
    order book, buy/sell trade tape, large-order prints, 24h volume.
  * The real NASDAQ PLTR: price, 1y history, 52-week range, volume, moving
    averages (50/200), RSI(14), and fundamentals where available (Yahoo).
  * News headlines (Google News RSS, free) + optional X/Twitter search.
  * An AI agent (Anthropic) that scores each headline bullish/bearish, writes a
    short thesis, and feeds a composite STRONG BUY / SIDEWAYS / STRONG SELL signal.

Everything is fetched server-side (works on Render; no browser CORS/CSP limits).
The frontend polls /api/state every second.

Env vars (all optional except where noted):
  ANTHROPIC_API_KEY   enable the AI agent (falls back to keyword scoring if unset)
  AI_MODEL            default "claude-3-5-haiku-latest"
  X_BEARER_TOKEN      enable X/Twitter buzz (skipped if unset)
  BYBIT_SYMBOL        override auto-discovery (e.g. "PLTRXUSDT")
  KRAKEN_PAIR         override auto-discovery (e.g. "PLTRXUSD")
  LARGE_TRADE_USD     tape print flagged as "large" (default 3000)
  FAST_INTERVAL       seconds between crypto polls (default 2)
  STOCK_INTERVAL      seconds between stock polls (default 30)
  AGENT_INTERVAL      seconds between AI/news runs (default 300)
"""

import os, asyncio, time, math, json, contextlib, datetime as dt
from typing import Any, Optional
import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import backtest as bt
import exchanges as ex

# ---------------------------------------------------------------- config
CFG = {
    "ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY", "").strip(),
    "AI_MODEL": os.getenv("AI_MODEL", "claude-3-5-haiku-latest").strip(),
    "X_BEARER_TOKEN": os.getenv("X_BEARER_TOKEN", "").strip(),
    "BYBIT_SYMBOL": os.getenv("BYBIT_SYMBOL", "").strip(),
    "KRAKEN_PAIR": os.getenv("KRAKEN_PAIR", "").strip(),
    "LARGE_TRADE_USD": float(os.getenv("LARGE_TRADE_USD", "3000")),
    "FAST_INTERVAL": float(os.getenv("FAST_INTERVAL", "1")),      # rotating price, ~instant
    "DEPTH_INTERVAL": float(os.getenv("DEPTH_INTERVAL", "3")),    # order book / tape / 1m klines
    "STOCK_INTERVAL": float(os.getenv("STOCK_INTERVAL", "30")),
    "NEWS_INTERVAL": float(os.getenv("NEWS_INTERVAL", "15")),     # fast headline pull
    "AGENT_INTERVAL": float(os.getenv("AGENT_INTERVAL", "60")),   # AI enrich (thesis + rescoring)
}
UA = {"User-Agent": "Mozilla/5.0 (PLTR-Signal-Desk)"}
HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- shared state
STATE: dict[str, Any] = {
    "meta": {"started": None, "stockAsOf": None, "cryptoAsOf": None, "agentAsOf": None,
             "bybitSymbol": None, "krakenPair": None, "cryptoSource": None,
             "aiEnabled": bool(CFG["ANTHROPIC_API_KEY"]), "errors": {}},
    "stock": {},      # real NASDAQ PLTR
    "crypto": {},     # tokenized PLTRX live market
    "news": [],       # scored headlines
    "tweets": [],     # scored X/Twitter posts
    "agent": {},      # AI thesis + sentiment
    "signal": {},     # composite call
    "backtest": {},   # open-window edge study
}
_LOCK = asyncio.Lock()
_client: Optional[httpx.AsyncClient] = None

from collections import deque
_LIVE = deque(maxlen=1400)   # (epoch_seconds, mark_price) for short-timeframe signals
_ROT = {"i": 0, "live": []}  # rotating exchange price pool


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


async def get(url: str, **kw) -> Optional[httpx.Response]:
    try:
        r = await _client.get(url, headers=UA, timeout=kw.pop("timeout", 12), **kw)
        r.raise_for_status()
        return r
    except Exception as e:
        return None


# ============================================================ CRYPTO: Bybit
async def bybit_discover() -> Optional[str]:
    if CFG["BYBIT_SYMBOL"]:
        return CFG["BYBIT_SYMBOL"]
    r = await get("https://api.bybit.com/v5/market/instruments-info?category=spot")
    if not r:
        return None
    try:
        rows = r.json()["result"]["list"]
        # prefer a USDT-quoted PLTR token
        cands = [x["symbol"] for x in rows if "PLTR" in x["symbol"].upper()]
        for s in cands:
            if s.upper().endswith("USDT"):
                return s
        return cands[0] if cands else None
    except Exception:
        return None


async def bybit_market(sym: str) -> Optional[dict]:
    t, ob, tr = await asyncio.gather(
        get(f"https://api.bybit.com/v5/market/tickers?category=spot&symbol={sym}"),
        get(f"https://api.bybit.com/v5/market/orderbook?category=spot&symbol={sym}&limit=50"),
        get(f"https://api.bybit.com/v5/market/recent-trade?category=spot&symbol={sym}&limit=60"),
    )
    if not (t and ob):
        return None
    try:
        ti = t.json()["result"]["list"][0]
        obj = ob.json()["result"]
        bids = [[float(p), float(s)] for p, s in obj.get("b", [])]
        asks = [[float(p), float(s)] for p, s in obj.get("a", [])]
        trades = []
        if tr:
            for x in tr.json()["result"]["list"]:
                px, sz = float(x["price"]), float(x["size"])
                trades.append({"px": px, "sz": sz, "usd": px * sz,
                               "side": x["side"].lower(), "ts": int(x["time"]),
                               "block": bool(x.get("isBlockTrade"))})
        return _shape_crypto(sym, "Bybit", ti_last=float(ti["lastPrice"]),
                             pct=float(ti.get("price24hPcnt", 0)) * 100,
                             high=float(ti.get("highPrice24h", 0)),
                             low=float(ti.get("lowPrice24h", 0)),
                             vol=float(ti.get("volume24h", 0)),
                             turnover=float(ti.get("turnover24h", 0)),
                             bid=float(ti.get("bid1Price", 0)),
                             ask=float(ti.get("ask1Price", 0)),
                             bids=bids, asks=asks, trades=trades)
    except Exception:
        return None


# ============================================================ CRYPTO: Kraken
async def kraken_discover() -> Optional[str]:
    if CFG["KRAKEN_PAIR"]:
        return CFG["KRAKEN_PAIR"]
    r = await get("https://api.kraken.com/0/public/AssetPairs")
    if not r:
        return None
    try:
        res = r.json()["result"]
        for k, v in res.items():
            alt = (v.get("altname") or "").upper()
            ws = (v.get("wsname") or "").upper()
            if "PLTR" in alt or "PLTR" in ws:
                return k
    except Exception:
        return None
    return None


async def kraken_market(pair: str) -> Optional[dict]:
    tk, dp, tr = await asyncio.gather(
        get(f"https://api.kraken.com/0/public/Ticker?pair={pair}"),
        get(f"https://api.kraken.com/0/public/Depth?pair={pair}&count=50"),
        get(f"https://api.kraken.com/0/public/Trades?pair={pair}"),
    )
    if not (tk and dp):
        return None
    try:
        tkey = list(tk.json()["result"].keys())[0]
        ti = tk.json()["result"][tkey]
        dkey = list(dp.json()["result"].keys())[0]
        d = dp.json()["result"][dkey]
        bids = [[float(p), float(s)] for p, s, *_ in d.get("bids", [])]
        asks = [[float(p), float(s)] for p, s, *_ in d.get("asks", [])]
        last = float(ti["c"][0]); op = float(ti["o"])
        pct = ((last - op) / op * 100) if op else 0.0
        trades = []
        if tr:
            rkey = [k for k in tr.json()["result"].keys() if k != "last"]
            if rkey:
                for row in tr.json()["result"][rkey[0]][-60:]:
                    px, sz = float(row[0]), float(row[1])
                    side = "buy" if row[3] == "b" else "sell"
                    trades.append({"px": px, "sz": sz, "usd": px * sz,
                                   "side": side, "ts": int(float(row[2]) * 1000),
                                   "block": False})
        return _shape_crypto(pair, "Kraken", ti_last=last, pct=pct,
                             high=float(ti["h"][1]), low=float(ti["l"][1]),
                             vol=float(ti["v"][1]), turnover=float(ti["v"][1]) * last,
                             bid=float(ti["b"][0]), ask=float(ti["a"][0]),
                             bids=bids, asks=asks, trades=trades)
    except Exception:
        return None


def _shape_crypto(sym, source, *, ti_last, pct, high, low, vol, turnover,
                  bid, ask, bids, asks, trades) -> dict:
    bids = sorted(bids, key=lambda x: -x[0])[:25]
    asks = sorted(asks, key=lambda x: x[0])[:25]
    bidvol = sum(s for _, s in bids)
    askvol = sum(s for _, s in asks)
    imb = ((bidvol - askvol) / (bidvol + askvol) * 100) if (bidvol + askvol) else 0.0
    spread = (ask - bid) if (ask and bid) else 0.0
    # tape pressure over the trade window
    buy_usd = sum(t["usd"] for t in trades if t["side"] == "buy")
    sell_usd = sum(t["usd"] for t in trades if t["side"] == "sell")
    tot = buy_usd + sell_usd
    buy_pct = round(buy_usd / tot * 100) if tot else 50
    large = [t for t in sorted(trades, key=lambda x: -x["ts"])
             if t["usd"] >= CFG["LARGE_TRADE_USD"]][:12]
    tape = sorted(trades, key=lambda x: -x["ts"])[:40]
    return {
        "symbol": sym, "source": source, "last": ti_last, "pct24h": pct,
        "high24h": high, "low24h": low, "vol24h": vol, "turnover24h": turnover,
        "bid": bid, "ask": ask, "spread": spread,
        "spreadPct": (spread / ti_last * 100) if ti_last else 0.0,
        "bids": bids, "asks": asks, "bidVol": bidvol, "askVol": askvol,
        "imbalance": imb, "buyPct": buy_pct, "sellPct": 100 - buy_pct,
        "buyUsd": buy_usd, "sellUsd": sell_usd,
        "tape": tape, "large": large,
    }


async def poll_depth():
    """Order book + trade tape from the best PLTR venue in the pool (Binance/OKX/Gate/Bitget/…)."""
    name = STATE["meta"].get("depthVenue")
    if not name:
        name = await ex.resolve_depth(_client, _ROT["live"])
        STATE["meta"]["depthVenue"] = name
    if not name:
        async with _LOCK:
            STATE["meta"]["errors"]["depth"] = "no PLTR order-book venue in the pool"
        recompute_signal(); return
    book = await ex.depth(_client, name)
    trades = await ex.tape(_client, name)
    if not book:
        async with _LOCK:
            STATE["meta"]["errors"]["depth"] = f"no order book from {name}"
        recompute_signal(); return
    bids, asks = book["bids"], book["asks"]
    bidvol = sum(s for _, s in bids); askvol = sum(s for _, s in asks)
    imb = ((bidvol - askvol) / (bidvol + askvol) * 100) if (bidvol + askvol) else 0.0
    buy_usd = sum(t["usd"] for t in trades if t["side"] == "buy")
    sell_usd = sum(t["usd"] for t in trades if t["side"] == "sell")
    tot = buy_usd + sell_usd
    buy_pct = round(buy_usd / tot * 100) if tot else 50
    large = [t for t in trades if t["usd"] >= CFG["LARGE_TRADE_USD"]][:12]
    spread = (asks[0][0] - bids[0][0]) if (bids and asks) else 0.0
    async with _LOCK:
        c = STATE["crypto"]
        c.update({"bids": bids, "asks": asks, "bidVol": bidvol, "askVol": askvol,
                  "imbalance": imb, "tape": trades[:40], "large": large,
                  "buyPct": buy_pct, "sellPct": 100 - buy_pct, "spread": spread,
                  "spreadPct": (spread / asks[0][0] * 100) if (asks and asks[0][0]) else 0.0,
                  "depthSource": name})
        STATE["meta"]["cryptoAsOf"] = now_iso()
        STATE["meta"]["errors"].pop("depth", None); STATE["meta"]["errors"].pop("crypto", None)
    recompute_signal()


async def rotate_price():
    """Round-robin one exchange from the live pool for the latest last/mark price."""
    live = _ROT["live"]
    if not live:
        return
    name = live[_ROT["i"] % len(live)]
    _ROT["i"] += 1
    t = await ex.fetch_one(_client, name)
    if not t or not t.get("price"):
        return
    mark = t.get("mark") or t["price"]
    async with _LOCK:
        c = STATE["crypto"]
        c.setdefault("exchanges", {})[name] = {"price": t["price"], "mark": mark,
                                               "pct24h": t.get("pct24h"), "kind": t.get("kind"),
                                               "symbol": t.get("symbol"), "vol24h": t.get("vol24h"),
                                               "ts": now_iso()}
        c["last"] = t["price"]; c["mark"] = mark; c["priceSource"] = name
        # cumulative volume across the pool (raw contracts/base, and a rough USD notional)
        cum = cum_usd = 0.0
        for v in c["exchanges"].values():
            if v.get("vol24h"):
                cum += v["vol24h"]
                if v.get("price"):
                    cum_usd += v["vol24h"] * v["price"]
        c["cumVol"] = cum
        c["cumVolUsd"] = cum_usd
        if t.get("pct24h") is not None:
            c["pct24h"] = t["pct24h"]
        if t.get("bid"):
            c["bid"] = t["bid"]
        if t.get("ask"):
            c["ask"] = t["ask"]
        STATE["meta"]["cryptoAsOf"] = now_iso()
        STATE["meta"]["cryptoSource"] = f"pool×{len(live)}"
    _LIVE.append((time.time(), mark))


async def seed_live():
    """Prefill the timeframe buffer with recent 1-minute closes so 1–15M signals work immediately."""
    order = [STATE["meta"].get("depthVenue")] + list(_ROT["live"])
    for name in [n for n in order if n]:
        cl = await ex.klines(_client, name, 20)
        cl = [x for x in cl if x]
        if len(cl) >= 5:
            now = time.time(); n = len(cl)
            for i, px in enumerate(cl):
                _LIVE.append((now - (n - 1 - i) * 60, px))
            return


def _price_ago(seconds):
    if not _LIVE:
        return None
    target = _LIVE[-1][0] - seconds
    best = None
    for ts, px in _LIVE:
        if ts <= target:
            best = px
        else:
            break
    return best


def compute_timeframes(stock):
    now_px = _LIVE[-1][1] if _LIVE else STATE["crypto"].get("mark")
    if not now_px:
        return []
    bias = 0.0  # larger-timeframe awareness from the real NASDAQ stock
    if stock.get("price") and stock.get("dma50") and stock.get("dma200"):
        p, d50, d200 = stock["price"], stock["dma50"], stock["dma200"]
        if p > d50 > d200:
            bias = 1.0
        elif p < d50 < d200:
            bias = -1.0
        elif p > d200:
            bias = 0.5
        elif p < d200:
            bias = -0.5
    out = []
    for tf in (1, 3, 5, 10, 15):
        past = _price_ago(tf * 60)
        if not past:
            out.append({"tf": f"{tf}M", "word": "…", "cls": "na", "ret": None})
            continue
        r = (now_px / past - 1) * 100
        k = math.sqrt(tf)
        radj = r + bias * 0.06 * k
        buy, strong = 0.12 * k, 0.40 * k
        if radj >= strong:
            w, cl = "STRONG BUY", "buy"
        elif radj >= buy:
            w, cl = "BUY", "buy"
        elif radj <= -strong:
            w, cl = "STRONG SELL", "sell"
        elif radj <= -buy:
            w, cl = "SELL", "sell"
        else:
            w, cl = "NEUTRAL", "side"
        out.append({"tf": f"{tf}M", "word": w, "cls": cl, "ret": round(r, 2)})
    return out


# ============================================================ STOCK: Yahoo
def _sma(vals, n):
    return round(sum(vals[-n:]) / n, 2) if len(vals) >= n else None


def _rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(-n, 0):
        ch = closes[i] - closes[i - 1]
        gains += max(ch, 0); losses += max(-ch, 0)
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return round(100 - 100 / (1 + rs), 1)


async def fetch_extended() -> Optional[dict]:
    """Market phase + pre-market / after-hours price via Yahoo's includePrePost chart (no crumb)."""
    r = await get("https://query1.finance.yahoo.com/v8/finance/chart/PLTR?range=1d&interval=1m&includePrePost=true")
    if not r:
        return None
    try:
        res = r.json()["chart"]["result"][0]
        meta = res["meta"]
        nowt = dt.datetime.now(dt.timezone.utc).timestamp()
        ctp = meta.get("currentTradingPeriod", {}) or {}
        def within(k):
            p = ctp.get(k) or {}
            return p.get("start") and p.get("end") and p["start"] <= nowt < p["end"]
        phase = "pre" if within("pre") else "regular" if within("regular") else "post" if within("post") else "closed"
        closes = res.get("indicators", {}).get("quote", [{}])[0].get("close", []) or []
        last = next((c for c in reversed(closes) if c is not None), None)
        reg = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        out = {"marketPhase": phase}
        if phase == "pre" and last and prev:
            out.update(extendedPrice=round(last, 2), extendedPct=round((last / prev - 1) * 100, 2), extendedLabel="Pre-market")
        elif phase == "post" and last and reg:
            out.update(extendedPrice=round(last, 2), extendedPct=round((last / reg - 1) * 100, 2), extendedLabel="After-hours")
        return out
    except Exception:
        return None


async def poll_stock():
    r = await get("https://query1.finance.yahoo.com/v8/finance/chart/PLTR?range=1y&interval=1d")
    if not r:
        async with _LOCK:
            STATE["meta"]["errors"]["stock"] = "Yahoo chart unreachable"
        return
    try:
        res = r.json()["chart"]["result"][0]
        meta = res["meta"]
        closes_raw = res["indicators"]["quote"][0]["close"]
        vols_raw = res["indicators"]["quote"][0]["volume"]
        ts = res["timestamp"]
        closes = [c for c in closes_raw if c is not None]
        price = meta.get("regularMarketPrice") or closes[-1]
        # previous daily close = the prior daily bar (NOT chartPreviousClose, which for a 1y range is a year ago)
        prev = closes[-2] if len(closes) >= 2 else closes[-1]
        # last 20 sessions for the mini chart
        last20 = [round(c, 2) for c in closes[-20:]]
        # today's range / volume from meta
        day_low = meta.get("regularMarketDayLow")
        day_high = meta.get("regularMarketDayHigh")
        vol_today = meta.get("regularMarketVolume")
        avg_vol = None
        vols = [v for v in vols_raw if v]
        if vols:
            avg_vol = round(sum(vols[-20:]) / min(20, len(vols)))
        stock = {
            "price": round(price, 2),
            "prevClose": round(prev, 2),
            "change": round(price - prev, 2),
            "changePct": round((price - prev) / prev * 100, 2) if prev else 0,
            "dayLow": round(day_low, 2) if day_low else None,
            "dayHigh": round(day_high, 2) if day_high else None,
            "wkLow": round(meta.get("fiftyTwoWeekLow"), 2) if meta.get("fiftyTwoWeekLow") else None,
            "wkHigh": round(meta.get("fiftyTwoWeekHigh"), 2) if meta.get("fiftyTwoWeekHigh") else None,
            "volume": vol_today,
            "avgVolume": avg_vol,
            "dma50": _sma(closes, 50),
            "dma200": _sma(closes, 200),
            "rsi": _rsi(closes, 14),
            "closes20": last20,
            "currency": meta.get("currency", "USD"),
            "exchange": meta.get("fullExchangeName", "NASDAQ"),
            "quoteTimeMs": (meta.get("regularMarketTime") or 0) * 1000 or None,
            "prevCloseThu": round(prev, 2),
            "lastClose": round(price, 2),  # on a weekend this is the Friday close
        }
        fund = await yahoo_fundamentals()
        if fund:
            stock.update(fund)
        ext = await fetch_extended()
        if ext:
            stock.update(ext)
        async with _LOCK:
            STATE["stock"].update(stock)
            STATE["meta"]["stockAsOf"] = now_iso()
            STATE["meta"]["errors"].pop("stock", None)
    except Exception as e:
        async with _LOCK:
            STATE["meta"]["errors"]["stock"] = f"parse: {e}"


async def yahoo_fundamentals() -> Optional[dict]:
    """Best-effort PE / market cap / beta / short interest via quoteSummary (needs a crumb)."""
    try:
        c = await _client.get("https://fc.yahoo.com", headers=UA, timeout=8)
        crumb_r = await _client.get(
            "https://query1.finance.yahoo.com/v1/test/getcrumb",
            headers=UA, cookies=c.cookies, timeout=8)
        crumb = crumb_r.text.strip()
        if not crumb or "<" in crumb:
            return None
        mods = "summaryDetail,defaultKeyStatistics,financialData,price,calendarEvents"
        u = (f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/PLTR"
             f"?modules={mods}&crumb={crumb}")
        r = await _client.get(u, headers=UA, cookies=c.cookies, timeout=10)
        d = r.json()["quoteSummary"]["result"][0]
        sd = d.get("summaryDetail", {}); ks = d.get("defaultKeyStatistics", {})
        fd = d.get("financialData", {}); pr = d.get("price", {})
        cal = d.get("calendarEvents", {})
        def raw(o, k): return (o.get(k) or {}).get("raw")
        cap = raw(pr, "marketCap") or raw(sd, "marketCap")
        earn = None
        try:
            ed = cal.get("earnings", {}).get("earningsDate", [])
            if ed: earn = ed[0].get("fmt")
        except Exception:
            pass
        return {
            "pe": round(raw(sd, "trailingPE"), 1) if raw(sd, "trailingPE") else None,
            "fwdPe": round(raw(sd, "forwardPE"), 1) if raw(sd, "forwardPE") else None,
            "marketCap": cap,
            "beta": raw(ks, "beta") or raw(sd, "beta"),
            "shortPctFloat": round(raw(ks, "shortPercentOfFloat") * 100, 2) if raw(ks, "shortPercentOfFloat") else None,
            "sharesShort": raw(ks, "sharesShort"),
            "shortRatio": raw(ks, "shortRatio"),
            "floatShares": raw(ks, "floatShares"),
            "sharesOut": raw(ks, "sharesOutstanding") or raw(pr, "sharesOutstanding"),
            "targetMean": raw(fd, "targetMeanPrice"),
            "targetHigh": raw(fd, "targetHighPrice"),
            "targetLow": raw(fd, "targetLowPrice"),
            "targetMedian": raw(fd, "targetMedianPrice"),
            "recommendation": (fd.get("recommendationKey") or "").replace("_", " ").title() or None,
            "numAnalysts": raw(fd, "numberOfAnalystOpinions"),
            "nextEarnings": earn,
        }
    except Exception:
        return None


# ============================================================ NEWS + X + AI AGENT
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

def _pub_ms(pub: str):
    try:
        return int(parsedate_to_datetime(pub).timestamp() * 1000)
    except Exception:
        return None

async def fetch_news_rss() -> list[dict]:
    url = ("https://news.google.com/rss/search?q=Palantir%20OR%20PLTR%20stock%20when:7d"
           "&hl=en-US&gl=US&ceid=US:en")
    r = await get(url)
    items = []
    if not r:
        return items
    try:
        root = ET.fromstring(r.text)
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            pub = (it.findtext("pubDate") or "").strip()
            src_el = it.find("{*}source")
            src = (src_el.text if src_el is not None else "") or "News"
            items.append({"headline": title, "url": link, "pub": pub, "ts": _pub_ms(pub),
                          "src": src, "kind": "news"})
            if len(items) >= 12:
                break
    except Exception:
        pass
    return items


async def fetch_x() -> list[dict]:
    if not CFG["X_BEARER_TOKEN"]:
        return []
    url = ('https://api.twitter.com/2/tweets/search/recent'
           '?query=(PLTR OR Palantir OR $PLTR) lang:en -is:retweet&max_results=25'
           '&tweet.fields=public_metrics,created_at&expansions=author_id&user.fields=username')
    try:
        r = await _client.get(url, headers={"Authorization": f"Bearer {CFG['X_BEARER_TOKEN']}", **UA}, timeout=12)
        j = r.json()
        users = {u["id"]: u.get("username", "") for u in j.get("includes", {}).get("users", [])}
        out = []
        for t in j.get("data", []):
            created = t.get("created_at")
            ts = None
            if created:
                try:
                    ts = int(dt.datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() * 1000)
                except Exception:
                    ts = None
            uname = users.get(t.get("author_id"), "")
            out.append({"text": t["text"], "created": created, "ts": ts, "user": uname,
                        "url": f"https://x.com/{uname}/status/{t['id']}" if uname else "",
                        "likes": (t.get("public_metrics") or {}).get("like_count", 0)})
        return out
    except Exception:
        return []


POS = ("beat","surge","soar","rally","record","upgrade","raise","bull","buy","gain","jump",
       "contract","win","award","partnership","expand","growth","outperform","strong","高")
NEG = ("miss","fall","drop","slide","plunge","downgrade","cut","bear","sell","loss","lawsuit",
       "probe","warn","weak","overvalued","short","selloff","concern","decline","risk")


def keyword_dir(text: str) -> str:
    t = text.lower()
    p = sum(t.count(w) for w in POS); n = sum(t.count(w) for w in NEG)
    return "up" if p > n else "down" if n > p else "flat"


async def run_backtest_job():
    days = int(os.getenv("BT_DAYS", "200"))
    res = await bt.run_backtest(days=days)
    async with _LOCK:
        STATE["backtest"] = res


async def run_news():
    """FAST loop: pull headlines + tweets and keyword-score them so the feed is near-live."""
    news = await fetch_news_rss()
    tweets = await fetch_x()
    for it in news:
        it["dir"] = keyword_dir(it["headline"]); it.setdefault("reason", "")
    scored_tw = [{"kind": "tweet", "src": ("@" + t["user"]) if t.get("user") else "X",
                  "headline": t["text"], "url": t.get("url", ""), "ts": t.get("ts"),
                  "dir": keyword_dir(t["text"]), "likes": t.get("likes", 0), "reason": ""} for t in tweets]
    cu = sum(1 for x in news if x["dir"] == "up")
    cd = sum(1 for x in news if x["dir"] == "down")
    cf = sum(1 for x in news if x["dir"] == "flat")
    async with _LOCK:
        if news:
            STATE["news"] = news
        STATE["tweets"] = scored_tw
        ag = STATE["agent"]
        ag["xCount"] = len(tweets)
        if not ag.get("aiThesis"):
            ag["counts"] = {"up": cu, "down": cd, "flat": cf}
            ag["net"] = cu - cd
            ag["engine"] = "keyword (live)"
            ag["thesis"] = _fallback_thesis(STATE["stock"], STATE["crypto"], cu, cd)
        if news:
            STATE["meta"]["newsAsOf"] = now_iso()
    recompute_signal()


async def run_agent():
    """SLOW loop: AI re-scores the current headlines and writes the desk read."""
    if not CFG["ANTHROPIC_API_KEY"]:
        return
    news = [dict(n) for n in STATE["news"] if n.get("kind", "news") == "news"]
    if not news:
        return
    tw = [{"text": t["headline"]} for t in STATE["tweets"]]
    stock = dict(STATE["stock"]); crypto = dict(STATE["crypto"])
    scored, thesis, ai_net = await ai_score(news, tw, stock, crypto)
    if not scored:
        return
    by = {s["headline"]: s for s in scored}
    async with _LOCK:
        for n in STATE["news"]:
            m = by.get(n.get("headline"))
            if m:
                n["dir"] = m.get("dir", n.get("dir", "flat")); n["reason"] = m.get("reason", "")
        cu = sum(1 for x in STATE["news"] if x.get("dir") == "up")
        cd = sum(1 for x in STATE["news"] if x.get("dir") == "down")
        cf = sum(1 for x in STATE["news"] if x.get("dir") == "flat")
        ag = STATE["agent"]
        ag["thesis"] = thesis or ag.get("thesis", ""); ag["aiThesis"] = True
        ag["engine"] = "anthropic:" + CFG["AI_MODEL"]
        ag["counts"] = {"up": cu, "down": cd, "flat": cf}
        ag["net"] = ai_net if ai_net is not None else (cu - cd)
        STATE["meta"]["agentAsOf"] = now_iso()
    recompute_signal()


def _fallback_thesis(stock, crypto, cu, cd):
    bias = "bullish" if cu > cd else "bearish" if cd > cu else "mixed"
    return (f"Headline flow is net {bias} ({cu} bullish / {cd} bearish). "
            "Add an ANTHROPIC_API_KEY to enable full AI analysis and a written read.")


async def ai_score(news, tweets, stock, crypto):
    """Ask Claude to tag each headline and write a short thesis. Returns (scored, thesis, net)."""
    headlines = "\n".join(f"{i}. {n['headline']}" for i, n in enumerate(news))
    tw = ""
    if tweets:
        tw = "\n\nRecent X/Twitter posts:\n" + "\n".join("- " + t["text"][:180] for t in tweets[:10])
    ctx = (f"PLTR last ${stock.get('price')}, {stock.get('changePct')}% today; "
           f"RSI {stock.get('rsi')}; vs 50DMA {stock.get('dma50')}, 200DMA {stock.get('dma200')}. "
           f"Tokenized PLTRX order-book imbalance {round(crypto.get('imbalance',0))}%, "
           f"tape {crypto.get('buyPct')}% buys.")
    prompt = (
        "You are a markets analyst for a Palantir (PLTR) trading desk. "
        "Classify each headline's implication for the PLTR share price as bullish, bearish, or neutral, "
        "then write a 2-3 sentence desk read blending the headlines with the market context.\n\n"
        f"Market context: {ctx}\n\nHeadlines:\n{headlines}{tw}\n\n"
        'Respond with ONLY valid JSON: {"items":[{"i":0,"dir":"up|down|flat","reason":"<=8 words"}],'
        '"thesis":"...","net":<integer -5..5, positive=bullish>}'
    )
    try:
        body = {"model": CFG["AI_MODEL"], "max_tokens": 900,
                "messages": [{"role": "user", "content": prompt}]}
        r = await _client.post("https://api.anthropic.com/v1/messages", json=body, timeout=40,
                               headers={"x-api-key": CFG["ANTHROPIC_API_KEY"],
                                        "anthropic-version": "2023-06-01",
                                        "content-type": "application/json"})
        txt = r.json()["content"][0]["text"]
        txt = txt[txt.find("{"): txt.rfind("}") + 1]
        parsed = json.loads(txt)
        by_i = {d["i"]: d for d in parsed.get("items", [])}
        scored = []
        for i, n in enumerate(news):
            d = by_i.get(i, {})
            it = dict(n); it["dir"] = d.get("dir", "flat"); it["reason"] = d.get("reason", "")
            scored.append(it)
        return scored, parsed.get("thesis", ""), parsed.get("net")
    except Exception:
        return [], "", None


# ============================================================ SIGNAL ENGINE
def recompute_signal():
    s = STATE["stock"]; c = STATE["crypto"]; a = STATE["agent"]
    factors = []
    score = 0.0

    # 1) long-term trend  — real NASDAQ stock is the anchor (tokenized follows it),
    #    so this factor carries the heaviest weight.
    if s.get("price") and s.get("dma200"):
        d = (s["price"] - s["dma200"]) / s["dma200"] * 100
        v = 2 if d > 8 else 1 if d > 0 else -1 if d > -8 else -2
        score += v * 1.8
        factors.append(["Trend (real stock)", "u" if v > 0 else "d" if v < 0 else "f",
                        f"{'+' if d>=0 else ''}{d:.0f}% vs 200-DMA"])

    # 2) short-term momentum (RSI + today)
    rsi = s.get("rsi"); ch = s.get("changePct")
    if rsi is not None:
        v = -1 if rsi > 70 else 1 if rsi < 30 else 0
        if ch is not None:
            v += 0.5 if ch > 1 else -0.5 if ch < -1 else 0
        score += v
        lab = "Overbought" if rsi > 70 else "Oversold" if rsi < 30 else "Neutral"
        factors.append(["Momentum", "u" if v > 0 else "d" if v < 0 else "f",
                        f"RSI {rsi} · {lab}"])

    # 3) order-book + tape (tokenized flow)
    if c:
        imb = c.get("imbalance", 0); bp = c.get("buyPct", 50)
        v = 0
        v += 1 if imb > 12 else -1 if imb < -12 else 0
        v += 0.5 if bp > 58 else -0.5 if bp < 42 else 0
        score += v
        factors.append(["Order flow (PLTRX)", "u" if v > 0 else "d" if v < 0 else "f",
                        f"book {imb:+.0f}% · tape {bp}% buy"])

    # 3b) basis — tokenized premium/discount vs the real (Robinhood/TradingView) stock price.
    #     Only trusted when the tokenized mark is "large enough" — a sane fraction of the
    #     stock price — so a dust/misparsed quote can't fake a basis. Tokenized tracks the
    #     stock, so a rich/cheap basis tends to converge back toward it.
    basis = None
    tok = c.get("mark") or c.get("last")
    sp = s.get("price")
    if sp and tok and (0.5 * sp) <= tok <= (1.6 * sp):
        basis = (tok - sp) / sp * 100
        c["premiumPct"] = round(basis, 2)
        c["stockRef"] = sp
        v = -0.5 if basis > 0.6 else 0.5 if basis < -0.6 else 0
        score += v
        lab = "premium" if basis > 0 else "discount"
        factors.append(["Basis (tokenized vs stock)", "d" if v < 0 else "u" if v > 0 else "f",
                        f"{basis:+.2f}% {lab}"])
    else:
        c["premiumPct"] = None

    # 4) valuation
    if s.get("pe"):
        v = -1 if s["pe"] > 120 else -0.5 if s["pe"] > 60 else 0
        score += v
        factors.append(["Valuation", "d" if v < 0 else "f", f"P/E {s['pe']:.0f}"])

    # 5) news sentiment
    if a:
        net = a.get("net", 0) or 0
        v = 1 if net >= 2 else -1 if net <= -2 else 0
        score += v
        factors.append(["News sentiment", "u" if v > 0 else "d" if v < 0 else "f",
                        f"net {net:+d}"])

    # map score -> call
    if score >= 3.5:
        word, cls, bias = "STRONG BUY", "buy", "Momentum, trend and flow aligned to the upside"
    elif score <= -3.5:
        word, cls, bias = "STRONG SELL", "sell", "Trend and flow rolling over — defensive"
    else:
        word, cls, bias = "SIDEWAYS", "side", "Mixed signals — range-trade, accumulate on dips"
    conf = max(20, min(90, round(50 + score * 8)))
    STATE["signal"] = {"word": word, "cls": cls, "bias": bias, "score": round(score, 1),
                       "conf": conf, "confLabel": "High" if conf >= 70 else "Moderate" if conf >= 45 else "Low",
                       "factors": factors}
    # projection band from recent volatility
    if s.get("closes20"):
        cl = s["closes20"]; lo, hi = min(cl), max(cl); px = s.get("price", cl[-1])
        rng = (hi - lo) or px * 0.05
        STATE["signal"]["proj"] = {
            "cur": round(px, 2), "low": round(px - rng * 0.5, 2), "high": round(px + rng * 0.5, 2),
            "bear": round(lo, 2), "base": round(px, 2), "bull": round(hi, 2)}
    # per-timeframe short-term signals (1/3/5/10/15M), aware of the larger trend
    STATE["signal"]["timeframes"] = compute_timeframes(s)
    if basis is not None:
        STATE["signal"]["basis"] = round(basis, 2)


# ============================================================ LOOPS
async def loop(fn, interval, name):
    while True:
        try:
            await fn()
        except Exception as e:
            async with _LOCK:
                STATE["meta"]["errors"][name] = str(e)
        await asyncio.sleep(interval)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    global _client
    _client = httpx.AsyncClient(follow_redirects=True)
    STATE["meta"]["started"] = now_iso()
    _ROT["live"] = await ex.probe_all(_client)          # which of the ~10 exchanges list PLTR
    STATE["meta"]["exchanges"] = _ROT["live"]
    STATE["meta"]["depthVenue"] = await ex.resolve_depth(_client, _ROT["live"])  # order book / tape source
    await seed_live()                                    # prefill 1–15M timeframe buffer
    # load a cached backtest if present, so the tab is populated instantly
    try:
        p = os.path.join(HERE, "static", "backtest.json")
        if os.path.exists(p):
            STATE["backtest"] = json.load(open(p))
    except Exception:
        pass
    # prime once, then start loops
    await poll_stock()
    await poll_depth()
    await rotate_price()
    await run_news()
    await run_agent()
    bt_interval = float(os.getenv("BT_INTERVAL", "86400"))
    tasks = [
        asyncio.create_task(loop(rotate_price, CFG["FAST_INTERVAL"], "price")),
        asyncio.create_task(loop(poll_depth, CFG["DEPTH_INTERVAL"], "depth")),
        asyncio.create_task(loop(poll_stock, CFG["STOCK_INTERVAL"], "stock")),
        asyncio.create_task(loop(run_news, CFG["NEWS_INTERVAL"], "news")),
        asyncio.create_task(loop(run_agent, CFG["AGENT_INTERVAL"], "agent")),
        asyncio.create_task(loop(run_backtest_job, bt_interval, "backtest")),
    ]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await _client.aclose()


app = FastAPI(title="PLTR Signal Desk", lifespan=lifespan)


@app.get("/api/state")
async def api_state():
    async with _LOCK:
        return JSONResponse(json.loads(json.dumps(STATE, default=str)))


@app.get("/api/backtest")
async def api_backtest():
    async with _LOCK:
        return JSONResponse(json.loads(json.dumps(STATE["backtest"], default=str)))


@app.post("/api/backtest/run")
async def api_backtest_run():
    asyncio.create_task(run_backtest_job())
    return {"started": True}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "cryptoAsOf": STATE["meta"]["cryptoAsOf"],
            "stockAsOf": STATE["meta"]["stockAsOf"]}


@app.get("/")
async def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
