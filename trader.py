"""
Opening Range Breakout (ORB) trader for the PLTR perpetual at the US open.
==========================================================================
Trades the PLTR USDT perpetual only.

Default plan (from the account owner): $700 capital, $100 margin batches at
10x, several small trades in the first 15 minutes, aiming at $1 net per batch
after fees. Fees per side: Bybit 0% (VIP), Binance 0.05%. At 10x a $100 batch
is $1,000 of PLTR, so $1 net needs a 0.10% move on Bybit but 0.20% on Binance,
where the round-trip fee alone is $1.

Timing: the opening range starts at 09:30:00 New York, which is 16:30 EAT
(17:30 EAT from 2 Nov 2026 to 12 Mar 2027, US winter time).

Strategy (per session):
  1. For the first `orbMinutes` after the open, record the high and low of the
     mid price from the live order book. That is the opening range.
  2. After it, go LONG when the mid breaks above the high by `bufferPct` and the
     book leans to bids (bid share >= `imbalanceMin`%), or SHORT when it breaks
     below the low with the book leaning to asks.
  3. Exits (EXIT OPTIONS). The open is violent, so a plain tight stop gets
     shaken out just before the real move. Tools against that, all optional:
       - emergency stop `hardStopPct` from entry: ALWAYS on, fires instantly.
         At 10x a ~10% move liquidates, so trading with no stop is not offered;
       - normal stop: $ (`usd`), a fraction of the opening range (`range`), or
         none (`hard`, emergency stop only);
       - `graceSec`: the normal stop is ignored this long after entry;
       - `confirmMs`: the stop must stay breached this long (a one-tick wick
         does not count);
       - `breakevenUsd`: once this profit shows, the stop moves to break-even
         (entry plus the round-trip fee);
       - first target `takeProfitUsd` (net of fees): bank `partialPct`% there
         (100 = the old fixed $1 exit) and lift the stop to break-even;
       - `trailPct`: the rest trails that far behind the best price, so a
         0.2%-in-a-second spike is ridden instead of capped at $1.
       - `reentrySec`: after any exit, wait before the next entry, so it does
         not buy the top of the spike it just sold.
     PRESETS holds one-tap combinations (Scalp, Runner, Wick-proof, Burst).
  4. Up to `maxTrades` trades. After a winning exit the same side re-arms on a
     fresh high (long) or low (short) beyond the exit price: that is how it
     takes several small trades out of one strong move. After a loss it waits
     for price to come back inside the range.
  5. Everything is closed `exitAfterMin` minutes after the open, or at once
     if the day's loss reaches `dailyLossPct`% of starting equity.

Speed: the agent re-checks the book 5 to 20 times a second (`tickHz`), on
every WebSocket update and on a timer so it never checks less than 5 times a
second. Orders pass through a token bucket capped at 20 per second. There is
deliberately NO minimum trade rate: forcing trades without a signal would
burn the account on fees, doubly so at 10x.

Modes:
  paper  live market data from the chosen exchange, simulated fills at the
         touch plus slippage and taker fees. No keys needed. The default.
  live   real orders with your API keys. Must be armed explicitly each day.

The AI supervisor (engine.py) reports a direction-free `marketRisk`: "elevated"
halves new batches, "halt" blocks entries and closes the position. It can never
open or enlarge a trade. (Its skip/exit calls on the rule trade are about that
trade's direction, so the ORB trader does not follow them.)
"""
import os, json, time, asyncio, datetime as dt
from zoneinfo import ZoneInfo
import signal_log

NY = ZoneInfo("America/New_York")
EAT = ZoneInfo("Africa/Nairobi")

MIN_STOP_PCT = 0.10      # no stop may sit closer to entry than this

# Fee per side, in %, by exchange. Deliberately the worst case for both venues
# (0.06% a side, 0.12% round trip) so results are never flattered by a VIP rate.
FEES = {"bybit": 0.06, "binanceusdm": 0.06}

