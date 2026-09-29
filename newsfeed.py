"""
Fast multi-source news aggregator.
==================================
Polls several free real-time sources in parallel and merges them, so a headline
shows the moment ANY of them prints it.

Sources (no API key):
  * SEC EDGAR filing feed  - 8-Ks often hit here before the wires. SEC blocks
                             requests without a real contact: set SEC_UA to
                             "Your Name your@email".
  * Yahoo Finance RSS, Nasdaq RSS, Seeking Alpha RSS - need a browser user-agent.
  * StockTwits             - SOCIAL, not news: shown in the feed but kept out of
                             the sentiment tally (chatter is not information).

What the merge does:
  * near-duplicate folding: the same story reworded by two outlets is one item,
    and `sources` counts how many outlets carried it (more = more weight);
  * `seen` = when this desk first saw it, so stale re-syndication can be spotted
    and items with no publish time still sort sensibly;
  * per-source health in HEALTH, so a dead feed is visible instead of silent.
"""
import os, asyncio, re, html, time, datetime as dt
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

TICKER = "PLTR"
CIK = "0001321655"
BROWSER = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
           "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8"}
SEC_HEADERS = {"User-Agent": os.getenv("SEC_UA", "PLTR-Signal-Desk admin@localhost"),
               "Accept": "application/atom+xml, application/xml"}

FEEDS = [
    ("SEC EDGAR", f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={CIK}"
                  f"&type=&dateb=&owner=include&count=15&output=atom", SEC_HEADERS),
    ("Yahoo Finance", f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={TICKER}&region=US&lang=en-US", BROWSER),
    ("Nasdaq", f"https://www.nasdaq.com/feed/rssoutbound?symbol={TICKER}", BROWSER),
    ("Seeking Alpha", f"https://seekingalpha.com/api/sa/combined/{TICKER}.xml", BROWSER),
    # Google News runs on its own slower loop (google_news.py): it rate-limits.
]

HEALTH = {}          # source -> {"ok": bool, "items": int, "at": iso, "err": str}
_SEEN = {}           # normalized headline -> first-seen ms (bounded below)


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


_STOP = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "is", "as", "at", "by", "with",
         "its", "it", "s", "from", "after", "stock", "shares", "pltr", "palantir", "technologies",
         "inc", "us", "u", "new", "says", "report", "reports"}


def _tokens(t):
    t = (t or "").lower()
    t = re.sub(r"(\d+(?:\.\d+)?)\s*(?:million|mln|m)\b", r"\1m", t)
    t = re.sub(r"(\d+(?:\.\d+)?)\s*(?:billion|bln|bn|b)\b", r"\1b", t)
    return {w for w in re.sub(r"[^a-z0-9.]+", " ", t).split() if w not in _STOP and len(w) > 1}


def same_story(a, b, thresh=0.5):
    """Content-word overlap (Jaccard, at least 3 shared words). Catches reworded syndication."""
    ta, tb = _tokens(a), _tokens(b)
    common = len(ta & tb)
    return common >= 3 and common / len(ta | tb) >= thresh


