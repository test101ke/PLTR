# PLTR Signal Desk

A real-time Palantir trading dashboard. FastAPI backend + Apple-glass frontend, built to run on **Render**.

It serves one page that live-updates every second with:

- **Tokenized PLTRX** (Bybit primary, Kraken fallback): live order book, buy/sell trade tape, large-order prints, order-book imbalance, 24h volume. This is the crypto-exchange depth data — order book, tape, large orders — for the tokenized Palantir market.
- **Real NASDAQ PLTR** (Yahoo Finance): price, 20-session chart, 52-week range, volume, 50/200-day moving averages, RSI(14), and fundamentals (P/E, market cap, beta, short interest, analyst targets) where available.
- **AMD / Power of 3** (`amd.py`): the session model — Asia accumulates a range, London sweeps one side (manipulation), New York expands (distribution). Detects the sweep, the reclaim, the resulting bias, and derives entry / invalidation / T1 / T2 with R:R. Runs on the tokenized PLTR perp, the only PLTR market that trades through Asia and London. Served at `/api/amd`, shown in the "AMD · Power of 3" tab with a session-shaded candle chart, and folded into the composite signal as its own indicator.
- **AI agent** (Anthropic): scores each news headline bullish/bearish/neutral, writes a short desk read, and feeds a composite **STRONG BUY / SIDEWAYS / STRONG SELL** signal with conviction and a projection band.
- **Fast news** (`newsfeed.py`): every free real-time source polled **in parallel** and de-duplicated — SEC EDGAR filings, Yahoo Finance, Nasdaq, Seeking Alpha, Google News and StockTwits — so a headline appears the moment any one of them prints it. Default poll is every 8s. (True HFT wires — Bloomberg, Reuters, Dow Jones, Benzinga Pro — are paid commercial products with no open-source equivalent; this is the fastest free stack.) Optional X/Twitter buzz with a bearer token.
- **Insiders & Congress** (`filings.py`): who else is trading PLTR, from public disclosures — **SEC Form 4** (officers, directors, 10%+ holders, filed within 2 business days, parsed straight from PLTR's EDGAR index at CIK 0001321655) and **STOCK Act** periodic transaction reports from US House and Senate members. Served at `/api/filings`, shown in the "Insiders & Congress" tab.

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

The UI is a **white one-pager** — every section (live market, overview, fundamentals, projection, open-window edge, AMD, insiders & congress) is visible on one scroll; the top tabs just jump to a section. A ◐ button toggles a dark theme.

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

## Endpoints
- `/` dashboard · `/api/state` full JSON state · `/api/amd` AMD payload · `/api/filings` insiders + congress · `/api/backtest` edge study · `/healthz` health check.
- PWA: `/manifest.webmanifest` · `/sw.js` (served from root so its scope covers the whole site) · `/favicon.ico`.

## Upgrades
- **X/Twitter sentiment**: set `X_BEARER_TOKEN` (X API v2 recent search).
- **Real-stock order book / options flow**: needs a paid feed (Polygon, Unusual Whales) — the backend is structured to add another poller.