DEFAULTS = {
    "exchange": "bybit", "symbol": "", "leverage": 10,
    "batchUsd": 100.0,        # margin per trade in USDT (0 = use marginPct instead)
    "marginPct": 10.0,
    "takeProfitUsd": 1.0,     # first target: net profit per batch after fees (0 = use targetR)
    "stopLossUsd": 1.5,       # usd stop mode: max loss per batch incl. fees
    # --- exits (see EXIT OPTIONS above); defaults = the "Runner" preset ---
    "stopMode": "range",      # usd | range | hard  (hard = emergency stop only)
    "rangeStopFrac": 0.5,     # range mode: stop this fraction of the opening range beyond entry
    "hardStopPct": 1.0,       # emergency stop, % of price from entry. Always on.
    "graceSec": 3.0,          # ignore the normal stop for this long after entry
    "confirmMs": 500.0,       # the stop must stay breached this long before it fires
    "breakevenUsd": 0.6,      # once this much profit shows, the stop moves to break-even (0 = off)
    "partialPct": 50.0,       # % of the batch banked at the first target (100 = fixed exit)
    "trailPct": 0.15,         # after the first target, trail the rest this % behind the best price (0 = off)
    "reentrySec": 5.0,        # after any exit, wait this long before a new entry (no chasing a spike top)
    "orbMinutes": 2, "bufferPct": 0.02, "targetR": 1.5, "maxTrades": 10, "exitAfterMin": 15,
    "dailyLossPct": 3.0, "imbalanceMin": 55.0, "maxNotional": 5000.0,
    "tickHz": 10, "maxOrdersPerSec": 20, "takerFeePct": -1.0, "slippagePct": 0.01,
    "paperEquity": 700.0,
}
BOUNDS = {
    "leverage": (1, 20), "marginPct": (1, 100), "batchUsd": (0, 1e6), "takeProfitUsd": (0, 1e4),
    "stopLossUsd": (0, 1e4), "rangeStopFrac": (0.1, 2), "hardStopPct": (0.2, 5), "graceSec": (0, 60),
    "confirmMs": (0, 5000), "reentrySec": (0, 300), "breakevenUsd": (0, 1e4), "partialPct": (0, 100), "trailPct": (0, 2), "orbMinutes": (1, 15), "bufferPct": (0, 1),
    "targetR": (0.5, 5), "maxTrades": (1, 10), "exitAfterMin": (5, 390), "dailyLossPct": (0.5, 20),
    "imbalanceMin": (50, 90), "maxNotional": (10, 1e7), "tickHz": (5, 20), "maxOrdersPerSec": (1, 20),
    "takerFeePct": (-1, 1), "slippagePct": (0, 1), "paperEquity": (10, 1e9),
}


# One-tap presets. They change exits only; leverage, batch size and risk limits stay yours.
PRESETS = {
    "scalp": {"label": "Scalp", "note": "Bank $1 and get out. $2.20 stop incl. fees (0.10% of price plus the "
                                        "$1.20 round-trip fee), no trailing.",
              "takeProfitUsd": 1.0, "partialPct": 100, "trailPct": 0, "stopMode": "usd", "stopLossUsd": 2.2,
              "graceSec": 0, "confirmMs": 0, "breakevenUsd": 0},
    "runner": {"label": "Runner", "note": "Bank half at $1, trail the rest 0.15% behind the best price. Stop at "
                                          "half the opening range, ignored for 3s, needs 0.5s to confirm.",
               "takeProfitUsd": 1.0, "partialPct": 50, "trailPct": 0.15, "stopMode": "range", "rangeStopFrac": 0.5,
               "graceSec": 3, "confirmMs": 500, "breakevenUsd": 0.6},
    "wickproof": {"label": "Wick-proof", "note": "Only the 1% emergency stop for 10s, then a full-range stop that "
                                                 "must hold 1s. Break-even at $0.5; bank 30% at $1, trail 0.25%.",
                  "takeProfitUsd": 1.0, "partialPct": 30, "trailPct": 0.25, "stopMode": "range", "rangeStopFrac": 1.0,
                  "graceSec": 10, "confirmMs": 1000, "breakevenUsd": 0.5},
    "burst": {"label": "Burst", "note": "For sharp 0.2%+ spikes: no fixed target, trail 0.08% once $0.5 is showing.",
              "takeProfitUsd": 0.5, "partialPct": 0, "trailPct": 0.08, "stopMode": "range", "rangeStopFrac": 0.5,
              "graceSec": 2, "confirmMs": 300, "breakevenUsd": 0.4},
}


def clean_config(cfg, base=None):
    """Merge user input into the config, clamping every number to its bounds."""
    out = dict(base or DEFAULTS)
    for k, v in (cfg or {}).items():
        if k not in DEFAULTS:
            continue
        if k in BOUNDS:
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            lo, hi = BOUNDS[k]
            v = min(hi, max(lo, v))
            if isinstance(DEFAULTS[k], int):
                v = int(round(v))
        else:
            v = str(v).strip()
            if k == "stopMode" and v not in ("usd", "range", "hard"):
                continue
        out[k] = v
    return out


def fee_pct(cfg):
    """Fee per side. -1 means 'use this exchange's rate from FEES'."""
    f = cfg.get("takerFeePct", -1)
    return FEES.get(cfg.get("exchange"), 0.05) if f is None or f < 0 else f


# ------------------------------------------------------------------ rate limit
class RateLimiter:
    """Sliding window: never more than `rate` acquisitions in any one second."""
    def __init__(self, rate):
        self.rate = int(rate)
        self.times = []
        self.lock = asyncio.Lock()

    async def acquire(self):
        async with self.lock:
            while True:
                now = time.monotonic()
                self.times = [t for t in self.times if now - t < 1.0]
                if len(self.times) < self.rate:
                    self.times.append(now)
                    return
                await asyncio.sleep(1.0 - (now - self.times[0]) + 1e-4)


