"""
Fast multi-source news aggregator.
==================================
There is no open-source equivalent of a Bloomberg/Reuters/Benzinga-Pro wire — those
are paid products. What genuinely helps is polling SEVERAL free real-time sources in
parallel and de-duplicating, so a headline shows the moment ANY of them prints it.

Sources (all free, no API key):
  * SEC EDGAR filing feed  — 8-K/10-Q/press exhibits often hit here BEFORE the wires
  * Yahoo Finance RSS      — fast syndication of the major wires
  * Nasdaq RSS             — exchange-side company news
  * Seeking Alpha RSS      — analyst/market commentary
  * Google News RSS        — broad catch-all
  * StockTwits API         — retail/social first-mention, frequently earliest of all
"""
import asyncio, re, html, datetime as dt
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

UA = {"User-Agent": "PLTR-Signal-Desk/1.0 (contact: dashboard@example.com)"}
TICKER = "PLTR"

FEEDS = [
    ("SEC EDGAR", f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={TICKER}&type=&dateb=&owner=include&count=15&output=atom"),
    ("Yahoo Finance", f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={TICKER}&region=US&lang=en-US"),
    ("Nasdaq", f"https://www.nasdaq.com/feed/rssoutbound?symbol={TICKER}"),
    ("Seeking Alpha", f"https://seekingalpha.com/api/sa/combined/{TICKER}.xml"),
    # Google News is NOT here on purpose: it rate-limits and ranks by relevance,
    # so it runs on its own 5-minute loop in google_news.py.
]


def _ms(s):
    if not s:
        return None
    try:
        return int(parsedate_to_datetime(s).timestamp() * 1000)
    except Exception:
        pass
    try:
        return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None


def _norm(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()[:90]


async def _get(client, url, timeout=8):
    try:
        r = await client.get(url, headers=UA, timeout=timeout)
        r.raise_for_status()
        return r
    except Exception:
        return None


def _parse_xml(text, src):
    """Handle both RSS <item> and Atom <entry>."""
    out = []
    try:
        root = ET.fromstring(text)
    except Exception:
        return out
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        def g(*names):
            for n in names:
                for ch in it:
                    if ch.tag.split("}")[-1] == n:
                        return (ch.text or "").strip() or (ch.attrib.get("href") or "")
            return ""
        title = html.unescape(g("title"))
        if not title:
            continue
        link = g("link")
        if not link:
            for ch in it:
                if ch.tag.split("}")[-1] == "link" and ch.attrib.get("href"):
                    link = ch.attrib["href"]; break
        pub = g("pubDate", "published", "updated", "date")
        out.append({"headline": title, "url": link, "src": src,
                    "ts": _ms(pub), "pub": pub, "kind": "news"})
    return out


async def _stocktwits(client):
    r = await _get(client, f"https://api.stocktwits.com/api/2/streams/symbol/{TICKER}.json")
    if not r:
        return []
    out = []
    try:
        for m in r.json().get("messages", [])[:15]:
            out.append({"headline": html.unescape(m.get("body", "") or "")[:220],
                        "url": f"https://stocktwits.com/message/{m.get('id')}",
                        "src": "StockTwits @" + (m.get("user", {}).get("username") or ""),
                        "ts": _ms(m.get("created_at")), "pub": m.get("created_at"),
                        "kind": "social"})
    except Exception:
        return []
    return out


async def fetch_all(client, limit=25):
    """Poll every source in parallel, merge, de-duplicate, newest first."""
    tasks = [_get(client, u) for _, u in FEEDS] + [_stocktwits(client)]
    res = await asyncio.gather(*tasks, return_exceptions=True)
    items = []
    for i, r in enumerate(res[:len(FEEDS)]):
        if isinstance(r, Exception) or r is None:
            continue
        items += _parse_xml(r.text, FEEDS[i][0])
    st = res[-1]
    if isinstance(st, list):
        items += st
    seen, merged = set(), []
    for it in sorted(items, key=lambda x: x.get("ts") or 0, reverse=True):
        k = _norm(it["headline"])
        if not k or k in seen:
            continue
        seen.add(k); merged.append(it)
        if len(merged) >= limit:
            break
    return merged
