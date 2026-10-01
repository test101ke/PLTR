"""
Offline checks for the ORB trader. No network, no real exchange.

    python tests/test_trader.py
"""
import os, sys, time, asyncio, tempfile, datetime as dt
os.environ["LOG_DIR"] = tempfile.mkdtemp()
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



def test_engine_heartbeat_shows_whether_the_agent_is_really_running():
    async def go():
        a = _agent(FakeFeed(hz=50))
        off = a.status()["engine"]
        await a.start("paper")
        await asyncio.sleep(0.5)
        on = a.status()["engine"]
        a.task.cancel(); await asyncio.sleep(0.05)     # loop dies but mode still says paper
        dead = a.status()["engine"]
        await a.stop()
        return off, on, dead, a.status()["engine"]
    off, on, dead, stopped = run(go())
    assert not off["running"] and not off["alive"]
    assert on["running"] and on["alive"] and on["beatAgeMs"] < 1000 and on["dataAgeMs"] < 1000 and on["since"]
    assert dead["running"] and not dead["alive"]
    assert not stopped["running"] and stopped["since"] is None

# ------------------------------------------------------------------ strategy
def _range(strat, lo=99.9, hi=100.1):
    for i, m in enumerate([lo, hi] * 30):
        strat.on_tick(at(i * 3), m - 0.01, m + 0.01, 50)


def preset(name, **kw):
    p = {k: v for k, v in tr.PRESETS[name].items() if k not in ("label", "note")}
    return cfg(**{**p, **kw})


def test_fees_assume_worst_case_0_06_per_side():
    assert tr.fee_pct(cfg(exchange="bybit")) == 0.06 and tr.fee_pct(cfg(exchange="binanceusdm")) == 0.06


def test_scalp_nets_one_dollar_after_0_12_round_trip():
    s = tr.OrbStrategy(preset("scalp"), 700.0)
    _range(s)
    acts = s.on_tick(at(200), 100.29, 100.31, 60)
    assert acts and acts[0][:2] == ("open", "long"), acts
    qty = s.size(700, 100.0)
    assert abs(qty * 100.0 - 1000) < 1e-6, "a $100 batch at 10x must be $1,000 of PLTR"
    fee = 100.0 * qty * 0.06 / 100
    s.opened("long", qty, 100.0, at(200), fee)
    assert abs((s.pos["target"] - 100.0) / 100.0 - 0.0022) < 1e-9, "$1 net + $1.20 fees on $1,000 = 0.22%"
    assert s.on_tick(at(205), 100.2201, 100.2401, 60) == [("close", "target")]
    assert abs(s.closed(100.22, fee) - 1.0) < 1e-6


def test_scalp_stop_loses_two_twenty_incl_fees():
    s = tr.OrbStrategy(preset("scalp"), 700.0)
    _range(s)
    qty = s.size(700, 100.0); fee = 100.0 * qty * 0.06 / 100
    s.opened("short", qty, 100.0, at(200), fee)
    st = s.pos["stop"]
    assert s.on_tick(at(205), st, st + 0.02, 40) == [("close", "stop")]
    assert abs((st - 100.0) / 100.0 - 0.001) < 1e-9, "stop must be the 0.10% floor, not squeezed by fees"
    assert abs(s.closed(st + 0.0, st * qty * 0.06 / 100) + 2.2) < 0.01


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


def test_ai_market_risk_blocks_halves_and_exits_but_never_grows():
    s = tr.OrbStrategy(cfg(), 700.0); _range(s)
    assert s.on_tick(at(200), 100.29, 100.31, 70, {"marketRisk": "halt"}) == []
    assert abs(s.size(700, 100, {"marketRisk": "elevated"}) - 5.0) < 1e-9
    assert abs(s.size(700, 100, {"marketRisk": "normal", "size": 3}) - 10.0) < 1e-9
    # a direction call about the rule trade must NOT block an ORB entry
    assert s.on_tick(at(201), 100.29, 100.31, 70, {"action": "stand_aside", "marketRisk": "normal"})
    s.opened("long", 10, 100.3, at(201))
    assert s.on_tick(at(202), 100.3, 100.32, 70, {"marketRisk": "halt"}) == [("close", "AI risk-off")]