# ------------------------------------------------------------------ strategy
def open_time(day):
    return dt.datetime.combine(day, dt.time(9, 30), tzinfo=NY)


class OrbStrategy:
    """Pure decision logic. Feed it ticks; it returns actions. No I/O."""

    def __init__(self, cfg, equity):
        self.cfg, self.start_equity = cfg, equity
        self.day = None
        self.reset_day(None)

    def reset_day(self, day):
        self.day, self.hi, self.lo = day, None, None
        self.trades, self.realized, self.halted = 0, 0.0, None
        self.pos = None            # {"side", "qty", "entry", "stop", "target", "openedAt"}
        self.armed = {"long": True, "short": True}
        self.after_win = None      # after a winning exit: {"side", "ext"} for continuation re-entry
        self.now, self.cool_until = None, None

    def phase(self, now):
        op = open_time(now.date())
        if now < op:
            return "before open"
        if now < op + dt.timedelta(minutes=self.cfg["orbMinutes"]):
            return "building range"
        if now < op + dt.timedelta(minutes=self.cfg["exitAfterMin"]):
            return "trading"
        return "session over"

    def unrealized(self, bid, ask):
        p = self.pos
        if not p:
            return 0.0
        px = bid if p["side"] == "long" else ask
        return (px - p["entry"]) * p["qty"] * (1 if p["side"] == "long" else -1)

    def on_tick(self, now, bid, ask, imbalance, ai=None):
        """Return a list of actions: ("open", side, reason) or ("close", reason)."""
        if now.date() != self.day:
            self.reset_day(now.date())
        self.now = now
        mid = (bid + ask) / 2
        ph = self.phase(now)
        acts = []
        if ph == "before open":
            return acts
        if ph == "building range":
            self.hi = mid if self.hi is None else max(self.hi, mid)
            self.lo = mid if self.lo is None else min(self.lo, mid)
            return acts
        if self.hi is None:            # started after the range window: no range, no trades
            return acts
        risk = (ai or {}).get("marketRisk")
        if self.pos:
            acts = self._manage(now, bid, ask, ph, risk)
            loss = self.realized + self.unrealized(bid, ask)
            if not acts and loss <= -self.start_equity * self.cfg["dailyLossPct"] / 100:
                acts.append(("close", "daily loss limit")); self.halted = "daily loss limit"
            return acts
        if self.realized <= -self.start_equity * self.cfg["dailyLossPct"] / 100:
            self.halted = "daily loss limit"
        if ph == "session over" or self.halted or self.trades >= self.cfg["maxTrades"]:
            return acts
        if self.lo < mid < self.hi:    # back inside the range: both sides re-arm
            self.armed = {"long": True, "short": True}
            self.after_win = None
        w = self.after_win             # after a win, the same side re-arms on a fresh extreme
        if w and not self.armed[w["side"]]:
            if (w["side"] == "long" and mid > w["ext"]) or (w["side"] == "short" and mid < w["ext"]):
                self.armed[w["side"]] = True
        if risk == "halt":             # the supervisor sees a disorderly market or a news shock
            return acts
        if self.cool_until and now < self.cool_until:
            return acts
        buf = self.cfg["bufferPct"] / 100
        if self.armed["long"] and mid > self.hi * (1 + buf) and imbalance >= self.cfg["imbalanceMin"]:
            acts.append(("open", "long", f"broke range high {self.hi:.2f}, bids {imbalance:.0f}%"))
        elif self.armed["short"] and mid < self.lo * (1 - buf) and imbalance <= 100 - self.cfg["imbalanceMin"]:
            acts.append(("open", "short", f"broke range low {self.lo:.2f}, asks {100 - imbalance:.0f}%"))
        return acts

    def size(self, equity, price, ai=None):
        margin = self.cfg["batchUsd"] if self.cfg.get("batchUsd") else equity * self.cfg["marginPct"] / 100
        margin = min(margin, equity)
        notional = min(margin * self.cfg["leverage"], self.cfg["maxNotional"])
        scale = 0.5 if (ai or {}).get("marketRisk") == "elevated" else 1.0
        return notional * scale / price

    def _profit(self, px):
        """Open profit in USDT on what is left, after the round-trip fee on it."""
        p = self.pos
        gross = (px - p["entry"]) * p["qty"] * p["sgn"]
        return gross - 2 * p["entry"] * p["qty"] * fee_pct(self.cfg) / 100

    def _manage(self, now, bid, ask, ph, risk):
        """Exits for an open position. Order: time, AI risk-off, emergency stop, first
        target (bank some, arm the trail), break-even lock, trail, confirmed stop."""
        p, c = self.pos, self.cfg
        sgn = p["sgn"]
        px = bid if sgn > 0 else ask
        better = (lambda a, b: a > b) if sgn > 0 else (lambda a, b: a < b)
        if better(px, p["best"]):
            p["best"] = px
        if ph == "session over":
            return [("close", "time exit")]
        if risk == "halt":
            return [("close", "AI risk-off")]
        if not better(px, p["hard"]):
            return [("close", "emergency stop")]
        profit = self._profit(px)
        be_price = p["entry"] * (1 + sgn * 2 * fee_pct(c) / 100)
        acts = []
        if not p["tp1"] and ((c["takeProfitUsd"] and profit >= c["takeProfitUsd"]) or
                             (not c["takeProfitUsd"] and not better(p["target"], px))):
            p["tp1"] = True
            self._lift_stop(be_price)
            if c["partialPct"] >= 100:
                return [("close", "target")]
            if c["partialPct"] > 0:
                acts.append(("close", "first target (partial)", c["partialPct"] / 100))
        if not p["be"] and c["breakevenUsd"] and profit >= c["breakevenUsd"]:
            p["be"] = True
            self._lift_stop(be_price)
        if p["tp1"] and c["trailPct"]:
            self._lift_stop(p["best"] * (1 - sgn * c["trailPct"] / 100), trail=True)
        if acts:
            return acts
        if p["stop"] is None:
            return []
        in_grace = (now - p["opened"]).total_seconds() < c["graceSec"] and not (p["tp1"] or p["be"])
        if in_grace or better(px, p["stop"]):
            p["breach"] = None
            return []
        p["breach"] = p["breach"] or now
        if (now - p["breach"]).total_seconds() * 1000 >= c["confirmMs"]:
            return [("close", p["stopKind"])]
        return []

    def _lift_stop(self, level, trail=False):
        """Move the stop in the trade's favour only, never back."""
        p = self.pos
        if p["stop"] is None or (level > p["stop"] if p["sgn"] > 0 else level < p["stop"]):
            p["stop"] = level
            p["stopKind"] = "trailing stop" if trail else "break-even stop"

    def opened(self, side, qty, price, now, entry_fee=0.0):
        c = self.cfg
        sgn = 1 if side == "long" else -1
        fees = 2 * price * qty * fee_pct(c) / 100            # round trip, in USDT
        hard = price * (1 - sgn * c["hardStopPct"] / 100)
        # No stop closer than MIN_STOP_PCT: at 0.12% round-trip fees a "$1.50 incl.
        # fees" stop on $1,000 left 0.03% of room, and the live-data replay showed
        # it stopped out almost every trade.
        floor = price * MIN_STOP_PCT / 100
        if c["stopMode"] == "usd":
            dist = max((c["stopLossUsd"] - fees) / qty, floor)
        elif c["stopMode"] == "range":
            dist = max(c["rangeStopFrac"] * (self.hi - self.lo), floor)
        else:
            dist = None                                       # hard: emergency stop only
        stop = None if dist is None else price - sgn * dist
        if stop is not None and (stop < hard if sgn > 0 else stop > hard):
            stop = None                                       # emergency stop is tighter anyway
        if c.get("takeProfitUsd"):
            target = price + sgn * (c["takeProfitUsd"] + fees) / qty
        else:
            target = price + sgn * (dist or price * 0.002) * c["targetR"]
        self.pos = {"side": side, "sgn": sgn, "qty": qty, "qty0": qty, "entry": price, "stop": stop,
                    "stopKind": "stop", "hard": hard, "target": target, "best": price, "tp1": False,
                    "be": False, "breach": None, "opened": now, "openedAt": now.isoformat(),
                    "entryFeeLeft": entry_fee, "pnl": 0.0, "id": f"{self.day}-{self.trades + 1}"}
        self.trades += 1
        self.armed[side] = False

    def closed(self, price, exit_fee, fraction=1.0):
        """Book a full or partial exit. Returns the P&L of this piece, after fees."""
        p = self.pos
        fraction = min(1.0, max(0.0, fraction))
        q = p["qty"] * fraction
        ef = p["entryFeeLeft"] * fraction
        pnl = (price - p["entry"]) * q * p["sgn"] - exit_fee - ef
        self.realized += pnl
        p["pnl"] += pnl
        p["qty"] -= q; p["entryFeeLeft"] -= ef
        if fraction >= 0.999 or p["qty"] <= p["qty0"] * 1e-6:
            self.after_win = {"side": p["side"], "ext": price} if p["pnl"] > 0 else None
            self.pos = None
            if self.now is not None:
                self.cool_until = self.now + dt.timedelta(seconds=self.cfg.get("reentrySec", 0))
        return pnl


