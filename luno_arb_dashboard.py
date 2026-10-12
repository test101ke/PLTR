#!/usr/bin/env python3
"""Luno arbitrage dashboard. Read-only, public API, no login, no trading.

Run:   python3 luno_arb_dashboard.py
Open:  http://localhost:8765

Each refresh it:
  1. Loads every Luno market (base, counter, status, min order size).
  2. Loads every ticker (best bid / best ask).
  3. Checks every 3-leg loop (A -> B -> C -> A) at best prices, minus your fee per leg.
  4. For the top loops, loads the top of each order book to see how much size is
     actually available at those prices and the max you could push through.
"""
import itertools, json, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

API = "https://api.luno.com"
PORT = 8765
DEPTH_CHECK = 15  # loops to depth-check per refresh (each costs 3 API calls)


def get(path):
    req = urllib.request.Request(API + path, headers={"User-Agent": "luno-arb-dashboard"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def build(fee):
    markets = {m["market_id"]: m for m in get("/api/exchange/1/markets")["markets"]}
    tickers = get("/api/1/tickers")["tickers"]
    now_ms = time.time() * 1000
    pairs, edges = [], {}
    for t in tickers:
        m = markets.get(t["pair"])
        if not m:
            continue
        bid, ask = float(t.get("bid") or 0), float(t.get("ask") or 0)
        active = t.get("status") == "ACTIVE" and m.get("trading_status") == "ACTIVE"
        spread = (ask - bid) / ask * 100 if ask > 0 and bid > 0 else None
        pairs.append({
            "pair": t["pair"], "base": m["base_currency"], "counter": m["counter_currency"],
            "bid": bid, "ask": ask, "spread": spread, "active": active,
            "vol24": float(t.get("rolling_24_hour_volume") or 0),
            "age_s": round((now_ms - int(t.get("timestamp") or now_ms)) / 1000),
            "min_volume": m.get("min_volume"),
        })
        if not active or bid <= 0 or ask <= 0:
            continue
        b, c = m["base_currency"], m["counter_currency"]
        edges[(b, c)] = {"pair": t["pair"], "side": "SELL", "price": bid, "rate": bid * (1 - fee), "min_volume": m.get("min_volume")}
        edges[(c, b)] = {"pair": t["pair"], "side": "BUY", "price": ask, "rate": (1 / ask) * (1 - fee), "min_volume": m.get("min_volume")}

    ccys = sorted({a for a, _ in edges})
    seen, loops = set(), []
    for a, b, c in itertools.permutations(ccys, 3):
        if (a, b) in edges and (b, c) in edges and (c, a) in edges:
            # same cycle starting elsewhere is the same trade; keep one rotation
            key = frozenset([(a, b), (b, c), (c, a)])
            if key in seen:
                continue
            seen.add(key)
            legs = [dict(edges[(a, b)], frm=a, to=b), dict(edges[(b, c)], frm=b, to=c), dict(edges[(c, a)], frm=c, to=a)]
            net = (legs[0]["rate"] * legs[1]["rate"] * legs[2]["rate"] - 1) * 100
            loops.append({"path": [a, b, c, a], "net_pct": net, "legs": legs})
    loops.sort(key=lambda x: x["net_pct"], reverse=True)

    for lp in loops[:DEPTH_CHECK]:
        depth_check(lp)
    return {"time": time.strftime("%H:%M:%S"), "fee": fee, "pairs": sorted(pairs, key=lambda p: p["pair"]),
            "currencies": ccys, "loops": loops[:60], "loop_count": len(loops)}


def depth_check(lp):
    """Max start-currency amount that fills every leg at its top-of-book price."""
    try:
        max_start, mult = float("inf"), 1.0  # mult: units of this leg's input per 1 unit of start
        for leg in lp["legs"]:
            ob = get(f"/api/1/orderbook_top?pair={leg['pair']}")
            if leg["side"] == "SELL":
                top = ob["bids"][0]
                cap_in = float(top["volume"])                       # base we can sell
            else:
                top = ob["asks"][0]
                cap_in = float(top["volume"]) * float(top["price"])  # counter we can spend
            leg["book_price"] = float(top["price"])
            leg["book_volume"] = float(top["volume"])
            leg["price_moved"] = abs(float(top["price"]) - leg["price"]) > 1e-12
            max_start = min(max_start, cap_in / mult)
            mult *= leg["rate"]
        lp["max_start"] = max_start
        lp["depth_ok"] = True
    except Exception as e:  # keep the dashboard alive if one book fails
        lp["depth_ok"] = False
        lp["depth_error"] = str(e)


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Luno Arb Scanner</title>
<style>
:root{--bg:#f6f7f5;--panel:#fff;--fg:#17201b;--mute:#5d6b63;--line:#dfe4e0;--accent:#0b6e4f;--good:#13804a;--bad:#b3261e;--warn:#9a6a00;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#101512;--panel:#18201b;--fg:#e6ece8;--mute:#93a29a;--line:#2a352f;--accent:#45c495;--good:#4cc77f;--bad:#ef6b62;--warn:#e0b44a;color-scheme:dark}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
.wrap{max-width:1200px;margin:0 auto;padding-inline:16px;padding-block:20px 40px;display:grid;gap:18px}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:end;justify-content:space-between}
h1{margin:0;font-size:22px}h2{margin:0 0 10px;font-size:15px;text-transform:uppercase;letter-spacing:.06em;color:var(--mute)}
.controls{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
label{color:var(--mute);font-size:13px}input,select,button{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--fg)}
button{cursor:pointer;background:var(--accent);color:#fff;border-color:var(--accent)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px}.tile b{display:block;font-size:22px;font-variant-numeric:tabular-nums}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px;min-width:0}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--mute);font-weight:600;font-size:12px}
.pos{color:var(--good);font-weight:700}.neg{color:var(--bad)}.warn{color:var(--warn)}.mute{color:var(--mute)}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;border:1px solid currentColor}
.legs{font-size:12px;color:var(--mute)}.err{color:var(--bad);font-weight:600}
</style></head><body><div class="wrap">
<header><div><h1>Luno Arb Scanner</h1><div class="mute" id="status">Loading…</div></div>
<div class="controls">
<label for="fee">Fee per trade %</label><input id="fee" type="number" step="0.01" value="0.10" style="width:80px">
<label for="every">Refresh</label><select id="every"><option value="0">Manual</option><option value="10" selected>10s</option><option value="30">30s</option><option value="60">60s</option></select>
<label><input id="onlyPos" type="checkbox"> Profitable only</label>
<button id="go">Scan now</button></div></header>
<div class="tiles"><div class="tile"><span class="mute">Pairs</span><b id="tPairs">–</b></div><div class="tile"><span class="mute">Loops checked</span><b id="tLoops">–</b></div>
<div class="tile"><span class="mute">Profitable after fees</span><b id="tPos">–</b></div><div class="tile"><span class="mute">Best loop</span><b id="tBest">–</b></div></div>
<section class="panel"><h2>Triangular loops (best first)</h2><div class="scroll"><table id="loops"></table></div>
<p class="mute" style="font-size:12px">Net % = result of 1 unit round trip at best bid/ask, minus fee on each of the 3 trades. "Max size" = largest starting amount that fills all 3 legs at top-of-book price (top 15 loops only). A price flagged "moved" changed between ticker and order book fetch.</p></section>
<section class="panel"><h2>All pairs</h2><div class="scroll"><table id="pairs"></table></div></section>
</div><script>
const $=id=>document.getElementById(id);let timer;
const f=(n,d=8)=>n==null?'–':(+n).toLocaleString(undefined,{maximumFractionDigits:d});
async function scan(){
 $('status').textContent='Scanning…';
 try{
  const r=await fetch('/data?fee='+(parseFloat($('fee').value)||0)/100);const d=await r.json();
  if(d.error)throw new Error(d.error);
  const pos=d.loops.filter(l=>l.net_pct>0);
  $('tPairs').textContent=d.pairs.length;$('tLoops').textContent=d.loop_count;$('tPos').textContent=pos.length;
  $('tBest').textContent=d.loops.length?(d.loops[0].net_pct>0?'+':'')+d.loops[0].net_pct.toFixed(3)+'%':'none';
  $('tBest').className=d.loops.length&&d.loops[0].net_pct>0?'pos':'neg';
  const shown=$('onlyPos').checked?pos:d.loops;
  $('loops').innerHTML='<tr><th>Net %</th><th>Route</th><th>Trades to place</th><th>Max size (start ccy)</th><th>Book check</th></tr>'+
   (shown.length?shown.map(l=>{
    const legs=l.legs.map((g,i)=>`${i+1}. ${g.side} ${g.pair} @ ${f(g.price)}${g.min_volume?` <span class="mute">(min ${g.min_volume})</span>`:''}${g.price_moved?' <span class="warn">moved</span>':''}`).join('<br>');
    const book=l.depth_ok===undefined?'<span class="mute">not checked</span>':l.depth_ok?'<span class="pill pos">ok</span>':`<span class="err">${l.depth_error}</span>`;
    return `<tr><td class="${l.net_pct>0?'pos':'neg'}">${l.net_pct>0?'+':''}${l.net_pct.toFixed(3)}%</td><td>${l.path.join(' → ')}</td><td class="legs">${legs}</td><td>${l.max_start!=null?f(l.max_start)+' '+l.path[0]:'–'}</td><td>${book}</td></tr>`}).join('')
   :'<tr><td colspan="5" class="mute">No loops match.</td></tr>');
  $('pairs').innerHTML='<tr><th>Pair</th><th>Bid</th><th>Ask</th><th>Spread %</th><th>24h vol</th><th>Min order</th><th>Status</th><th>Ticker age</th></tr>'+
   d.pairs.map(p=>`<tr><td>${p.pair}</td><td>${f(p.bid)}</td><td>${f(p.ask)}</td><td>${p.spread==null?'–':p.spread.toFixed(3)}</td><td>${f(p.vol24,4)}</td><td>${p.min_volume??'–'}</td><td>${p.active?'<span class="pill pos">active</span>':'<span class="pill neg">inactive</span>'}</td><td class="${p.age_s>60?'warn':''}">${p.age_s}s</td></tr>`).join('');
  $('status').textContent=`Updated ${d.time} · fee ${(d.fee*100).toFixed(2)}% per trade · currencies: ${d.currencies.join(', ')}`;
 }catch(e){$('status').innerHTML='<span class="err">Scan failed: '+e.message+'</span>'}
}
function sched(){clearInterval(timer);const s=+$('every').value;if(s)timer=setInterval(scan,s*1000)}
$('go').onclick=scan;$('every').onchange=sched;$('onlyPos').onchange=scan;scan();sched();
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/data":
            try:
                fee = float(parse_qs(u.query).get("fee", ["0.001"])[0])
                body = json.dumps(build(fee)).encode()
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode()
            self._send(body, "application/json")
        else:
            self._send(PAGE.encode(), "text/html; charset=utf-8")

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"Luno arb dashboard: http://localhost:{PORT}  (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
