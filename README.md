# PLTR Signal Desk

A real-time Palantir trading dashboard. FastAPI backend + Apple-glass frontend, built to run on **Render**.

It serves one page that live-updates every second with:

- **Tokenized PLTRX** (Bybit primary, Kraken fallback): live order book, buy/sell trade tape, large-order prints, order-book imbalance, 24h volume. This is the crypto-exchange depth data — order book, tape, large orders — for the tokenized Palantir market.
- **Real NASDAQ PLTR** (Yahoo Finance): price, 20-session chart, 52-week range, volume, 50/200-day moving averages, RSI(14), and fundamentals (P/E, market cap, beta, short interest, analyst targets) where available.
- **AMD / Power of 3** (`amd.py`): the session model — Asia accumulates a range, London sweeps one side (manipulation), New York expands (distribution). Detects the sweep, the reclaim, the resulting bias, and derives entry / invalidation / T1 / T2 with R:R. Runs on the tokenized PLTR perp, the only PLTR market that trades through Asia and London. Served at `/api/amd` only (removed from the dashboard: a 181-day replay found no edge).
- **AI agent** (Anthropic): scores each news headline bullish/bearish/neutral, writes a short desk read, and feeds a composite **STRONG BUY / SIDEWAYS / STRONG SELL** signal with conviction and a projection band.
- **Fast news** (`newsfeed.py`): every free real-time source polled **in parallel** and de-duplicated — SEC EDGAR filings, Yahoo Finance, Nasdaq, Seeking Alpha, Google News and StockTwits — so a headline appears the moment any one of them prints it. Default poll is every 8s. (True HFT wires — Bloomberg, Reuters, Dow Jones, Benzinga Pro — are paid commercial products with no open-source equivalent; this is the fastest free stack.) Optional X/Twitter buzz with a bearer token.
- **Insiders & Congress** (`filings.py`): who else is trading PLTR, from public disclosures — **SEC Form 4** (officers, directors, 10%+ holders, filed within 2 business days, parsed straight from PLTR's EDGAR index at CIK 0001321655) and **STOCK Act** periodic transaction reports from US House and Senate members. Served at `/api/filings` only (removed from the dashboard).

> Not financial advice. Tokenized PLTRX is a separate, thinner market that tracks — but can diverge from — the NASDAQ stock, especially outside US hours. Signals are model-derived and can be wrong.

## Deploy to Render (blueprint — easiest)

1. Put this folder in a Git repo and push to GitHub (see below).
2. In Render: **New → Blueprint**, connect the repo. Render reads `render.yaml` and creates the web service.
3. Set the secret env var **`ANTHROPIC_API_KEY`** (enables the AI agent). Optional: `X_BEARER_TOKEN`.
4. Deploy. Open the service URL — the dashboard is at `/`.

### Push to GitHub
```bash
cd pltr-signal-desk
git init && git add . && git commit -m "PLTR Signal Desk"
gh repo create pltr-signal-desk --private --source=. --push   # or create on github.com and:
# git remote add origin git@github.com:<you>/pltr-signal-desk.git && git push -u origin main
```

### Deploy without the blueprint
New → **Web Service** → connect repo →
- Runtime **Python 3**, Build `pip install -r requirements.txt`,
- Start `uvicorn main:app --host 0.0.0.0 --port $PORT`,
- add env vars from `.env.example`.

**Plan note:** Render's **free** plan sleeps on idle (cold starts drop the live loop until the next visit). For an always-on desk, use **Starter**.

## Run locally (access by localhost or LAN IP)

**Easiest — one word:**
```bash
cd ~/Documents/Claude/Projects/pltr-signal-desk
./pltr
```
First run sets up the virtual env and installs everything; later runs just start it. (Same thing: `bash run_local.sh`, or double-click `run_local.command` on a Mac.)
```bat
run_local.bat              REM Windows
```
It installs deps, binds to `0.0.0.0:8000`, and prints both URLs:
- **This machine:** `http://localhost:8000`
- **Phone / other laptop on the same Wi-Fi:** `http://<this-machine-IP>:8000`

Find your IP if needed: macOS `ipconfig getifaddr en0`, Windows `ipconfig`, Linux `hostname -I`. Change the port with `PORT=9000 bash run_local.sh`. For the AI agent locally, copy `.env.example` to `.env` and add `ANTHROPIC_API_KEY`.

### Host it on an IP address

`./pltr` binds `0.0.0.0`, so the dashboard already answers on **every IP this machine has** — it prints them all at startup. Use whichever fits:

| Reach | URL | Setup |
|---|---|---|
| This machine | `http://localhost:8000` | none |
| Anything on your Wi-Fi (phone, other laptop) | `http://<LAN-IP>:8000` | none — the launcher prints the IP |
| One specific interface only | `HOST=192.168.1.50 ./pltr` | pin the bind address |
| The public internet | your router's WAN IP | forward port 8000 on the router to this machine |
| The public internet, no router config | a tunnel URL | `cloudflared tunnel --url http://localhost:8000` |
| Always-on public URL | your Render URL | already deployed — see above |

macOS firewall may prompt on first run — allow incoming connections for Python, or the LAN IP won't answer.

**Manual:**
```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

The UI is a one-pager built around the open trade: **Open plan** first, then market (order book and tape), news, the backtest summary and a 20-session chart. A ◐ button toggles a dark theme.

## Install it as an app

The dashboard is a **PWA**, so it installs to a phone home screen or a desktop dock and opens full-screen with no browser chrome.

- **iPhone / iPad** — open the URL in Safari → Share → *Add to Home Screen*.
- **Android** — Chrome shows an *Install app* prompt, or menu → *Add to Home screen*.
- **Desktop** — Chrome/Edge show an install icon in the address bar.

Layout fills whatever screen it lands on: fluid up to ultra-wide desktops, and on mobile it fills the viewport dynamically (`100dvh`) and respects notch/home-bar safe areas. The service worker caches only the shell — every `/api/` call is network-only, because stale market data is worse than none.

**"i" buttons.** Any indicator or bit of jargon that isn't plain English has a small ⓘ next to it — tap for a short explanation of what it measures and how to read it.

**Order book units.** The order book has a **PLTR ⇄ USDT** toggle: read resting size as share counts or as dollar value. The tape and large-order prints follow the same setting, which is remembered per browser.

## How it works
Three async loops keep a cached `STATE`; the page polls `/api/state` every second.

| Loop | Default | Source |
|---|---|---|
| Rotating mark price | 1s | one of ~10 exchanges per tick, round-robin |
| Order book / tape | 3s | best available venue (`DEPTH_PRIORITY`) |
| Stock (price, technicals) | 30s | Yahoo Finance |
| News | 8s | EDGAR + Yahoo + Nasdaq + Seeking Alpha + Google + StockTwits, in parallel |
| AI agent (rescoring, thesis) | 60s | Anthropic |
| AMD sessions | 60s | intraday candles from the depth venue |
| Insiders & Congress | 30min | SEC EDGAR Form 4 + STOCK Act datasets |

The PLTRX symbol is **auto-discovered** on startup (scans Bybit/Kraken instruments for `PLTR`). Override with `BYBIT_SYMBOL` / `KRAKEN_PAIR` if needed. All tunables are env vars — see `.env.example` / `render.yaml`.

## Open-session trade (16:30 EAT)

The desk runs one trade: the first 15 minutes after the NASDAQ open. That is **16:30 EAT**, or **17:30 EAT** from 2 Nov 2026 to 12 Mar 2027 (US winter time). NYSE holidays and 13:00 early closes are handled.

**Backtest** (`python backtest.py`, re-run daily by the desk, served at `/api/backtest`):
- Each day records the overnight move, the prior day's change, an event flag (|overnight| >= `BT_EVENT_PCT`%, default 2.5) and the pre-open news score.
- Rules follow or fade the first 2-minute move, the overnight move, the prior day, or news. Every trade pays `BT_FEE_PCT` (default 0.10%).
- **Newest days count most.** Each morning the rule is re-picked from past days only, with a day `k` sessions old weighted `0.5^(k/half-life)`. The half-life (10, 20, 40, 80 sessions or equal weights) is chosen on the oldest 70% of days and judged on the newest 30%.
- It passes only with a 70%+ test hit rate whose 95% floor is above 50%, 20+ test trades, and a profit after fees.

**Live engine** (`engine.py`), two layers:
- *Fast layer, every second:* ranks rules with the same recency weights, takes the top rule's call 2 minutes after the open, checks a price stop (`GUARD_STOP_PCT`, default 1%) every tick, records the outcome at +15 minutes and immediately re-ranks with it.
- *AI supervisor (Claude, `SUPERVISOR_MODEL`, default `claude-opus-5-5`):* wakes 30 minutes before the open, on every new headline, after the decision and near the stop (at most every 10s, else every 60s). It can only reduce size, skip the day, move the stop within 0.3% to 1.5%, or exit early. It can never flip direction or add size. Every decision is logged to `logs/signals.jsonl`. It uses server-side refusal fallbacks (`fallbacks: "default"`). Without `ANTHROPIC_API_KEY` the fast layer runs alone.

**News** (`newsfeed.py` + `google_news.py`): SEC EDGAR (set `SEC_UA` to "Your Name your@email", SEC blocks anonymous requests), Yahoo, Nasdaq, Seeking Alpha and Google News. Reworded copies of one story are folded together and count more the more outlets carry them. StockTwits is shown but never counted. The score halves every 6 hours of age. Each source's health is shown under the news feed.

**Tests** (offline, no internet): `python tests/test_backtest.py`, `tests/test_engine.py`, `tests/test_news.py`, `tests/test_trader.py`.

Research scripts (AMD and timeframe replays) live in `research/`.

## Live trade button (ORB at the open)

The **Trade** button in the top bar opens the Opening Range Breakout trader (`trader.py`). It trades the **PLTR USDT perpetual only**.

- **When:** the range is the first 2 minutes after 09:30 New York (16:30 EAT; 17:30 EAT from 2 Nov to 12 Mar). Trading runs until 15 minutes after the open, then everything is closed.
- **How:** a break above the range high with the order book leaning to bids goes long; a break below the low with asks leaning goes short. Each trade is a **$100 margin batch at 10x** ($1,000 of PLTR), with a **$1 take-profit after fees** and a **$1 stop including fees**. Up to 10 trades per session. After a win, the same side re-enters on a fresh high or low; after a loss, it waits for price to return inside the range. Every setting is editable in the panel.
- **Fees:** Bybit 0% per side (VIP), Binance 0.05% per side. $1 net on $1,000 needs a 0.10% move on Bybit and 0.20% on Binance.
- **Speed:** the agent re-checks the live order book (WebSocket, REST fallback) 5 to 20 times a second. Orders are capped at 20 in any one second. There is no minimum trade rate: it only trades on a signal.
- **Paper mode (default):** live order book from the exchange, simulated fills at the touch plus slippage and fees. No keys needed. Starting capital $700.
- **Live mode:** enter API keys in the panel (Bybit, Binance, OKX, Bitget), test them, then press **Go live** and type `LIVE`. Live is armed for one session only. **Stop & flatten** cancels orders and closes the position. Trading halts at a 3% daily loss.
- **AI supervisor:** before each entry it can skip it or shrink it, and it can order an exit. It cannot open or enlarge trades.
- **Security:** keys stay in server memory only (never on disk, never sent back to the browser) and are forgotten on restart; or set `EXCHANGE_ID`, `EXCHANGE_API_KEY`, `EXCHANGE_API_SECRET`. Make keys **trade-only, withdrawals off, IP-restricted**. Trade endpoints answer only to this machine unless you set `TRADE_TOKEN`, which the panel then asks for. On Render, set `TRADE_TOKEN`.
- Every closed trade is logged to `logs/outcomes.jsonl` (`kind: orb_trade`, with the mode).

## Endpoints
- `/` dashboard · `/api/state` full JSON state · `/api/amd` AMD payload · `/api/filings` insiders + congress · `/api/backtest` edge study · `/healthz` health check.
- PWA: `/manifest.webmanifest` · `/sw.js` (served from root so its scope covers the whole site) · `/favicon.ico`.

## Upgrades
- **X/Twitter sentiment**: set `X_BEARER_TOKEN` (X API v2 recent search).
- **Real-stock order book / options flow**: needs a paid feed (Polygon, Unusual Whales) — the backend is structured to add another poller.