# ------------------------------------------------------------------ brokers
class PaperBroker:
    """Fills at the touch plus slippage; charges the taker fee on both legs."""
    live = False

    def __init__(self, cfg):
        self.cfg, self.equity = cfg, cfg["paperEquity"]

    async def setup(self, symbol):
        return self.equity

    async def market(self, side, qty, bid, ask, reduce_only=False):
        slip = self.cfg["slippagePct"] / 100
        px = ask * (1 + slip) if side == "buy" else bid * (1 - slip)
        fee = px * qty * fee_pct(self.cfg) / 100
        return {"price": px, "qty": qty, "fee": fee}

    async def flatten(self):
        return None

    async def close(self):
        return None


class LiveBroker:
    """Real orders through ccxt. Market orders; exits are reduce-only."""
    live = True

    def __init__(self, cfg, exchange):
        self.cfg, self.ex, self.symbol = cfg, exchange, None

    async def setup(self, symbol):
        self.symbol = symbol
        try:
            await self.ex.set_margin_mode("isolated", symbol)
        except Exception:
            pass                                     # already isolated, or venue has no such call
        await self.ex.set_leverage(int(self.cfg["leverage"]), symbol)
        bal = await self.ex.fetch_balance()
        usdt = (bal.get("USDT") or {})
        return float(usdt.get("total") or usdt.get("free") or 0)

    async def market(self, side, qty, bid, ask, reduce_only=False):
        amount = float(self.ex.amount_to_precision(self.symbol, qty))
        if amount <= 0:
            raise ValueError("order size rounds to zero at this exchange's step size")
        params = {"reduceOnly": True} if reduce_only else {}
        o = await self.ex.create_order(self.symbol, "market", side, amount, None, params)
        px = float(o.get("average") or o.get("price") or (ask if side == "buy" else bid))
        fee = (o.get("fee") or {}).get("cost")
        fee = float(fee) if fee is not None else px * amount * fee_pct(self.cfg) / 100
        return {"price": px, "qty": float(o.get("filled") or amount), "fee": fee, "id": o.get("id")}

    async def flatten(self):
        """Kill switch: cancel resting orders and close any open position."""
        try:
            await self.ex.cancel_all_orders(self.symbol)
        except Exception:
            pass
        for p in await self.ex.fetch_positions([self.symbol]):
            qty = abs(float(p.get("contracts") or 0))
            if qty:
                side = "sell" if p.get("side") == "long" else "buy"
                await self.ex.create_order(self.symbol, "market", side, qty, None, {"reduceOnly": True})

    async def close(self):
        try:
            await self.ex.close()
        except Exception:
            pass


