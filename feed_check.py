"""
Proves paper trading works on PUBLIC data with no API keys.
    python feed_check.py [seconds]
1. Tests every public feed (reachable, PLTR perp listed, live order book).
2. Starts a paper agent with no keys, lets it run, prints its engine heartbeat.
Exit code 1 if the agent never comes alive.
"""
import sys, asyncio, json
import trader as tr


async def main(secs):
    print("PUBLIC FEEDS (no API keys)")
    for r in await tr.diagnose():
        print(f"  {r['exchange']:12} {'OK  ' if r.get('ok') else 'FAIL'} {r['ms']:>6} ms  "
              + (f"{r['symbol']} bid {r['bid']} ask {r['ask']}" if r.get("ok") else r.get("error", "")))
    a = tr.TradeAgent(user="feed-check")
    a.keys.creds = None                      # make sure: no keys at all
    try:
        await a.start("paper")
    except Exception as e:
        print("PAPER START FAILED:", e); return 1
    alive = 0
    for i in range(secs):
        await asyncio.sleep(1)
        e, s, b = a.engine(), a.stats, a.book or {}
        alive += e["alive"]
        print(f"  t+{i+1:>2}s {a.cfg['exchange']:12} alive={e['alive']} beat={e['beatAgeMs']}ms "
              f"data={e['dataAgeMs']}ms checks/s={s['checksPerSec']} bid={b.get('bid')} ask={b.get('ask')}")
    print("events:", json.dumps([x["msg"] for x in a.events], indent=1))
    await a.stop("feed check done")
    print(f"RESULT: alive {alive}/{secs} seconds on {a.cfg['exchange']}")
    return 0 if alive >= secs // 2 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 20)))