def _mark(src, ok, n=0, err=""):
    HEALTH[src] = {"ok": ok, "items": n, "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                   "err": err[:80]}


async def _get(client, url, headers, timeout=8):
    try:
        r = await client.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r, ""
    except Exception as e:
        return None, (getattr(getattr(e, "response", None), "status_code", None) and
                      f"HTTP {e.response.status_code}") or type(e).__name__


def _parse_xml(text, src):
    """Handle both RSS <item> and Atom <entry>."""
    out = []
    try:
        root = ET.fromstring(text)
    except Exception:
        return out
    for it in root.iter():
        if it.tag.split("}")[-1] not in ("item", "entry"):
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
        out.append({"headline": title, "url": link, "src": src, "ts": _ms(pub), "pub": pub, "kind": "news"})
    return out


async def _feed(client, name, url, headers):
    r, err = await _get(client, url, headers)
    if not r:
        _mark(name, False, err=err); return []
    items = _parse_xml(r.text, name)
    _mark(name, bool(items), len(items), "" if items else "no items parsed")
    return items


async def _stocktwits(client):
    r, err = await _get(client, f"https://api.stocktwits.com/api/2/streams/symbol/{TICKER}.json", BROWSER)
    if not r:
        _mark("StockTwits", False, err=err); return []
    out = []
    try:
        for m in r.json().get("messages", [])[:15]:
            out.append({"headline": html.unescape(m.get("body", "") or "")[:220],
                        "url": f"https://stocktwits.com/message/{m.get('id')}",
                        "src": "StockTwits @" + (m.get("user", {}).get("username") or ""),
                        "ts": _ms(m.get("created_at")), "pub": m.get("created_at"), "kind": "social"})
    except Exception:
        _mark("StockTwits", False, err="bad JSON"); return []
    _mark("StockTwits", True, len(out))
    return out


def merge(items, limit=30, now_ms=None):
    """Fold near-duplicates, stamp first-seen, newest first. News and social are
    limited separately so chatter can never push real headlines out."""
    now_ms = now_ms or int(time.time() * 1000)
    merged = []
    for it in sorted(items, key=lambda x: x.get("ts") or 0, reverse=True):
        if not _norm(it.get("headline")):
            continue
        dup = next((m for m in merged if m["kind"] == it.get("kind", "news")
                    and same_story(m["headline"], it["headline"])), None)
        if dup:
            if it["src"] not in dup["srcs"]:
                dup["srcs"].append(it["src"]); dup["sources"] = len(dup["srcs"])
            if it.get("ts") and (not dup.get("ts") or it["ts"] < dup["ts"]):
                dup["ts"] = it["ts"]           # earliest print wins
            continue
        m = dict(it); m.setdefault("kind", "news")
        m["srcs"] = [m["src"]]; m["sources"] = 1
        k = _norm(m["headline"])
        m["seen"] = _SEEN.setdefault(k, now_ms)
        merged.append(m)
    if len(_SEEN) > 2000:                          # keep the first-seen map bounded
        for k in list(_SEEN)[:1000]:
            _SEEN.pop(k, None)
    news = [m for m in merged if m["kind"] == "news"][:limit]
    social = [m for m in merged if m["kind"] != "news"][:10]
    return news + social


async def fetch_all(client):
    """Raw items from every fast source. The caller merges them with Google News once."""
    tasks = [_feed(client, n, u, h) for n, u, h in FEEDS] + [_stocktwits(client)]
    res = await asyncio.gather(*tasks, return_exceptions=True)
    return [x for r in res if isinstance(r, list) for x in r]


# ------------------------------------------------------------------ scoring
# Whole words only. Substring matching used to read "against" as "gain",
# "window" as "win", "execute" as "cut" and "shortly" as "short".
POS = re.compile(r"\b(beats?|surg\w*|soar\w*|rall(?:y|ies|ied)|record|upgrad\w*|rais(?:e|es|ed|ing)"
                 r"|bullish|gains?|gained|jump\w*|contracts?|wins?|won|award\w*|partnerships?|expand\w*"
                 r"|growth|outperform\w*|strong(?:er)?|tops|buyback)\b")
NEG = re.compile(r"\b(miss(?:es|ed)?|fall\w*|fell|drops?|dropped|slid\w*|slump\w*|plung\w*|downgrad\w*"
                 r"|cuts?|bearish|loss(?:es)?|lawsuits?|probe\w*|investigat\w*|warn\w*|weak\w*"
                 r"|overvalued|short sellers?|short report|sell-?off|declin\w*|tumbl\w*|sink\w*|sank)\b")


def keyword_dir(text):
    t = (text or "").lower()
    p, n = len(POS.findall(t)), len(NEG.findall(t))
    return "up" if p > n else "down" if n > p else "flat"


def score(items, now_ms=None, half_life_h=6.0):
    """Recency-weighted news score: +1/-1 per headline, halved every `half_life_h`
    hours of age, boosted (sqrt) when several outlets carried it. Social excluded."""
    now_ms = now_ms or int(time.time() * 1000)
    total = 0.0
    for it in items:
        if it.get("kind", "news") != "news":
            continue
        sign = {"up": 1, "down": -1}.get(it.get("dir"), 0)
        if not sign:
            continue
        age_h = max(0.0, (now_ms - (it.get("ts") or it.get("seen") or now_ms)) / 3.6e6)
        total += sign * 0.5 ** (age_h / half_life_h) * min(it.get("sources", 1), 4) ** 0.5
    return round(total, 3)