# ------------------------------------------------------------------ keys
class KeyStore:
    """The trader's working copy of the keys. Never logged or sent to the
    browser (only a masked form is). Saving them for later is the caller's job
    (accounts.py stores them encrypted)."""

    def __init__(self, use_env=True):
        self.creds = None
        ex, k, s = os.getenv("EXCHANGE_ID"), os.getenv("EXCHANGE_API_KEY"), os.getenv("EXCHANGE_API_SECRET")
        if use_env and ex and k and s:
            self.creds = {"exchange": ex, "apiKey": k, "secret": s, "password": os.getenv("EXCHANGE_API_PASSWORD", "")}
        self.validated = None

    def set(self, exchange, api_key, secret, password=""):
        self.creds = {"exchange": exchange.strip(), "apiKey": api_key.strip(), "secret": secret.strip(),
                      "password": (password or "").strip()}
        self.validated = None

    def clear(self):
        self.creds, self.validated = None, None

    def masked(self):
        if not self.creds:
            return None
        k = self.creds["apiKey"]
        return {"exchange": self.creds["exchange"], "apiKey": (k[:4] + "…" + k[-4:]) if len(k) > 8 else "…",
                "validated": self.validated}


def make_exchange(exchange_id, creds=None, pro=True):
    """A ccxt exchange object: WebSocket-capable (ccxt.pro) when available."""
    import ccxt.pro as ccxtpro
    import ccxt.async_support as ccxta
    mod = ccxtpro if pro and hasattr(ccxtpro, exchange_id) else ccxta
    if not hasattr(mod, exchange_id):
        raise ValueError(f"unknown exchange '{exchange_id}'")
    opts = {"enableRateLimit": True, "options": {"defaultType": "swap"}}
    if exchange_id == "bybit":
        opts["options"]["fetchMarkets"] = {"types": ["linear"]}     # perps only: faster start
    if creds:
        opts.update({"apiKey": creds["apiKey"], "secret": creds["secret"]})
        if creds.get("password"):
            opts["password"] = creds["password"]
    return getattr(mod, exchange_id)(opts)


