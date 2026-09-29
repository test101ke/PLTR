"""
Offline checks for the ORB trader. No network, no real exchange.

    python tests/test_trader.py
"""
import os, sys, time, asyncio, tempfile, datetime as dt
os.environ["LOG_DIR"] = tempfile.mkdtemp()
os.environ.pop("TRADE_TOKEN", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import trader as tr

NY = tr.NY
DAY = dt.date(2026, 9, 30)


def at(sec_after_open):
    return tr.open_time(DAY) + dt.timedelta(seconds=sec_after_open)


def run(c):
    return asyncio.run(c)


def cfg(**kw):
    return tr.clean_config(kw)


# ------------------------------------------------------------------ speed
def test_rate_limit_never_exceeds_20_per_second():
    async def go():
        lim = tr.RateLimiter(20)
        stamps = []
        for _ in range(50):
            await lim.acquire(); stamps.append(time.monotonic())
        return stamps
    st = run(go())
    worst = max(sum(1 for t in st if s <= t < s + 1.0) for s in st)
    assert worst <= 20, f"{worst} orders inside one second"
    assert st[-1] - st[0] >= 2.0, "50 orders at 20/s must take at least 2 seconds"


class FakeFeed:
    """Stands in for a ccxt.pro exchange: one PLTR market, scripted books."""
    def __init__(self, hz=100, bursts=None):
        self.markets = {"PLTR/USDT:USDT": {"base": "PLTR", "quote": "USDT", "swap": True},
                        "BTC/USDT:USDT": {"base": "BTC", "quote": "USDT", "swap": True}}
        self.hz, self.bursts, self.n = hz, bursts, 0

    async def load_markets(self):
        return self.markets

    async def watch_order_book(self, sym, limit):
        if self.bursts is not None and self.n >= self.bursts:
            await asyncio.sleep(3600)           # feed goes quiet
        self.n += 1
        await asyncio.sleep(1 / self.hz)
        return {"bids": [[100.0, 5.0]], "asks": [[100.02, 5.0]], "timestamp": int(time.time() * 1000)}

    async def close(self):
        pass


def _agent(feed, **kw):
    a = tr.TradeAgent(exchange_factory=lambda ex, creds=None, pro=True: feed)
    a.configure(kw)
    return a


def _measure(feed, **kw):
    async def go():
        a = _agent(feed, **kw)
        await a.start("paper")
        await asyncio.sleep(2.3)
        s = dict(a.stats); await a.stop()
        return s
    return run(go())


def test_checks_per_second_between_5_and_20():
    s = _measure(FakeFeed(hz=200), tickHz=20)
    assert 15 <= s["checksPerSec"] <= 20.5, s           # fast feed: capped at tickHz
    s = _measure(FakeFeed(hz=200, bursts=1), tickHz=20)
    assert 4.5 <= s["checksPerSec"] <= 6, s             # quiet feed: still >= 5 checks a second


# ------------------------------------------------------------------ strategy
def _range(strat, lo=99.9, hi=100.1):
    for i, m in enumerate([lo, hi] * 30):
        strat.on_tick(at(i * 3), m - 0.01, m + 0.01, 50)


def test_bybit_batch_makes_one_dollar_net_then_continues():
    c = cfg(exchange="bybit")                          # 0% fees
    s = tr.OrbStrategy(c, 700.0)
    _range(s)
    acts = s.on_tick(at(200), 100.29, 100.31, 60)
    assert acts and acts[0][:2] == ("open", "long"), acts
    qty = s.size(700, 100.31)
    assert abs(qty * 100.31 - 1000) < 1e-6, "a $100 batch at 10x must be $1,000 of PLTR"
    s.opened("long", qty, 100.31, at(200))
    assert abs((s.pos["target"] - 100.31) / 100.31 - 0.001) < 1e-9, "$1 on $1,000 is a 0.10% move with no fees"
    assert s.on_tick(at(210), s.pos["target"], s.pos["target"] + 0.02, 60) == [("close", "target")]
    pnl = s.closed(s.pos["target"], 0.0)
    assert abs(pnl - 1.0) < 1e-6
    # continuation: a fresh high beyond the exit re-arms the long side
    assert s.on_tick(at(215), 100.40, 100.41, 60) == [], "no re-entry at the same level"
    acts = s.on_tick(at(220), 100.45, 100.47, 60)
    assert acts and acts[0][:2] == ("open", "long"), acts


def test_binance_fees_are_inside_the_dollar():
    c = cfg(exchange="binanceusdm")                    # 0.05% a side
    s = tr.OrbStrategy(c, 700.0)
    _range(s)
    qty = s.size(700, 100.0)
    s.opened("long", qty, 100.0, at(200))
    assert abs((s.pos["target"] - 100.0) / 100.0 - 0.002) < 1e-9, "Binance needs 0.20% for $1 net"
    fees = 2 * 100.0 * qty * 0.05 / 100
    assert abs(s.closed(s.pos["target"], fees) - 1.0) < 1e-6


def test_stop_loses_about_a_dollar():
    s = tr.OrbStrategy(cfg(exchange="bybit"), 700.0)
    _range(s)
    s.on_tick(at(200), 99.69, 99.71, 40)
    qty = s.size(700, 99.69)
    s.opened("short", qty, 99.69, at(200))
    assert s.on_tick(at(205), s.pos["stop"] - 0.02, s.pos["stop"], 40) == [("close", "stop")]
    assert abs(s.closed(s.pos["stop"], 0.0) + 1.0) < 1e-6


def test_no_trades_outside_first_15_minutes_and_forced_exit():
    s = tr.OrbStrategy(cfg(), 700.0)
    assert s.on_tick(at(-60), 101, 101.02, 90) == [], "nothing before 16:30 EAT"
    _range(s)
    assert s.on_tick(at(16 * 60), 100.5, 100.52, 90) == [], "no entries after 15 minutes"
    s2 = tr.OrbStrategy(cfg(), 700.0); _range(s2)
    s2.opened("long", 5, 100.3, at(300))
    assert s2.on_tick(at(15 * 60 + 1), 100.3, 100.32, 50) == [("close", "time exit")]


def test_book_must_lean_with_the_breakout():
    s = tr.OrbStrategy(cfg(), 700.0); _range(s)
    assert s.on_tick(at(200), 100.29, 100.31, 45) == [], "breakout up with the book leaning to asks"


def test_daily_loss_limit_halts():
    s = tr.OrbStrategy(cfg(dailyLossPct=3), 700.0); _range(s)
    s.realized = -21.5                                  # 3% of 700 = 21
    assert s.on_tick(at(200), 100.29, 100.31, 70) == [] and s.halted == "daily loss limit"


def test_ai_can_block_and_shrink_but_not_grow():
    s = tr.OrbStrategy(cfg(), 700.0); _range(s)
    assert s.on_tick(at(200), 100.29, 100.31, 70, {"action": "stand_aside"}) == []
    assert abs(s.size(700, 100, {"action": "reduce", "size": 0.5}) - 5.0) < 1e-9
    assert abs(s.size(700, 100, {"action": "go", "size": 3}) - 10.0) < 1e-9


def test_pltr_only():
    m = FakeFeed().markets
    assert tr.find_symbol(m) == "PLTR/USDT:USDT"
    assert tr.find_symbol(m, "BTC/USDT:USDT") is None


def test_settings_are_bounded():
    c = tr.clean_config({"leverage": 125, "tickHz": 100, "maxOrdersPerSec": 500, "maxTrades": 99})
    assert c["leverage"] == 20 and c["tickHz"] == 20 and c["maxOrdersPerSec"] == 20 and c["maxTrades"] == 10


# ------------------------------------------------------------------ paper + live wiring
def test_paper_round_trip_through_agent():
    async def go():
        a = _agent(FakeFeed(), exchange="bybit")
        a.feed_ex = FakeFeed(); a.symbol = "PLTR/USDT:USDT"
        a.broker = tr.PaperBroker(a.cfg); a.equity = 700.0
        a.strategy = tr.OrbStrategy(a.cfg, 700.0); a.mode = "paper"
        _range(a.strategy)
        a.book = {"bid": 100.29, "ask": 100.31, "imb": 60}
        await a._evaluate(at(200))
        assert a.strategy.pos and a.strategy.pos["side"] == "long"
        t = a.strategy.pos["target"]
        a.book = {"bid": t + 0.05, "ask": t + 0.07, "imb": 60}
        await a._evaluate(at(210))
        return a
    a = run(go())
    assert len(a.trades) == 1 and a.trades[0]["why"] == "target" and a.trades[0]["pnl"] > 0.9


class FakeCcxt:
    """Records the calls a live broker makes."""
    def __init__(self):
        self.calls, self.markets = [], FakeFeed().markets

    async def load_markets(self): return self.markets
    async def set_margin_mode(self, *a): self.calls.append(("margin", a))
    async def set_leverage(self, *a): self.calls.append(("leverage", a))
    async def fetch_balance(self): return {"USDT": {"total": 700.0}}
    def amount_to_precision(self, s, q): return f"{q:.1f}"

    async def create_order(self, sym, typ, side, amt, px, params):
        self.calls.append(("order", sym, typ, side, amt, params))
        return {"average": 100.0, "filled": amt, "fee": {"cost": 0.0}, "id": "1"}

    async def cancel_all_orders(self, s): self.calls.append(("cancel", s))
    async def fetch_positions(self, syms): return []
    async def close(self): pass


def test_live_broker_sets_leverage_and_closes_reduce_only():
    ex = FakeCcxt()
    b = tr.LiveBroker(cfg(leverage=10), ex)
    assert run(b.setup("PLTR/USDT:USDT")) == 700.0
    assert ("leverage", (10, "PLTR/USDT:USDT")) in ex.calls
    run(b.market("buy", 9.97, 100, 100.02))
    run(b.market("sell", 9.97, 100, 100.02, reduce_only=True))
    orders = [c for c in ex.calls if c[0] == "order"]
    assert orders[0][3:] == ("buy", 10.0, {}) and orders[1][3:] == ("sell", 10.0, {"reduceOnly": True})


def test_live_needs_confirmation_and_validated_keys():
    a = tr.TradeAgent(exchange_factory=lambda ex, creds=None, pro=True: FakeCcxt())
    for confirm, err in (("", "type LIVE"), ("LIVE", "API keys")):
        try:
            run(a.start("live", confirm)); assert False, "live started without safeguards"
        except PermissionError as e:
            assert err in str(e)
    a.keys.set("bybit", "abcd1234efgh5678", "s3cret")
    assert run(a.validate_keys())["ok"]
    st = run(a.start("live", "LIVE"))
    assert st["mode"] == "live" and st["liveArmed"] and st["keys"]["apiKey"] == "abcd…5678"
    assert "s3cret" not in str(st), "secret leaked into status"
    run(a.stop())


def test_trade_endpoints_refuse_remote_callers():
    from fastapi.testclient import TestClient
    import main
    c = TestClient(main.app)                               # host "testclient": not localhost
    assert c.get("/api/trade/status").status_code == 403
    assert c.post("/api/trade/start", json={"mode": "live", "confirm": "LIVE"}).status_code == 403
    os.environ["TRADE_TOKEN"] = "t0ken"
    try:
        assert c.get("/api/trade/status", headers={"X-Trade-Token": "wrong"}).status_code == 403
        r = c.get("/api/trade/status", headers={"X-Trade-Token": "t0ken"})
        assert r.status_code == 200 and r.json()["mode"] == "off"
    finally:
        os.environ.pop("TRADE_TOKEN")


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn(); print("PASS", name)
            except AssertionError as e:
                fails += 1; print("FAIL", name, "-", e)
    sys.exit(1 if fails else 0)
