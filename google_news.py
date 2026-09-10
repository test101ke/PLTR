"""
Google News scraper — the freshest-first pass over Google's index.
==================================================================

Why this is its own module and its own slow loop:

  * Google News RSS ranks by RELEVANCE, not date. A broad `when:7d` query keeps
    handing back the same week-old "best" articles, which is exactly why the feed
    looked frozen. The fix is several NARROW recency windows (12h / 1d / 2d) run
    as separate queries and then sorted by publication time ourselves.
  * Google rate-limits. Polling it every few seconds gets the desk throttled and
    makes the feed *worse*. Once every 5 minutes is the sweet spot, so this runs
    on its own cadence while the tolerant feeds (Yahoo, Nasdaq, EDGAR,
    StockTwits) keep polling every few seconds.

Titles come back as "Headline - Publisher"; we split the publisher off and use it
as the source label, so the feed shows Reuters/Bloomberg/Barron's rather than a
wall of "Google News".
"""
import asyncio, re, datetime as dt
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
      "Accept-Language": "en-US,en;q=0.9"}

BASE = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"

# Narrow windows first — those are the ones that surface genuinely new stories.
QUERIES = [
    'PLTR when:12h',
    'Palantir when:12h',
    'Palantir stock when:1d',
    'PLTR stock when:1d',
    '"Palantir Technologies" when:2d',
    'Palantir (contract OR earnings OR guidance OR AIP OR government) when:2d',
]


def _q(s):
    from urllib.parse import quote
    return quote(s, safe="")


def _ms(v):
    if not v:
        return None
    try:
        return int(parsedate_to_datetime(v).timestamp() * 1000)
    except Exception:
        return None


def _norm(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()[:90]


def _split_publisher(title, src):
    """Google formats titles as 'Headline - Publisher'. Prefer the RSS <source>."""
    if src:
        # strip a trailing ' - Publisher' that duplicates the source element
        tail = " - " + src
        if title.endswith(tail):
            title = title[: -len(tail)]
        return title.strip(), src.strip()
    m = re.match(r"^(.*)\s+-\s+([^-]{2,40})$", title or "")
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return (title or "").strip(), "Google News"


def _parse(text):
    out = []
    try:
        root = ET.fromstring(text)
    except Exception:
        return out
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        if not title:
            continue
        link = (it.findtext("link") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        src_el = None
        for ch in it:
            if ch.tag.split("}")[-1] == "source":
                src_el = ch
                break
        src = (src_el.text or "").strip() if src_el is not None else ""
        headline, publisher = _split_publisher(title, src)
        out.append({"headline": headline, "url": link, "pub": pub, "ts": _ms(pub),
                    "src": publisher, "kind": "news", "via": "google"})
    return out


async def _get(client, url, timeout=12):
    try:
        r = await client.get(url, headers=UA, timeout=timeout, follow_redirects=True)
        if r.status_code != 200:
            return None
        return r.text
    except Exception:
        return None


async def fetch(client, limit=30, max_age_h=None):
    """Run every query in parallel, merge, de-duplicate, newest first."""
    urls = [BASE.format(q=_q(q)) for q in QUERIES]
    pages = await asyncio.gather(*[_get(client, u) for u in urls], return_exceptions=True)
    items = []
    for pg in pages:
        if isinstance(pg, str):
            items += _parse(pg)
    if max_age_h:
        cut = (dt.datetime.now(dt.timezone.utc).timestamp() - max_age_h * 3600) * 1000
        items = [i for i in items if (i.get("ts") or 0) >= cut]
    seen, merged = set(), []
    for it in sorted(items, key=lambda x: x.get("ts") or 0, reverse=True):
        k = _norm(it["headline"])
        if not k or k in seen:
            continue
        seen.add(k)
        merged.append(it)
        if len(merged) >= limit:
            break
    return merged