def find_symbol(markets, wanted=""):
    """The PLTR USDT perpetual. This desk trades PLTR only: any other symbol is refused."""
    if wanted:
        m = markets.get(wanted) or {}
        return wanted if (m.get("base") or "").upper() in ("PLTR", "PLTRX") and m.get("swap") else None
    for sym, m in markets.items():
        if m.get("swap") and m.get("quote") == "USDT" and (m.get("base") or "").upper() in ("PLTR", "PLTRX"):
            return sym
    return None


# ------------------------------------------------------------------ agent
class TradeAgent:
    """Runs the ORB strategy on a live order-book stream, in paper or live mode."""

    def __init__(self, get_ai=None, exchange_factory=make_exchange, user=None, settings=None):
        self.user = user
        self.cfg = clean_config(settings or {})
        self.keys = KeyStore(use_env=user is None)
        self.get_ai = get_ai or (lambda: None)
        self.exchange_factory = exchange_factory
        self.mode, self.live_armed_day = "off", None
        self.task, self.feed_ex, self.broker, self.strategy = None, None, None, None
        self.symbol, self.equity = None, None
        self.book = None                    # latest {"bid", "ask", "imb", "ts"}
        self.limiter = RateLimiter(self.cfg["maxOrdersPerSec"])
        self.events, self.trades = [], []
        self.history = load_history(user)     # this account's closed trades, both modes
        self.stats = {"checksPerSec": 0.0, "ordersLastSec": 0, "bookUpdatesPerSec": 0.0}
        self._order_times, self._busy = [], False
        self.error = None

    # ---------------------------------------------------------- control
    def status(self):
        s = self.strategy
        now = dt.datetime.now(NY)
        return {
            "mode": self.mode, "liveArmed": self.live_armed_day == now.date(), "config": self.cfg,
            "feePct": fee_pct(self.cfg),
            "presets": {k: {"label": v["label"], "note": v["note"]} for k, v in PRESETS.items()},
            "keys": self.keys.masked(), "symbol": self.symbol, "equity": self.equity, "error": self.error,
            "phase": s.phase(now) if s else "stopped",
            "openEAT": open_time(now.date()).astimezone(EAT).strftime("%H:%M"),
            "range": {"high": s.hi, "low": s.lo} if s else None,
            "position": s.pos if s else None, "tradesToday": s.trades if s else 0,
            "realized": round(s.realized, 4) if s else 0.0,
            "unrealized": round(s.unrealized(self.book["bid"], self.book["ask"]), 4) if (s and self.book) else 0.0,
            "halted": s.halted if s else None, "book": self.book, "stats": self.stats,
            "events": self.events[-30:], "trades": self.trades[-20:],
            "view": self.mode if self.mode != "off" else (self.history[-1]["mode"] if self.history else "paper"),
            "performance": {m: performance(self.history, m, today=now.date().isoformat(),
                                           capital=self.cfg["paperEquity"] if m == "paper" else None)
                            for m in ("paper", "live")},
        }

    def apply_preset(self, name):
        if name not in PRESETS:
            raise ValueError(f"unknown preset '{name}'")
        return self.configure({k: v for k, v in PRESETS[name].items() if k not in ("label", "note")})

    def configure(self, cfg):
        if self.mode != "off":
            raise RuntimeError("stop the agent before changing settings")
        self.cfg = clean_config(cfg, self.cfg)
        self.limiter = RateLimiter(self.cfg["maxOrdersPerSec"])
        return self.cfg

    async def validate_keys(self):
        """Log in and read the balance. Proves the keys work without trading."""
        if not self.keys.creds:
            raise RuntimeError("no API keys set")
        ex = self.exchange_factory(self.keys.creds["exchange"], self.keys.creds, pro=False)
        try:
            bal = await ex.fetch_balance()
            usdt = bal.get("USDT") or {}
            self.keys.validated = {"ok": True, "usdt": usdt.get("total"), "at": _iso()}
        except Exception as e:
            self.keys.validated = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}", "at": _iso()}
        finally:
            try:
                await ex.close()
            except Exception:
                pass
        return self.keys.validated

    async def start(self, mode, confirm=""):
        if self.mode != "off":
            raise RuntimeError(f"already running in {self.mode} mode")
        if mode not in ("paper", "live"):
            raise ValueError("mode must be paper or live")
        exchange_id = self.cfg["exchange"]
        if mode == "live":
            if confirm != "LIVE":
                raise PermissionError("type LIVE to confirm real-money trading")
            if not self.keys.creds or not (self.keys.validated or {}).get("ok"):
                raise PermissionError("set and validate API keys first")
            exchange_id = self.keys.creds["exchange"]
            self.cfg["exchange"] = exchange_id          # fees follow the exchange actually traded
        self.error = None
        try:
            self.feed_ex = self.exchange_factory(exchange_id, None, pro=True)
            await self.feed_ex.load_markets()
            self.symbol = find_symbol(self.feed_ex.markets, self.cfg["symbol"])
            if not self.symbol:
                raise RuntimeError(f"no PLTR USDT perpetual found on {exchange_id}")
            if mode == "live":
                ex = self.exchange_factory(exchange_id, self.keys.creds, pro=False)
                self.broker = LiveBroker(self.cfg, ex)
                await ex.load_markets()
            else:
                self.broker = PaperBroker(self.cfg)
            self.equity = await self.broker.setup(self.symbol)
        except Exception as e:
            for x in (self.broker, self.feed_ex):
                if x is not None:
                    try:
                        await x.close()
                    except Exception:
                        pass
            self.broker = self.feed_ex = None
            msg = str(e) if isinstance(e, RuntimeError) else f"could not reach {exchange_id} ({type(e).__name__}): {str(e)[:140]}"
            raise RuntimeError(msg) from None
        if mode == "live":
            self.live_armed_day = dt.datetime.now(NY).date()
        self.strategy = OrbStrategy(self.cfg, self.equity)
        self.mode = mode
        self._event(f"started {mode} on {exchange_id} {self.symbol}, equity {self.equity:.2f} USDT, "
                    f"{self.cfg['leverage']}x")
        self.task = asyncio.create_task(self._run())
        return self.status()

    async def stop(self, reason="stopped by user"):
        """Kill switch: flatten, cancel, shut the stream."""
        if self.task:
            self.task.cancel()
            self.task = None
        try:
            if self.strategy and self.strategy.pos and self.book:
                await self._close(reason)
            if self.broker:
                await self.broker.flatten()
        except Exception as e:
            self.error = f"flatten failed: {e}. CHECK THE EXCHANGE MANUALLY."
        for x in (self.broker, self.feed_ex):
            if x is not None:
                try:
                    await x.close()
                except Exception:
                    pass
        self._event(f"{self.mode} stopped: {reason}")
        self.mode, self.live_armed_day, self.feed_ex = "off", None, None
        return self.status()

    # ---------------------------------------------------------- loop
    async def _run(self):
        """Book stream + timer. Evaluates on every update, at most tickHz/s, at least 5/s."""
        hz = self.cfg["tickHz"]
        min_gap, max_gap = 1.0 / hz, 1.0 / 5
        last_eval, checks, updates, win_start = 0.0, 0, 0, time.monotonic()
        stream = asyncio.create_task(self._stream())
        try:
            while True:
                if stream.done() and stream.exception():
                    self.error = f"market stream: {stream.exception()}"
                    stream = asyncio.create_task(self._stream())
                now_m = time.monotonic()
                gap = now_m - last_eval
                fresh = self.book is not None and self.book.get("_new")
                if self.book and gap >= min_gap and (fresh or gap >= max_gap):
                    if fresh:
                        updates += 1
                    self.book["_new"] = False
                    last_eval = now_m
                    checks += 1
                    await self._evaluate()
                if now_m - win_start >= 1.0:
                    el = now_m - win_start
                    self.stats["checksPerSec"] = round(checks / el, 1)
                    self.stats["bookUpdatesPerSec"] = round(updates / el, 1)
                    checks = updates = 0; win_start = now_m
                    self._roll_day()
                await asyncio.sleep(min_gap / 4)
        finally:
            stream.cancel()

    async def _stream(self):
        ex, sym = self.feed_ex, self.symbol
        ws = hasattr(ex, "watch_order_book")
        while True:
            try:
                ob = await (ex.watch_order_book(sym, 20) if ws else ex.fetch_order_book(sym, 20))
            except Exception as e:
                if ws:                       # WebSocket unavailable: fall back to REST polling
                    ws, self.error = False, f"websocket down ({type(e).__name__}), polling REST"
                    continue
                raise
            bids, asks = ob.get("bids") or [], ob.get("asks") or []
            if bids and asks:
                bv = sum(b[1] for b in bids[:10]); av = sum(a[1] for a in asks[:10])
                self.book = {"bid": bids[0][0], "ask": asks[0][0], "imb": 100 * bv / (bv + av) if bv + av else 50.0,
                             "ts": ob.get("timestamp") or int(time.time() * 1000), "_new": True}
            if not ws:
                await asyncio.sleep(1.0 / self.cfg["tickHz"])

    def _roll_day(self):
        """Live trading is armed for one session only: after it, drop back to paper."""
        today = dt.datetime.now(NY).date()
        if self.mode == "live" and self.live_armed_day and today != self.live_armed_day \
                and not (self.strategy and self.strategy.pos):
            self._event("live session over: disarmed, switching to stop (re-arm tomorrow)")
            asyncio.create_task(self.stop("live disarmed after its session"))

    async def _evaluate(self, now=None):
        if self._busy:
            return
        b, s = self.book, self.strategy
        now = now or dt.datetime.now(NY)
        ai = self.get_ai()
        self._busy = True
        try:
            for act in s.on_tick(now, b["bid"], b["ask"], b["imb"], ai):
                if act[0] == "open":
                    await self._open(act[1], act[2], now, ai)
                elif s.pos:
                    await self._close(act[1], act[2] if len(act) > 2 else 1.0)
        except Exception as e:
            self.error = f"{type(e).__name__}: {str(e)[:160]}"
            self._event("order error: " + self.error)
        finally:
            self._busy = False

    async def _order(self, side, qty, reduce_only):
        await self.limiter.acquire()
        t = time.monotonic()
        self._order_times = [x for x in self._order_times if t - x < 1.0] + [t]
        self.stats["ordersLastSec"] = len(self._order_times)
        return await self.broker.market(side, qty, self.book["bid"], self.book["ask"], reduce_only)

    async def _open(self, side, reason, now, ai):
        s = self.strategy
        eq = self.equity + s.realized
        qty = s.size(eq, self.book["ask"] if side == "long" else self.book["bid"], ai)
        if qty <= 0:
            return
        fill = await self._order("buy" if side == "long" else "sell", qty, False)
        s.opened(side, fill["qty"], fill["price"], now, fill["fee"])
        stop = s.pos["stop"]
        self._event(f"OPEN {side} {fill['qty']:.4f} @ {fill['price']:.2f} ({reason}); "
                    f"stop {'emergency only' if stop is None else f'{stop:.3f}'} "
                    f"(emergency {s.pos['hard']:.3f}), first target {s.pos['target']:.3f}")

    async def _close(self, reason, fraction=1.0):
        s = self.strategy
        p = s.pos
        qty = p["qty"] * fraction
        try:
            fill = await self._order("sell" if p["side"] == "long" else "buy", qty, True)
        except ValueError:
            if fraction < 1.0:                     # partial rounds to zero at the exchange step
                self._event("partial exit too small for the exchange: riding full size")
                return
            raise
        frac = min(1.0, fill["qty"] / p["qty"]) if p["qty"] else 1.0
        side, entry = p["side"], p["entry"]
        pnl = s.closed(fill["price"], fill["fee"], frac)
        row = {"user": self.user, "mode": self.mode, "day": str(s.day), "tradeId": p.get("id"), "capital": self.equity,
               "side": side, "qty": round(fill["qty"], 6),
               "entry": entry, "exit": fill["price"], "pnl": round(pnl, 4), "why": reason,
               "leverage": self.cfg["leverage"], "at": _iso()}
        self.trades.append(row)
        self.history.append(row)
        signal_log.log_outcome({"kind": "orb_trade", **row})
        self._event(f"CLOSE {'part ' if frac < 0.999 else ''}{side} {fill['qty']:.4f} @ {fill['price']:.2f} "
                    f"({reason}) pnl {pnl:+.2f} USDT")

    def _event(self, msg):
        self.events.append({"at": _iso(), "msg": msg})
        self.events = self.events[-200:]