# ------------------------------------------------------------------ exit scenarios (paper fills)
def simulate(c, path, side="long"):
    """Run a price path (seconds after open, mid) through the agent with paper fills.
    Returns total P&L in USDT after fees and slippage."""
    async def go():
        a = _agent(FakeFeed(), **c)
        a.broker = tr.PaperBroker(a.cfg); a.equity = 700.0; a.mode = "paper"
        a.strategy = tr.OrbStrategy(a.cfg, 700.0); _range(a.strategy)
        imb = 70 if side == "long" else 30
        for sec, mid in path:
            a.book = {"bid": mid - 0.005, "ask": mid + 0.005, "imb": imb}
            await a._evaluate(at(sec))
        if a.strategy.pos:                                   # mark anything left at the last price
            a.book = {"bid": path[-1][1] - 0.005, "ask": path[-1][1] + 0.005, "imb": imb}
            await a._close("end of test")
        return a
    return run(go())


def _pnl(a):
    return round(sum(t["pnl"] for t in a.trades), 3)


# entry at ~100.3; one-tick wick to 100.05 (past a $1.5 stop), then a run to 101.0
WHIPSAW = [(200, 100.30), (200.2, 100.28), (201.0, 100.05), (201.2, 100.25), (203, 100.40),
           (205, 100.60), (208, 100.80), (212, 101.00), (215, 100.95)]
# a 0.7% spike within about a second, then a pullback
SPIKE = [(200, 100.30), (200.25, 100.45), (200.5, 100.65), (200.75, 100.85), (201.0, 101.00),
         (201.5, 100.98), (202.0, 100.80), (203.0, 100.70)]


def test_whipsaw_scalp_is_shaken_out_but_the_others_survive():
    scalp = simulate(preset("scalp"), WHIPSAW)
    assert scalp.trades[0]["why"] == "stop" and scalp.trades[0]["pnl"] < -2.0, scalp.trades
    for name in ("runner", "wickproof", "burst"):
        a = simulate(preset(name), WHIPSAW)
        assert a.trades[0]["why"] != "stop" and _pnl(a) > 3.0, (name, a.trades)


def test_spike_is_ridden_past_one_dollar_and_the_top_is_not_chased():
    scalp, runner, burst = (simulate(preset(n), SPIKE) for n in ("scalp", "runner", "burst"))
    assert [x["why"] for x in scalp.trades] == ["target"], "re-entry cooldown must stop it buying the spike top"
    assert _pnl(runner) > 2.0 and _pnl(burst) > 2.0, (runner.trades, burst.trades)
    assert any(x["why"] == "first target (partial)" for x in runner.trades)
    assert burst.trades[0]["why"] == "trailing stop"


def test_emergency_stop_fires_even_during_grace():
    crash = [(200, 100.30), (200.5, 99.0)]                   # -1.3% inside the 10s grace
    a = simulate(preset("wickproof"), crash)
    assert a.trades[0]["why"] == "emergency stop", a.trades


def test_break_even_lock_turns_a_reversal_into_a_scratch():
    path = [(200, 100.30), (203, 100.60), (206, 100.45)] + [(207 + i / 10, 100.43 - i * 0.004) for i in range(10)] \
        + [(210, 99.90)]                                     # 10 ticks a second, like the live feed
    a = simulate(preset("runner", breakevenUsd=0.3, takeProfitUsd=5), path)
    assert a.trades[0]["why"] == "break-even stop" and abs(_pnl(a)) < 0.6, a.trades


def test_stop_needs_to_hold_for_confirm_ms():
    c = preset("runner", graceSec=0, confirmMs=800)
    s = tr.OrbStrategy(c, 700.0); _range(s)
    s.opened("long", 10, 100.3, at(200))
    stop = s.pos["stop"]
    assert s.on_tick(at(201), stop - 0.01, stop, 70) == []          # breach starts
    assert s.on_tick(at(201.5), stop - 0.01, stop, 70) == []        # 500ms: not yet
    assert s.on_tick(at(201.9), stop - 0.01, stop, 70) == [("close", "stop")]


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
        a = _agent(FakeFeed(), **{k: v for k, v in tr.PRESETS["scalp"].items() if k not in ("label", "note")})
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
    assert len(a.trades) == 1 and a.trades[0]["why"] == "target" and a.trades[0]["pnl"] > 0.85


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


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn(); print("PASS", name)
            except AssertionError as e:
                fails += 1; print("FAIL", name, "-", e)
    sys.exit(1 if fails else 0)
