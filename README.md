# PLTR Signal Desk

A real-time Palantir trading dashboard. FastAPI backend + Apple-glass frontend, built to run on **Render**.

It serves one page that live-updates every second with:

- **Tokenized PLTRX** (Bybit primary, Kraken fallback): live order book, buy/sell trade tape, large-order prints, order-book imbalance, 24h volume. This is the crypto-exchange depth data — order book, tape, large orders — for the tokenized Palantir market.
- **Real NASDAQ PLTR** (Yahoo Finance): price, 20-session chart, 52-week range, volume, 50/200-day moving averages, RSI(14), and fundamentals (P/E, market cap, beta, short interest, analyst targets) where available.
- **AMD / Power of 3** (`amd.py`): the session model — Asia accumulates a range, London sweeps one side (manipulation), New York expands (distribution). Detects the sweep, the reclaim, the resulting bias, and derives entry / invalidation / T1 / T2 with R:R. Runs on the tokenized PLTR perp, the only PLTR market that trades through Asia and London. Served at `/api/amd`, shown in the "AMD · Power of 3" tab with a session-shaded candle chart, and folded into the composite signal as its own indicator.
- **AI agent** (Anthropic): scores each news headline bullish/bearish/neutral, writes a short desk read, and feeds a composite **STRONG BUY / SIDEWAYS / STRONG SELL** signal with conviction and a projection band.
- **News**: Google News RSS (free, no key). Optional X/Twitter buzz with a bearer token.

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

The UI is a **white one-pager** — every section (live market, overview, fundamentals, projection, open-window edge) is visible on one scroll; the top tabs just jump to a section. A ◐ button toggles a dark theme.

## How it works
Three async loops keep a cached `STATE`; the page polls `/api/state` every second.

| Loop | Default | Source |
|---|---|---|
| Crypto (order book, tape) | 2s | Bybit v5 / Kraken public API |
| Stock (price, technicals) | 30s | Yahoo Finance |
| AI agent (news, signal) | 300s | Google News RSS + Anthropic |

The PLTRX symbol is **auto-discovered** on startup (scans Bybit/Kraken instruments for `PLTR`). Override with `BYBIT_SYMBOL` / `KRAKEN_PAIR` if needed. All tunables are env vars — see `.env.example` / `render.yaml`.

## Endpoints
- `/` dashboard · `/api/state` full JSON state · `/healthz` health check.

## Upgrades
- **X/Twitter sentiment**: set `X_BEARER_TOKEN` (X API v2 recent search).
- **Real-stock order book / options flow**: needs a paid feed (Polygon, Unusual Whales) — the backend is structured to add another poller.