def load_history(user=None):
    """One account's closed ORB trades from the outcomes log, oldest first."""
    rows = []
    try:
        with open(signal_log.OUTCOMES) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("kind") == "orb_trade" and r.get("mode") in ("paper", "live") and r.get("user") == user:
                    rows.append(r)
    except OSError:
        pass
    return rows


def performance(rows, mode, today, capital=None):
    """Profitability for one mode. Partial exits of one position count as one trade."""
    groups, order = {}, []
    for i, r in enumerate(x for x in rows if x.get("mode") == mode):
        k = r.get("tradeId") or f"row{i}"
        if k not in groups:
            groups[k] = {"day": r.get("day"), "side": r.get("side"), "pnl": 0.0, "at": r.get("at")}
            order.append(k)
        groups[k]["pnl"] += float(r.get("pnl") or 0)
        groups[k]["at"] = r.get("at")
        if capital is None and r.get("capital"):
            capital = float(r["capital"])
    trades = [groups[k] for k in order]
    n = len(trades)
    pnls = [t["pnl"] for t in trades]
    total = sum(pnls)
    todays = [t["pnl"] for t in trades if t["day"] == today]
    run, curve = 0.0, []
    for p in pnls[-300:]:
        run += p
        curve.append(round(run, 3))
    return {
        "trades": n, "wins": sum(1 for p in pnls if p > 0),
        "winRate": round(sum(1 for p in pnls if p > 0) / n * 100, 1) if n else None,
        "total": round(total, 2), "today": round(sum(todays), 2), "todayTrades": len(todays),
        "avg": round(total / n, 3) if n else None,
        "best": round(max(pnls), 2) if n else None, "worst": round(min(pnls), 2) if n else None,
        "capital": capital, "returnPct": round(total / capital * 100, 2) if capital else None,
        "sessions": len({t["day"] for t in trades}), "curve": curve,
        "recent": [{**t, "pnl": round(t["pnl"], 3)} for t in trades[-8:]][::-1],
    }


def _iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
