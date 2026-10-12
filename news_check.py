"""
Live news check: every source's health, the merged headlines, their direction
and the recency-weighted score the dashboard and AI supervisor use.
    python news_check.py
"""
import asyncio, datetime as dt, httpx
import newsfeed, google_news


async def main():
    async with httpx.AsyncClient(follow_redirects=True, timeout=15) as c:
        try:
            goog = await google_news.fetch(c, limit=30)
            newsfeed._mark("Google News", bool(goog), len(goog), "" if goog else "no items")
        except Exception as e:
            goog = []
            newsfeed._mark("Google News", False, err=f"{type(e).__name__}: {e}"[:120])
        fast = await newsfeed.fetch_all(c)
    print("SOURCES")
    for k, v in sorted(newsfeed.HEALTH.items()):
        print(f"  {k:15} {'OK  ' if v.get('ok') else 'FAIL'} {v.get('items', 0):>3} items  {v.get('err', '')}")
    merged = newsfeed.merge(goog + fast)
    now = dt.datetime.now(dt.timezone.utc)
    print(f"\nHEADLINES ({len(merged)} after de-duplication), newest first")
    for m in merged:
        age = (now.timestamp() * 1000 - (m.get("ts") or 0)) / 3600e3 if m.get("ts") else None
        d = m["dir"] = newsfeed.keyword_dir(m["headline"])
        print(f"  {('%5.1fh' % age) if age is not None else '    ?':>6} {d:>5} {m['kind']:6} "
              f"[{', '.join(m['srcs'])[:40]}] {m['headline'][:140]}")
    real = [m for m in merged if m["kind"] == "news"]
    print("\nSCORE (news only, 6h half-life):", newsfeed.score(real))
    print("SCORE (with social):", newsfeed.score(merged))


asyncio.run(main())
